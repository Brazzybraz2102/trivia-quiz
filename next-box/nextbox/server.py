"""HTTP API for the web app, phone app and Home Assistant. LAN only.

Two ways in:
- people sign in with a username + password (session cookie, or a bearer token for native apps)
- Home Assistant and scripts send the shared X-NextBox-Key header (no admin access)

Roles: user < admin (support) < superadmin (debug). Every admin action is written to the audit log.
"""
from __future__ import annotations

import dataclasses
import json
import platform
import re
import secrets
import shutil
import sys
import time
import traceback
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import jobs, printer, scan
from .auth import ROLES, Accounts, AuthError, default_prefs, rank
from .config import PROJECT_DIR
from .connections import Builder, Connections, NotConnected
from .events import Events, ServerSettings
from .feedback import Feedback, FeedbackError
from .jobs import Context, print_text
from .mylist import TAG_COLORS, LocalTasks, Tags, is_local, parse_dump
from .printer import COLORS, DRIVERS
from .printers import REASONS, PrinterError, Printers
from .providers import PROVIDERS, READY, ProviderError
from .store import normalize_id
from .vault import Vault

WEB_DIR = PROJECT_DIR / "web"
READY_NAMES = {k: v["name"] for k, v in READY.items()}
MAX_PHOTO_BYTES = 15 * 1024 * 1024
COOKIE = "nextbox_session"
STARTED = time.time()


class LoginBody(BaseModel):
    username: str
    password: str
    want_token: bool = False  # native apps: return a bearer token instead of relying on cookies


class PasswordBody(BaseModel):
    current: str
    new: str


class FeedbackBody(BaseModel):
    mode: str = "open"          # "guided" or "open"
    type: str = "other"
    page: str = ""
    rating: int | None = None
    trying: str = ""
    happened: str = ""
    expected: str = ""
    message: str = ""
    include_debug: bool = False  # recent errors, kept privately with the sender's identity


class FeedbackResponse(BaseModel):
    status: str | None = None
    reply: str | None = None


class TodayBody(BaseModel):
    source: str = "manual"  # "auto" only from Home Assistant
    dry_run: bool = False


class ListBody(BaseModel):
    query: str
    title: str | None = None
    dry_run: bool = False


class TaskBody(BaseModel):
    task_id: str
    dry_run: bool = False


class TextBody(BaseModel):
    text: str
    title: str = "NOTE"
    todo: bool = False  # also create a Todoist task
    dry_run: bool = False


class ConfirmBody(BaseModel):
    decisions: dict[int, str]  # {row: "confirm" | "skip"}


class NewUserBody(BaseModel):
    username: str
    role: str = "user"
    beta: bool = False


class TaskIn(BaseModel):
    content: str | None = None
    description: str | None = None
    priority: int | None = None
    due_date: str | None = None
    due_time: str | None = None
    repeat: str | None = None
    tags: list[str] | None = None
    steps: list | None = None
    minutes: int | None = None


class DumpBody(BaseModel):
    text: str


class TagBody(BaseModel):
    color: str


class PickBody(BaseModel):
    task_ids: list[str] | None = None
    query: str | None = None
    dry_run: bool | None = None


class FocusBody(BaseModel):
    task_id: str | None = None
    dry_run: bool | None = None


class ConnectBody(BaseModel):
    provider: str
    fields: dict[str, str]


class PrinterBody(BaseModel):
    name: str | None = None
    driver: str | None = None
    address: str | None = None
    width_px: int | None = None
    dpi: int | None = None
    stock_color: str | None = None
    ink: str | None = None
    model: str | None = None
    label: str | None = None
    label_height_mm: float | None = None
    gap_mm: float | None = None
    cut: bool | None = None


# Shown to everyone once; their "I understand" is recorded. Bump the version if it changes.
DATA_NOTICE_VERSION = 3
DATA_NOTICE = ("The admin can see how you use Next Box: your printed tickets (including the tasks on "
               "them), photo read-backs, settings, activity, signed-in devices, and how many open tasks "
               "are on your built-in list (not what they say). Tasks on your built-in list are kept on "
               "this Next Box computer. Your password and your to-do app password or token are never "
               "visible to anyone, and feedback stays anonymous.")


class UserPatch(BaseModel):
    role: str | None = None
    disabled: bool | None = None
    beta: bool | None = None
    debug: bool | None = None


class SuperUserPatch(UserPatch):
    email: str | None = None
    household_id: str | None = None
    must_change: bool | None = None


class SuperNewUser(BaseModel):
    username: str
    role: str = "user"
    beta: bool = False
    email: str | None = None
    household_id: str | None = None


class BulkBody(BaseModel):
    usernames: list[str]
    action: str  # disable | enable | signout | beta_on | beta_off


class HouseholdBody(BaseModel):
    name: str


@dataclasses.dataclass
class Caller:
    name: str              # username, or "key" for Home Assistant/scripts
    role: str = "user"     # "service" for the shared key
    debug: bool = False
    token: str | None = None
    prefs: dict = dataclasses.field(default_factory=default_prefs)
    household: str = "home"


