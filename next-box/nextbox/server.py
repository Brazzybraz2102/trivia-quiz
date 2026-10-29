"""HTTP API for the web app, phone app and Home Assistant. LAN only.

Two ways in:
- people sign in with a username + password (session cookie, or a bearer token for native apps)
- Home Assistant and scripts send the shared X-NextBox-Key header (no admin access)

Roles: user < admin (support) < superadmin (debug). Every admin action is written to the audit log.
"""
from __future__ import annotations

import asyncio
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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import coach as coaching
from . import discover, jobs, printer, quiz, render, scan, upkeep
from . import friends as friendship
from . import marks as mark_styles
from .auth import PREF_HINTS, ROLES, Accounts, AuthError, default_prefs, rank
from .config import PROJECT_DIR, app_version
from .connections import Builder, Connections, NotConnected
from .events import Events, ServerSettings
from .feedback import Feedback, FeedbackError
from .jobs import Context, print_text
from .journal import Journal, Slides, Wins, journal_zip, note_lines, slugs as find_slugs
from .mylist import (TAG_COLORS, LocalTasks, StepChanged, TagExists, Tags, _iso_day, is_local, parse_dump,
                     preview_dump, split_dump)
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
BIG_UPLOADS = {"/scan", "/notes/image", "/notes/preview/image"}  # a photo: up to MAX_UPLOAD_BYTES
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
    display_name: str | None = None  # left out: unchanged


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
    query: str = Field(max_length=200)
    title: str | None = Field(default=None, max_length=80)
    dry_run: bool = False


class TaskBody(BaseModel):
    task_id: str = Field(max_length=200)
    dry_run: bool = False


class TextBody(BaseModel):
    text: str = Field(max_length=jobs.MAX_TEXT)  # the label shows the first part; see jobs.MAX_BODY
    title: str = Field(default="NOTE", max_length=80)
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
    slides: int | None = Field(None, ge=-1, le=10_000)  # PATCH: Undo after Break it down puts the slide count back


class DumpBody(BaseModel):
    text: str


class ReopenBody(BaseModel):
    prev_due: str | None = Field(None, max_length=40)
    prev_steps: list[bool] | None = Field(None, max_length=50)
    slides: int | None = Field(None, ge=-1, le=10_000)  # its slide count before Done cleared it


class MoveBody(BaseModel):
    to: str | None = Field(None, max_length=20)  # "tomorrow", "today", YYYY-MM-DD, or none (My list only)
    undo: bool = False  # putting a move back: no slide counted, and the one it counted is taken back


class SkipBody(BaseModel):
    due: str | None = Field(None, max_length=40)  # the day the screen showed when Skip was pressed


class DoneBody(BaseModel):
    due: str | None = Field(None, max_length=40)  # the day the screen showed: a repeat that moved on isn't done twice


class StepBody(BaseModel):
    done: bool
    text: str | None = Field(None, max_length=400)  # the step as the screen showed it


class CalendarBody(BaseModel):
    kind: str
    fields: dict[str, str]


class TagBody(BaseModel):
    color: str


class NewTagBody(BaseModel):
    name: str
    color: str


class CalendarName(BaseModel):
    name: str


class PickBody(BaseModel):
    task_ids: list[str] | None = None
    query: str | None = Field(default=None, max_length=200)
    dry_run: bool | None = None


class JournalBody(BaseModel):
    notes: str | None = None  # PATCH: left out keeps the notes (moving a page to another day)
    day: str | None = None


class LinesBody(BaseModel):
    text: str = Field(max_length=20_000)  # a journal page's notes, as on screen


class FocusBody(BaseModel):
    task_id: str | None = Field(default=None, max_length=200)
    dry_run: bool | None = None
    # A linked app's task: tiny steps to print on the card only (never written to the app or kept).
    steps: list[str] | None = Field(default=None, max_length=12)


class BreakBody(BaseModel):
    hint: str = Field(default="", max_length=coaching.HINT_CHARS)  # what makes it hard (optional)


class LookBackBody(BaseModel):
    include_notes: bool = False  # the week's journal notes go too only when the person ticks it


class ConnectBody(BaseModel):
    provider: str
    fields: dict[str, str]


class FriendAddBody(BaseModel):  # one of the two
    username: str | None = Field(default=None, max_length=80)
    code: str | None = Field(default=None, max_length=80)


class NoteBody(BaseModel):
    to: list[str] = Field(default_factory=list, max_length=100)
    kind: str = Field(default="note", max_length=12)
    text: str | None = Field(default=None, max_length=4000)  # friends.Notes.check says the real limit in words
    color: str | None = Field(default=None, max_length=20)


class NotePreviewBody(BaseModel):
    kind: str = Field(default="note", max_length=12)
    text: str | None = Field(default=None, max_length=4000)
    color: str | None = Field(default=None, max_length=20)


class ReactBody(BaseModel):
    reaction: str | None = Field(default=None, max_length=16)  # one of friends.REACTIONS; null takes it back


class KeepBody(BaseModel):
    keep: bool = True


class NotePrintBody(BaseModel):
    confirm: bool = False  # the app sends true only after "Send this to the printer?" was answered yes
    dry_run: bool = False


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
# (Saying the journal stays private shows the admin nothing more, so that line needed no bump; the same
# goes for notes between friends, which the admin can't see either.)
DATA_NOTICE_VERSION = 3
DATA_NOTICE = ("The admin can see how you use Next Box: your printed tickets (including the tasks on "
               "them), photo read-backs, settings, activity, signed-in devices, and how many open tasks "
               "are on your built-in list (not what they say). Tasks on your built-in list are kept on "
               "this Next Box computer. Your journal stays private, even from the admin. So do the "
               "notes you send your friends: no admin screen shows them or who your friends are. Your password and "
               "your to-do app password or token are never visible to anyone, and feedback stays anonymous.")


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


