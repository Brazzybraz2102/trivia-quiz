"""HTTP API for the web app, phone app and Home Assistant. LAN only.

Two ways in:
- people sign in with a username + password (session cookie, or a bearer token for native apps)
- Home Assistant and scripts send the shared X-NextBox-Key header (no admin access)

Roles: user < admin (support) < superadmin (debug). Every admin action is written to the audit log.
"""
from __future__ import annotations

import dataclasses
import json
import os
import platform
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import jobs, printer, scan, upkeep
from .auth import ROLES, Accounts, AuthError, default_prefs, rank
from .config import PROJECT_DIR
from .connections import Builder, Connections, NotConnected
from .events import Events, ServerSettings
from .feedback import Feedback, FeedbackError
from .jobs import Context, print_text
from .journal import Journal, Slides, Wins, slugs as find_slugs
from .mylist import TAG_COLORS, LocalTasks, Tags, is_local, parse_dump
from .printer import COLORS, DRIVERS
from .printers import REASONS, PrinterError, Printers
from .printers import validate as validate_printer
from .providers import ProviderError, display_name, oauth
from .providers import providers as provider_list
from .providers.base import TaskNotFound
from .store import normalize_id
from .vault import Vault

WEB_DIR = PROJECT_DIR / "web"
UPDATE_URL = ("https://raw.githubusercontent.com/brazzybraz2102/trivia-quiz/"
              "claude/ticket-todoist-printer-nldz8a/next-box/pyproject.toml")
MAX_PHOTO_BYTES = 15 * 1024 * 1024
MAX_UPLOAD_BYTES = 16 * 1024 * 1024  # a whole /scan request (the photo plus the form around it)
MAX_BODY_BYTES = 256 * 1024          # every other request: the longest is a 20,000-character journal page
COOKIE = "nextbox_session"
OAUTH_COOKIE = "nb_oauth"            # proves the browser finishing "Sign in with Google" is the one that started
RESET_HOLD = 72 * 3600               # after an admin sets a one-time password: no data download or account delete
STARTED = time.time()
# Journal photos and tickets: never kept by the browser, so after signing out on a shared phone
# nobody can open them again from its history or cache.
PRIVATE = {"Cache-Control": "no-store", "Vary": "Cookie"}


class LoginBody(BaseModel):
    username: str
    password: str
    want_token: bool = False  # native apps: return a bearer token instead of relying on cookies


class PasswordBody(BaseModel):
    current: str
    new: str


class ProfileBody(BaseModel):
    email: str | None = None


class DeleteMeBody(BaseModel):
    password: str
    confirm: str  # must be the username


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


class ScanLabelBody(BaseModel):
    label_id: str  # the ticket the photo shows, picked or typed


class UndoBody(BaseModel):
    row: int


class NewUserBody(BaseModel):
    username: str
    role: str = "user"
    beta: bool = False


class StepIn(BaseModel):
    text: str
    done: bool = False


class TaskIn(BaseModel):
    content: str | None = None
    description: str | None = None
    priority: int | None = None
    due_date: str | None = None
    due_time: str | None = None
    repeat: str | None = None
    tags: list[str] | None = None
    steps: list[str | StepIn] | None = None
    minutes: int | None = None


class DumpBody(BaseModel):
    text: str


class CalendarBody(BaseModel):
    kind: str
    fields: dict[str, str]


class TagBody(BaseModel):
    color: str


class PickBody(BaseModel):
    task_ids: list[str] | None = None
    query: str | None = None
    dry_run: bool | None = None


class JournalBody(BaseModel):
    notes: str
    day: str | None = None


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


class SignupBody(BaseModel):
    username: str
    password: str
    invite: str
    email: str | None = None
    agree: bool = False  # the Terms of Use and Privacy Policy


TERMS_VERSION = 1  # bump when terms.html or privacy.html change in a way people should re-read


class InviteBody(BaseModel):
    days: int = 7
    beta: bool = True
    note: str = ""
    own_household: bool = False
    household_id: str | None = None


@dataclasses.dataclass
class Caller:
    name: str              # username, or "key" for Home Assistant/scripts
    role: str = "user"     # "service" for the shared key
    debug: bool = False
    token: str | None = None
    prefs: dict = dataclasses.field(default_factory=default_prefs)
    household: str = "home"