def create_app(ctx: Context, vision: scan.VisionFn | None = None,
               accounts: Accounts | None = None, builder: Builder | None = None) -> FastAPI:
    """`ctx.tasks_factory`, when set, is one to-do app shared by everyone (tests only).
    Otherwise each person's own connection is used."""
    app = FastAPI(title="Next Box", docs_url="/docs")
    # No allow_credentials: cookies only work same-origin; other clients use headers.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy",
                                    "default-src 'self'; img-src 'self' data: blob:; "
                                    "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        return response

    key = ctx.settings.server_key
    data_dir = Path(ctx.settings.data_dir)
    accounts = accounts or Accounts(data_dir)
    events = Events(data_dir)
    server_settings = ServerSettings(data_dir)
    vault = Vault(data_dir)
    feedback_store = Feedback(data_dir)
    registry = ctx.printers or Printers(data_dir, ctx.settings)
    build = _build_id()
    _migrate_feedback(events, feedback_store, accounts)
    connections = Connections(ctx.settings, accounts, vault, **({"builder": builder} if builder else {}))

    # ------------------------------------------------------------------ auth
    def _bearer(request: Request) -> str | None:
        h = request.headers.get("authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else None

    def _token(request: Request) -> str | None:
        return _bearer(request) or request.cookies.get(COOKIE)

    def auth(request: Request, x_nextbox_key: str | None = Header(default=None)) -> Caller:
        if x_nextbox_key:
            if key and secrets.compare_digest(x_nextbox_key.encode(), key.encode()):
                return Caller("key", role="service")
            raise HTTPException(401, "wrong X-NextBox-Key")
        token = _token(request)
        name = accounts.session_user(token)
        if not name:
            raise HTTPException(401, "sign in required")
        if not _bearer(request) and request.method not in {"GET", "HEAD", "OPTIONS"}:
            # Cookie-authenticated writes must come from this site's own pages.
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                raise HTTPException(403, "cross-site request refused")
        user = accounts.get(name)
        if user["must_change"] and not request.url.path.startswith("/auth/"):
            raise HTTPException(403, "Choose your own password first (Settings → Account).")
        return Caller(name, role=user["role"], debug=user["debug"], token=token, prefs=user["prefs"],
                      household=user["household_id"])

    def require(min_role: str):
        def dep(caller: Caller = Depends(auth)) -> Caller:
            if caller.role == "service" or rank(caller.role) < rank(min_role):
                raise HTTPException(403, f"{min_role} only")
            return caller
        return dep

    def person(caller: Caller = Depends(auth)) -> Caller:
        if caller.role == "service":
            raise HTTPException(403, "sign in as a person for this")
        return caller

    admin_only, super_only = require("admin"), require("superadmin")

    def can_manage(caller: Caller, target: dict) -> bool:
        """Staff (superadmin) manage anyone else; a household's admin manages its plain users only."""
        if target["username"] == caller.name:
            return False
        if caller.role == "superadmin":
            return True
        return target["household_id"] == caller.household and rank(target["role"]) < rank(caller.role)

    def scope(caller: Caller) -> str | None:
        """Which household's people an admin screen shows: all of them for staff."""
        return None if caller.role == "superadmin" else caller.household

    def audit(caller: Caller, action: str, **detail) -> None:
        events.log(caller.name, action, kind="audit", detail=detail, household=caller.household)

    # -------------------------------------------------------------- running
    def acting_as(caller: Caller) -> str:
        """Whose tasks and tickets: the person, or the owner for Home Assistant/scripts."""
        if caller.role == "service":
            return accounts.owner() or caller.name
        return caller.name

    def household_of(caller: Caller) -> str:
        if caller.role == "service":
            return accounts.household_of(acting_as(caller)) or "home"
        return caller.household

    def rctx(caller: Caller) -> Context:
        s = server_settings.get()
        name = acting_as(caller)
        prefs = caller.prefs
        if caller.role == "service" and accounts.get(name):
            prefs = accounts.get(name)["prefs"]
        if ctx.tasks_factory is not None:
            factory = ctx.tasks_factory
        else:
            factory = lambda: connections.provider_for(name)  # noqa: E731
        return dataclasses.replace(ctx, user=name, household=household_of(caller), prefs=prefs, printers=ctx.printers,
                                   printing_paused=s["printing_paused"],
                                   auto_print_enabled=s["auto_print_enabled"],
                                   tasks_factory=factory, _tasks=None,
                                   tag_colors=Tags(accounts.db, name).colors(),
                                   wins=lambda: wins_yesterday(name))

    def wins_yesterday(name: str) -> int:
        """Tasks finished yesterday: ticked on My list, or marked done on a photo read-back."""
        today = ctx.today()
        start = time.mktime((today - timedelta(days=1)).timetuple())
        end = time.mktime(today.timetuple())
        done = sum(1 for e in events.query(user=name, since=start, limit=1000)
                   if e["action"] == "task_done" and e["ts"] < end)
        y = (today - timedelta(days=1)).isoformat()
        for rec in ctx.store.list_scans(200, by=name, since=y):
            if rec.get("created_at", "")[:10] == y:
                done += sum(1 for a in rec.get("applied", [])
                            if a.get("mark") == "done" and not is_local(str(a.get("task_id", ""))))
        return done

    def my_list(caller: Caller) -> LocalTasks:
        return LocalTasks(accounts.db, caller.name, caller.household, today=ctx.today)

    def run(caller: Caller, action: str, fn, *args, **kwargs):
        start = time.time()
        try:
            result = fn(*args, **kwargs)
        except HTTPException:
            raise
        except NotConnected as exc:
            raise HTTPException(409, str(exc)) from exc
        except scan.PhotoError as exc:
            raise HTTPException(415, str(exc)) from exc
        except ValueError as exc:  # something the person can fix: an empty list, a bad date
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            events.log(caller.name, action, ok=False, kind="error", error=str(exc),
                       trace=traceback.format_exc(), detail={"args": _safe_args(args, kwargs)})
            raise HTTPException(502, str(exc)) from exc
        detail = {"ms": int((time.time() - start) * 1000)}
        if isinstance(result, dict):
            detail.update({k: result[k] for k in ("id", "status", "kind", "reason") if k in result})
        if caller.debug:
            detail["args"] = _safe_args(args, kwargs)
            if isinstance(result, dict):
                detail["result"] = {k: v for k, v in result.items() if k not in {"png", "vision"}}
        events.log(caller.name, action, detail=detail)
        return result

    # ----------------------------------------------------------- endpoints
    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/auth/login")
    def login(body: LoginBody, request: Request, response: Response):
        client = request.client.host if request.client else "?"
        username = body.username.strip().lower()
        if accounts.too_many_failures(client):
            raise HTTPException(429, "too many attempts; wait 5 minutes")
        if not accounts.verify(username, body.password):
            user = accounts.get(username)
            # Only say "turned off" to someone who knows the password; everyone else gets the
            # same answer as a wrong password, and it counts toward the lockout.
            if user and user["disabled"] and accounts.password_ok(username, body.password):
                events.log(username, "login", ok=False, kind="auth", detail={"ip": client, "why": "disabled"})
                raise HTTPException(403, "this account is turned off; ask an admin")
            accounts.record_failure(client)
            events.log(username or "?", "login", ok=False, kind="auth", detail={"ip": client})
            raise HTTPException(401, "wrong username or password")
        accounts.clear_failures(client)
        token = accounts.create_session(username, client, request.headers.get("user-agent", ""))
        accounts.touch_login(username)
        events.log(username, "login", kind="auth", detail={"ip": client})
        user = accounts.get(username)
        out = {"username": username, "role": user["role"], "must_change": user["must_change"]}
        if body.want_token:
            return {**out, "token": token}
        _set_cookie(response, request, token)
        return out

    def _set_cookie(response: Response, request: Request, token: str) -> None:
        response.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/")

    @app.post("/auth/logout")
    def logout(request: Request, response: Response):
        accounts.end_session(_token(request))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/auth/me")
    def me(request: Request):
        name = accounts.session_user(_token(request))
        if not name:
            raise HTTPException(401, "sign in required" if accounts.has_users()
                                else "no accounts yet: run `nextbox user add <name>` on the desktop")
        user = accounts.get(name)
        s = server_settings.get()
        consent = user.get("consent") or {}
        return {"username": name, "role": user["role"], "beta": user["beta"], "debug": user["debug"],
                "household_id": user["household_id"],
                "must_change": user["must_change"], "prefs": user["prefs"],
                "announcement": s["announcement"], "printing_paused": s["printing_paused"],
                "data_notice": DATA_NOTICE,
                "consented": consent.get("version") == DATA_NOTICE_VERSION, "consented_at": consent.get("at")}

    @app.post("/auth/consent")
    def consent(request: Request):
        name = accounts.session_user(_token(request))
        if not name:
            raise HTTPException(401, "sign in required")
        accounts.record_consent(name, DATA_NOTICE_VERSION)
        events.log(name, "data_notice_accepted", kind="auth", detail={"version": DATA_NOTICE_VERSION})
        return {"ok": True}

    @app.get("/version")
    def version():
        return {"version": _version(), "build": build, "credit": ctx.settings.credit}

    @app.post("/auth/password")
    def change_password(body: PasswordBody, request: Request, response: Response,
                        caller: Caller = Depends(person)):
        if not accounts.verify(caller.name, body.current):
            raise HTTPException(401, "current password is wrong")
        try:
            accounts.set_password(caller.name, body.new)  # signs out every device...
        except AuthError as exc:
            raise HTTPException(400, str(exc))
        token = accounts.create_session(caller.name, request.client.host if request.client else "",
                                        request.headers.get("user-agent", ""))
        _set_cookie(response, request, token)               # ...except this one
        events.log(caller.name, "password_changed", kind="auth")
        return {"ok": True, **({"token": token} if _bearer(request) else {})}

    @app.get("/auth/sessions")
    def my_sessions(caller: Caller = Depends(person)):
        return accounts.sessions_for(caller.name, caller.token)

    @app.post("/auth/signout-others")
    def signout_others(caller: Caller = Depends(person)):
        n = accounts.revoke_user_sessions(caller.name, keep=caller.token)
        events.log(caller.name, "signout_others", kind="auth", detail={"sessions": n})
        return {"signed_out": n}

    @app.get("/settings")
    def get_settings(caller: Caller = Depends(person)):
        return caller.prefs

    @app.patch("/settings")
    def patch_settings(changes: dict, caller: Caller = Depends(person)):
        try:
            prefs = accounts.set_prefs(caller.name, changes)
        except AuthError as exc:
            raise HTTPException(400, str(exc))
        events.log(caller.name, "settings", detail={"changed": changes})
        return prefs

    # ------------------------------------------------------------- feedback (anonymous)
    def optional_person(request: Request) -> Caller | None:
        """Feedback works on every page, including before sign-in."""
        name = accounts.session_user(_token(request))
        user = accounts.get(name) if name else None
        return Caller(name, role=user["role"]) if user else None

    @app.post("/feedback")
    def send_feedback(body: FeedbackBody, request: Request):
        caller = optional_person(request)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "cross-site request refused")
        debug = None
        if body.include_debug and caller:
            debug = {"recent_errors": [{k: e.get(k) for k in ("ts", "action", "error")}
                                       for e in events.query(user=caller.name, kind="error", limit=5)]}
        try:
            item = feedback_store.submit(
                mode=body.mode, kind=body.type, page=body.page, rating=body.rating,
                fields={"trying": body.trying, "happened": body.happened, "expected": body.expected},
                message=body.message, who=caller.name if caller else None,
                ip=request.client.host if request.client else "", agent=request.headers.get("user-agent", ""),
                names=accounts.list_users(), debug=debug)
        except FeedbackError as exc:
            raise HTTPException(400, str(exc)) from exc
        # Deliberately not written to the events log: that would link the person to the post.
        return {**item, "mine": True}

    @app.get("/feedback")
    def feedback_board(type: str | None = None, status: str | None = None,
                       caller: Caller = Depends(person)):
        return feedback_store.board(caller.name, include_hidden=rank(caller.role) >= rank("admin"),
                                    kind=type or None, status=status or None)

    @app.delete("/feedback/{fid}")
    def withdraw_feedback(fid: str, caller: Caller = Depends(person)):
        is_admin = rank(caller.role) >= rank("admin")
        if not (feedback_store.is_author(fid, caller.name) or is_admin):
            raise HTTPException(403, "you can only withdraw your own feedback")
        try:
            feedback_store.withdraw(fid)
        except KeyError:
            raise HTTPException(404) from None
        if is_admin and not feedback_store.is_author(fid, caller.name):
            audit(caller, "remove_feedback", feedback=fid)
        return {"ok": True}

    @app.get("/status")
    def status(caller: Caller = Depends(auth)):
        s = server_settings.get()
        everyone = registry.all(household_of(caller))
        mine = next((p for p in everyone if p.id == caller.prefs.get("default_printer")), everyone[0] if everyone else None)
        return {
            "printer_reachable": bool(mine) and printer.is_reachable(mine),
            "printer": mine.name if mine else None,
            "dry_run_forced": ctx.settings.dry_run or s["printing_paused"]
                              or bool(caller.prefs.get("always_dry_run")),
            "printing_paused": s["printing_paused"],
        }

    @app.get("/tasks")
    def tasks(query: str = jobs.TODAY_QUERY, caller: Caller = Depends(auth)):
        c = rctx(caller)
        return run(caller, "tasks", lambda q: c.tasks.filter_tasks(q), query)

    @app.post("/print/today")
    def p_today(body: TodayBody, caller: Caller = Depends(auth)):
        if body.source not in {"manual", "auto"}:
            raise HTTPException(400, "source must be manual or auto")
        return run(caller, f"print_today_{body.source}", jobs.print_today, rctx(caller),
                   source=body.source, dry_run=body.dry_run)

    @app.post("/print/list")
    def p_list(body: ListBody, caller: Caller = Depends(auth)):
        return run(caller, "print_list", jobs.print_filter, rctx(caller), body.query, body.title,
                   dry_run=body.dry_run)

    @app.post("/print/task")
    def p_task(body: TaskBody, caller: Caller = Depends(auth)):
        return run(caller, "print_task", jobs.print_task, rctx(caller), body.task_id,
                   dry_run=body.dry_run)

    @app.post("/print/text")
    def p_text(body: TextBody, caller: Caller = Depends(auth)):
        if not body.text.strip():
            raise HTTPException(400, "text is empty")
        if body.todo:
            return run(caller, "add_and_print", jobs.add_and_print, rctx(caller), body.text,
                       dry_run=body.dry_run)
        return run(caller, "print_text", jobs.print_text, rctx(caller), body.text, body.title,
                   dry_run=body.dry_run)

    # ----------------------------------------------------------- My list
    @app.get("/list")
    def get_list(include_done: bool = False, caller: Caller = Depends(person)):
        """The built-in list, plus today's tasks from a linked app (they stay in that app)."""
        mine = my_list(caller)
        tasks = mine.all_open()
        if include_done:
            from sqlalchemy import select as _select
            from . import db as _db
            with accounts.db.connect() as conn:
                rows = conn.execute(_select(_db.tasks).where(_db.tasks.c.owner == caller.name,
                                                             _db.tasks.c.done_at.is_not(None))
                                    .order_by(_db.tasks.c.done_at.desc()).limit(50)).mappings().all()
            tasks += [mine._task(r) for r in rows]
        status = connections.status(caller.name)
        linked, linked_error = [], ""
        if status.get("connected"):
            try:
                linked = rctx(caller).tasks.linked.today()
                linked = [{**t, "source": status["provider"], "labels": list(t.get("labels") or [])} for t in linked]
            except Exception as exc:  # the list still works when the linked app is down
                linked_error = f"{status.get('name', 'Your app')} didn't answer: {exc}"[:300]
        return {"tasks": tasks, "linked": linked, "linked_name": status.get("name") if status.get("connected") else None,
                "linked_error": linked_error, "tags": Tags(accounts.db, caller.name).all(),
                "one_thing": _one_thing(caller)}

    def _one_thing(caller: Caller):
        c = rctx(caller)
        try:
            return jobs.one_thing(c)
        except Exception:  # a linked app that's down shouldn't hide what's on your own list
            return jobs.one_thing(dataclasses.replace(c, tasks_factory=lambda: my_list(caller), _tasks=None))

    @app.post("/list")
    def add_list_task(body: TaskIn, caller: Caller = Depends(person)):
        fields = body.model_dump(exclude_none=True)
        for name in fields.get("tags", []):
            _ensure_tag(caller, name)
        task = run(caller, "list_add", my_list(caller).create, fields)
        return task

    def _ensure_tag(caller: Caller, name: str) -> None:
        tags = Tags(accounts.db, caller.name)
        try:
            known = {t["name"] for t in tags.all()}
            from .mylist import tag_name
            if tag_name(name) not in known:
                tags.set(name, "white")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/list/dump")
    def brain_dump(body: DumpBody, caller: Caller = Depends(person)):
        """Type or paste everything on your mind, one per line. #tag !urgent tomorrow 3pm ~15m."""
        items = parse_dump(body.text, ctx.today())
        if not items:
            raise HTTPException(400, "Nothing to add. Put one task on each line.")
        mine = my_list(caller)
        for item in items:
            for name in item.get("tags", []):
                _ensure_tag(caller, name)
        added = run(caller, "brain_dump", lambda: [mine.create(i) for i in items])
        return {"added": added}

    def _local_id(task_id: str) -> str:
        if not is_local(task_id):
            raise HTTPException(400, "That task lives in your linked app; change it there.")
        return task_id

    @app.patch("/list/{task_id}")
    def edit_list_task(task_id: str, body: TaskIn, caller: Caller = Depends(person)):
        fields = body.model_dump(exclude_unset=True)
        for name in fields.get("tags") or []:
            _ensure_tag(caller, name)
        return run(caller, "list_edit", my_list(caller).update, _local_id(task_id), fields)

    @app.post("/list/{task_id}/done")
    def done_list_task(task_id: str, caller: Caller = Depends(person)):
        c = rctx(caller)
        run(caller, "list_done", c.tasks.close_task, task_id)
        if not is_local(task_id):
            events.log(caller.name, "task_done", detail={"source": "linked"}, household=caller.household)
        return {"ok": True}

    @app.post("/list/{task_id}/reopen")
    def reopen_list_task(task_id: str, caller: Caller = Depends(person)):
        return run(caller, "list_reopen", my_list(caller).reopen, _local_id(task_id))

    @app.delete("/list/{task_id}")
    def delete_list_task(task_id: str, confirm: bool = False, caller: Caller = Depends(person)):
        if not confirm:  # deleting a task always needs the person's explicit yes
            raise HTTPException(400, "Deleting needs confirm=true.")
        run(caller, "list_delete", rctx(caller).tasks.delete_task, task_id)
        return {"ok": True}

    @app.get("/tags")
    def get_tags(caller: Caller = Depends(person)):
        return {"tags": Tags(accounts.db, caller.name).all(), "colors": list(TAG_COLORS)}

    @app.put("/tags/{name}")
    def put_tag(name: str, body: TagBody, caller: Caller = Depends(person)):
        try:
            return Tags(accounts.db, caller.name).set(name, body.color)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/tags/{name}")
    def delete_tag(name: str, caller: Caller = Depends(person)):
        try:
            Tags(accounts.db, caller.name).remove(name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/print/stickers")
    def p_stickers(body: PickBody, caller: Caller = Depends(auth)):
        return run(caller, "print_stickers", jobs.print_stickers, rctx(caller), body.task_ids, body.query,
                   dry_run=body.dry_run)

    @app.post("/print/strips")
    def p_strips(body: PickBody, caller: Caller = Depends(auth)):
        return run(caller, "print_strips", jobs.print_strips, rctx(caller), body.task_ids, body.query,
                   dry_run=body.dry_run)

    @app.post("/print/focus")
    def p_focus(body: FocusBody, caller: Caller = Depends(auth)):
        return run(caller, "print_focus", jobs.print_focus, rctx(caller), body.task_id, dry_run=body.dry_run)

    @app.get("/printed")
    def printed(limit: int = 50, caller: Caller = Depends(auth)):
        return ctx.store.list_printed(limit, by=acting_as(caller))

    @app.get("/printed/{label_id}/png")
    def printed_png(label_id: str, caller: Caller = Depends(auth)):
        label_id = normalize_id(label_id)
        path = ctx.store.png_path(label_id)
        if not re.fullmatch(r"[A-Z0-9-]{4,16}", label_id) or not path.exists():
            raise HTTPException(404)
        rec = ctx.store.get_printed(label_id)
        # People see their own tickets; the superadmin sees everyone's (everyone agreed, see DATA_NOTICE).
        if not rec or (rec.get("by") != acting_as(caller) and caller.role != "superadmin"):
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.post("/scan")
    async def do_scan(photo: UploadFile = File(...), label_id: str | None = Form(default=None),
                      caller: Caller = Depends(auth)):
        data = await photo.read()
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(413, "photo too large")
        media_type = photo.content_type or "image/jpeg"
        if media_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            raise HTTPException(415, f"unsupported image type {media_type}")
        try:
            data, media_type = scan.prepare_photo(data)  # upright, resized JPEG
        except scan.PhotoError as exc:
            raise HTTPException(415, str(exc)) from exc
        except ValueError as exc:  # something the person can fix: an empty list, a bad date
            raise HTTPException(400, str(exc)) from exc
        fn = vision or scan.claude_vision(ctx.settings)
        return run(caller, "scan", scan.scan_photo, rctx(caller), fn, data, media_type, label_id or None)

    @app.get("/scans")
    def scans(limit: int = 20, caller: Caller = Depends(auth)):
        return ctx.store.list_scans(limit, by=acting_as(caller))

    def _own_scan(scan_id: str, caller: Caller) -> dict:
        rec = ctx.store.get_scan(scan_id) if re.fullmatch(r"[A-Za-z0-9-]{4,16}", scan_id) else None
        if not rec or rec.get("by") != acting_as(caller):
            raise HTTPException(404)
        return rec

    @app.get("/scan/{scan_id}")
    def get_scan(scan_id: str, caller: Caller = Depends(auth)):
        return _own_scan(scan_id, caller)

    @app.post("/scan/{scan_id}/confirm")
    def confirm_scan(scan_id: str, body: ConfirmBody, caller: Caller = Depends(auth)):
        _own_scan(scan_id, caller)
        return run(caller, "scan_confirm", scan.confirm, rctx(caller), scan_id, body.decisions)

    # ----------------------------------------------------------- printers & label colors
    @app.get("/printing-options")
    def printing_options(caller: Caller = Depends(person)):
        admin = rank(caller.role) >= rank("admin")
        return {"printers": [p.public(admin) for p in registry.all(caller.household)], "colors": COLORS,
                "reasons": REASONS, "drivers": DRIVERS}

    @app.get("/admin/printers/status")
    def printers_status(caller: Caller = Depends(admin_only)):
        return {p.id: printer.is_reachable(p) for p in registry.all(caller.household)}

    @app.post("/admin/printers")
    def add_printer(body: PrinterBody, caller: Caller = Depends(admin_only)):
        try:
            cfg = registry.add(body.model_dump(exclude_none=True), caller.household)
        except PrinterError as exc:
            raise HTTPException(400, str(exc)) from exc
        audit(caller, "add_printer", printer=cfg.id, name=cfg.name, driver=cfg.driver)
        return cfg.public(True)

    @app.patch("/admin/printers/{pid}")
    def edit_printer(pid: str, body: PrinterBody, caller: Caller = Depends(admin_only)):
        try:
            cfg = registry.update(pid, body.model_dump(exclude_none=True), caller.household)
        except KeyError:
            raise HTTPException(404) from None
        except PrinterError as exc:
            raise HTTPException(400, str(exc)) from exc
        audit(caller, "edit_printer", printer=pid)
        return cfg.public(True)

    @app.delete("/admin/printers/{pid}")
    def delete_printer(pid: str, caller: Caller = Depends(admin_only)):
        registry.remove(pid, caller.household)
        audit(caller, "remove_printer", printer=pid)
        return {"ok": True}

    @app.post("/admin/printers/{pid}/test")
    def test_printer(pid: str, dry_run: bool = False, caller: Caller = Depends(admin_only)):
        cfg = registry.get(pid, caller.household)
        if not cfg:
            raise HTTPException(404)
        c = dataclasses.replace(rctx(caller), printers=_OnlyPrinter(cfg),
                                prefs={**caller.prefs, "default_printer": cfg.id, "color_rules": {"note": "any"}})
        text = f"{cfg.name}\n{DRIVERS[cfg.driver]['name']}\n{cfg.stock_color} labels · {cfg.width_px} dots"
        return run(caller, "test_print", print_text, c, text, "TEST PRINT", dry_run=dry_run)

    # ----------------------------------------------------------- to-do app connection
    @app.get("/providers")
    def providers():
        return PROVIDERS

    @app.get("/connection")
    def get_connection(caller: Caller = Depends(person)):
        return connections.status(caller.name)

    @app.put("/connection")
    def put_connection(body: ConnectBody, caller: Caller = Depends(person)):
        try:
            status = connections.connect(caller.name, body.provider, body.fields)
        except ProviderError as exc:
            events.log(caller.name, "connect", ok=False, detail={"provider": body.provider}, error=str(exc))
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # a server we can't talk to at all
            events.log(caller.name, "connect", ok=False, kind="error", detail={"provider": body.provider},
                       error=str(exc), trace=traceback.format_exc())
            raise HTTPException(400, f"Couldn't connect: {exc}") from exc
        events.log(caller.name, "connect", detail={"provider": body.provider, "account": status.get("account")})
        return status

    @app.delete("/connection")
    def delete_connection(caller: Caller = Depends(person)):
        connections.disconnect(caller.name)
        events.log(caller.name, "disconnect")
        return connections.status(caller.name)

    # ----------------------------------------------------------- admin (support)
    def _target(username: str) -> dict:
        user = accounts.get(username)
        if not user:
            raise HTTPException(404, f"no user {username}")
        return user

    @app.get("/admin/users")
    def admin_users(caller: Caller = Depends(admin_only)):
        prints: dict[str, int] = {}
        for r in ctx.store.list_printed(10_000, household=scope(caller)):
            prints[r.get("by", "")] = prints.get(r.get("by", ""), 0) + 1
        out = []
        for u in accounts.all_users(scope(caller)):
            out.append({**{k: u[k] for k in ("username", "role", "disabled", "beta", "debug",
                                             "must_change", "created", "last_login")},
                        "sessions": len(accounts.sessions_for(u["username"])),
                        "app": connections.status(u["username"]).get("name"),
                        "prints": prints.get(u["username"], 0),
                        "errors_7d": sum(1 for e in events.query(user=u["username"], kind="error", limit=500)
                                         if e["ts"] > time.time() - 7 * 86400)})
        return out

    @app.post("/admin/users")
    def admin_create(body: NewUserBody, caller: Caller = Depends(admin_only)):
        if body.role not in ROLES:
            raise HTTPException(400, "unknown role")
        if caller.role != "superadmin" and body.role != "user":
            raise HTTPException(403, "only a superadmin can create admins")
        temp = accounts.temp_password()
        try:
            accounts.create(body.username, temp, role=body.role, beta=body.beta, must_change=True,
                            household_id=caller.household)
        except AuthError as exc:
            raise HTTPException(400, str(exc))
        audit(caller, "create_user", target=body.username.lower(), role=body.role, beta=body.beta)
        return {"username": body.username.lower(), "temp_password": temp}

    @app.post("/admin/users/{username}/reset-password")
    def admin_reset(username: str, caller: Caller = Depends(admin_only)):
        target = _target(username)
        if not can_manage(caller, target):
            raise HTTPException(403, "you can't reset this account")
        temp = accounts.temp_password()
        accounts.set_password(target["username"], temp, must_change=True)
        audit(caller, "reset_password", target=target["username"])
        return {"username": target["username"], "temp_password": temp}

    @app.patch("/admin/users/{username}")
    def admin_patch(username: str, body: UserPatch, caller: Caller = Depends(admin_only)):
        target = _target(username)
        changes = body.model_dump(exclude_none=True)
        if not changes:
            raise HTTPException(400, "nothing to change")
        if not can_manage(caller, target):
            raise HTTPException(403, "you can't change this account")
        if ({"role", "debug"} & set(changes)) and caller.role != "superadmin":
            raise HTTPException(403, "only a superadmin can change roles or debug mode")
        try:
            updated = accounts.update(target["username"], **changes)
        except AuthError as exc:
            raise HTTPException(400, str(exc))
        audit(caller, "update_user", target=target["username"], changes=changes)
        return updated

    @app.post("/admin/users/{username}/signout")
    def admin_signout(username: str, caller: Caller = Depends(admin_only)):
        target = _target(username)
        if not can_manage(caller, target):
            raise HTTPException(403, "you can't sign out this account")
        n = accounts.revoke_user_sessions(target["username"])
        audit(caller, "signout_user", target=target["username"], sessions=n)
        return {"signed_out": n}

    @app.delete("/admin/users/{username}")
    def admin_delete(username: str, caller: Caller = Depends(super_only)):
        target = _target(username)
        if target["username"] == caller.name:
            raise HTTPException(400, "you can't delete yourself")
        try:
            accounts.remove_user(target["username"])
        except AuthError as exc:
            raise HTTPException(400, str(exc))
        feedback_store.forget_user(target["username"])  # their posts stay, unlinked
        LocalTasks(accounts.db, target["username"]).delete_for_user()
        Tags(accounts.db, target["username"]).delete_for_user()
        audit(caller, "delete_user", target=target["username"])
        return {"ok": True}

    @app.get("/admin/users/{username}/activity")
    def admin_activity(username: str, limit: int = 100, caller: Caller = Depends(admin_only)):
        target = _target(username)
        if target["username"] != caller.name and not can_manage(caller, target):
            raise HTTPException(403, "you can't see this account's activity")
        evs = events.query(user=username.lower(), limit=min(limit, 500))
        if caller.role != "superadmin":
            evs = [{k: v for k, v in e.items() if k != "trace"} for e in evs]
        return evs

    @app.get("/admin/feedback")
    def admin_feedback(caller: Caller = Depends(admin_only)):
        return feedback_store.board(caller.name, include_hidden=True)

    @app.patch("/admin/feedback/{fid}")
    def respond_feedback(fid: str, body: FeedbackResponse, caller: Caller = Depends(admin_only)):
        try:
            item = feedback_store.respond(fid, status=body.status, reply=body.reply,
                                          names=accounts.list_users())
        except KeyError:
            raise HTTPException(404) from None
        except FeedbackError as exc:
            raise HTTPException(400, str(exc)) from exc
        audit(caller, "respond_feedback", feedback=fid, status=body.status, replied=body.reply is not None)
        return item

    # ----------------------------------------------------------- usage (superadmin)
    # ------------------------------------------------- super admin: every user, every household
    def _user_rows() -> list[dict]:
        from sqlalchemy import func as _func
        from sqlalchemy import select as _select

        from . import db as _db
        week, month = time.time() - 7 * 86400, time.time() - 30 * 86400
        since30 = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(month))
        prints: dict[str, int] = {}
        for r in ctx.store.list_printed(100_000, since=since30):
            prints[r.get("by", "")] = prints.get(r.get("by", ""), 0) + 1
        errors: dict[str, int] = {}
        for e in events.query(kind="error", since=week, limit=10_000):
            errors[e["user"]] = errors.get(e["user"], 0) + 1
        with accounts.db.connect() as conn:
            open_tasks = dict(conn.execute(_select(_db.tasks.c.owner, _func.count()).where(
                _db.tasks.c.done_at.is_(None)).group_by(_db.tasks.c.owner)).all())
            live = dict(conn.execute(_select(_db.sessions.c.username, _func.count()).where(
                _db.sessions.c.expires > int(time.time())).group_by(_db.sessions.c.username)).all())
        homes = {h["id"]: h["name"] for h in accounts.households()}
        rows = []
        for u in accounts.all_users():
            conn_ = u.get("connection") or {}
            consent = u.get("consent") or {}
            rows.append({
                "username": u["username"], "email": u.get("email"), "role": u["role"],
                "household_id": u["household_id"], "household": homes.get(u["household_id"], u["household_id"]),
                "disabled": u["disabled"], "beta": u["beta"], "debug": u["debug"], "must_change": u["must_change"],
                "created": u["created"], "last_login": u["last_login"], "sessions": live.get(u["username"], 0),
                "app": READY_NAMES.get(conn_.get("provider"), conn_.get("provider")) if conn_ else None,
                "open_tasks": open_tasks.get(u["username"], 0), "prints_30d": prints.get(u["username"], 0),
                "errors_7d": errors.get(u["username"], 0),
                "consented": consent.get("version") == DATA_NOTICE_VERSION,
                "active_7d": bool(u["last_login"] and u["last_login"] > week),
            })
        return rows

    def _guard_self(caller: Caller, username: str, changes: dict) -> None:
        """A superadmin can't lock themselves out from this panel."""
        if username == caller.name and (changes.get("disabled") or changes.get("role") not in (None, "superadmin")):
            raise HTTPException(400, "you can't turn off or demote your own account here")

    @app.get("/super/overview")
    def super_overview(caller: Caller = Depends(super_only)):
        rows = _user_rows()
        return {
            "people": len(rows), "active_7d": sum(r["active_7d"] for r in rows),
            "turned_off": sum(r["disabled"] for r in rows), "beta": sum(r["beta"] for r in rows),
            "debug": sum(r["debug"] for r in rows), "temp_password": sum(r["must_change"] for r in rows),
            "admins": sum(r["role"] == "admin" for r in rows), "superadmins": sum(r["role"] == "superadmin" for r in rows),
            "with_errors": sum(r["errors_7d"] > 0 for r in rows), "households": len(accounts.households()),
            "prints_30d": sum(r["prints_30d"] for r in rows), "open_tasks": sum(r["open_tasks"] for r in rows),
        }

    @app.get("/super/users")
    def super_users(q: str = "", role: str = "", status: str = "", household: str = "",
                    caller: Caller = Depends(super_only)):
        rows = _user_rows()
        q = q.strip().lower()
        if q:
            rows = [r for r in rows if q in r["username"] or q in (r["email"] or "") or q in r["household"].lower()]
        if role:
            rows = [r for r in rows if r["role"] == role]
        if household:
            rows = [r for r in rows if r["household_id"] == household]
        checks = {"active": lambda r: r["active_7d"] and not r["disabled"], "off": lambda r: r["disabled"],
                  "beta": lambda r: r["beta"], "debug": lambda r: r["debug"], "temp": lambda r: r["must_change"],
                  "errors": lambda r: r["errors_7d"] > 0, "never": lambda r: not r["last_login"],
                  "no_consent": lambda r: not r["consented"]}
        if status:
            if status not in checks:
                raise HTTPException(400, f"status is one of {', '.join(checks)}")
            rows = [r for r in rows if checks[status](r)]
        return rows

    @app.get("/super/users.csv")
    def super_users_csv(caller: Caller = Depends(super_only)):
        import csv
        import io
        cols = ["username", "email", "role", "household", "disabled", "beta", "debug", "must_change", "created",
                "last_login", "sessions", "app", "open_tasks", "prints_30d", "errors_7d", "consented"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in _user_rows():
            # Spreadsheet apps run cells that start with = + - @ as formulas: neutralize them.
            w.writerow({k: ("'" + v if isinstance(v, str) and v[:1] in "=+-@" else v) for k, v in r.items()})
        audit(caller, "export_users")
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=nextbox-users.csv"})

    @app.get("/super/users/{username}")
    def super_user_detail(username: str, caller: Caller = Depends(super_only)):
        target = _target(username)
        name = target["username"]
        row = next(r for r in _user_rows() if r["username"] == name)
        return {**row, "prefs": target["prefs"], "connection": connections.status(name),
                "devices": accounts.sessions_for(name), "tags": Tags(accounts.db, name).all(),
                "recent": events.query(user=name, limit=15),
                "tickets": [{k: t.get(k) for k in ("id", "kind", "title", "created_at", "dry_run")}
                            for t in ctx.store.list_printed(8, by=name)]}

    @app.post("/super/users")
    def super_create(body: SuperNewUser, caller: Caller = Depends(super_only)):
        if body.role not in ROLES:
            raise HTTPException(400, "unknown role")
        home = body.household_id or caller.household
        if not accounts.household(home):
            raise HTTPException(400, "no such household")
        temp = accounts.temp_password()
        try:
            accounts.create(body.username, temp, role=body.role, beta=body.beta, must_change=True,
                            household_id=home, email=body.email or None)
        except AuthError as exc:
            raise HTTPException(400, str(exc))
        audit(caller, "create_user", target=body.username.lower(), role=body.role, household=home)
        return {"username": body.username.lower(), "temp_password": temp}

    @app.patch("/super/users/{username}")
    def super_patch(username: str, body: SuperUserPatch, caller: Caller = Depends(super_only)):
        target = _target(username)
        changes = body.model_dump(exclude_unset=True)
        if "email" in changes and changes["email"] == "":
            changes["email"] = None
        changes = {k: v for k, v in changes.items() if v is not None or k == "email"}
        if not changes:
            raise HTTPException(400, "nothing to change")
        _guard_self(caller, target["username"], changes)
        try:
            updated = accounts.update(target["username"], **changes)
        except AuthError as exc:
            raise HTTPException(400, str(exc))
        if changes.get("must_change"):
            accounts.revoke_user_sessions(target["username"])
        audit(caller, "update_user", target=target["username"], changes=changes)
        return updated

    @app.post("/super/users/{username}/reset-settings")
    def super_reset_settings(username: str, caller: Caller = Depends(super_only)):
        target = _target(username)
        with accounts.db.begin() as conn:
            from . import db as _db
            conn.execute(_db.users.update().where(_db.users.c.username == target["username"]).values(prefs=default_prefs()))
        audit(caller, "reset_settings", target=target["username"])
        return {"ok": True}

    @app.post("/super/users/{username}/unlink")
    def super_unlink(username: str, caller: Caller = Depends(super_only)):
        """Forget their linked to-do app (their built-in list stays). For a broken or stuck connection."""
        target = _target(username)
        connections.disconnect(target["username"])
        audit(caller, "unlink_app", target=target["username"])
        return {"ok": True}

    @app.post("/super/users/bulk")
    def super_bulk(body: BulkBody, caller: Caller = Depends(super_only)):
        actions = {"disable": {"disabled": True}, "enable": {"disabled": False},
                   "beta_on": {"beta": True}, "beta_off": {"beta": False}, "signout": None}
        if body.action not in actions:
            raise HTTPException(400, f"action is one of {', '.join(actions)}")
        if not body.usernames or len(body.usernames) > 500:
            raise HTTPException(400, "pick 1 to 500 people")
        results = {}
        for raw in dict.fromkeys(u.strip().lower() for u in body.usernames):
            if raw == caller.name and body.action in ("disable", "signout"):
                results[raw] = "skipped: that's you"
                continue
            if not accounts.get(raw):
                results[raw] = "no such user"
                continue
            try:
                if body.action == "signout":
                    results[raw] = f"signed out {accounts.revoke_user_sessions(raw)} device(s)"
                else:
                    accounts.update(raw, **actions[body.action])
                    results[raw] = "done"
            except AuthError as exc:
                results[raw] = str(exc)
        audit(caller, "bulk_users", op=body.action, count=len(results))
        return {"results": results}

    @app.get("/super/households")
    def super_households(caller: Caller = Depends(super_only)):
        return accounts.households()

    @app.post("/super/households")
    def super_new_household(body: HouseholdBody, caller: Caller = Depends(super_only)):
        if not body.name.strip():
            raise HTTPException(400, "give the household a name")
        hid = accounts.create_household(body.name)
        audit(caller, "create_household", household_id=hid)
        return accounts.household(hid)

    @app.get("/super/usage")
    def usage(days: int = 30, caller: Caller = Depends(super_only)):
        days = max(1, min(days, 365))
        since = date.fromtimestamp(time.time() - days * 86400).isoformat()
        tickets = [r for r in ctx.store.list_printed(100_000) if r["created_at"][:10] >= since]
        scans_ = [r for r in ctx.store.list_scans(100_000) if r["created_at"][:10] >= since]
        acts = [e for e in events.query(limit=5000) if e["kind"] in ("activity", "auth") and e["ok"]
                and date.fromtimestamp(e["ts"]).isoformat() >= since]
        series: dict[str, dict] = {}
        for i in range(min(days, 30)):
            d = date.fromtimestamp(time.time() - i * 86400).isoformat()
            series[d] = {"date": d, "tickets": 0, "scans": 0, "people": set()}
        for r in tickets:
            if r["created_at"][:10] in series:
                series[r["created_at"][:10]]["tickets"] += 1
        for r in scans_:
            if r["created_at"][:10] in series:
                series[r["created_at"][:10]]["scans"] += 1
        for e in acts:
            d = date.fromtimestamp(e["ts"]).isoformat()
            if d in series:
                series[d]["people"].add(e["user"])
        people = []
        for u in accounts.all_users():
            name = u["username"]
            mine = [r for r in tickets if r.get("by") == name]
            my_scans = [r for r in scans_ if r.get("by") == name]
            my_acts = [e for e in acts if e["user"] == name]
            features: dict[str, int] = {}
            for e in my_acts:
                features[e["action"]] = features.get(e["action"], 0) + 1
            by_reason: dict[str, int] = {}
            for r in mine:
                by_reason[r.get("reason", r["kind"])] = by_reason.get(r.get("reason", r["kind"]), 0) + 1
            people.append({
                "username": name, "role": u["role"], "beta": u["beta"],
                "app": connections.status(name).get("name"),
                "consented_at": (u.get("consent") or {}).get("at"),
                "last_active": max([u.get("last_login") or 0] + [int(e["ts"]) for e in my_acts]) or None,
                "tickets": len(mine), "printed": sum(1 for r in mine if not r["dry_run"]),
                "by_reason": by_reason, "scans": len(my_scans),
                "marks_applied": sum(len(r["applied"]) for r in my_scans),
                "marks_confirmed": sum(1 for r in my_scans for p in r["needs_confirmation"] if p["status"] == "applied"),
                "marks_skipped": sum(1 for r in my_scans for p in r["needs_confirmation"] if p["status"] == "skipped"),
                "top_actions": sorted(features.items(), key=lambda kv: -kv[1])[:6],
                "prefs": u["prefs"],
            })
        return {"days": days, "totals": {"people": len(people), "active": len({e["user"] for e in acts}),
                                         "tickets": len(tickets), "printed": sum(1 for r in tickets if not r["dry_run"]),
                                         "scans": len(scans_)},
                "series": [{**v, "people": len(v["people"])} for v in sorted(series.values(), key=lambda v: v["date"])],
                "people": sorted(people, key=lambda p: -(p["last_active"] or 0))}

    @app.get("/super/users/{username}/tickets")
    def user_tickets(username: str, limit: int = 30, caller: Caller = Depends(super_only)):
        _target(username)
        return {"tickets": ctx.store.list_printed(min(limit, 200), by=username.lower()),
                "scans": ctx.store.list_scans(min(limit, 200), by=username.lower())}

    # ----------------------------------------------------------- superadmin (debug)
    @app.get("/super/diagnostics")
    def diagnostics(caller: Caller = Depends(super_only)):
        todoist: dict = {"ok": False}  # "your to-do app": the superadmin's own connection
        start = time.time()
        try:
            info = rctx(caller).tasks.check()
            todoist.update(ok=True, app=connections.status(caller.name).get("name", "shared"),
                           projects=len(info.get("lists", [])))
        except Exception as exc:
            todoist["error"] = str(exc)[:300]
        todoist["ms"] = int((time.time() - start) * 1000)
        apps: dict[str, int] = {}
        for u in accounts.list_users():
            name = connections.status(u).get("name", "not connected")
            apps[name] = apps.get(name, 0) + 1
        start = time.time()
        printers_now = registry.all(caller.household)
        reachable = any(printer.is_reachable(p) for p in printers_now) if printers_now else False
        usage = shutil.disk_usage(data_dir)
        size = sum(p.stat().st_size for p in data_dir.rglob("*") if p.is_file())
        auto = json.loads((data_dir / "auto.json").read_text()) if (data_dir / "auto.json").exists() else {}
        return {
            "version": _version(), "python": sys.version.split()[0], "platform": platform.platform(),
            "uptime_s": int(time.time() - STARTED),
            "config": ctx.settings.redacted(),
            "printer": {"reachable": reachable, "ms": int((time.time() - start) * 1000),
                        "ip": ", ".join(f"{p.name} ({p.stock_color})" for p in printers_now) or None,
                        "label": ctx.settings.label},
            "todoist": todoist,
            "apps": apps,
            "vision": {"model": ctx.settings.vision_model, "key_set": bool(ctx.settings.anthropic_api_key)},
            "data_dir": {"path": str(data_dir), "used_mb": round(size / 1e6, 1),
                         "disk_free_gb": round(usage.free / 1e9, 1)},
            "counts": {"users": len(accounts.list_users()), "printed": len(ctx.store.list_printed(100_000)),
                       "scans": len(ctx.store.list_scans(100_000)),
                       "errors_24h": sum(1 for e in events.query(kind="error", limit=1000)
                                         if e["ts"] > time.time() - 86400)},
            "auto_print_last": auto,
            "server_settings": server_settings.get(),
        }

    @app.get("/super/feedback/{fid}/identity")
    def reveal_feedback(fid: str, caller: Caller = Depends(super_only)):
        """Who sent it, from the one file that knows. Audited by feedback id only, so the audit
        log itself never names the sender."""
        try:
            ident = feedback_store.identity(fid)
        except KeyError:
            raise HTTPException(404) from None
        audit(caller, "reveal_feedback_sender", feedback=fid)
        return ident

    @app.get("/super/events")
    def super_events(user: str | None = None, kind: str | None = None, errors_only: bool = False,
                     limit: int = 200, caller: Caller = Depends(super_only)):
        return events.query(user=user or None, kind=kind or None,
                            ok=False if errors_only else None, limit=min(limit, 1000))

    @app.get("/super/settings")
    def get_server_settings(caller: Caller = Depends(super_only)):
        return server_settings.get()

    @app.patch("/super/settings")
    def patch_server_settings(changes: dict, caller: Caller = Depends(super_only)):
        try:
            updated = server_settings.update(changes)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        audit(caller, "server_settings", changes=changes)
        return updated

    @app.get("/super/users/{username}/bundle")
    def debug_bundle(username: str, caller: Caller = Depends(super_only)):
        user = _target(username)
        name = user["username"]
        bundle = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "by": caller.name,
            "user": user,  # never contains the password hash
            "sessions": accounts.sessions_for(name),
            "events": events.query(user=name, limit=300),
            "connection": connections.status(name),  # never includes the sealed secret
            "printed": ctx.store.list_printed(20, by=name),
            "scans": ctx.store.list_scans(20, by=name),
            "server": {"version": _version(), "config": ctx.settings.redacted(),
                       "server_settings": server_settings.get()},
        }
        audit(caller, "debug_bundle", target=name)
        body = json.dumps(bundle, indent=2, ensure_ascii=False, default=str)
        return Response(body, media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="nextbox-debug-{name}-{time.strftime("%Y%m%d-%H%M")}.json"'})

    if Path(WEB_DIR).is_dir():
        app.mount("/app", StaticFiles(directory=WEB_DIR, html=True), name="app")

        @app.get("/")
        def root():
            return RedirectResponse("/app/")

    return app


def _migrate_feedback(events: Events, store: Feedback, accounts: Accounts) -> None:
    """Older versions logged feedback (with names) in events.jsonl. Move it to the anonymous
    board and the identity file, and remove it from the log."""
    for ev in events.take("feedback"):
        d = ev.get("detail") or {}
        try:
            store.submit(mode="open", kind="other", page=d.get("page", ""), rating=None, fields={},
                         message=d.get("message", ""), who=ev.get("user"), ip="", agent=d.get("agent", ""),
                         names=accounts.list_users(),
                         debug={"recent_errors": d["recent_errors"]} if d.get("recent_errors") else None,
                         today=date.fromtimestamp(ev.get("ts", time.time())), ts=ev.get("ts"),
                         enforce_limit=False)
        except FeedbackError:
            continue


class _OnlyPrinter:
    """A one-printer registry, for test prints."""
    def __init__(self, cfg):
        self.cfg = cfg

    def all(self, household=None):
        return [self.cfg]


def _build_id() -> str:
    """Short git commit of this copy, for the revision mark ("" when it isn't a git checkout)."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(PROJECT_DIR), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _safe_args(args: tuple, kwargs: dict) -> dict:
    """Arguments worth seeing when debugging; contexts, images and functions are skipped."""
    out = {}
    for i, a in enumerate(args):
        if isinstance(a, (str, int, float, bool)) or a is None:
            out[f"arg{i}"] = a if not isinstance(a, str) else a[:300]
        elif isinstance(a, dict):
            out[f"arg{i}"] = {str(k): v for k, v in list(a.items())[:20]}
    out.update({k: v for k, v in kwargs.items() if isinstance(v, (str, int, float, bool)) or v is None})
    return out


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("nextbox")
    except Exception:
        return "dev"


def build_default_app() -> FastAPI:
    from .config import load_settings
    from .store import Store

    import os

    os.umask(0o077)  # every file Next Box writes is readable by this desktop user only
    settings = load_settings()
    if not settings.server_key:
        raise SystemExit("NEXTBOX_KEY is not set in .env; refusing to start an unauthenticated server")
    store = Store(settings.data_dir)
    store.prune(settings.retention_days)
    return create_app(Context(settings=settings, store=store, printers=Printers(settings.data_dir, settings)))