TERMS_VERSION = 6  # bump when terms.html or privacy.html change in a way people should re-read
#   2: the Privacy Policy covers the Journal (photos and handwriting kept, private to you) and says
#      Claude (Anthropic) reads your handwriting as well as your marks.
#   3: with Journal mode off, a photo with handwriting (notes or a win) is still kept as a journal page.
#   4: it says what the AI helpers (Break it down, Look back on my week) send, only when tapped.
#   5: Snap also sends the marks you use (each symbol, its action and your words for what it means).
#   6: Friends: what's kept when you add friends and send notes, doodles, photos and hearts, who sees them
#      (only the two of you), 30 days unless kept, and that a note prints only when you tap Print.
DB_NAMES = {"sqlite": "SQLite", "postgresql": "PostgreSQL"}  # for the About page


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
               calendar_builder=None, coach: coaching.Coach | None = None) -> FastAPI:
    """`ctx.tasks_factory`, when set, is one to-do app shared by everyone (tests only).
    Otherwise each person's own connection is used. `coach` stands in for the AI helpers in tests."""
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
        if request.url.path.startswith(("/friends", "/notes")):  # personal: never kept by a browser or Cloudflare
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "Cookie"
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
    # Downloading the whole journal builds a big file: 10 an hour. One day's page to print: 60.
    journal_window, journal_day_window = _Window(10, 3600), _Window(60, 3600)
    export_window = _Window(5, 3600)   # Download my data (.zip): every photo and ticket image
    search_window = _Window(60, 60)    # journal search looks through every page: plenty for typing
    journal_add_window = _Window(300, 3600)  # new typed pages: far more than anyone writes
    coach_window = _Window(20, 3600)  # the AI helpers, both together: each one is a paid call
    ai = {"coach": coach}             # the real one is made on first use
    connections = Connections(ctx.settings, accounts, vault, **({"builder": builder} if builder else {}),
                              **({"calendar_builder": calendar_builder} if calendar_builder else {}),
                              today=ctx.today)

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

    def prints_here(household: str) -> bool:
        """Labels go straight to a label printer: Home, or a household with a printer of its own. Anyone
        else prints from their own device for now (jobs.PRINT_AWAY), and the app words things to match."""
        return jobs.prints_here(household, registry.all(household))

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
        return dataclasses.replace(ctx, user=name, household=household_of(caller), prefs=prefs, printers=registry,
                                   can_add_printer=rank(caller.role) >= rank("admin"),
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
        `linked` counts the linked app's tasks with no name to show (their text isn't stored);
        `other` (only when there are some) counts My list wins with no name (filled in from an
        older version, which kept none): they weren't in a linked app."""
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
            elif r["source"] in ("linked", "readback"):
                day["linked"] += 1
            else:
                day["other"] = day.get("other", 0) + 1
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
            raise HTTPException(400, "Type the invite code you were given.")
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
        # Joined with a real invite: their typos while joining (a code, a taken name) stop counting. Each
        # invite works once, so this can't be used to keep guessing codes.
        accounts.clear_failures(client, "?")
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
        printers = registry.all(user["household_id"])
        return {"username": name, "role": user["role"], "beta": user["beta"], "debug": user["debug"],
                "household_id": user["household_id"], "email": user.get("email"), "version": _version(),
                "household": (accounts.household(user["household_id"]) or {}).get("name", ""),
                "prints_here": jobs.prints_here(user["household_id"], printers),
                # The same printing flags as GET /status, so the app's Preview only box is right as soon as
                # this answers, before /status (which waits up to 2 s on whether the printer is online).
                "printers": len(printers),
                "dry_run_forced": ctx.settings.dry_run or s["printing_paused"]
                                  or bool(user["prefs"].get("always_dry_run")),
                "created": user.get("created"), "last_login": user.get("last_login"),
                "protected": accounts.protected() == name,
                "can_delete": accounts.removable(name),  # not the protected admin or the last superadmin
                "must_change": user["must_change"], "prefs": user["prefs"],
                "announcement": s["announcement"], "printing_paused": s["printing_paused"],
                "data_notice": DATA_NOTICE,
                "consented": consent.get("version") == DATA_NOTICE_VERSION, "consented_at": consent.get("at"),
                # Everyone agrees to the current Terms and Privacy Policy, including people an admin added.
                "terms_ok": consent.get("terms") == TERMS_VERSION, "terms_version": TERMS_VERSION,
                # They agreed to an older notice or older terms: say what's new.
                "notice_changed": consent.get("version") not in (None, DATA_NOTICE_VERSION)
                                  or consent.get("terms") not in (None, TERMS_VERSION),
                "password_reset": _reset_notice(user),
                "ai_ready": ai_ready(),  # the AI helpers have a key (or a stand-in) to work with
                "scan_ready": vision_ready(),  # Snap can read photos (its own stand-in in tests)
                "setup_quiz": _quiz_due(name, user["prefs"])}  # show the setup questions after the notice

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
            raise HTTPException(403, f"An admin reset your password recently, so downloading all your data and "
                                     f"deleting your account are paused until {until}.")

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
    def about(request: Request):
        """Public facts for the About, Privacy and Terms pages (no sign-in needed)."""
        # Through the public address, Cloudflare carries the traffic even if NEXTBOX_HOSTING was never set.
        hosting = os.environ.get("NEXTBOX_HOSTING", "").strip()[:80] or ("Cloudflare" if from_internet(request) else "")
        return {"name": "Next Box", "version": _version(), "build": build,
                "operator": os.environ.get("NEXTBOX_OPERATOR", "").strip() or "Braznarkel",
                "contact": os.environ.get("NEXTBOX_CONTACT", "").strip(),
                "retention_days": ctx.settings.retention_days, "terms_version": TERMS_VERSION,
                # What this copy really runs on, so the pages never name a database or host it doesn't use.
                "database": DB_NAMES.get(accounts.db.dialect.name, accounts.db.dialect.name),
                "hosting": hosting}

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
        """Your name to show and your email, together: both are checked before either is saved."""
        given = body.model_fields_set
        name = (body.display_name or "").strip()
        try:
            if "display_name" in given and len(name) > 40:
                raise AuthError(PREF_HINTS["display_name"])
            if "email" in given:
                accounts._check_email((body.email or "").strip() or None)  # its format, before anything is saved
                user = accounts.update(caller.name, email=(body.email or "").strip() or None)
            else:
                user = accounts.get(caller.name)
            prefs = accounts.set_prefs(caller.name, {"display_name": name}) if "display_name" in given else user["prefs"]
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        events.log(caller.name, "profile_changed", kind="auth")
        return {"email": user.get("email"), "prefs": prefs}

    @app.get("/auth/export/check")
    def export_check(caller: Caller = Depends(person)):
        """Would Download my data work right now? Asking doesn't count as a download. The app asks
        first, so a hold or the hourly limit shows as a message instead of a page of raw JSON."""
        _reset_hold(caller)
        if export_window.full(caller.name):
            raise HTTPException(429, "That's a lot of downloads this hour. Try again in a bit.")
        return {"ok": True}

    @app.get("/auth/export")
    def export_my_data(format: str = "json", caller: Caller = Depends(person)):
        """Everything Next Box keeps about you, as a file. Never includes passwords or tokens.
        format=zip: the same data.json plus your journal photos and ticket images."""
        from sqlalchemy import select as _select

        from . import db as _db
        _reset_hold(caller)
        if format == "zip" and not export_window.allow(caller.name):  # the heaviest download there is
            raise HTTPException(429, "That's a lot of downloads this hour. Try again in a bit.")
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
            "journal": Journal(accounts.db, caller.name).pages("0000-00-00", "9999-99-99"),  # photos: in the .zip
            "wins": Wins(accounts.db, caller.name).between("0000-00-00", "9999-99-99"),
            "devices": accounts.sessions_for(caller.name, caller.token),
            # Your friends, requests, blocks and your own sent and received notes (pictures: in the .zip)
            "friends": friendship.export(accounts.db, caller.name),
        }
        events.log(caller.name, "export_data", kind="auth", detail={"format": "zip" if format == "zip" else "json"})
        if format == "zip":
            return _export_zip(caller, data)
        return Response(json.dumps(data, indent=2, default=str), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="next-box-{caller.name}.json"', **PRIVATE})

    def _export_zip(caller: Caller, data: dict):
        """data.json, journal/<page>.jpg for each page with a photo, tickets/<id>.png for each ticket.
        Built in a temporary file (kept in memory while small), then streamed."""
        import tempfile
        import zipfile

        from fastapi.responses import StreamingResponse
        journal = Journal(accounts.db, caller.name)
        out = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("data.json", json.dumps(data, indent=2, default=str))
            for page in data["journal"]:
                photo = journal.photo(page["id"]) if page.get("has_photo") else None
                if photo:
                    z.writestr(f"journal/{page['day']}-{page['id']}.jpg", photo, compress_type=zipfile.ZIP_STORED)
            for t in data["tickets"]:
                png = ctx.store.get_png(t["id"]) if t.get("id") else None
                if png:
                    z.writestr(f"tickets/{t['id']}.png", png, compress_type=zipfile.ZIP_STORED)
            notes = friendship.Notes(accounts.db, caller.name)
            done: set[str] = set()
            for n in data["friends"]["notes"]["received"] + data["friends"]["notes"]["sent"]:
                if n["has_image"] and n["id"] not in done:
                    done.add(n["id"])
                    pic = notes.image(n["id"])
                    ext = "png" if friendship.image_type(pic) == "image/png" else "jpg"
                    z.writestr(f"notes/{n['id']}.{ext}", pic, compress_type=zipfile.ZIP_STORED)
        size = out.tell()
        out.seek(0)

        def chunks():
            try:
                while block := out.read(256 * 1024):
                    yield block
            finally:
                out.close()
        return StreamingResponse(chunks(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="next-box-{caller.name}.zip"', "Content-Length": str(size),
            **PRIVATE})

    @app.post("/auth/delete-account")
    def delete_my_account(body: DeleteMeBody, request: Request, response: Response,
                          caller: Caller = Depends(person)):
        """Delete yourself and everything Next Box keeps about you. Your linked apps aren't touched."""
        if body.confirm.strip().lower() != caller.name:
            raise HTTPException(400, "Type your username to confirm.")
        if not _own_password_ok(caller.name, body.password):
            raise HTTPException(403, "That password didn't match.")
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
                "how": "On the Next Box computer: ~/.local/share/nextbox/app/scripts/update.sh"}

    @app.get("/auth/sessions")
    def my_sessions(caller: Caller = Depends(person)):
        return accounts.sessions_for(caller.name, caller.token)

    @app.delete("/auth/sessions/{sid}")
    def end_my_session(sid: str, caller: Caller = Depends(person)):
        """Sign out one of your other devices (the short id from GET /auth/sessions)."""
        try:
            accounts.end_session_id(caller.name, sid, keep=caller.token)
        except KeyError:
            raise HTTPException(404, "That device is already signed out.") from None
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        events.log(caller.name, "signout_device", kind="auth")
        return {"ok": True}

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
        # Which marks they use is said by count only: what each one means to them stays in Settings.
        logged = {k: (f"{len(v)} marks" if k == "marks" and isinstance(v, list) else v) for k, v in changes.items()}
        events.log(caller.name, "settings", detail={"changed": logged})
        return prefs

    @app.get("/settings/marks")
    def get_mark_styles(caller: Caller = Depends(person)):
        """The ways of marking a list (presets with how to use them, the actions, the symbols to pick
        from) and the marks this person uses now."""
        return {**mark_styles.public(), "style": caller.prefs.get("mark_style", mark_styles.DEFAULT_STYLE),
                "marks": mark_styles.effective(caller.prefs)}

    @app.get("/settings/marks/legend.png")
    def mark_legend(style: str = "", marks: str = Query("", max_length=4000), caller: Caller = Depends(person)):
        """The legend as the daily label prints it, for Settings' preview: a preset's (`style`) or a list
        being edited (`marks`, as JSON). Drawn in the Easy-to-read font when that's on. Nothing prints."""
        import io as _io
        if marks:
            try:
                given = json.loads(marks)
            except ValueError:
                raise HTTPException(400, "That isn't a list of marks.") from None
            bad = mark_styles.problem(given)
            if bad:
                raise HTTPException(400, bad)
        else:
            given = mark_styles.effective({"mark_style": style} if style else caller.prefs)
        with render.reading_font(bool(caller.prefs.get("reading_mode"))):
            img = render.legend_image(given)
        out = _io.BytesIO()
        img.convert("L").save(out, "PNG")
        return Response(out.getvalue(), media_type="image/png", headers=PRIVATE)

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
            "prints_here": prints_here(household_of(caller)),
            # How many printers this household has (0: "No printer added"), and whether this person can
            # add one in Admin → Printers. Who may add printers is unchanged: admins and staff.
            "printers": len(everyone),
            "can_add_printer": rank(caller.role) >= rank("admin"),
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
    def get_list(include_done: bool = False,
                 since: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$"),
                 caller: Caller = Depends(person)):
        """The built-in list, plus today's tasks from a linked app (they stay in that app). `since` (local
        time, like /printed): also `done_since`, the tasks finished from then on (the routine card's day)."""
        try:
            since_ts = int(datetime.fromisoformat(since).timestamp()) if since else None
        except ValueError:
            raise HTTPException(400, "since looks like 2026-10-16T01:00") from None
        mine = my_list(caller)
        today = ctx.today()
        tasks = sorted(mine.all_open(), key=lambda t: jobs.list_order(t, today))  # as the ticket lists them
        if include_done:
            from sqlalchemy import select as _select
            from . import db as _db
            with accounts.db.connect() as conn:
                rows = conn.execute(_select(_db.tasks).where(_db.tasks.c.owner == caller.name,
                                                             _db.tasks.c.done_at.is_not(None))
                                    .order_by(_db.tasks.c.done_at.desc()).limit(50)).mappings().all()
            tasks += [mine._task(r) for r in rows]
        status = connections.status(caller.name)
        linked, linked_error, linked_undo = [], "", False
        candidates = mine.today()  # for Just one thing: what today's ticket would list (asked of the app once)
        if status.get("connected"):
            try:
                app_tasks = rctx(caller).tasks.linked
                linked_undo = getattr(app_tasks, "reopen_task", None) is not None  # can a ✓ be undone there?
                fetched = [{**t, "source": status["provider"], "labels": list(t.get("labels") or [])}
                           for t in app_tasks.today()]
                candidates += fetched
                linked = sorted(fetched, key=lambda t: jobs.list_order(t, today))
            except Exception as exc:  # the list still works when the linked app is down
                linked_error = f"{status.get('name', 'Your app')} didn't answer: {exc}"[:300]
        # Slugs, as the Journal and the daily label count them: moved to a later day slug_after+ times.
        slug_after = int(caller.prefs.get("slug_after", 3))
        open_ids = {str(t["id"]) for t in tasks if not t.get("done")} | {str(t["id"]) for t in linked}
        slides = {tid: n for tid, n in Slides(accounts.db, caller.name).counts().items()
                  if n >= slug_after and tid in open_ids}
        wins = Wins(accounts.db, caller.name)
        return {"tasks": tasks, "linked": linked, "linked_name": status.get("name") if status.get("connected") else None,
                "slides": slides, "slug_after": slug_after,
                "linked_error": linked_error, "linked_undo": linked_undo, "tags": _tags_with_uses(caller.name),
                "done_today": len(wins.between(today.isoformat(), today.isoformat())),
                **({"done_since": wins.count_since(since_ts)} if since_ts is not None else {}),
                # Ever had a task here, open or done: finishing the last one isn't a brand-new start.
                "has_tasks": bool(tasks) or _has_own_tasks(caller),
                "one_thing": min(candidates, key=lambda t: jobs._sort_key(t, today), default=None),
                "marks_waiting": sum(len(_waiting(r)) for r in ctx.store.list_scans(50, by=caller.name))}

    def _waiting(rec: dict) -> list[dict]:
        """Marks on a read-back still waiting for the person to confirm or skip."""
        return [p for p in rec.get("needs_confirmation", []) if p.get("status") == "pending"]

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
                "printers": len(registry.all(caller.household)),  # 0: the printer step says "No printer added."
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

    # ----------------------------------------------------------- setup questions (SPEC §27)
    def _quiz_due(name: str, prefs: dict) -> bool:
        """New accounts get the setup questions once, after the data notice. Anyone who already uses
        Next Box (their own tasks, a ticket or a linked app) finds them in Settings → Help instead."""
        if prefs.get("setup_quiz_done"):
            return False
        return not (_has_own_tasks(Caller(name)) or ctx.store.list_printed(1, by=name)
                    or connections.status(name).get("connected"))

    @app.post("/setup/quiz")
    def setup_quiz(body: dict, caller: Caller = Depends(person)):
        """{answers: {print, journal, rows, helps, plan_at, review_at}} (any of them), or {skip: true}.
        Saves the settings the answers mean (quiz.changes_for) and returns them as `changed`, with all
        the person's settings. Answering how many tasks or what helps ticks the Get started guide's "Make it
        fit your brain" step; printing on paper or having no printer yet puts away its printer step."""
        skip = body.get("skip") is True
        try:
            changes = {} if skip else quiz.changes_for(body.get("answers") or {})
        except quiz.QuizError as exc:
            raise HTTPException(400, str(exc)) from exc
        extra: dict = {"setup_quiz_done": True}
        if changes.keys() & {"max_rows", "gentle_words", "reading_mode", "routine_mode"}:
            extra["setup_done"] = sorted(set(caller.prefs.get("setup_done") or []) | {"focus"})
        if changes.get("print_with") in ("paper", "none"):
            extra["setup_dismissed"] = sorted(set(caller.prefs.get("setup_dismissed") or []) | {"printer"})
        changed = {k: v for k, v in changes.items() if caller.prefs.get(k) != v}  # not what was so already
        prefs = accounts.set_prefs(caller.name, {**changes, **extra})
        # Which settings changed, never the answers themselves (what helps can hint at a diagnosis).
        events.log(caller.name, "setup_quiz", detail={"skipped": skip, "changed": sorted(changed)})
        return {"prefs": prefs, "changed": changed}

    @app.post("/list")
    def add_list_task(body: TaskIn, caller: Caller = Depends(person)):
        fields = body.model_dump(exclude_none=True)
        fields.pop("slides", None)
        new = _new_tags(caller, fields)
        task = run(caller, "list_add", my_list(caller).create_many, [fields], new_tags=new)[0]
        return task

    def _new_tags(caller: Caller, fields: dict) -> list[str]:
        """Check a task's tags before anything is saved, and say which are new. They're made (white)
        together with the task, so a save that fails never leaves tags behind."""
        if fields.get("tags") is None:
            return []
        from .mylist import tag_name
        try:
            names = list(dict.fromkeys(tag_name(str(t)) for t in fields["tags"]))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if len(names) > 8:
            raise HTTPException(400, "8 tags on one task is the most. Pick the ones that matter.")
        fields["tags"] = names
        known = {t["name"] for t in Tags(accounts.db, caller.name).all()}
        new = [n for n in names if n not in known]
        if new and len(known) + len(new) > 30:
            raise HTTPException(400, "30 tags is the limit; fewer tags are easier to use anyway. "
                                     "Remove one in Settings to make room.")
        return new

    @app.post("/list/dump")
    def brain_dump(body: DumpBody, preview: bool = False, caller: Caller = Depends(person)):
        """Type or paste everything on your mind, one per line. #tag !urgent tomorrow 3pm ~15m.
        All or nothing: a date or time that can't be real stops the whole dump before anything is
        added, and says which line. 200 lines at a time: the rest come back as skipped_text.
        Lines ticked already ([x]) are left out. With prefs.new_tasks_to = linked, the tasks go
        to the linked app instead (and are never kept here). preview=1: only say what would
        happen (dump_preview); nothing is added, saved or logged."""
        if preview:
            return dump_preview(body.text, caller)
        head, rest = split_dump(body.text)
        items = parse_dump(head, ctx.today())
        problems = [{"line": i["line"], "text": i["text"][:120], "problem": i["problem"]}
                    for i in items if i.get("problem")]
        if problems:
            first, more = problems[0], len(problems) - 1
            return JSONResponse(status_code=400, content={"problems": problems[:50], "detail": (
                f"Nothing was added yet. Line {first['line']}: {first['problem']}."
                + (f" {more} more line{'s' if more != 1 else ''} need a look too." if more else "")
                + " Fix it and add again.")})
        left_out = [{"line": i["line"], "text": i["text"][:120]} for i in items if i.get("ticked")]
        items = [i for i in items if not i.get("ticked")]
        if not items:
            raise HTTPException(400, "Those lines are ticked already, so there's nothing to add."
                                if left_out else "Nothing to add. Put one task on each line.")
        if caller.prefs.get("new_tasks_to") == "linked":
            linked = getattr(rctx(caller).tasks, "linked", None)
            if linked is not None:
                return {**run(caller, "brain_dump_linked", _dump_to_linked, linked, items, rest), "left_out": left_out}
        # A new #tag starts white. Past the 30-tag limit, the #word stays in the task's words instead.
        known = {t["name"] for t in Tags(accounts.db, caller.name).all()}
        new: list[str] = []
        kept_words: list[str] = []
        for item in items:
            keep = []
            for name in item.get("tags", []):
                if name in known or name in new or len(known) + len(new) < 30:
                    keep.append(name)
                    if name not in known and name not in new:
                        new.append(name)
                else:
                    item["content"] = f"{item['content']} #{name}"[:500]
                    kept_words.append(f"#{name}")
            item["tags"] = keep
        added = run(caller, "brain_dump", my_list(caller).create_many, items, new_tags=new)
        out = {"added": added, "to": "My list", "skipped_text": rest, "left_out": left_out, "notes": []}
        if kept_words:
            out["notes"].append(f"You have 30 tags, the most there can be, so {', '.join(dict.fromkeys(kept_words))} "
                                "stayed in the words. Remove a tag in Settings to make room.")
        return out

    def dump_preview(text: str, caller: Caller) -> dict:
        """Brain dump's live preview: each line as it would be added (problems and ticked lines too),
        lines with no task words that would be dropped, where the tasks would go, and which #tags
        are new (or stay in the words, past the 30-tag limit). Reads only: nothing is written."""
        from sqlalchemy import select as _select

        from . import db as _db
        out = preview_dump(text, ctx.today())
        with accounts.db.connect() as conn:  # not Tags.all(): that saves the starter tags
            known = {r[0] for r in conn.execute(_select(_db.tags.c.name).where(_db.tags.c.owner == caller.name))}
        new: list[str] = []
        over: list[str] = []
        for item in out["lines"]:
            for name in item.get("tags", []):
                if name in known or name in new or name in over:
                    continue
                (new if len(known) + len(new) < 30 else over).append(name)
        to, notes = "My list", []
        if caller.prefs.get("new_tasks_to") == "linked":  # as brain_dump decides it
            try:
                linked = getattr(rctx(caller).tasks, "linked", None)
            except Exception:  # a preview never fails because the linked app can't be reached
                linked = None
            if linked is not None:
                to = display_name(getattr(linked, "key", "")) or "your linked app"
                notes.append(f"These go to {to}, which doesn't take Next Box's tags, urgency or how long.")
        return {**out, "to": to, "new_tags": new, "over_tags": over, "notes": notes}

    def _due_words(item: dict) -> str | None:
        """A parsed line's day, time and repeat in words Todoist understands."""
        day, at, repeat = item.get("due_date"), item.get("due_time"), item.get("repeat")
        if repeat:
            unit = {"daily": "day", "weekdays": "weekday", "weekly": "week", "monthly": "month"}[repeat]
            return f"every {unit}" + (f" at {at}" if at else "") + (f" starting {day}" if day else "")
        return f"{day} {at}" if day and at else day

    def _dump_to_linked(linked, items: list[dict], rest: str) -> dict:
        """Brain dump into the linked app. Apps that take a due date (Todoist) get the day, time and
        repeat; the others get each line as it was typed, so no date is lost on the way."""
        import inspect
        name = display_name(getattr(linked, "key", "")) or "your linked app"
        dated = "due_string" in inspect.signature(linked.add_task).parameters
        added, lost = [], []
        for n, item in enumerate(items):
            try:
                if dated:
                    task = linked.add_task(item["content"], description=item.get("description", ""),
                                           due_string=_due_words(item))
                    lost += [w for w, on in (("tags", item.get("tags")), ("urgency", item.get("priority", 1) > 1),
                                             ("how long", item.get("minutes"))) if on and w not in lost]
                else:
                    task = linked.add_task(item["text"], description="")
            except (ProviderError, OSError) as exc:  # stopped part way: the rest go back in the box
                left = "\n".join([i["text"] for i in items[n:]] + ([rest] if rest else []))
                return {"added": added, "to": name, "skipped_text": left, "notes": [],
                        "error": f"{name} stopped answering after {len(added)}: {exc}"[:300]}
            added.append({"id": str(task.get("id", "")), "content": task.get("content") or item["content"],
                          "source": getattr(linked, "key", "linked")})
        notes = [f"{name} doesn't take {' or '.join(lost)} from Next Box: add them there."] if lost else []
        if not dated:
            notes = [f"{name} got each line as you typed it. Set days and times there."]
        return {"added": added, "to": name, "skipped_text": rest, "notes": notes}

    def _local_id(task_id: str) -> str:
        if not is_local(task_id):
            raise HTTPException(400, "That task lives in your linked app; change it there.")
        return task_id

    @app.patch("/list/{task_id}")
    def edit_list_task(task_id: str, body: TaskIn, caller: Caller = Depends(person)):
        fields = body.model_dump(exclude_unset=True)
        restore = fields.pop("slides", None)  # Undo after Break it down: the count the new steps cleared
        new = _new_tags(caller, fields)
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
        result = run(caller, "list_edit", mine.update, _local_id(task_id), fields, new_tags=new)
        slides = Slides(accounts.db, caller.name)
        if before and fields.get("due_date") and fields["due_date"] > before:
            slides.bump(task_id)  # moved to a later day
        new_steps = [s["text"] for s in result.get("steps") or []]
        if old_steps is not None and set(new_steps) - set(old_steps):
            # A new step: made smaller, a fresh start. (Ticking, reordering or removing steps isn't.)
            was = slides.counts().get(task_id)
            slides.clear(task_id)
            if was is not None:
                result = {**result, "slides_was": was}  # what Undo sends back
        if restore is not None:
            slides.set(task_id, restore)
        return result

    @app.post("/list/{task_id}/done")
    def done_list_task(task_id: str, body: DoneBody | None = None, caller: Caller = Depends(person)):
        """Done. `undo` is what /reopen needs to put it back (a repeat's day and ticked steps), or
        None when it was finished already (a double tap counts once) or its app can't reopen it.
        body.due: the day the screen showed, so a My list repeat that has moved on since (Done on
        another device) isn't finished a second time. `stale`: nothing was done because the task
        changed since that screen loaded (done, skipped or moved on another device), so the app says
        so and shows the list as it is now instead of "Done ✓"."""
        c = rctx(caller)
        seen = ((body.due if body else None) or "")[:10]
        undo = run(caller, "list_done", c.tasks.close_task, task_id,
                   **({"seen_due": seen} if seen and is_local(task_id) else {}))
        if undo is None and is_local(task_id):  # nothing changed, its slide count included
            return {"ok": True, "undo": None, "stale": True}
        slides = Slides(accounts.db, caller.name)
        slid = slides.counts().get(task_id)
        slides.clear(task_id)
        if not is_local(task_id):  # My list counts its own wins; a linked app's task counts here
            events.log(caller.name, "task_done", detail={"source": "linked", "task_id": task_id[:200]},
                       household=caller.household)
            Wins(accounts.db, caller.name, caller.household).add(task_id, ctx.today().isoformat(), "linked",
                                                                 once_a_day=True)
            undo = {} if getattr(getattr(c.tasks, "linked", None), "reopen_task", None) else None
        if undo is not None and slid is not None:
            undo = {**undo, "slides": slid}  # Undo puts its slide count back too
        return {"ok": True, "undo": undo}

    @app.post("/list/{task_id}/reopen")
    def reopen_list_task(task_id: str, body: ReopenBody | None = None, caller: Caller = Depends(person)):
        """Not done after all. A repeat goes back to body.prev_due (from /done's `undo`) with its
        ticked steps; a linked app's task is reopened there, where the app allows it."""
        body = body or ReopenBody()
        if is_local(task_id):
            task = run(caller, "list_reopen", my_list(caller).reopen, task_id,
                       prev_due=body.prev_due, prev_steps=body.prev_steps)
            if body.slides is not None:
                Slides(accounts.db, caller.name).set(task_id, body.slides)
            return task
        tasks = rctx(caller).tasks
        linked = getattr(tasks, "linked", tasks)
        reopen = getattr(tasks, "reopen_task", None)
        if reopen is None or (linked is not None and getattr(linked, "reopen_task", None) is None):
            name = tasks.linked_name() if hasattr(tasks, "linked_name") else "your linked app"
            raise HTTPException(400, f"Reopen it in {name}.")
        run(caller, "list_reopen", reopen, task_id)  # not linked any more: 409
        Wins(accounts.db, caller.name, caller.household).remove_last(task_id)
        if body.slides is not None:
            Slides(accounts.db, caller.name).set(task_id, body.slides)
        return {"ok": True}

    @app.post("/list/{task_id}/move")
    def move_list_task(task_id: str, body: MoveBody, caller: Caller = Depends(person)):
        """→ Tomorrow (or any day) in one tap, on My list or in the linked app (through its
        set_due_date, so a repeating rule survives). A move to a later day counts as a slide, as
        read-back's → does. `undo` is what puts it back: the same call with undo=true, which takes
        that slide back. Already on that day: nothing changes (moved=false)."""
        today = ctx.today()
        to = (body.to or "").strip().lower()
        if to in ("", "none"):
            target = None
        elif to in ("today", "tomorrow"):
            target = (today + timedelta(days=1 if to == "tomorrow" else 0)).isoformat()
        else:
            try:
                target = _iso_day(to)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        local = is_local(task_id)

        def go() -> dict:
            tasks = my_list(caller) if local else rctx(caller).tasks
            task = tasks.get_task(task_id)
            if task.get("done"):
                raise ValueError("That task is finished already.")
            prev = ((task.get("due") or {}).get("date") or "")[:10] or None
            if prev == target:
                return {"ok": True, "moved": False, "from": prev, "to": target, "task": task if local else None}
            if to == "tomorrow" and not body.undo and prev and target and prev > target:
                # → Tomorrow never moves a task sooner: one due later stays where it is.
                return {"ok": True, "moved": False, "later": True, "from": prev, "to": prev,
                        "task": task if local else None}
            if local:
                # This time only: keeps its time, a weekday repeat skips weekends, a repeat keeps its own day.
                task = tasks.move(task_id, target)
                now = ((task.get("due") or {}).get("date") or "")[:10] or None
            elif target is None:
                name = tasks.linked_name() if hasattr(tasks, "linked_name") else "your linked app"
                raise ValueError(f"Take its date off in {name}.")
            else:
                tasks.set_due_date(task_id, target)
                try:  # what the app says now: a move it didn't make isn't counted as a slide
                    now = ((tasks.get_task(task_id).get("due") or {}).get("date") or "")[:10] or target
                except Exception:
                    now = target
                if now == prev:  # nothing moved: say so, and don't count a slide
                    name = tasks.linked_name() if hasattr(tasks, "linked_name") else "your linked app"
                    raise ValueError(f"{name} kept it on the same day. Change its date in {name}.")
            slides, slid = Slides(accounts.db, caller.name), None
            if prev and now and not body.undo and now > prev:
                slid = slides.bump(task_id)
            elif prev and now and body.undo and now < prev:
                slides.unbump(task_id)
            return {"ok": True, "moved": True, "from": prev, "to": now, "undo": {"to": prev, "undo": True},
                    "slid": slid, "task": task if local else None}
        return run(caller, "list_move", go)

    @app.patch("/list/{task_id}/steps/{index}")
    def tick_list_step(task_id: str, index: int, body: StepBody, caller: Caller = Depends(person)):
        """Tick (or untick) one tiny step. `text` guards against steps changed on another device:
        then nothing changes and 409 says so (the app shows the steps as they are now)."""
        _local_id(task_id)

        def go() -> dict:
            try:
                return my_list(caller).tick_step(task_id, index, body.done, text=body.text)
            except StepChanged as exc:  # a ValueError, which run() would call a 400
                raise HTTPException(409, str(exc)) from exc
        return run(caller, "list_step", go)

    @app.post("/list/{task_id}/skip")
    def skip_list_task(task_id: str, body: SkipBody | None = None, caller: Caller = Depends(person)):
        """Skip this time: a repeating My list task moves on to its next day. Not a win and not a
        slide. body.due: the day the screen showed, so a double tap skips once. `undo` is what
        /unskip needs, or None when it had moved on already."""
        mine, seen = my_list(caller), ((body.due if body else None) or "")[:10]
        _local_id(task_id)

        def go() -> dict | None:
            if seen and ((mine.get_task(task_id).get("due") or {}).get("date") or "")[:10] != seen:
                return None  # it moved on already
            return mine.skip(task_id)
        undo = run(caller, "list_skip", go)
        return {"ok": True, "undo": undo, "task": mine.get_task(task_id)}

    @app.post("/list/{task_id}/unskip")
    def unskip_list_task(task_id: str, body: ReopenBody, caller: Caller = Depends(person)):
        if not body.prev_due:
            raise HTTPException(400, "Which day should it go back to?")
        return run(caller, "list_unskip", my_list(caller).unskip, _local_id(task_id), body.prev_due,
                   prev_steps=body.prev_steps)

    @app.delete("/list/{task_id}")
    def delete_list_task(task_id: str, confirm: bool = False, caller: Caller = Depends(person)):
        if not confirm:  # deleting a task always needs the person's explicit yes
            raise HTTPException(400, "Deleting needs confirm=true.")
        run(caller, "list_delete", rctx(caller).tasks.delete_task, task_id)
        return {"ok": True}

    @app.get("/tags")
    def get_tags(caller: Caller = Depends(person)):
        """Each tag with `uses`: how many open tasks carry it (so removing one can say so)."""
        return {"tags": _tags_with_uses(caller.name), "colors": list(TAG_COLORS)}

    def _tags_with_uses(name: str) -> list[dict]:
        tags = Tags(accounts.db, name)
        uses = tags.usage()
        return [{**t, "uses": uses.get(t["name"], 0)} for t in tags.all()]

    @app.post("/tags")
    def add_tag(body: NewTagBody, caller: Caller = Depends(person)):
        """A new tag (the name goes in the body, so any typed name works). An existing tag keeps its color."""
        try:
            return Tags(accounts.db, caller.name).add(body.name, body.color)
        except TagExists as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/tags/{name}")
    def put_tag(name: str, body: TagBody, caller: Caller = Depends(person)):
        try:
            return Tags(accounts.db, caller.name).set(name, body.color)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/tags/{name}")
    def delete_tag(name: str, strip: bool = False, caller: Caller = Depends(person)):
        """strip=true: take the tag off the tasks that have it too (they keep their other tags)."""
        try:
            changed = Tags(accounts.db, caller.name).remove(name, strip=strip)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "tasks_changed": changed}

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
        steps = body.steps
        if steps is not None and (not body.task_id or is_local(body.task_id)):
            steps = None  # My list prints its own saved steps
        return run(caller, "print_focus", jobs.print_focus, rctx(caller), body.task_id, dry_run=body.dry_run,
                   steps=steps)

    @app.get("/printed")
    def printed(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100_000),
                since: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$"),
                caller: Caller = Depends(auth)):
        # `since`: made at or after this local time (the routine card asks for its day's tickets)
        return ctx.store.list_printed(limit, by=acting_as(caller), offset=offset, since=since)

    @app.get("/printed/{label_id}/png")
    def printed_png(label_id: str, download: bool = False, caller: Caller = Depends(auth)):
        """The ticket's picture (stickers: all of them in one tall image, with dashed cut lines between).
        `download=1` is Save label image: the same picture as a file named after the ticket, to open in a
        label printer's own app. Nothing is printed."""
        label_id = normalize_id(label_id)
        if not re.fullmatch(r"[A-Z0-9-]{4,16}", label_id):
            raise HTTPException(404)
        rec = ctx.store.get_printed(label_id)
        # People see their own tickets; the superadmin sees everyone's (everyone agreed, see DATA_NOTICE).
        if not rec or (rec.get("by") != acting_as(caller) and caller.role != "superadmin"):
            raise HTTPException(404)
        how = "attachment" if download else "inline"
        headers = {"Content-Disposition": f'{how}; filename="next-box-{label_id}.png"', **PRIVATE}
        path = ctx.store.png_path(label_id)
        if path.exists():
            return FileResponse(path, media_type="image/png", headers=headers)
        # png/ is only a cache (it isn't moved to a new PC, for one): the database keeps every ticket's picture.
        data = ctx.store.get_png(label_id)
        if not data:
            raise HTTPException(404)
        return Response(content=data, media_type="image/png", headers=headers)

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
        parts = ctx.store.get_parts(label_id)
        if not parts and rec.get("kind") == "stickers":  # one sticker from before parts were kept: still real size
            from PIL import Image
            import io as _io
            parts = [Image.open(_io.BytesIO(png))]
        data = paper_pdf.ticket_pdf(png, parts, size, code=label_id)
        events.log(caller.name, "print_paper", detail={"id": label_id, "paper": size})
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="next-box-{label_id}.pdf"', **PRIVATE})

    @app.post("/scan")
    async def do_scan(photo: UploadFile = File(...), label_id: str | None = Form(default=None),
                      caller: Caller = Depends(auth)):
        if not vision_ready():  # no AI key: say how to fix it, and don't use up the hour's photos
            raise HTTPException(503, no_key(caller))
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
        c = rctx(caller)
        fn = vision or scan.claude_vision(ctx.settings, mark_styles.effective(c.prefs))  # their own marks
        rec = run(caller, "scan", scan.scan_photo, c, fn, data, media_type, label_id or None)
        return _scan_reply(caller, rec)

    def _scan_reply(caller: Caller, rec: dict) -> dict:
        if rec.get("status") == "failed":  # the photo is kept; staff see why reading it failed
            events.log(caller.name, "scan", ok=False, kind="error", error=rec.get("vision_error", ""),
                       detail={"id": rec["id"]}, household=caller.household)
        if caller.role == "service":  # Home Assistant and scripts never see the journal
            return _for_key(caller, rec)
        if rec.get("journal_id"):  # the notes live only in the private journal page: show them back to its owner
            page = Journal(accounts.db, acting_as(caller)).get(rec["journal_id"])
            rec = {**rec, "notes": page["notes"] if page else "", "win": page["win"] if page else ""}
        return rec

    def _for_key(caller: Caller, rec: dict) -> dict:
        """A read-back as the shared key sees it: not even which journal page it made."""
        return {k: v for k, v in rec.items() if k != "journal_id"} if caller.role == "service" else rec

    @app.get("/scans")
    def scans(limit: int = Query(20, ge=1, le=500), pending: bool = False, caller: Caller = Depends(auth)):
        """Your read-backs, newest first. pending=true: only those with marks still waiting for you."""
        if pending:
            return [_for_key(caller, r) for r in ctx.store.list_scans(50, by=acting_as(caller)) if _waiting(r)][:limit]
        return [_for_key(caller, r) for r in ctx.store.list_scans(limit, by=acting_as(caller))]

    def _own_scan(scan_id: str, caller: Caller) -> dict:
        rec = ctx.store.get_scan(scan_id) if re.fullmatch(r"[A-Za-z0-9-]{4,16}", scan_id) else None
        if not rec or rec.get("by") != acting_as(caller):
            raise HTTPException(404)
        return rec

    @app.get("/scan/{scan_id}")
    def get_scan(scan_id: str, caller: Caller = Depends(auth)):
        return _for_key(caller, _own_scan(scan_id, caller))

    # Confirm, relabel, retry and undo are a person's taps (a held ✗ deletes a task), so the shared
    # key can't do them, and can't use them to read the journal page a photo made.
    @app.post("/scan/{scan_id}/confirm")
    def confirm_scan(scan_id: str, body: ConfirmBody, caller: Caller = Depends(person)):
        _own_scan(scan_id, caller)
        return run(caller, "scan_confirm", scan.confirm, rctx(caller), scan_id, body.decisions)

    @app.post("/scan/{scan_id}/label")
    def relabel_scan(scan_id: str, body: ScanLabelBody, caller: Caller = Depends(person)):
        """Which ticket the photo shows, when the code couldn't be read or didn't match what was typed."""
        _own_scan(scan_id, caller)
        rec = run(caller, "scan_label", scan.relabel, rctx(caller), scan_id, body.label_id[:40])
        return _scan_reply(caller, rec)

    @app.post("/scan/{scan_id}/retry")
    def retry_scan(scan_id: str, caller: Caller = Depends(person)):
        """Read a kept photo again after reading it failed. Only ever when the person taps Try again."""
        rec = _own_scan(scan_id, caller)
        if rec.get("status") != "failed":
            return _scan_reply(caller, rec)
        if not vision_ready():
            raise HTTPException(503, no_key(caller))
        image = Journal(accounts.db, acting_as(caller)).photo(rec["journal_id"]) if rec.get("journal_id") else None
        if not image:
            raise HTTPException(409, "That photo wasn't kept, so snap it again.")
        if not scan_window.allow(caller.name):
            raise HTTPException(429, "That's a lot of photos this hour. Try again in a bit.")
        c = rctx(caller)
        fn = vision or scan.claude_vision(ctx.settings, mark_styles.effective(c.prefs))
        return _scan_reply(caller, run(caller, "scan_retry", scan.retry, c, fn, scan_id, image))

    @app.post("/scan/{scan_id}/undo")
    def undo_scan(scan_id: str, body: UndoBody, caller: Caller = Depends(person)):
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
        if undone and undone["mark"] == "done" and undone.get("slides") is not None:
            Slides(accounts.db, acting_as(caller)).set(undone["task_id"], undone["slides"])  # as before the ✓
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
        tickets = _day_tickets(caller.name, first, last, pages)
        changes = _readback_changes(caller.name, first, last)
        today = ctx.today()
        out = []
        for i in range(days):
            day = (last - timedelta(days=i)).isoformat()
            mine = [p for p in pages if p["day"] == day]
            if mine or wins[day]["count"] or wins[day]["items"] or day in changes:
                # "Today" and "Yesterday" by this Next Box's clock, the one that files the pages.
                out.append({"day": day, "pages": mine, "wins": wins[day], "is_today": day == today.isoformat(),
                            "is_yesterday": day == (today - timedelta(days=1)).isoformat(),
                            "ticket": tickets.get(day), "changes": changes.get(day)})
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

    # ----------------------------------------------------------- AI helpers (nextbox/coach.py)
    # Only when the person taps: never automatic. Only that a helper ran is logged, never what was
    # sent or what came back; journal notes go only when the person ticks "include my journal notes".
    def ai_ready() -> bool:
        return ai["coach"] is not None or bool(ctx.settings.anthropic_api_key)

    def vision_ready() -> bool:
        """Snap can read a photo: there's a key (or a stand-in vision in tests). Not ai_ready, which
        also counts a stand-in coach."""
        return vision is not None or bool(ctx.settings.anthropic_api_key)

    def no_key(caller: Caller) -> str:
        """No AI key: the owner is told how to add one, everyone else who to ask."""
        return coaching.NO_KEY_OWNER if caller.role == "superadmin" else coaching.NO_KEY

    def helper(caller: Caller) -> coaching.Coach:
        if not caller.prefs.get("ai_helpers", True):
            raise HTTPException(409, coaching.TURNED_OFF)
        if not ai_ready():
            raise HTTPException(503, no_key(caller))
        if ai["coach"] is None:
            ai["coach"] = coaching.claude_coach(ctx.settings)
        return ai["coach"]

    def ask(caller: Caller, action: str, fn, message: str):
        """One call to the AI. Returns (answer, ms)."""
        if not coach_window.allow(caller.name):
            raise HTTPException(429, coaching.TOO_MANY)
        start = time.time()
        try:
            out = fn(message)
        except coaching.CoachError as exc:
            events.log(caller.name, action, ok=False, error=exc.kind, household=caller.household)
            raise HTTPException(exc.status, exc.message) from None
        except Exception as exc:  # noqa: BLE001 - the type only: a message could quote what was sent
            events.log(caller.name, action, ok=False, kind="error", error=type(exc).__name__,
                       household=caller.household)
            raise HTTPException(500, "Something went wrong with the AI helper. Try again in a bit.") from None
        return out, int((time.time() - start) * 1000)

    @app.post("/list/{task_id}/breakdown")
    def break_it_down(task_id: str, body: BreakBody | None = None, caller: Caller = Depends(person)):
        """"Break it down": 3-6 tiny first steps for a task, to review before anything is saved.
        Sends only the task's name, how long it takes, its day and steps (and the optional hint)."""
        coach_fn = helper(caller)
        if not task_id.strip() or len(task_id) > 200:
            raise HTTPException(404)
        c = rctx(caller)
        try:
            task = c.tasks.get_task(task_id)
        except TaskNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except NotConnected as exc:
            raise HTTPException(409, str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from exc
        hint = (body.hint if body else "").strip()
        local = is_local(task_id)
        named = getattr(c.tasks, "linked_name", None)
        about = {"id": str(task["id"]), "content": task.get("content", ""), "local": local,
                 "steps": list(task.get("steps") or []) if local else [],
                 "due_date": ((task.get("due") or {}).get("date") or "")[:10] or None,
                 "app": None if local else (named() if named else "your linked app")}
        has = [str(s.get("text", "")) for s in task.get("steps") or [] if isinstance(s, dict)]  # sent too
        if coaching.crisis(about["content"], hint, *has):  # not sent anywhere, and not logged
            return {"task": about, "steps": [], "support": coaching.SUPPORT}
        result, ms = ask(caller, "ai_breakdown", coach_fn.breakdown, coaching.breakdown_message(task, hint))
        if result.concern:  # the model was worried: only the kind nudge, never steps
            events.log(caller.name, "ai_breakdown", detail={"ms": ms}, household=caller.household)
            return {"task": about, "steps": [], "support": coaching.SUPPORT}
        steps = coaching.clean_steps(result.steps)
        if not steps:
            events.log(caller.name, "ai_breakdown", ok=False, error="empty", household=caller.household)
            raise HTTPException(422, coaching.CANT_HELP)
        events.log(caller.name, "ai_breakdown", detail={"ms": ms, "steps": len(steps)}, household=caller.household)
        return {"task": about, "steps": steps}

    @app.post("/journal/look-back")
    def look_back(body: LookBackBody, caller: Caller = Depends(person)):
        """"Look back on my week": a short, kind note about the last 7 days. Sends the week's wins
        (counts and task names) and slugs, and the journal notes only with include_notes. Nothing is
        kept: "Save to my journal" saves the returned `text` through POST /journal."""
        coach_fn = helper(caller)
        last = ctx.today()
        first = last - timedelta(days=6)
        wins = wins_by_day(caller.name, first, last)
        try:
            slug_list = find_slugs(rctx(caller).tasks.today(), Slides(accounts.db, caller.name).counts(), last,
                                   int(caller.prefs.get("slug_after", 3)))
        except Exception:  # noqa: BLE001 - a linked app that's down: the week's wins still count
            slug_list = []
        pages = _journal(caller).pages(first.isoformat(), last.isoformat())
        has_notes = any((p.get("notes") or "").strip() or p.get("win") for p in pages)
        notes = body.include_notes and has_notes
        week = {"first": first.isoformat(), "last": last.isoformat(),
                "wins": sum(w["count"] for w in wins.values()), "slugs": len(slug_list), "notes": notes}
        # Everything that would be sent is checked first: the task names in the wins and slugs, and the
        # notes when they're ticked. Crisis words get only the kind nudge: not sent, not logged.
        said = [i for w in wins.values() for i in w["items"]] + [s.get("content") or "" for s in slug_list]
        if notes:
            said += [f"{p.get('win') or ''}\n{p.get('notes') or ''}" for p in pages]
        if coaching.crisis(*said):
            return {"week": week, "concern": True, "support": coaching.SUPPORT}
        if not week["wins"] and not slug_list and not notes:
            return {"week": week, "empty": True, "has_notes": has_notes}
        message = coaching.week_message(first, last, wins, slug_list, pages if notes else None)
        note, ms = ask(caller, "ai_look_back", coach_fn.week_note, message)
        events.log(caller.name, "ai_look_back", detail={"ms": ms}, household=caller.household)
        if note.concern:
            return {"week": week, "concern": True, "support": coaching.SUPPORT}
        if not slug_list:
            note = note.model_copy(update={"slug": ""})  # nothing slid: no observation about it
        note = note.model_copy(update={k: getattr(note, k).strip() for k in ("went_well", "slug", "suggestion")})
        if not note.went_well and not note.suggestion:
            raise HTTPException(422, coaching.CANT_HELP)
        return {"week": week, "note": {"went_well": note.went_well, "slug": note.slug, "suggestion": note.suggestion},
                "text": coaching.note_text(note, first, last)}

    @app.post("/journal")
    def journal_add(body: JournalBody, caller: Caller = Depends(person)):
        if body.day:  # a page for a day the Journal tab shows: up to a year back, or tomorrow
            try:
                day = date.fromisoformat(body.day)
            except ValueError:
                raise HTTPException(400, "day looks like 2026-10-16") from None
            if not ctx.today() - timedelta(days=366) <= day <= ctx.today() + timedelta(days=1):
                raise HTTPException(400, "Pick a day in the last year (or tomorrow).")
        if not journal_add_window.allow(caller.name):
            raise HTTPException(429, "That's a lot of new pages this hour. Try again in a bit.")
        try:
            page = _journal(caller).add(body.day or ctx.today().isoformat(), body.notes)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        events.log(caller.name, "journal_add", household=caller.household)  # no text: it's private
        return page

    @app.patch("/journal/{page_id}")
    def journal_edit(page_id: str, body: JournalBody, caller: Caller = Depends(person)):
        """Change a page's notes, or move it to another day (`day`: today or earlier)."""
        day = _past_day(body.day) if body.day is not None else None
        try:
            return _journal(caller).update(_page_id(page_id), body.notes, day)
        except KeyError:
            raise HTTPException(404) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def _past_day(value: str) -> str:
        try:
            day = date.fromisoformat(value)
        except ValueError:
            raise HTTPException(400, "day looks like 2026-10-16") from None
        if day > ctx.today():
            raise HTTPException(400, "That day hasn't happened yet. Pick today or an earlier day.")
        if day < date(2000, 1, 1):
            raise HTTPException(400, "Pick a day after 2000.")
        return day.isoformat()

    # What else each journal day shows: its ticket and what read-back changed.
    def _day_tickets(name: str, first: date, last: date, pages: list[dict]) -> dict[str, dict]:
        """The ticket to show with each day: the one a page of that day was read back from, else that
        day's daily ticket (a real print before a preview, then the latest)."""
        def short(rec: dict) -> dict:
            return {"id": rec["id"], "kind": rec.get("kind", ""), "title": rec.get("title", ""),
                    "dry_run": bool(rec.get("dry_run"))}
        out: dict[str, dict] = {}
        for p in pages:
            if p["day"] in out or not p.get("label_id"):
                continue
            rec = ctx.store.get_printed(p["label_id"])
            if rec and rec.get("by") == name and not rec.get("failed"):
                out[p["day"]] = short(rec)
        recs = ctx.store.list_printed(500, by=name, since=first.isoformat(),
                                      until=(last + timedelta(days=1)).isoformat())
        daily: dict[str, list[dict]] = {}
        for rec in recs:
            if rec.get("kind") == "today" and not rec.get("failed"):
                daily.setdefault(rec["created_at"][:10], []).append(rec)
        for day, found in daily.items():
            if day not in out:
                out[day] = short(max(found, key=lambda r: (not r.get("dry_run"), r["created_at"])))
        return out

    def _readback_changes(name: str, first: date, last: date) -> dict[str, dict]:
        """What photo read-backs moved or dropped on each day (finished tasks are the day's wins)."""
        lo, hi = first.isoformat(), last.isoformat()
        out: dict[str, dict] = {}
        recs = ctx.store.list_scans(500, by=name, since=lo, until=(last + timedelta(days=2)).isoformat())
        for rec in reversed(recs):  # oldest first
            day = rec.get("day") or ""
            if not lo <= day <= hi or rec.get("status") == "failed":
                continue
            seen = out.setdefault(day, {"moved": [], "dropped": []})
            done = [a for a in rec.get("applied", []) if a.get("status") != "undone"]
            done += [p for p in rec.get("needs_confirmation", []) if p.get("status") == "applied"]
            for entry in done:
                what = {"tomorrow": "moved", "drop": "dropped"}.get(entry.get("mark"))
                if what and entry.get("content") and entry["content"] not in seen[what]:
                    seen[what].append(entry["content"])
        return {d: c for d, c in out.items() if c["moved"] or c["dropped"]}

    @app.post("/journal/lines")
    def journal_lines(body: LinesBody, caller: Caller = Depends(person)):
        """"Add lines to my list": a page's lines as possible tasks, with what Brain dump would make of
        each. Nothing is saved or logged here; adding goes through /list/dump."""
        try:
            listed = {t["content"].casefold() for t in my_list(caller).all_open()}
        except Exception:  # noqa: BLE001 - only for "already on your list"
            listed = set()
        return {"lines": note_lines(body.text, ctx.today(), listed)}

    @app.get("/journal/search")
    def journal_search(q: str = Query("", max_length=200), caller: Caller = Depends(person)):
        """Your pages whose notes hold every word, newest first, with the matches marked. Not logged."""
        q = " ".join(q.split())
        if len(q) < 2:
            raise HTTPException(400, "Type at least 2 letters to search.")
        if not search_window.allow(caller.name):
            raise HTTPException(429, "That's a lot of searches in a minute. Try again in a moment.")
        found, total = _journal(caller).search(q)
        return {"q": q, "results": found, "total": total}

    # ----------------------------------------------------------- download my journal
    # Only ever the owner's own pages. Not in events, admin screens or debug bundles.
    def _export_days(caller: Caller, since: str = "", until: str = "") -> list[dict]:
        """The days to download, oldest first: {day, pages, wins}, only days with something on them."""
        def bound(value: str, name: str) -> date | None:
            try:
                return date.fromisoformat(value) if value else None
            except ValueError:
                raise HTTPException(400, f"{name} looks like 2026-10-16") from None
        lo, hi = bound(since, "since"), bound(until, "until")
        journal = _journal(caller)
        spans = [s for s in (journal.span(), Wins(accounts.db, caller.name).span()) if s[0]]
        if not spans:
            return []
        first = max(date.fromisoformat(min(s[0] for s in spans)), lo or date.min)
        last = min(date.fromisoformat(max(s[1] for s in spans)), hi or date.max)
        if first > last:
            return []
        by_day: dict[str, list[dict]] = {}
        for p in journal.pages(first.isoformat(), last.isoformat()):
            by_day.setdefault(p["day"], []).append(p)
        wins = wins_by_day(caller.name, first, last)
        days = sorted(set(by_day) | {d for d, w in wins.items() if w["count"]})
        return [{"day": d, "pages": by_day.get(d, []), "wins": wins[d]} for d in days]

    @app.get("/journal/export/summary")
    def journal_export_summary(since: str = "", until: str = "", caller: Caller = Depends(person)):
        """What "Download my journal" would hold, before downloading: days, pages, photos, about how big."""
        _reset_hold(caller)
        days = _export_days(caller, since, until)
        size = _journal(caller).stats(days[0]["day"], days[-1]["day"])["bytes"] if days else 0
        return {"days": len(days), "pages": sum(len(d["pages"]) for d in days),
                "photos": sum(1 for d in days for p in d["pages"] if p["has_photo"]),
                "wins": sum(d["wins"]["count"] for d in days),
                "first": days[0]["day"] if days else None, "last": days[-1]["day"] if days else None,
                "bytes": size, "paper": caller.prefs.get("paper_size", "letter"),
                "can_download": not journal_window.full(caller.name)}

    @app.get("/journal/export")
    def journal_export(format: str = "zip", day: str = "", since: str = "", until: str = "", paper: str = "",
                       caller: Caller = Depends(person)):
        """Download my journal. format=zip: a folder for each day with notes.txt, the photos and
        wins.txt. format=pdf: a page for each day (photo, typed notes, wins) on the `paper_size` paper.
        day=YYYY-MM-DD: just that day, as a PDF to print ("Print my journal page")."""
        import tempfile

        from fastapi.responses import StreamingResponse

        from . import paper as paper_pdf
        if format not in ("zip", "pdf"):
            raise HTTPException(400, "format is zip or pdf")
        size = paper or caller.prefs.get("paper_size", "letter")
        if size not in paper_pdf.POINTS:
            raise HTTPException(400, "paper is letter, a4, a5 or half_letter")
        if day:
            since = until = _past_day(day)
        one_page = bool(day) and format == "pdf"  # "Print my journal page"
        if not one_page:  # any download waits out an admin's password reset, like Download my data
            _reset_hold(caller)
        days = _export_days(caller, since, until)
        if not days:
            raise HTTPException(404, "Nothing in your journal on that day." if day
                                else "Nothing in your journal for those days yet.")
        if not (journal_day_window if one_page else journal_window).allow(caller.name):
            raise HTTPException(429, "That's a lot of downloads this hour. Try again in a bit.")
        journal = _journal(caller)
        stamp = day or ctx.today().isoformat()
        how = "inline" if one_page else "attachment"  # one day opens, ready to print
        if format == "pdf":
            # Sent a sheet at a time while it's drawn, so a long journal starts downloading at once
            # (a proxy like Cloudflare gives up on a reply that hasn't started after 100 s).
            title = f"Next Box journal, {paper_pdf.day_title(day)}" if day else "Next Box journal"
            name = f"next-box-journal-{stamp}.pdf"
            return StreamingResponse(paper_pdf.journal_pdf_chunks(days, size, journal.photo, title=title),
                                     media_type="application/pdf",
                                     headers={"Content-Disposition": f'{how}; filename="{name}"', **PRIVATE})
        out = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
        journal_zip(days, journal.photo, out, root=f"next-box-journal-{stamp}")
        media, name = "application/zip", f"next-box-journal-{stamp}.zip"
        length = out.tell()
        out.seek(0)

        def chunks():
            try:
                while block := out.read(256 * 1024):
                    yield block
            finally:
                out.close()
        return StreamingResponse(chunks(), media_type=media, headers={
            "Content-Disposition": f'{how}; filename="{name}"', "Content-Length": str(length), **PRIVATE})

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

    # ----------------------------------------------------------- friends and notes (friends.py, SPEC §34)
    # Private to the two friends: no admin or superadmin endpoint reads these tables, events carry counts
    # only (never a name, a word or a picture), and nothing here is saved as a ticket. The API contract is
    # at the top of nextbox/friends.py. Every answer is no-store (security_headers).
    friend_window = _Window(friendship.REQUESTS_PER_DAY, 86400)  # every try at adding someone counts
    code_window = _Window(60, 3600)              # friend links looked up: far too few to guess one
    note_window = _Window(friendship.NOTES_PER_DAY, 86400)  # backs up the database's own daily counts
    note_preview_window = _Window(300, 3600)     # "how it prints" while composing
    note_photo_preview_window = _Window(120, 3600)  # ...of a photo: each one is decoded (the app sends a small copy)
    # Pictures are decoded and re-encoded off the server's event loop (in a worker thread), two at a time at
    # most, so one big photo never holds up anyone else's requests and memory stays bounded.
    picture_slots = threading.BoundedSemaphore(2)
    TOO_MANY_NOTES = (f"That's {friendship.NOTES_PER_DAY} notes in the last 24 hours, the most for a day. "
                      "Try again later.")

    def _privately(caller: Caller, action: str, fn, *args, **kwargs):
        """Call friends.py. Its FriendError is the sentence to show, with its status. Anything else is logged
        as its type only: an error's message or a stack's values could quote a note."""
        try:
            return fn(*args, **kwargs)
        except friendship.FriendError as exc:
            raise HTTPException(exc.status, exc.message) from None
        except HTTPException:
            raise
        except Exception as exc:
            events.log(caller.name, action, ok=False, kind="error", error=type(exc).__name__,
                       trace="".join(traceback.format_tb(exc.__traceback__))[-6000:], household=caller.household)
            raise HTTPException(500, "Something went wrong. It's been logged so it can be fixed.") from None

    def _friend_link(request: Request, code: str) -> str:
        """The link to share: on NEXTBOX_PUBLIC_URL when it's set, else the address this came in on (https
        when it came through Cloudflare, which reaches Next Box over plain http)."""
        base = oauth.public_url()
        if not base and from_internet(request) and re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?", request.headers.get("host", "")):
            base = f"https://{request.headers['host']}"
        base = base or str(request.base_url).rstrip("/")
        return f"{base}/app/#friend={code}"

    def _friends_page(caller: Caller, request: Request) -> dict:
        mine = friendship.Friends(accounts.db, caller.name)
        code = mine.code()
        return {**mine.lists(), "code": code, "link": _friend_link(request, code),
                "receipts": caller.prefs.get("friend_receipts", True) is not False}

    @app.get("/friends")
    def friends_page(request: Request, caller: Caller = Depends(person)):
        return _privately(caller, "friends", _friends_page, caller, request)

    @app.post("/friends/code/rotate")
    def friends_new_link(request: Request, caller: Caller = Depends(person)):
        code = _privately(caller, "friend_link_new", friendship.Friends(accounts.db, caller.name).rotate_code)
        events.log(caller.name, "friend_link_new", household=caller.household)
        return {"code": code, "link": _friend_link(request, code)}

    @app.get("/friends/code/qr.png")
    def friends_qr(request: Request, caller: Caller = Depends(person)):
        """Your friend link as a QR code, for a friend's phone camera."""
        import io as _io
        try:
            import qrcode
        except ImportError:  # updated without its packages: the link itself still works
            raise HTTPException(503, "The QR code isn't ready on this Next Box yet: copy or share the link instead. "
                                     "(On the Next Box computer: scripts/update.sh)") from None
        link = _friend_link(request, _privately(caller, "friend_qr", friendship.Friends(accounts.db, caller.name).code))
        qr = qrcode.QRCode(border=4, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(link)
        qr.make(fit=True)
        out = _io.BytesIO()
        qr.make_image(fill_color="black", back_color="white").save(out, format="PNG")
        return Response(out.getvalue(), media_type="image/png", headers=PRIVATE)

    @app.get("/friends/code/{code}")
    def friends_lookup(code: str, caller: Caller = Depends(person)):
        """Whose friend link this is, so the app can ask "Add Lee?" before sending a request."""
        if not code_window.allow(caller.name):
            raise HTTPException(429, "That's a lot of friend links this hour. Try again later.")
        return _privately(caller, "friend_lookup", friendship.Friends(accounts.db, caller.name).lookup, code[:80])

    @app.post("/friends/add")
    def friends_add(body: FriendAddBody, request: Request, caller: Caller = Depends(person)):
        """Ask to be friends, by exact username or friend link. By username the answer is the same whether
        or not the name has an account, so nobody can use it to find out who's here."""
        if not (body.username or "").strip() and not (body.code or "").strip():
            raise HTTPException(400, "Type their username, or open their friend link.")
        if not friend_window.allow(caller.name):
            raise HTTPException(429, friendship.TOO_MANY_REQUESTS)
        if body.code and not code_window.allow(caller.name):
            raise HTTPException(429, "That's a lot of friend links this hour. Try again later.")
        mine = friendship.Friends(accounts.db, caller.name)
        if body.code:
            result = _privately(caller, "friend_request", mine.request, code=body.code)
        else:
            result = _privately(caller, "friend_request", mine.request, username=body.username)
        events.log(caller.name, "friend_request", household=caller.household)  # no names: whom isn't anyone's business
        return {**result, "friends": _friends_page(caller, request)}

    def _friend_change(action: str, method: str):
        def change(username: str, request: Request, caller: Caller = Depends(person)):
            mine = friendship.Friends(accounts.db, caller.name)
            _privately(caller, action, getattr(mine, method), username[:80])
            events.log(caller.name, action, household=caller.household)
            return _friends_page(caller, request)
        change.__name__ = action  # each its own name (and OpenAPI operation id)
        return change

    app.post("/friends/{username}/accept")(_friend_change("friend_accept", "accept"))
    app.post("/friends/{username}/decline")(_friend_change("friend_decline", "decline"))
    app.delete("/friends/{username}")(_friend_change("friend_remove", "remove"))
    app.post("/friends/{username}/block")(_friend_change("friend_block", "block"))
    app.delete("/friends/{username}/block")(_friend_change("friend_unblock", "unblock"))

    def _notes(caller: Caller) -> friendship.Notes:
        return friendship.Notes(accounts.db, caller.name)

    def _note_draw(row: dict):
        """Draws the note's label at a printer's width (jobs.NoteDraw), from its sender, on the day it came."""
        with accounts.db.connect() as conn:
            shown = friendship.names(conn, [row["sender"]])
        when = date.fromtimestamp(row["created"]).strftime("%b %-d")
        pic = friendship.picture(row["image"]) if row.get("image") else None

        def draw(width: int, height: int | None):
            return render.render_friend_note(shown[row["sender"]], when, row["kind"], row.get("body") or "",
                                             row.get("color"), pic, width=width, max_height=height)
        return draw

    def _png(img) -> bytes:
        import io as _io
        out = _io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()

    def _note_allowed(caller: Caller) -> None:
        if not note_window.allow(caller.name):
            raise HTTPException(429, TOO_MANY_NOTES)

    def _send_notes(caller: Caller, to: list[str], kind: str, text, color, image, counted: bool = False) -> dict:
        if not counted:
            _note_allowed(caller)
        result = _privately(caller, "note_send", _notes(caller).send, to, kind, text, color, image)
        events.log(caller.name, "note_send", detail={"count": result["sent"]}, household=caller.household)
        return result

    async def _upload(image: UploadFile | None, kind: str) -> bytes:
        """A photo or doodle from the phone, read up to its limit, then re-encoded (friends.clean_photo or
        clean_doodle): EXIF and location gone, size capped, anything that isn't a picture refused."""
        if kind not in ("draw", "photo"):
            raise HTTPException(400, "Send a drawing or a photo here (a note or a heart goes to POST /notes).")
        if image is None:
            raise HTTPException(400, "Pick a photo first." if kind == "photo" else "Draw something first.")
        limit = friendship.PHOTO_UPLOAD if kind == "photo" else friendship.DOODLE_UPLOAD
        # Wait for a picture slot first: a waiting upload holds neither a worker thread nor its 15 MB.
        deadline = time.monotonic() + 60
        while not picture_slots.acquire(blocking=False):
            if time.monotonic() > deadline:
                raise HTTPException(503, "Next Box is busy with other pictures. Try again in a moment.")
            await asyncio.sleep(0.05)
        try:
            chunks, size = [], 0
            while chunk := await image.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, "That photo is too big. Pick one under 15 MB." if kind == "photo"
                                        else "That drawing is too big to send.")
                chunks.append(chunk)
            data = b"".join(chunks)
            if kind == "photo" and (image.content_type or "") in scan.HEIC_TYPES and not scan.heic_supported():
                raise HTTPException(415, scan.HEIC_HELP)
            return await run_in_threadpool(_clean_picture, kind, data)
        finally:
            picture_slots.release()

    def _clean_picture(kind: str, data: bytes) -> bytes:
        """In a worker thread, never on the event loop (_upload holds one of the two picture slots)."""
        try:
            return friendship.clean_photo(data) if kind == "photo" else friendship.clean_doodle(data)
        except friendship.FriendError as exc:
            raise HTTPException(exc.status, exc.message) from None

    def _recipients(to: list[str]) -> list[str]:
        return [t for item in to for t in str(item).split(",") if t.strip()][:100]

    @app.post("/notes")
    def notes_send(body: NoteBody, caller: Caller = Depends(person)):
        if body.kind in ("draw", "photo"):
            raise HTTPException(400, "Send a drawing or a photo with POST /notes/image.")
        return _send_notes(caller, _recipients(body.to), body.kind, body.text, body.color, None)

    @app.post("/notes/image")
    async def notes_send_image(image: UploadFile | None = File(default=None), kind: str = Form(default="photo"),
                               to: list[str] = Form(default=[]), caption: str = Form(default="", max_length=4000),
                               caller: Caller = Depends(person)):
        people = _recipients(to)
        if not people:
            raise HTTPException(400, "Pick a friend to send it to.")
        notes = _notes(caller)
        # Everything cheap first (the caption, the day's limits, that each one is a friend), so a picture is
        # only read and decoded when it can actually be sent.
        _privately(caller, "note_send", notes.check, kind, caption, None, b"?")
        if kind not in ("draw", "photo"):
            raise HTTPException(400, "Send a drawing or a photo here (a note or a heart goes to POST /notes).")
        _note_allowed(caller)
        await run_in_threadpool(_privately, caller, "note_send", notes.ready, people)
        data = await _upload(image, kind)
        return await run_in_threadpool(_send_notes, caller, people, kind, caption, None, data, True)

    def _preview_allowed(caller: Caller) -> None:
        if not note_preview_window.allow(caller.name):
            raise HTTPException(429, "That's a lot of previews. Try again in a bit.")

    def _preview(caller: Caller, kind: str, text, color, image: bytes | None) -> Response:
        """How it prints: the black-and-white label, from you, today. Nothing is kept or sent."""
        words, color = _privately(caller, "note_preview", _notes(caller).check, kind,
                                  text if (text or "").strip() or kind != "note" else "…", color, image or b"?")
        row = {"sender": caller.name, "created": int(time.time()), "kind": kind, "body": words, "color": color,
               "image": image}
        with render.reading_font(bool(caller.prefs.get("reading_mode"))):
            img = render.finalize(_note_draw(row)(render.WIDTH, None), render.WIDTH)
        return Response(_png(img.convert("L")), media_type="image/png", headers=PRIVATE)

    @app.post("/notes/preview")
    def notes_preview(body: NotePreviewBody, caller: Caller = Depends(person)):
        if body.kind in ("draw", "photo"):
            raise HTTPException(400, "Preview a drawing or a photo with POST /notes/preview/image.")
        _preview_allowed(caller)
        return _preview(caller, body.kind, body.text, body.color, None)

    @app.post("/notes/preview/image")
    async def notes_preview_image(image: UploadFile | None = File(default=None), kind: str = Form(default="photo"),
                                  caption: str = Form(default="", max_length=4000), caller: Caller = Depends(person)):
        _preview_allowed(caller)
        if kind == "photo" and not note_photo_preview_window.allow(caller.name):
            raise HTTPException(429, "That's a lot of photo previews. Try again in a bit.")
        data = await _upload(image, kind)
        return await run_in_threadpool(_preview, caller, kind, caption, None, data)

    @app.get("/notes/feed")
    def notes_feed(limit: int = Query(30, ge=1, le=100), cursor: str | None = Query(None, max_length=60),
                   caller: Caller = Depends(person)):
        """Notes friends sent you, one per screen in the app: unseen first, then the rest, newest first."""
        return _privately(caller, "notes_feed", _notes(caller).feed, limit, cursor)

    @app.get("/notes/count")
    def notes_count(caller: Caller = Depends(person)):
        return _privately(caller, "notes_count", _notes(caller).counts)

    @app.get("/notes/sent")
    def notes_sent(limit: int = Query(30, ge=1, le=100), cursor: str | None = Query(None, max_length=60),
                   caller: Caller = Depends(person)):
        return _privately(caller, "notes_sent", _notes(caller).sent, limit, cursor)

    @app.get("/notes/{note_id}/image")
    def note_image(note_id: str, caller: Caller = Depends(person)):
        data = _privately(caller, "note_image", _notes(caller).image, note_id)
        return Response(data, media_type=friendship.image_type(data), headers=PRIVATE)

    @app.post("/notes/{note_id}/seen")
    def note_seen(note_id: str, caller: Caller = Depends(person)):
        return _privately(caller, "note_seen", _notes(caller).mark_seen, note_id)

    @app.post("/notes/{note_id}/react")
    def note_react(note_id: str, body: ReactBody, caller: Caller = Depends(person)):
        return _privately(caller, "note_react", _notes(caller).react, note_id, body.reaction)

    @app.post("/notes/{note_id}/keep")
    def note_keep(note_id: str, body: KeepBody, caller: Caller = Depends(person)):
        return _privately(caller, "note_keep", _notes(caller).keep, note_id, body.keep)

    @app.delete("/notes/{note_id}")
    def note_delete(note_id: str, confirm: bool = False, caller: Caller = Depends(person)):
        """Delete for me (from your feed, or from your Sent list)."""
        if not confirm:
            raise HTTPException(400, "Deleting a note needs confirm=true.")
        _privately(caller, "note_delete", _notes(caller).delete, note_id)
        events.log(caller.name, "note_delete", household=caller.household)
        return {"ok": True}

    @app.get("/notes/{note_id}/label.png")
    def note_label(note_id: str, download: bool = False, caller: Caller = Depends(person)):
        """How the note comes out of your printer (die-cut labels and all). download=1: Save label image.
        Nothing prints and nothing is kept."""
        row = _privately(caller, "note_label", _notes(caller).get, note_id)
        img = _privately(caller, "note_label", jobs.friend_note_preview, rctx(caller), _note_draw(row))
        how = "attachment" if download else "inline"
        return Response(_png(img), media_type="image/png",
                        headers={"Content-Disposition": f'{how}; filename="next-box-note-{note_id}.png"', **PRIVATE})

    @app.get("/notes/{note_id}/paper.pdf")
    def note_paper(note_id: str, paper: str = "", caller: Caller = Depends(person)):
        """Print on paper: the note's label on a page, for any printer (from your own device)."""
        from . import paper as paper_pdf
        size = paper or caller.prefs.get("paper_size", "letter")
        if size not in paper_pdf.PAGES:
            raise HTTPException(400, "paper is letter, a4, a5 or half_letter")
        row = _privately(caller, "note_paper", _notes(caller).get, note_id)
        with render.reading_font(bool(caller.prefs.get("reading_mode"))):
            img = render.finalize(_note_draw(row)(render.WIDTH, None), render.WIDTH)
        data = paper_pdf.ticket_pdf(_png(img.convert("L")), None, size)
        events.log(caller.name, "note_paper", detail={"paper": size}, household=caller.household)
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="next-box-note-{note_id}.pdf"', **PRIVATE})

    @app.post("/notes/{note_id}/print")
    def note_print(note_id: str, body: NotePrintBody, caller: Caller = Depends(person)):
        """Print a note a friend sent you, on your own household's printer. Only ever because you tapped
        Print and said yes to "Send this to the printer?" (confirm: true): nothing prints a note by itself."""
        if not body.confirm:
            raise HTTPException(400, "Tap Print, then say yes to send it to the printer.")
        notes = _notes(caller)
        row = _privately(caller, "note_print", notes.received, note_id)
        result = _privately(caller, "note_print", jobs.print_friend_note, rctx(caller), _note_draw(row),
                            dry_run=body.dry_run)
        if result["status"] == "printed":
            _privately(caller, "note_print", notes.mark_printed, note_id)
        events.log(caller.name, "note_print", ok=result["status"] != "failed", detail={"status": result["status"]},
                   household=caller.household)
        return {**result, "prints_here": prints_here(caller.household)}

    # ----------------------------------------------------------- printers & label colors
    @app.get("/printing-options")
    def printing_options(caller: Caller = Depends(person)):
        admin = rank(caller.role) >= rank("admin")
        ps = [p.public(admin) for p in registry.all(caller.household)]
        if admin:  # so Remove can say how many people use it as their usual printer
            usual: dict[str, int] = {}
            for u in accounts.all_users(caller.household):
                pid = (u.get("prefs") or {}).get("default_printer")
                usual[pid] = usual.get(pid, 0) + 1
            for p in ps:
                p["usual_for"] = usual.get(p["id"], 0)
        return {"printers": ps, "colors": COLORS, "reasons": REASONS, "drivers": DRIVERS}

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

    _probe_cache: dict[tuple, tuple[float, bool]] = {}  # (household, id, driver, address) -> (when, answered)

    def _probe(caller: Caller, ps: list) -> tuple[dict[str, bool], int]:
        """Knock on every printer at once (1 s each), at most once a minute per printer and address,
        so offline printers don't hold a page up. Returns ({id: answered}, how many were knocked on)."""
        now, out, todo = time.time(), {}, []
        for p in ps:
            hit = _probe_cache.get((caller.household, p.id, p.driver, p.address))
            if hit and hit[0] > now - 60:
                out[p.id] = hit[1]
            elif not _usable(caller, p):
                out[p.id] = False  # saved before addresses were checked: never knocked on
            else:
                todo.append(p)
        if todo:
            with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
                answers = list(pool.map(lambda p: printer.is_reachable(p, timeout=1.0), todo))
            for p, ok in zip(todo, answers):
                _probe_cache[(caller.household, p.id, p.driver, p.address)] = (now, ok)
                out[p.id] = ok
        return {p.id: out[p.id] for p in ps}, len(todo)

    @app.get("/admin/printers/status")
    def printers_status(caller: Caller = Depends(admin_only)):
        result, knocked = _probe(caller, registry.all(caller.household))
        if knocked:
            audit(caller, "printer_check", printers=knocked)
        return result

    # Set up a printer (the walkthrough): find it on the network, and ask a Brother which labels are in it.
    # Both only listen or read, never print. Only staff, or an admin of Home: the Next Box computer's network is
    # Home's, so a friend's household can't find (or read) their own printers from here (jobs.PRINT_AWAY).
    def _may_look(caller: Caller) -> None:
        if _restricted(caller):
            raise HTTPException(403, jobs.PRINT_AWAY)

    @app.post("/admin/printers/discover")
    def discover_printers(caller: Caller = Depends(admin_only)):
        _may_look(caller)
        try:
            found, how = discover.find()
        except discover.FindError as exc:
            audit(caller, "printer_discover", found=0, how=None)
            return {"printers": [], "how": None, "why": str(exc)}
        audit(caller, "printer_discover", found=len(found), how=how)
        return {"printers": [dataclasses.asdict(f) for f in found], "how": how, "why": ""}

    @app.get("/admin/printers/media")
    def printer_media(address: str, caller: Caller = Depends(admin_only)):
        _may_look(caller)
        try:
            out = discover.read_media(address)
        except PrinterError as exc:
            raise HTTPException(400, str(exc)) from exc
        audit(caller, "printer_media", read=out["read"], label=out["label"])
        return out

    @app.post("/admin/printers")
    def add_printer(body: PrinterBody, caller: Caller = Depends(admin_only)):
        try:
            cfg = registry.add(body.model_dump(exclude_none=True), caller.household, restricted=_restricted(caller))
        except PrinterError as exc:
            raise HTTPException(400, str(exc)) from exc
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
        audit(caller, "edit_printer", printer=pid, changed=sorted(changes))
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

    @app.patch("/calendars/{cal_id}")
    def rename_calendar(cal_id: str, body: CalendarName, caller: Caller = Depends(person)):
        try:
            entry = connections.rename_calendar(caller.name, cal_id, body.name)
        except KeyError:
            raise HTTPException(404, "That calendar was already removed.") from None
        except ProviderError as exc:
            raise HTTPException(400, str(exc)) from exc
        events.log(caller.name, "calendar_rename")
        return entry

    @app.get("/calendars/today")
    def calendar_today(caller: Caller = Depends(person)):
        failing: dict[str, str] = {}
        found, problems = connections.events_for(caller.name, ctx.today(), failing=failing)
        return {"events": found, "problems": problems, "failing": failing}  # failing: {calendar id: what's wrong}

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
    def get_connection(check: bool = False, caller: Caller = Depends(person)):
        """check=1 also asks the linked app whether the saved sign-in still works (ok, error)."""
        if not check:
            return connections.status(caller.name)
        result = connections.check(caller.name)
        if not result["ok"]:
            events.log(caller.name, "connection_check", ok=False, detail={"provider": result.get("provider")},
                       error=result["error"])
        return result

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
        if caller.prefs.get("new_tasks_to") == "linked":  # no linked app now: new tasks go to My list
            accounts.set_prefs(caller.name, {"new_tasks_to": "nextbox"})
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
        if not accounts.revoke_invite(code):
            used = mine[code.strip().upper()]["status"] == "used"
            raise HTTPException(409, "that invite was already used" if used else "that invite has expired")
        audit(caller, "revoke_invite", code=code.strip().upper(),
              household_id=mine[code.strip().upper()]["household_id"])
        return {"ok": True}

    @app.post("/admin/invites/clear")
    def clear_invites(caller: Caller = Depends(admin_only)):
        """Tidy the list: forget the used and expired codes this admin can see. Open ones stay."""
        done = [i["code"] for i in accounts.invites(scope(caller)) if i["status"] != "open"]
        n = accounts.clear_done_invites(done)
        audit(caller, "clear_invites", removed=n)
        return {"removed": n}

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
        if username == caller.name and changes.get("must_change"):  # it would sign you out mid-session
            raise HTTPException(400, "change your own password in Profile → Password")

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
        cols = ["username", "email", "role", "household", "household_id", "protected", "disabled", "beta", "debug",
                "must_change", "consented", "created", "last_login", "last_active", "sessions", "app", "open_tasks",
                "prints_30d", "errors_7d"]

        def cell(k, v):
            if isinstance(v, bool):
                return "yes" if v else "no"
            if k in ("created", "last_login", "last_active"):  # dates a spreadsheet can read
                return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M") if v else ""
            # Spreadsheet apps run cells that start with = + - @ as formulas: neutralize them.
            return "'" + v if isinstance(v, str) and v[:1] in "=+-@" else v
        buf = io.StringIO()
        buf.write("\ufeff")  # so Excel reads names and emails with accents as UTF-8
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in _user_rows():
            r = {**r, "last_active": max(r["last_login"] or 0, r["last_seen"] or 0) or None}
            w.writerow({k: cell(k, v) for k, v in r.items()})
        audit(caller, "export_users")
        name = f"nextbox-users-{date.today().isoformat()}.csv"
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.get("/super/users/{username}")
    def super_user_detail(username: str, caller: Caller = Depends(super_only)):
        target = _target(username)
        name = target["username"]
        row = next(r for r in _user_rows() if r["username"] == name)
        if name != caller.name:
            audit(caller, "view_user", target=name, what="details")
        return {**row, "prefs": target["prefs"], "connection": connections.status(name),
                "devices": accounts.sessions_for(name),
                "tag_count": len(Tags(accounts.db, name).all()),  # how many, not what they say (tags describe tasks)
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
        try:
            name = accounts.check_household_name(body.name)
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        hid = accounts.create_household(name)
        audit(caller, "create_household", household_id=hid)
        return accounts.household(hid)

    @app.patch("/super/households/{hid}")
    def super_rename_household(hid: str, body: HouseholdBody, caller: Caller = Depends(super_only)):
        try:
            home = accounts.rename_household(hid, body.name)
        except KeyError:
            raise HTTPException(404, "no such household") from None
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        audit(caller, "rename_household", household_id=hid)
        return home

    @app.delete("/super/households/{hid}")
    def super_delete_household(hid: str, caller: Caller = Depends(super_only)):
        """Only an empty one: people are moved or deleted first, one by one, from Manage."""
        try:
            accounts.remove_empty_household(hid)
        except KeyError:
            raise HTTPException(404, "no such household") from None
        except AuthError as exc:
            raise HTTPException(409, str(exc)) from exc
        registry.delete_for_household(hid)
        friendship.sweep(accounts.db)  # friends' rows that name someone who has gone
        audit(caller, "delete_household", household_id=hid)
        return {"ok": True}

    @app.get("/super/usage")
    def usage(days: int = 30, caller: Caller = Depends(super_only)):
        days = max(1, min(days, 365))
        first = date.fromtimestamp(time.time()) - timedelta(days=days - 1)  # today and the days before it
        since = first.isoformat()
        tickets = [r for r in ctx.store.list_printed(100_000) if r["created_at"][:10] >= since]
        scans_ = [r for r in ctx.store.list_scans(100_000) if r["created_at"][:10] >= since]
        start_ts = datetime.combine(first, datetime.min.time()).timestamp()
        acts = [e for kind in ("activity", "auth")  # only the period's events, so a busy server isn't cut short
                for e in events.query(kind=kind, ok=True, since=start_ts, limit=200_000)]
        weekly = days > 31  # longer periods: one bar a week, keyed by its Monday

        def bucket(day: str) -> str:
            if not weekly:
                return day
            d = date.fromisoformat(day)
            return (d - timedelta(days=d.weekday())).isoformat()
        series: dict[str, dict] = {}
        for i in range(days):
            k = bucket((first + timedelta(days=i)).isoformat())
            series.setdefault(k, {"date": k, "tickets": 0, "scans": 0, "people": set()})
        for r in tickets:
            if (k := bucket(r["created_at"][:10])) in series:
                series[k]["tickets"] += 1
        for r in scans_:
            if (k := bucket(r["created_at"][:10])) in series:
                series[k]["scans"] += 1
        for e in acts:
            if (k := bucket(date.fromtimestamp(e["ts"]).isoformat())) in series:
                series[k]["people"].add(e["user"])
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
                "consented": (u.get("consent") or {}).get("version") == DATA_NOTICE_VERSION,  # the current notice
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
        return {"days": days, "bucket": "week" if weekly else "day",
                "totals": {"people": len(people), "active": len({e["user"] for e in acts}),
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
            linked = connections.status(caller.name)
            todoist.update(ok=True, app=linked.get("name"), linked=bool(linked.get("connected")),
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
        answered, _ = _probe(caller, printers_now)
        reachable = any(answered.values())
        usage = shutil.disk_usage(data_dir)
        size = sum(p.stat().st_size for p in data_dir.rglob("*") if p.is_file())
        # Home Assistant's once-a-day print: the last day it was claimed, for real and as a preview.
        claimed = ctx.store.auto_state()
        auto = {"enabled": bool(server_settings.get().get("auto_print_enabled", True)),
                **{k: max((day for key, day in claimed.items() if key.endswith(":" + k)), default=None)
                   for k in ("real", "dry")}}
        return {
            "version": _version(), "python": sys.version.split()[0], "platform": platform.platform(),
            "uptime_s": int(time.time() - STARTED),
            "config": ctx.settings.redacted(),
            "printer": {"reachable": reachable, "ms": int((time.time() - start) * 1000),
                        "ip": ", ".join(f"{p.name} ({p.stock_color})" for p in printers_now) or None,
                        "label": ctx.settings.label,
                        "printers": [{"name": p.name, "driver": p.driver, "stock_color": p.stock_color,
                                      "label": p.label if p.driver == "brother_ql" else None,
                                      "reachable": answered[p.id]} for p in printers_now]},
            "todoist": todoist,
            "apps": apps,
            "vision": {"model": ctx.settings.vision_model, "key_set": bool(ctx.settings.anthropic_api_key)},
            "coach": {"model": ctx.settings.coach_model, "key_set": bool(ctx.settings.anthropic_api_key)},
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
        if wants_page and page.exists() and not request.url.path.startswith(("/auth", "/super", "/admin", "/list", "/print",
                                                                              "/friends", "/notes")):
            return Response(page.read_text(), status_code=404, media_type="text/html")
        detail = getattr(exc, "detail", None) or "Not Found"
        return JSONResponse({"detail": detail}, status_code=404)

    @app.post("/app/share", include_in_schema=False)
    async def share_in(request: Request):
        """Share to Next Box (manifest.json share_target). On the phone the app's service worker answers
        this itself and hands a photo straight to Snap; this is what happens without it. Text opens Quick
        add filled in (it's carried in the address's #, which never comes back here); a photo can't be
        handed over from here, so Snap asks for it again. Nothing is kept or logged, and it needs no
        sign-in because it only says where to go."""
        try:
            size = int(request.headers.get("content-length") or 0)
        except ValueError:
            size = 0
        photo, text = size > MAX_BODY_BYTES, ""
        if not photo:
            try:
                form = await request.form()
            except Exception as e:  # over 256 KB with no Content-Length (a photo, as a phone may send it), or not a form
                form, photo = None, getattr(e, "status_code", 0) == 413
            if form is not None:
                photo = any(not isinstance(v, str) and (getattr(v, "filename", "") or getattr(v, "size", 0))
                            for _, v in form.multi_items())
                text = share_text(*(form.get(k) for k in ("title", "text", "url")))
                await form.close()
        target = "/app/#snap&shared=missed" if photo else "/app/#add" + (f"&text={quote(text, safe='')}" if text else "")
        return RedirectResponse(target, status_code=303)

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

    def full(self, key: str) -> bool:
        """Would the next try be refused? Asking doesn't count as a try."""
        now = time.time()
        with self._lock:
            return len([t for t in self._hits.get(key, []) if now - t < self.seconds]) >= self.limit


class _BodyLimit:
    """Refuse oversized requests before reading them: 16 MB for a photo read-back, 256 KB for
    everything else. Bodies sent without a Content-Length are counted as they arrive. A share
    (/app/share, no sign-in) is held to 256 KB too, but answers a bigger one itself: Snap, to pick it again."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        big = scope.get("path") in BIG_UPLOADS
        limit = MAX_UPLOAD_BYTES if big else MAX_BODY_BYTES
        msg = "photo too large" if big else "That's more than Next Box takes in one go."
        length = dict(scope.get("headers") or []).get(b"content-length", b"")
        if length.isdigit() and int(length) > limit and scope.get("path") != "/app/share":  # a share's photo: answered unread
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


SHARE_TEXT_MAX = 2000  # characters of shared text that go on to Quick add


def share_text(title=None, text=None, url=None) -> str:
    """What someone shared, as Quick add lines: the title, text and link once each (a phone often puts
    the link in the text too). A shared list keeps its lines and leaves the title off, so the note's
    name doesn't become a task. Web app sw.js does the same on the phone."""
    parts = [p.strip() for p in (title, text, url) if isinstance(p, str) and p.strip()]
    low = [p.lower() for p in parts]  # "Dentist" is in "Call the dentist" too (lower(), as sw.js's toLowerCase)
    keep = [p for i, p in enumerate(parts)
            if low[i] not in low[:i] and not any(j != i and low[i] != q and low[i] in q for j, q in enumerate(low))]
    lines = isinstance(text, str) and "\n" in text.strip()
    if lines and isinstance(title, str) and title.strip() in keep and title.strip() != text.strip():
        keep.remove(title.strip())
    return ("\n" if lines else " ").join(keep)[:SHARE_TEXT_MAX]


def _version() -> str:
    """pyproject.toml is the source of truth (installed metadata can lag behind an update)."""
    return app_version()  # the same answer as `nextbox --version` and `nextbox doctor`


def build_default_app() -> FastAPI:
    from .config import load_settings
    from .store import Store

    import os

    os.umask(0o077)  # every file Next Box writes is readable by this desktop user only
    settings = load_settings()
    from .move import MOVED_EXIT, moved_away_message, read_marker
    marker = read_marker(settings.data_dir)
    if marker is not None:  # moved to another PC (scripts/move-out.sh): this copy's data is old
        print(moved_away_message(marker), file=sys.stderr)
        raise SystemExit(MOVED_EXIT)
    if not settings.server_key:
        raise SystemExit("NEXTBOX_KEY is not set in .env; refusing to start an unauthenticated server")
    store = Store(settings.data_dir)
    store.prune(settings.retention_days)
    return create_app(Context(settings=settings, store=store, printers=Printers(settings.data_dir, settings)))