def create_app(ctx: Context, vision: scan.VisionFn | None = None,
               accounts: Accounts | None = None, builder: Builder | None = None,
               calendar_builder=None) -> FastAPI:
    """`ctx.tasks_factory`, when set, is one to-do app shared by everyone (tests only).
    Otherwise each person's own connection is used."""
    # The API explorer is off unless asked for: once Next Box is on the internet, nobody needs a map.
    show_docs = os.environ.get("NEXTBOX_API_DOCS", "").strip() in ("1", "true", "yes")
    app = FastAPI(title="Next Box", docs_url="/docs" if show_docs else None, redoc_url=None,
                  openapi_url="/openapi.json" if show_docs else None)
    # No allow_credentials: cookies only work same-origin; other clients use headers.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])
    app.add_middleware(_BodyLimit)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.url.path.startswith("/app"):
            # Always check for a newer copy, so an update shows up right away on phones and through
            # Cloudflare (which otherwise keeps .js files for hours).
            response.headers["Cache-Control"] = "no-cache"
        if request.url.scheme == "https":  # reached through the Cloudflare address: https only from now on
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
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
    upkeep.run_startup_fixes(accounts.db, ctx.store)
    # In memory, per person: photo read-backs cost money; calendar and app checks reach other servers.
    scan_window, calendar_window, connect_window = _Window(30, 3600), _Window(20, 3600), _Window(20, 3600)
    connections = Connections(ctx.settings, accounts, vault, **({"builder": builder} if builder else {}),
                              **({"calendar_builder": calendar_builder} if calendar_builder else {}))

    # ------------------------------------------------------------------ auth
    def _bearer(request: Request) -> str | None:
        h = request.headers.get("authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else None

    def _token(request: Request) -> str | None:
        return _bearer(request) or request.cookies.get(COOKIE)

    def _same_origin(request: Request) -> None:
        """Writes from a browser must come from this site's own pages (scripts send no Origin)."""
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "cross-site request refused")

    def auth(request: Request, x_nextbox_key: str | None = Header(default=None)) -> Caller:
        if x_nextbox_key:
            if from_internet(request):
                # Home Assistant and scripts are on the home network. The shared key never works
                # through the public address, so it can't be guessed from outside.
                raise HTTPException(403, "the Next Box key only works on the home network")
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
        # On a one-time password only choosing your own works: not your data download, account
        # deletion, profile or devices, which would let whoever set it act as you.
        if user["must_change"] and request.url.path != "/auth/password":
            raise HTTPException(403, "Choose your own password first (Settings → Account).")
        return Caller(name, role=user["role"], debug=user["debug"], token=token, prefs=user["prefs"],
                      household=user["household_id"])

    def from_internet(request: Request) -> bool:
        """Came in through Cloudflare (it adds these headers; a home-network request never has them)."""
        return any(h in request.headers for h in ("cf-connecting-ip", "cf-ray", "cdn-loop"))

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
        events.log(caller.name, action, kind="audit", detail=detail, household=caller.household,
                   target=detail.get("target"))

    def _guard_protected(caller: Caller, target_name: str) -> None:
        """The protected admin is changed only from the Next Box computer (`nextbox user ...`),
        so another superadmin can't reset, sign out, move or lock out the owner."""
        if target_name == accounts.protected() and caller.name != target_name:
            raise HTTPException(400, "That's the protected admin. Only the Next Box computer can change it "
                                     "(nextbox user …).")

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
                                   events=lambda day: connections.events_for(name, day),
                                   wins=lambda: wins_yesterday(name),
                                   slides=lambda: Slides(accounts.db, name).counts(),
                                   on_move=lambda tid: Slides(accounts.db, name).bump(tid),
                                   on_done=lambda tid, **kw: finished(name, household_of(caller), tid, **kw),
                                   save_page=lambda **kw: Journal(accounts.db, name, household_of(caller)).save_snap(**kw))

    def finished(name: str, household: str, task_id: str, day: str | None = None, scan_id: str | None = None) -> None:
        """A read-back finished a task. My list counted the win itself; a linked app's task counts
        here, once per day (it may have been ticked in the app too)."""
        Slides(accounts.db, name).clear(task_id)
        if not is_local(task_id):
            Wins(accounts.db, name, household).add(task_id, day or ctx.today().isoformat(), "readback",
                                                   scan_id=scan_id, once_a_day=True)

    def wins_by_day(name: str, first: date, last: date) -> dict[str, dict]:
        """What someone finished each day from `first` to `last`: {day: {"count", "items", "linked"}}.
        `items` names My list tasks and linked tasks read back from a ticket that's still kept;
        `linked` counts the linked app's tasks with no name to show (their text isn't stored)."""
        days = {(first + timedelta(days=i)).isoformat(): {"count": 0, "items": [], "linked": 0}
                for i in range((last - first).days + 1)}
        names: dict[tuple, str] = {}
        rows = Wins(accounts.db, name).between(first.isoformat(), last.isoformat())
        for scan_id in {r["scan_id"] for r in rows if r["scan_id"] and not r["content"]}:
            rec = ctx.store.get_scan(scan_id) or {}
            for a in rec.get("applied", []) + rec.get("needs_confirmation", []):
                names[(scan_id, str(a.get("task_id")))] = a.get("content", "")
        for r in rows:
            if r["day"] not in days:
                continue
            day = days[r["day"]]
            day["count"] += 1
            label = r["content"] or names.get((r["scan_id"], r["task_id"]))
            if label:
                day["items"].append(label)
            else:
                day["linked"] += 1
        return days

    def wins_yesterday(name: str) -> int:
        """Tasks finished yesterday: ticked on My list, or marked done on a photo read-back."""
        y = ctx.today() - timedelta(days=1)
        return wins_by_day(name, y, y)[y.isoformat()]["count"]

    def my_list(caller: Caller) -> LocalTasks:
        return LocalTasks(accounts.db, caller.name, caller.household, today=ctx.today)

    def run(caller: Caller, action: str, fn, *args, **kwargs):
        start = time.time()
        try:
            result = fn(*args, **kwargs)
        except HTTPException:
            raise
        except TaskNotFound as exc:  # ticked or changed on another device, or not theirs
            raise HTTPException(404, str(exc)) from exc
        except NotConnected as exc:
            raise HTTPException(409, str(exc)) from exc
        except scan.PhotoError as exc:
            raise HTTPException(415, str(exc)) from exc
        except ValueError as exc:  # something the person can fix: an empty list, a bad date
            raise HTTPException(400, str(exc)) from exc
        except ProviderError as exc:  # their app or server said no: worth showing, not a Next Box fault
            events.log(caller.name, action, ok=False, error=str(exc), detail={"args": _safe_args(args, kwargs)})
            raise HTTPException(502, str(exc)) from exc
        except Exception as exc:
            events.log(caller.name, action, ok=False, kind="error", error=str(exc),
                       trace=traceback.format_exc(), detail={"args": _safe_args(args, kwargs)})
            raise HTTPException(500, "Something went wrong. It's been logged so it can be fixed.") from exc
        detail = {"ms": int((time.time() - start) * 1000)}
        if isinstance(result, dict):
            detail.update({k: result[k] for k in ("id", "status", "kind", "reason") if k in result})
        if caller.debug:
            # Ids, counts and kinds only: never task text, notes or titles (admins read events).
            detail["args"] = _safe_args(args, kwargs)
            detail["result"] = _debug_result(result)
        events.log(caller.name, action, detail=detail)
        return result

    # ----------------------------------------------------------- endpoints
    @app.get("/health")
    def health():
        return {"ok": True}

    TOO_MANY = "Too many tries. Wait {n} minutes, then try again. Forgot it? Ask your admin for a one-time password."

    @app.post("/auth/login")
    def login(body: LoginBody, request: Request, response: Response):
        from hashlib import sha256
        _same_origin(request)
        client = request.client.host if request.client else "?"
        known = accounts.find_login(body.username)  # username or email; None if there's no such account
        username = known or body.username.strip().lower()
        # Whose password the guesses were at, without keeping what was typed (it may be a password).
        tag = sha256(username.encode()).hexdigest()[:16]
        account = f"user:{tag}"  # slows guesses at one account spread over many addresses
        if accounts.too_many_failures(client):
            raise HTTPException(429, TOO_MANY.format(n=5))
        if accounts.too_many_failures(account, limit=10, window=900):
            raise HTTPException(429, TOO_MANY.format(n=15))
        if not accounts.verify(username, body.password):
            user = accounts.get(known) if known else None
            # Only say "turned off" to someone who knows the password; everyone else gets the
            # same answer as a wrong password, and it counts toward the lockout.
            if user and user["disabled"] and accounts.password_ok(known, body.password):
                events.log(known, "login", ok=False, kind="auth", detail={"ip": client, "why": "disabled"})
                raise HTTPException(403, "this account is turned off; ask an admin")
            accounts.record_failure(client, tag)
            accounts.record_failure(account, tag)
            # Never log what was typed as the name: only a real account, or "?".
            events.log(known or "?", "login", ok=False, kind="auth", detail={"ip": client})
            here, there = 5 - accounts.failure_count(client), 10 - accounts.failure_count(account, 900)
            left, pause = min(here, there), 5 if here <= there else 15
            msg = "That username or password didn't match."
            if left <= 0:
                msg += f" That was the last try for now: wait {pause} minutes, then try again."
            elif left <= 2:
                msg += f" {left} {'try' if left == 1 else 'tries'} left before a {pause}-minute pause."
            raise HTTPException(401, msg)
        accounts.clear_failures(client, tag)
        token = accounts.create_session(username, client, request.headers.get("user-agent", ""))
        accounts.touch_login(username)
        events.log(username, "login", kind="auth", detail={"ip": client})
        user = accounts.get(username)
        out = {"username": username, "role": user["role"], "must_change": user["must_change"]}
        if body.want_token:
            return {**out, "token": token}
        _set_cookie(response, request, token)
        return out

    @app.post("/auth/signup")
    def signup(body: SignupBody, request: Request, response: Response):
        """Join with an invite code. There's no open sign-up: every new account needs a code."""
        _same_origin(request)
        client = request.client.host if request.client else "?"
        if accounts.too_many_failures(client):
            raise HTTPException(429, "Too many tries. Wait 5 minutes, then try again.")
        if not body.invite.strip():
            raise HTTPException(400, "you need an invite code to join")
        if not body.agree:
            raise HTTPException(400, "Please agree to the Terms of Use and Privacy Policy to join.")
        try:
            user = accounts.sign_up(body.username, body.password, email=body.email or None, invite=body.invite)
        except AuthError as exc:
            # Wrong codes and taken names or emails all count toward the lockout, so neither codes
            # nor accounts can be found by trying lots of them.
            accounts.record_failure(client)
            events.log("?", "signup", ok=False, kind="auth", detail={"ip": client}, error=str(exc))
            raise HTTPException(400, str(exc)) from exc
        name = user["username"]
        accounts.record_consent(name, None, terms=TERMS_VERSION)  # they ticked "I agree" to join
        events.log(name, "signup", kind="auth", detail={"ip": client, "household": user["household_id"],
                                                        "terms_version": TERMS_VERSION},
                   household=user["household_id"])
        token = accounts.create_session(name, client, request.headers.get("user-agent", ""))
        accounts.touch_login(name)
        _set_cookie(response, request, token)
        return {"username": name, "role": user["role"]}

    def _set_cookie(response: Response, request: Request, token: str) -> None:
        # Lax (not Strict) so coming back from "Sign in with Google" keeps you signed in. Cross-site
        # writes are still refused: browsers don't send Lax cookies on them, and auth() checks Origin.
        response.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https", path="/")

    @app.post("/auth/logout")
    def logout(request: Request, response: Response):
        _same_origin(request)
        accounts.end_session(_token(request))
        response.delete_cookie(COOKIE, path="/")
        # Journal photos and tickets are sent no-store; this also clears anything an older version
        # cached (browsers honour it on https and localhost).
        response.headers["Clear-Site-Data"] = '"cache"'
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
                "household_id": user["household_id"], "email": user.get("email"), "version": _version(),
                "household": (accounts.household(user["household_id"]) or {}).get("name", ""),
                "created": user.get("created"), "last_login": user.get("last_login"),
                "protected": accounts.protected() == name,
                "must_change": user["must_change"], "prefs": user["prefs"],
                "announcement": s["announcement"], "printing_paused": s["printing_paused"],
                "data_notice": DATA_NOTICE,
                "consented": consent.get("version") == DATA_NOTICE_VERSION, "consented_at": consent.get("at"),
                # Everyone agrees to the current Terms and Privacy Policy, including people an admin added.
                "terms_ok": consent.get("terms") == TERMS_VERSION, "terms_version": TERMS_VERSION,
                # They agreed to an older notice or older terms: say what's new.
                "notice_changed": consent.get("version") not in (None, DATA_NOTICE_VERSION)
                                  or consent.get("terms") not in (None, TERMS_VERSION),
                "password_reset": _reset_notice(user)}

    def _reset_notice(user: dict) -> dict | None:
        """Once they've chosen their own password: which admin last set a one-time one, and when
        (for 30 days), so nobody can quietly use an account they reset."""
        at = user.get("reset_at")
        if not at or user["must_change"] or at < time.time() - 30 * 86400:
            return None
        return {"by": user.get("reset_by"), "at": at}

    def _reset_hold(caller: Caller) -> None:
        """For 3 days after an admin set a one-time password, the data download and account delete
        wait: whoever knew that password can't take the journal or remove the account."""
        at = (accounts.get(caller.name) or {}).get("reset_at") or 0
        if at > time.time() - RESET_HOLD:
            until = date.fromtimestamp(at + RESET_HOLD).strftime("%b %-d")
            raise HTTPException(403, f"An admin reset your password recently, so downloading your data and "
                                     f"deleting your account are paused until {until}. This keeps your journal private.")

    @app.post("/auth/consent")
    def consent(request: Request):
        _same_origin(request)
        name = accounts.session_user(_token(request))
        if not name:
            raise HTTPException(401, "sign in required")
        accounts.record_consent(name, DATA_NOTICE_VERSION, terms=TERMS_VERSION)
        events.log(name, "data_notice_accepted", kind="auth",
                   detail={"version": DATA_NOTICE_VERSION, "terms_version": TERMS_VERSION})
        return {"ok": True}

    @app.get("/about")
    def about():
        """Public facts for the About, Privacy and Terms pages (no sign-in needed)."""
        return {"name": "Next Box", "version": _version(), "build": build,
                "operator": os.environ.get("NEXTBOX_OPERATOR", "").strip() or "Braznarkel",
                "contact": os.environ.get("NEXTBOX_CONTACT", "").strip(),
                "retention_days": ctx.settings.retention_days, "terms_version": TERMS_VERSION}

    @app.get("/version")
    def version():
        return {"version": _version(), "build": build, "credit": ctx.settings.credit}

    def _own_password_ok(name: str, password: str) -> bool:
        """Your password, when you change it or delete your account. Wrong ones count toward a
        lockout, so a signed-in device left unlocked can't be used to guess it."""
        key = f"pw:{name}"
        if accounts.too_many_failures(key):
            raise HTTPException(429, "Too many tries. Wait 5 minutes, then try again.")
        if not accounts.verify(name, password):
            accounts.record_failure(key)
            return False
        accounts.clear_failures(key)
        return True

    @app.post("/auth/password")
    def change_password(body: PasswordBody, request: Request, response: Response,
                        caller: Caller = Depends(person)):
        if not _own_password_ok(caller.name, body.current):
            raise HTTPException(401, "Your current password didn't match.")
        if body.new == body.current:  # also stops keeping the temporary password an admin saw
            raise HTTPException(400, "Pick a password that's different from your current one.")
        try:
            accounts.set_password(caller.name, body.new)  # signs out every device...
        except AuthError as exc:
            raise HTTPException(400, str(exc))
        token = accounts.create_session(caller.name, request.client.host if request.client else "",
                                        request.headers.get("user-agent", ""))
        _set_cookie(response, request, token)               # ...except this one
        events.log(caller.name, "password_changed", kind="auth")
        return {"ok": True, **({"token": token} if _bearer(request) else {})}

    # ---------------------------------------------------------- your own account
    @app.patch("/auth/profile")
    def edit_profile(body: ProfileBody, caller: Caller = Depends(person)):
        try:
            user = accounts.update(caller.name, email=(body.email or "").strip() or None)
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        events.log(caller.name, "profile_changed", kind="auth")
        return {"email": user.get("email")}

    @app.get("/auth/export")
    def export_my_data(caller: Caller = Depends(person)):
        """Everything Next Box keeps about you, as a file. Never includes passwords or tokens."""
        from sqlalchemy import select as _select

        from . import db as _db
        _reset_hold(caller)
        user = accounts.get(caller.name)
        mine = my_list(caller)
        with accounts.db.connect() as conn:
            tasks = [mine._task(r) for r in conn.execute(
                _select(_db.tasks).where(_db.tasks.c.owner == caller.name)).mappings()]
        data = {
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "account": {k: user.get(k) for k in ("username", "email", "role", "household_id", "created", "last_login")},
            "settings": user["prefs"], "tags": Tags(accounts.db, caller.name).all(), "tasks": tasks,
            "linked_app": {k: v for k, v in connections.status(caller.name).items() if k not in ("config",)},
            "calendars": [{k: c.get(k) for k in ("name", "provider", "calendars")} for c in connections.calendars(caller.name)],
            "tickets": [{k: v for k, v in t.items() if k != "png"} for t in ctx.store.list_printed(10_000, by=caller.name)],
            "read_backs": ctx.store.list_scans(10_000, by=caller.name),
            "journal": Journal(accounts.db, caller.name).pages("0000-00-00", "9999-99-99"),  # photos: GET /journal/{id}/photo
            "wins": Wins(accounts.db, caller.name).between("0000-00-00", "9999-99-99"),
            "devices": accounts.sessions_for(caller.name),
        }
        events.log(caller.name, "export_data", kind="auth")
        return Response(json.dumps(data, indent=2, default=str), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="next-box-{caller.name}.json"'})

    @app.post("/auth/delete-account")
    def delete_my_account(body: DeleteMeBody, request: Request, response: Response,
                          caller: Caller = Depends(person)):
        """Delete yourself and everything Next Box keeps about you. Your linked apps aren't touched."""
        if body.confirm.strip().lower() != caller.name:
            raise HTTPException(400, "type your username to confirm")
        if not _own_password_ok(caller.name, body.password):
            raise HTTPException(403, "wrong password")
        _reset_hold(caller)
        try:
            upkeep.purge_user(accounts, ctx.store, events, feedback_store, caller.name)  # feedback stays, unlinked
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        events.log("?", "account_deleted", kind="audit")  # no name: they asked to be forgotten
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    _update_cache: dict = {}

    @app.get("/super/update-check")
    def update_check(caller: Caller = Depends(super_only)):
        """Is there a newer Next Box? Looks at the published version at most every 6 hours."""
        current = _version()
        hit = _update_cache.get("latest")
        if not hit or hit[0] < time.time() - 6 * 3600:
            latest = None
            try:
                import tomllib

                import httpx
                r = httpx.get(UPDATE_URL, timeout=5.0)
                if r.status_code == 200:
                    latest = tomllib.loads(r.text)["project"]["version"]
            except Exception:
                latest = None
            hit = (time.time(), latest)
            _update_cache["latest"] = hit
        latest = hit[1]

        def as_tuple(v):
            return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3])
        return {"current": current, "latest": latest,
                "update_available": bool(latest) and as_tuple(latest) > as_tuple(current),
                "how": "On the Next Box computer: cd ~/next-box && scripts/update.sh"}

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
                names=_scrub_names(accounts), debug=debug)
        except FeedbackError as exc:
            raise HTTPException(400, str(exc)) from exc
        # Deliberately not written to the events log: that would link the person to the post.
        return {**item, "mine": True}

    @app.get("/feedback")
    def feedback_board(type: str | None = None, status: str | None = None,
                       caller: Caller = Depends(person)):
        # Moderation is for Next Box staff: the board spans every household.
        return feedback_store.board(caller.name, include_hidden=caller.role == "superadmin",
                                    kind=type or None, status=status or None)

    @app.delete("/feedback/{fid}")
    def withdraw_feedback(fid: str, caller: Caller = Depends(person)):
        is_staff = caller.role == "superadmin"
        own = feedback_store.is_author(fid, caller.name)  # before withdraw() forgets who sent it
        if not (own or is_staff):
            raise HTTPException(403, "you can only withdraw your own feedback")
        try:
            feedback_store.withdraw(fid)
        except KeyError:
            raise HTTPException(404) from None
        if is_staff and not own:  # withdrawing your own post isn't moderation, and mustn't name you
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
                "one_thing": _one_thing(caller),
                "marks_waiting": sum(len(_waiting(r)) for r in ctx.store.list_scans(50, by=caller.name))}

    def _waiting(rec: dict) -> list[dict]:
        """Marks on a read-back still waiting for the person to confirm or skip."""
        return [p for p in rec.get("needs_confirmation", []) if p.get("status") == "pending"]

    def _one_thing(caller: Caller):
        c = rctx(caller)
        try:
            return jobs.one_thing(c)
        except Exception:  # a linked app that's down shouldn't hide what's on your own list
            return jobs.one_thing(dataclasses.replace(c, tasks_factory=lambda: my_list(caller), _tasks=None))

    # ----------------------------------------------------------- Get started guide
    SETUP_STEPS = ("tasks", "home", "app", "calendar", "printer", "preview", "today", "snap", "focus")
    NEW_STEPS = ("today", "snap")  # added later: people who'd already finished the others aren't sent back
    _reach_cache: dict[str, tuple[float, bool]] = {}

    def _has_own_tasks(caller: Caller) -> bool:
        from sqlalchemy import select as _select

        from . import db as _db
        with accounts.db.connect() as conn:  # open or done
            return conn.execute(_select(_db.tasks.c.id).where(_db.tasks.c.owner == caller.name).limit(1)).first() is not None

    @app.get("/setup")
    def setup_guide(caller: Caller = Depends(person)):
        """The Get started checklist. Steps tick themselves off from what the person has done, and
        stay ticked (prefs.setup_done); optional ones can be skipped (prefs.setup_dismissed)."""
        dismissed = set(caller.prefs.get("setup_dismissed") or [])
        ticked = {k for k in caller.prefs.get("setup_done") or [] if k in SETUP_STEPS}
        done: dict[str, bool] = {k: True for k in ticked}
        linked = None
        if "tasks" not in ticked or "app" not in ticked:
            linked = bool(connections.status(caller.name).get("connected"))
        if "tasks" not in ticked:  # finishing your only task doesn't untick it
            done["tasks"] = linked or _has_own_tasks(caller)
        if "app" not in ticked:
            done["app"] = linked
        if "calendar" not in ticked:
            done["calendar"] = bool(connections.calendars(caller.name))
        if "preview" not in ticked or "today" not in ticked:
            tickets = ctx.store.list_printed(200, by=caller.name)
            done["preview"] = done.get("preview") or any(
                t.get("kind") in ("stickers", "strips") and t.get("manifest") for t in tickets)
            done["today"] = done.get("today") or any(t.get("kind") == "today" for t in tickets)
        if "snap" not in ticked:
            done["snap"] = bool(ctx.store.list_scans(1, by=caller.name))
        if "printer" not in dismissed and "printer" not in ticked:
            hit = _reach_cache.get(caller.household)
            if not hit or hit[0] < time.time() - 60:  # don't knock on the printer every page load
                mine = registry.all(caller.household)[:3]
                hit = (time.time(), any(printer.is_reachable(p, timeout=1.0) for p in mine))
                _reach_cache[caller.household] = hit
            done["printer"] = hit[1]
        if caller.prefs.get("setup_hidden") or all(done.get(k) or k in dismissed
                                                    for k in SETUP_STEPS if k not in NEW_STEPS):
            done.update({k: True for k in NEW_STEPS})
        newly = {k for k in SETUP_STEPS if done.get(k)} - ticked
        if newly:
            accounts.set_prefs(caller.name, {"setup_done": sorted(ticked | newly)})
        steps = [{"key": k, "done": bool(done.get(k)) or k in dismissed, "skipped": k in dismissed and not done.get(k)}
                 for k in SETUP_STEPS]
        return {"steps": steps, "hidden": bool(caller.prefs.get("setup_hidden")),
                "can_add_printer": rank(caller.role) >= rank("admin"),
                "all_done": all(s["done"] for s in steps)}

    @app.post("/setup/{step}/{action}")
    def setup_mark(step: str, action: str, caller: Caller = Depends(person)):
        if step not in SETUP_STEPS and step != "guide":
            raise HTTPException(404, "no such step")
        prefs = caller.prefs
        if step == "guide":
            if action not in ("hide", "show"):
                raise HTTPException(400, "hide or show")
            accounts.set_prefs(caller.name, {"setup_hidden": action == "hide"})
            return {"ok": True}
        dismissed = [x for x in (prefs.get("setup_dismissed") or []) if x in SETUP_STEPS]
        if action == "done":
            dismissed = sorted(set(dismissed) | {step})
        elif action == "undo":
            dismissed = [x for x in dismissed if x != step]
        else:
            raise HTTPException(400, "done or undo")
        accounts.set_prefs(caller.name, {"setup_dismissed": dismissed})
        return {"ok": True}

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
        mine = my_list(caller)
        before = None
        old_steps = None
        if fields.get("due_date") or fields.get("steps"):
            try:
                had = mine.get_task(_local_id(task_id))
                before = ((had.get("due") or {}).get("date") or "")[:10]
                old_steps = [s["text"] for s in had.get("steps") or []]
            except Exception:
                before = None
        result = run(caller, "list_edit", mine.update, _local_id(task_id), fields)
        if before and fields.get("due_date") and fields["due_date"] > before:
            Slides(accounts.db, caller.name).bump(task_id)  # moved to a later day
        new_steps = [s["text"] for s in result.get("steps") or []]
        if old_steps is not None and new_steps and new_steps != old_steps:
            Slides(accounts.db, caller.name).clear(task_id)  # made smaller: a fresh start
        return result

    @app.post("/list/{task_id}/done")
    def done_list_task(task_id: str, caller: Caller = Depends(person)):
        c = rctx(caller)
        run(caller, "list_done", c.tasks.close_task, task_id)
        Slides(accounts.db, caller.name).clear(task_id)
        if not is_local(task_id):  # My list counts its own wins; a linked app's task counts here
            events.log(caller.name, "task_done", detail={"source": "linked", "task_id": task_id[:200]},
                       household=caller.household)
            Wins(accounts.db, caller.name, caller.household).add(task_id, ctx.today().isoformat(), "linked",
                                                                 once_a_day=True)
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
    def printed(limit: int = Query(50, ge=1, le=500), caller: Caller = Depends(auth)):
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
        return FileResponse(path, media_type="image/png", headers=PRIVATE)

    @app.get("/printed/{label_id}/pdf")
    def printed_pdf(label_id: str, paper: str = "", caller: Caller = Depends(auth)):
        """Print on paper: the ticket laid out on a Letter or A4 page, for any printer."""
        from . import paper as paper_pdf
        label_id = normalize_id(label_id)
        if not re.fullmatch(r"[A-Z0-9-]{4,16}", label_id):
            raise HTTPException(404)
        rec = ctx.store.get_printed(label_id)
        if not rec or (rec.get("by") != acting_as(caller) and caller.role != "superadmin"):
            raise HTTPException(404)
        png = ctx.store.get_png(label_id)
        if not png:
            raise HTTPException(404)
        size = paper or caller.prefs.get("paper_size", "letter")
        if size not in paper_pdf.PAGES:
            raise HTTPException(400, "paper is letter, a4, a5 or half_letter")
        data = paper_pdf.ticket_pdf(png, ctx.store.get_parts(label_id), size)
        events.log(caller.name, "print_paper", detail={"id": label_id, "paper": size})
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="next-box-{label_id}.pdf"', **PRIVATE})

    @app.post("/scan")
    async def do_scan(photo: UploadFile = File(...), label_id: str | None = Form(default=None),
                      caller: Caller = Depends(auth)):
        if not scan_window.allow(caller.name):  # each read-back is a paid call to the vision service
            raise HTTPException(429, "That's a lot of photos this hour. Try again in a bit.")
        chunks, size = [], 0
        while chunk := await photo.read(1 << 20):
            size += len(chunk)
            if size > MAX_PHOTO_BYTES:
                raise HTTPException(413, "photo too large")
            chunks.append(chunk)
        data = b"".join(chunks)
        media_type = photo.content_type or "image/jpeg"
        if media_type in scan.HEIC_TYPES and not scan.heic_supported():
            raise HTTPException(415, scan.HEIC_HELP)
        if media_type not in {"image/jpeg", "image/png", "image/webp", "image/gif", *scan.HEIC_TYPES}:
            raise HTTPException(415, f"unsupported image type {media_type}")
        try:
            data, media_type = scan.prepare_photo(data)  # upright, resized JPEG
        except scan.PhotoError as exc:
            raise HTTPException(415, str(exc)) from exc
        except ValueError as exc:  # something the person can fix: an empty list, a bad date
            raise HTTPException(400, str(exc)) from exc
        fn = vision or scan.claude_vision(ctx.settings)
        rec = run(caller, "scan", scan.scan_photo, rctx(caller), fn, data, media_type, label_id or None)
        return _scan_reply(caller, rec)

    def _scan_reply(caller: Caller, rec: dict) -> dict:
        if rec.get("status") == "failed":  # the photo is kept; staff see why reading it failed
            events.log(caller.name, "scan", ok=False, kind="error", error=rec.get("vision_error", ""),
                       detail={"id": rec["id"]}, household=caller.household)
        if rec.get("journal_id"):  # the notes live only in the private journal page: show them back to its owner
            page = Journal(accounts.db, acting_as(caller)).get(rec["journal_id"])
            rec = {**rec, "notes": page["notes"] if page else "", "win": page["win"] if page else ""}
        return rec

    @app.get("/scans")
    def scans(limit: int = Query(20, ge=1, le=500), pending: bool = False, caller: Caller = Depends(auth)):
        """Your read-backs, newest first. pending=true: only those with marks still waiting for you."""
        if pending:
            return [r for r in ctx.store.list_scans(50, by=acting_as(caller)) if _waiting(r)][:limit]
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

    @app.post("/scan/{scan_id}/label")
    def relabel_scan(scan_id: str, body: ScanLabelBody, caller: Caller = Depends(auth)):
        """Which ticket the photo shows, when the code couldn't be read or didn't match what was typed."""
        _own_scan(scan_id, caller)
        rec = run(caller, "scan_label", scan.relabel, rctx(caller), scan_id, body.label_id[:40])
        return _scan_reply(caller, rec)

    @app.post("/scan/{scan_id}/retry")
    def retry_scan(scan_id: str, caller: Caller = Depends(auth)):
        """Read a kept photo again after reading it failed. Only ever when the person taps Try again."""
        rec = _own_scan(scan_id, caller)
        if rec.get("status") != "failed":
            return _scan_reply(caller, rec)
        image = Journal(accounts.db, acting_as(caller)).photo(rec["journal_id"]) if rec.get("journal_id") else None
        if not image:
            raise HTTPException(409, "That photo wasn't kept, so snap it again.")
        if not scan_window.allow(caller.name):
            raise HTTPException(429, "That's a lot of photos this hour. Try again in a bit.")
        fn = vision or scan.claude_vision(ctx.settings)
        return _scan_reply(caller, run(caller, "scan_retry", scan.retry, rctx(caller), fn, scan_id, image))

    @app.post("/scan/{scan_id}/undo")
    def undo_scan(scan_id: str, body: UndoBody, caller: Caller = Depends(auth)):
        """Reverse one mark the read-back applied (a misread ✓ or →)."""
        _own_scan(scan_id, caller)
        c = rctx(caller)

        def go():
            try:
                return scan.undo(c, scan_id, body.row)
            except scan.CantUndo as exc:
                raise HTTPException(409, str(exc)) from exc
        rec = dict(run(caller, "scan_undo", go))
        undone = rec.pop("undone", None)  # only on the call that undid it, so a double tap takes back one
        if undone and undone["mark"] == "tomorrow":
            Slides(accounts.db, acting_as(caller)).unbump(undone["task_id"])
        elif undone and not is_local(undone["task_id"]):  # My list took its own win back when it reopened
            Wins(accounts.db, acting_as(caller)).remove_last(undone["task_id"], scan_id=scan_id)
        return rec

    # ----------------------------------------------------------- journal, wins, slugs
    # Journal pages are private: only their owner can read them, not even a superadmin.
    def _journal(caller: Caller) -> Journal:
        return Journal(accounts.db, caller.name, caller.household)

    def _page_id(page_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{12}", page_id):
            raise HTTPException(404)
        return page_id

    @app.get("/journal")
    def journal_days(until: str = "", days: int = 14, caller: Caller = Depends(person)):
        """Days with journal pages or wins, newest first: {day, pages, wins}."""
        try:
            last = date.fromisoformat(until) if until else ctx.today()
        except ValueError:
            raise HTTPException(400, "until is YYYY-MM-DD") from None
        if not date(2000, 1, 1) <= last <= ctx.today() + timedelta(days=366):
            raise HTTPException(400, "until is a day between 2000 and next year")
        days = max(1, min(days, 60))
        first = last - timedelta(days=days - 1)
        pages = _journal(caller).pages(first.isoformat(), last.isoformat())
        wins = wins_by_day(caller.name, first, last)
        today = ctx.today()
        out = []
        for i in range(days):
            day = (last - timedelta(days=i)).isoformat()
            mine = [p for p in pages if p["day"] == day]
            if mine or wins[day]["count"] or wins[day]["items"]:
                # "Today" and "Yesterday" by this Next Box's clock, the one that files the pages.
                out.append({"day": day, "pages": mine, "wins": wins[day], "is_today": day == today.isoformat(),
                            "is_yesterday": day == (today - timedelta(days=1)).isoformat()})
        # The next older day with anything on it, so "Older days" skips empty weeks and knows when to stop.
        older = [d for d in (_journal(caller).newest_day_before(first.isoformat()),
                             Wins(accounts.db, caller.name).newest_day_before(first.isoformat())) if d]
        return {"days": out, "next_until": max(older) if older else (first - timedelta(days=1)).isoformat(),
                "has_more": bool(older), "today": today.isoformat(), "total_pages": _journal(caller).count()}

    @app.get("/journal/wins")
    def journal_wins(days: int = 7, caller: Caller = Depends(person)):
        days = max(1, min(days, 60))
        last = ctx.today()
        wins = wins_by_day(caller.name, last - timedelta(days=days - 1), last)
        series = [{"day": d, "count": w["count"], "is_today": d == last.isoformat()} for d, w in sorted(wins.items())]
        return {"series": series, "total": sum(x["count"] for x in series)}

    @app.delete("/journal/slugs/{task_id:path}")
    def not_a_slug(task_id: str, caller: Caller = Depends(person)):
        """"Not a slug": off the slugs list until it's moved again."""
        if not task_id.strip() or len(task_id) > 200:
            raise HTTPException(404)
        Slides(accounts.db, caller.name).dismiss(task_id)
        return {"ok": True}

    @app.get("/journal/slugs")
    def journal_slugs(caller: Caller = Depends(person)):
        """Tasks that keep sliding to another day, gently, so they can be made smaller."""
        c = rctx(caller)
        try:
            tasks = c.tasks.today()
        except Exception as exc:
            return {"slugs": [], "error": str(exc)[:200]}
        moves = Slides(accounts.db, caller.name).counts()
        return {"slugs": find_slugs(tasks, moves, ctx.today(), int(caller.prefs.get("slug_after", 3)))}

    @app.post("/journal")
    def journal_add(body: JournalBody, caller: Caller = Depends(person)):
        if body.day:  # a page for a day the Journal tab shows: up to a year back, or tomorrow
            try:
                day = date.fromisoformat(body.day)
            except ValueError:
                raise HTTPException(400, "day looks like 2026-10-16") from None
            if not ctx.today() - timedelta(days=366) <= day <= ctx.today() + timedelta(days=1):
                raise HTTPException(400, "Pick a day in the last year (or tomorrow).")
        try:
            page = _journal(caller).add(body.day or ctx.today().isoformat(), body.notes)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        events.log(caller.name, "journal_add", household=caller.household)  # no text: it's private
        return page

    @app.patch("/journal/{page_id}")
    def journal_edit(page_id: str, body: JournalBody, caller: Caller = Depends(person)):
        try:
            return _journal(caller).update(_page_id(page_id), body.notes)
        except KeyError:
            raise HTTPException(404) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/journal/{page_id}")
    def journal_delete(page_id: str, confirm: bool = False, caller: Caller = Depends(person)):
        if not confirm:
            raise HTTPException(400, "Deleting a journal page needs confirm=true.")
        try:
            _journal(caller).delete(_page_id(page_id))
        except KeyError:
            raise HTTPException(404) from None
        events.log(caller.name, "journal_delete", household=caller.household)
        return {"ok": True}

    @app.get("/journal/{page_id}/photo")
    def journal_photo(page_id: str, caller: Caller = Depends(person)):
        data = _journal(caller).photo(_page_id(page_id))
        if not data:
            raise HTTPException(404)
        return Response(data, media_type="image/jpeg", headers=PRIVATE)

    # ----------------------------------------------------------- printers & label colors
    @app.get("/printing-options")
    def printing_options(caller: Caller = Depends(person)):
        admin = rank(caller.role) >= rank("admin")
        return {"printers": [p.public(admin) for p in registry.all(caller.household)], "colors": COLORS,
                "reasons": REASONS, "drivers": DRIVERS}

    def _restricted(caller: Caller) -> bool:
        """Households other than Home (the server owner's), unless it's staff: network printers on
        printer ports only, so nobody can point this computer at its own services or odd ports."""
        return caller.role != "superadmin" and caller.household != "home"

    def _usable(caller: Caller, cfg) -> bool:
        if not _restricted(caller):
            return True
        try:
            validate_printer(dataclasses.asdict(cfg), restricted=True)
            return True
        except PrinterError:
            return False  # saved before addresses were checked: never knocked on

    _status_cache: dict[str, tuple[float, dict]] = {}

    @app.get("/admin/printers/status")
    def printers_status(caller: Caller = Depends(admin_only)):
        hit = _status_cache.get(caller.household)
        if hit and hit[0] > time.time() - 60:  # at most one round of knocking a minute
            return hit[1]
        mine = registry.all(caller.household)
        result = {p.id: _usable(caller, p) and printer.is_reachable(p) for p in mine}
        _status_cache[caller.household] = (time.time(), result)
        audit(caller, "printer_check", printers=len(mine))
        return result

    @app.post("/admin/printers")
    def add_printer(body: PrinterBody, caller: Caller = Depends(admin_only)):
        try:
            cfg = registry.add(body.model_dump(exclude_none=True), caller.household, restricted=_restricted(caller))
        except PrinterError as exc:
            raise HTTPException(400, str(exc)) from exc
        _status_cache.pop(caller.household, None)
        audit(caller, "add_printer", printer=cfg.id, name=cfg.name, driver=cfg.driver)
        return cfg.public(True)

    @app.patch("/admin/printers/{pid}")
    def edit_printer(pid: str, body: PrinterBody, caller: Caller = Depends(admin_only)):
        changes = body.model_dump(exclude_none=True)
        try:
            cfg = registry.update(pid, changes, caller.household, restricted=_restricted(caller))
        except KeyError:
            raise HTTPException(404) from None
        except PrinterError as exc:
            raise HTTPException(400, str(exc)) from exc
        _status_cache.pop(caller.household, None)
        audit(caller, "edit_printer", printer=pid, changed=sorted(changes))
        return cfg.public(True)

    @app.delete("/admin/printers/{pid}")
    def delete_printer(pid: str, caller: Caller = Depends(admin_only)):
        registry.remove(pid, caller.household)
        _status_cache.pop(caller.household, None)
        audit(caller, "remove_printer", printer=pid)
        return {"ok": True}

    @app.post("/admin/printers/{pid}/test")
    def test_printer(pid: str, dry_run: bool = False, caller: Caller = Depends(admin_only)):
        cfg = registry.get(pid, caller.household)
        if not cfg:
            raise HTTPException(404)
        if not _usable(caller, cfg):
            raise HTTPException(400, "This printer's address isn't one Next Box can use. Edit it first.")
        c = dataclasses.replace(rctx(caller), printers=_OnlyPrinter(cfg),
                                prefs={**caller.prefs, "default_printer": cfg.id, "color_rules": {"note": "any"}})
        text = f"{cfg.name}\n{DRIVERS[cfg.driver]['name']}\n{cfg.stock_color} labels · {cfg.width_px} dots"
        return run(caller, "test_print", print_text, c, text, "TEST PRINT", dry_run=dry_run)

    # ----------------------------------------------------------- to-do app connection
    @app.get("/providers")
    def providers():
        return provider_list()

    # ---------------------------------------------------------------- calendars
    @app.get("/calendars")
    def get_calendars(caller: Caller = Depends(person)):
        return {"calendars": connections.calendars(caller.name), "kinds": connections.calendar_kinds()}

    @app.post("/calendars")
    def add_calendar(body: CalendarBody, caller: Caller = Depends(person)):
        if not calendar_window.allow(caller.name):  # each try fetches from another server
            raise HTTPException(429, "That's a lot of tries this hour. Try again in a bit.")
        try:
            entry = connections.add_calendar(caller.name, body.kind, body.fields)
        except ProviderError as exc:
            events.log(caller.name, "calendar_add", ok=False, detail={"kind": body.kind}, error=str(exc))
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            events.log(caller.name, "calendar_add", ok=False, kind="error", detail={"kind": body.kind},
                       error=str(exc), trace=traceback.format_exc())
            raise HTTPException(400, f"Couldn't add that calendar: {exc}") from exc
        events.log(caller.name, "calendar_add", detail={"kind": body.kind})
        return entry

    @app.delete("/calendars/{cal_id}")
    def remove_calendar(cal_id: str, caller: Caller = Depends(person)):
        connections.remove_calendar(caller.name, cal_id)
        events.log(caller.name, "calendar_remove")
        return {"ok": True}

    @app.get("/calendars/today")
    def calendar_today(caller: Caller = Depends(person)):
        found, problems = connections.events_for(caller.name, ctx.today())
        return {"events": found, "problems": problems}

    # ------------------------------------------------- sign in with Google / Microsoft / TickTick
    @app.get("/connect/{app_name}/{purpose}/start")
    def signin_start(app_name: str, purpose: str, request: Request, caller: Caller = Depends(person)):
        try:
            url, nonce = oauth.start(app_name, purpose, caller.name)
        except ProviderError as exc:
            raise HTTPException(400, str(exc)) from exc
        response = RedirectResponse(url, status_code=303)
        # Only this browser can finish it: a consent link sent to someone else fails at the callback.
        response.set_cookie(OAUTH_COOKIE, nonce, max_age=600, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https", path="/connect/")
        return response

    @app.get("/connect/{app_name}/callback")
    def signin_callback(app_name: str, request: Request, state: str = "", code: str = "", error: str = ""):
        """Where Google/Microsoft/TickTick send the person back. Who it's for comes from the
        one-time state, and it must be the same browser that started it (the nb_oauth cookie, and
        the signed-in account when there is one)."""
        def back(msg: str) -> RedirectResponse:
            response = RedirectResponse(f"/app/#settings&signin={quote(msg)}", status_code=303)
            response.delete_cookie(OAUTH_COOKIE, path="/connect/")
            return response
        who = oauth.who_started(state)
        if error or not code or not who:
            return back("Sign-in was cancelled." if error else "That sign-in expired. Try again.")
        session = request.cookies.get(COOKIE)
        if session and accounts.session_user(session) != who:
            return back(oauth.ELSEWHERE)
        try:
            result = connections.finish_signin(who, app_name, state, code,
                                               nonce=request.cookies.get(OAUTH_COOKIE, ""))
        except ProviderError as exc:
            events.log(who, "signin", ok=False, detail={"app": app_name}, error=str(exc))
            # The details stay in their activity; the page only shows messages it knows (app.js SIGNIN_MSGS).
            return back(oauth.ELSEWHERE if str(exc) == oauth.ELSEWHERE else "That sign-in didn't work. Try again.")
        events.log(who, "signin", detail={"app": app_name, "kind": result["kind"]})
        return back("Connected " + oauth.APPS[app_name]["name"])

    @app.get("/super/signin-setup")
    def signin_setup(caller: Caller = Depends(super_only)):
        return oauth.setup_status()

    @app.get("/connection")
    def get_connection(caller: Caller = Depends(person)):
        return connections.status(caller.name)

    @app.put("/connection")
    def put_connection(body: ConnectBody, caller: Caller = Depends(person)):
        if not connect_window.allow(caller.name):  # each try signs in to another server
            raise HTTPException(429, "That's a lot of tries this hour. Try again in a bit.")
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
                                             "must_change", "created", "last_login", "last_seen")},
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
        _guard_protected(caller, target["username"])
        temp = accounts.temp_password()
        accounts.set_password(target["username"], temp, must_change=True)
        accounts.mark_reset(target["username"], caller.name)  # they're told, and data download/delete wait
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
        _guard_protected(caller, target["username"])
        n = accounts.revoke_user_sessions(target["username"])
        audit(caller, "signout_user", target=target["username"], sessions=n)
        return {"signed_out": n}

    @app.delete("/admin/users/{username}")
    def admin_delete(username: str, caller: Caller = Depends(super_only)):
        target = _target(username)
        if target["username"] == caller.name:
            raise HTTPException(400, "you can't delete yourself")
        try:  # everything they had goes too, so a new account with the same name starts empty
            upkeep.purge_user(accounts, ctx.store, events, feedback_store, target["username"])
        except AuthError as exc:
            raise HTTPException(400, str(exc))
        audit(caller, "delete_user", target=target["username"])  # after the purge, so it stays
        return {"ok": True}

    @app.get("/admin/users/{username}/activity")
    def admin_activity(username: str, limit: int = Query(100, ge=1, le=500), caller: Caller = Depends(admin_only)):
        target = _target(username)
        if target["username"] != caller.name and not can_manage(caller, target):
            raise HTTPException(403, "you can't see this account's activity")
        if caller.role == "superadmin":  # staff also see what admins did to the account
            evs = events.query(involving=target["username"], limit=limit)
        else:
            evs = [{k: v for k, v in e.items() if k != "trace"}
                   for e in events.query(user=target["username"], limit=limit)]
        if target["username"] != caller.name:
            audit(caller, "view_user", target=target["username"], what="activity")
        return evs

    # ------------------------------------------------------------- invites
    @app.get("/admin/invites")
    def list_invites(caller: Caller = Depends(admin_only)):
        homes = {h["id"]: h["name"] for h in accounts.households()}
        return [{**i, "household": homes.get(i["household_id"], i["household_id"])}
                for i in accounts.invites(scope(caller))]

    @app.post("/admin/invites")
    def make_invite(body: InviteBody, caller: Caller = Depends(admin_only)):
        home = body.household_id or caller.household
        if caller.role != "superadmin" and (body.own_household or home != caller.household):
            raise HTTPException(403, "household admins invite people into their own household only")
        if not accounts.household(home):
            raise HTTPException(400, "no such household")
        try:
            code = accounts.create_invite(home, caller.name, days=body.days, beta=body.beta, note=body.note,
                                          own_household=body.own_household)
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        audit(caller, "create_invite", household_id=home, own_household=body.own_household, days=body.days)
        return {"code": code, "link": f"/app/#join={code}"}

    @app.delete("/admin/invites/{code}")
    def revoke_invite(code: str, caller: Caller = Depends(admin_only)):
        mine = {i["code"]: i for i in accounts.invites(scope(caller))}
        if code.strip().upper() not in mine:
            raise HTTPException(404, "no such invite")
        accounts.revoke_invite(code)
        audit(caller, "revoke_invite", code=code.strip().upper(),
              household_id=mine[code.strip().upper()]["household_id"])
        return {"ok": True}

    # Feedback moderation is staff-only: the board is shared by every household.
    @app.get("/admin/feedback")
    def admin_feedback(caller: Caller = Depends(super_only)):
        return feedback_store.board(caller.name, include_hidden=True)

    @app.patch("/admin/feedback/{fid}")
    def respond_feedback(fid: str, body: FeedbackResponse, caller: Caller = Depends(super_only)):
        try:
            item = feedback_store.respond(fid, status=body.status, reply=body.reply,
                                          names=_scrub_names(accounts))
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
        protected_name = accounts.protected()
        rows = []
        for u in accounts.all_users():
            conn_ = u.get("connection") or {}
            consent = u.get("consent") or {}
            rows.append({
                "username": u["username"], "email": u.get("email"), "role": u["role"],
                "household_id": u["household_id"], "household": homes.get(u["household_id"], u["household_id"]),
                "disabled": u["disabled"], "beta": u["beta"], "debug": u["debug"], "must_change": u["must_change"],
                "created": u["created"], "last_login": u["last_login"], "last_seen": u.get("last_seen"),
                "sessions": live.get(u["username"], 0),
                "app": display_name(conn_.get("provider")) if conn_ else None,
                "open_tasks": open_tasks.get(u["username"], 0), "prints_30d": prints.get(u["username"], 0),
                "errors_7d": errors.get(u["username"], 0),
                "consented": consent.get("version") == DATA_NOTICE_VERSION,
                "protected": u["username"] == protected_name,
                # Phones stay signed in for weeks: using the app counts, not just signing in.
                "active_7d": max(u["last_login"] or 0, u.get("last_seen") or 0) > week,
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
        if name != caller.name:
            audit(caller, "view_user", target=name, what="details")
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
        if {"must_change", "household_id", "email"} & set(changes):
            _guard_protected(caller, target["username"])
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
        _guard_protected(caller, target["username"])
        with accounts.db.begin() as conn:
            from . import db as _db
            conn.execute(_db.users.update().where(_db.users.c.username == target["username"]).values(prefs=default_prefs()))
        audit(caller, "reset_settings", target=target["username"])
        return {"ok": True}

    @app.post("/super/users/{username}/unlink")
    def super_unlink(username: str, caller: Caller = Depends(super_only)):
        """Forget their linked to-do app (their built-in list stays). For a broken or stuck connection."""
        target = _target(username)
        _guard_protected(caller, target["username"])
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
        protected_name = accounts.protected()
        for raw in dict.fromkeys(u.strip().lower() for u in body.usernames):
            if raw == caller.name and body.action in ("disable", "signout"):
                results[raw] = "skipped: that's you"
                continue
            if not accounts.get(raw):
                results[raw] = "no such user"
                continue
            if raw == protected_name and raw != caller.name:
                results[raw] = "skipped: protected admin"
            else:
                try:
                    if body.action == "signout":
                        results[raw] = f"signed out {accounts.revoke_user_sessions(raw)} device(s)"
                    else:
                        accounts.update(raw, **actions[body.action])
                        results[raw] = "done"
                except AuthError as exc:
                    results[raw] = str(exc)
            audit(caller, "bulk_users", op=body.action, target=raw, result=results[raw])  # one per person
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
                "last_active": max([u.get("last_login") or 0, u.get("last_seen") or 0]
                                   + [int(e["ts"]) for e in my_acts]) or None,
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
    def user_tickets(username: str, limit: int = Query(30, ge=1, le=200), caller: Caller = Depends(super_only)):
        name = _target(username)["username"]
        if name != caller.name:
            audit(caller, "view_user", target=name, what="tickets")
        return {"tickets": ctx.store.list_printed(limit, by=name),
                "scans": ctx.store.list_scans(limit, by=name)}

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
                     target: str | None = None, limit: int = Query(200, ge=1, le=1000),
                     caller: Caller = Depends(super_only)):
        """`user`: what they did and what admins did to their account. `target`: only the latter."""
        return events.query(involving=(user or "").strip().lower() or None, target=target or None,
                            kind=kind or None, ok=False if errors_only else None, limit=limit)

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

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        """People get a friendly page; apps and scripts get the usual JSON."""
        page = Path(WEB_DIR) / "404.html"
        wants_page = "text/html" in request.headers.get("accept", "") and request.method == "GET"
        if wants_page and page.exists() and not request.url.path.startswith(("/auth", "/super", "/admin", "/list", "/print")):
            return Response(page.read_text(), status_code=404, media_type="text/html")
        detail = getattr(exc, "detail", None) or "Not Found"
        return JSONResponse({"detail": detail}, status_code=404)

    if Path(WEB_DIR).is_dir():
        @app.get("/app/", include_in_schema=False)
        @app.get("/app/index.html", include_in_schema=False)
        def app_page():
            # The version in the script's address means a new version is never served from an old copy.
            html = (Path(WEB_DIR) / "index.html").read_text()
            return Response(html.replace('src="app.js"', f'src="app.js?v={_version()}-{build}"'),
                            media_type="text/html")

        app.mount("/app", StaticFiles(directory=WEB_DIR, html=True), name="app")

        @app.get("/")
        def root():
            return RedirectResponse("/app/")

    return app


def _scrub_names(accounts: Accounts) -> list[str]:
    """Names to take out of feedback: usernames, and the names people show (often their real
    ones), whole and word by word (3+ letters)."""
    names: list[str] = []
    for u in accounts.all_users():
        names.append(u["username"])
        shown = str((u.get("prefs") or {}).get("display_name") or "").strip()
        if shown:
            names.append(shown)
            names += [w for w in re.findall(r"[^\W\d_][\w'’-]*", shown) if len(w) >= 3]
    return names


def _migrate_feedback(events: Events, store: Feedback, accounts: Accounts) -> None:
    """Older versions logged feedback (with names) in events.jsonl. Move it to the anonymous
    board and the identity file, and remove it from the log."""
    for ev in events.take("feedback"):
        d = ev.get("detail") or {}
        try:
            store.submit(mode="open", kind="other", page=d.get("page", ""), rating=None, fields={},
                         message=d.get("message", ""), who=ev.get("user"), ip="", agent=d.get("agent", ""),
                         names=_scrub_names(accounts),
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


# Debug details are read by admins, so they hold shapes, not words: task text, journal notes, titles
# and filters never get into events. Strings are kept only when they look like an id.
_ID_LIKE = re.compile(r"nb:\d{1,15}|\d{6}-[A-Z0-9]{4}|\d{1,20}|[0-9a-f]{8,16}")


def _shape(v):
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        return v if _ID_LIKE.fullmatch(v) else f"str({len(v)})"
    if isinstance(v, (list, tuple, set, dict)):
        return f"{type(v).__name__}({len(v)})"
    return type(v).__name__


def _safe_args(args: tuple, kwargs: dict) -> dict:
    """Arguments worth seeing when debugging: ids, numbers and switches as they are, everything
    else as its type and length. Contexts, images and functions are skipped."""
    out = {}
    for i, a in enumerate(args):
        if isinstance(a, (str, int, float, bool)) or a is None:
            out[f"arg{i}"] = _shape(a)
        elif isinstance(a, dict):
            out[f"arg{i}"] = {str(k)[:40]: _shape(v) for k, v in list(a.items())[:20]}
    out.update({k: _shape(v) for k, v in kwargs.items() if isinstance(v, (str, int, float, bool)) or v is None})
    return out


_RESULT_KEEP = ("id", "status", "kind", "reason", "label_id", "label_code_read", "journal_id", "message")
_RESULT_COUNT = ("applied", "needs_confirmation", "ignored", "errors", "manifest", "also", "missing")


def _debug_result(result) -> dict:
    """What an action returned, for debug mode: ids, statuses and counts. Never notes, content,
    description, steps, text or titles."""
    if isinstance(result, list):
        return {"count": len(result)}
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    out = {k: result[k] for k in _RESULT_KEEP
           if k in result and (result[k] is None or isinstance(result[k], (str, int, float, bool)))}
    out.update({f"{k}_count": len(result[k]) for k in _RESULT_COUNT if isinstance(result.get(k), list)})
    return out


class _Window:
    """At most `limit` tries per `seconds` for each person (in memory; resets on restart)."""

    def __init__(self, limit: int, seconds: int):
        self.limit, self.seconds = limit, seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [t for t in self._hits.get(key, []) if now - t < self.seconds]
            if len(recent) >= self.limit:
                self._hits[key] = recent
                return False
            self._hits[key] = recent + [now]
            return True


class _BodyLimit:
    """Refuse oversized requests before reading them: 16 MB for a photo read-back, 256 KB for
    everything else. Bodies sent without a Content-Length are counted as they arrive."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        big = scope.get("path") == "/scan"
        limit = MAX_UPLOAD_BYTES if big else MAX_BODY_BYTES
        msg = "photo too large" if big else "That's more than Next Box takes in one go."
        length = dict(scope.get("headers") or []).get(b"content-length", b"")
        if length.isdigit() and int(length) > limit:
            response = JSONResponse({"detail": msg}, status_code=413, headers={"Connection": "close"})
            return await response(scope, receive, send)
        seen = 0

        async def counted():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise HTTPException(413, msg)  # FastAPI turns this into the usual JSON answer
            return message
        return await self.app(scope, counted, send)


def _version() -> str:
    """pyproject.toml is the source of truth (installed metadata can lag behind an update)."""
    try:
        import tomllib
        with open(PROJECT_DIR / "pyproject.toml", "rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except Exception:
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
