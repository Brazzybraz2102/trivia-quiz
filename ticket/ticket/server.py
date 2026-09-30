"""HTTP API for the web app, phone app and Home Assistant. LAN only.

Two ways in:
- people sign in with a username + password (session cookie, or a bearer token for native apps)
- Home Assistant and scripts send the shared X-Ticket-Key header
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path

from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import jobs, printer, scan
from .auth import Accounts
from .store import normalize_id
from .config import PROJECT_DIR
from .jobs import Context

WEB_DIR = PROJECT_DIR / "web"
MAX_PHOTO_BYTES = 15 * 1024 * 1024
COOKIE = "ticket_session"


class LoginBody(BaseModel):
    username: str
    password: str
    want_token: bool = False  # native apps: return a bearer token instead of relying on cookies


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


def create_app(ctx: Context, vision: scan.VisionFn | None = None,
               accounts: Accounts | None = None) -> FastAPI:
    app = FastAPI(title="ticket", docs_url="/docs")
    # No allow_credentials: cookies only work same-origin; other clients use headers.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])
    key = ctx.settings.ticket_key
    accounts = accounts or Accounts(ctx.settings.data_dir)

    def _bearer(request: Request) -> str | None:
        h = request.headers.get("authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else None

    def auth(request: Request, x_ticket_key: str | None = Header(default=None)) -> str:
        """Returns who is calling: a username, or "key" for Home Assistant/scripts."""
        if x_ticket_key:
            if key and secrets.compare_digest(x_ticket_key.encode(), key.encode()):
                return "key"
            raise HTTPException(401, "wrong X-Ticket-Key")
        bearer = _bearer(request)
        user = accounts.session_user(bearer or request.cookies.get(COOKIE))
        if not user:
            raise HTTPException(401, "sign in required")
        if not bearer and request.method not in {"GET", "HEAD", "OPTIONS"}:
            # Cookie-authenticated writes must come from this site's own pages.
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                raise HTTPException(403, "cross-site request refused")
        return user

    @app.post("/auth/login")
    def login(body: LoginBody, request: Request, response: Response):
        client = request.client.host if request.client else "?"
        if accounts.too_many_failures(client):
            raise HTTPException(429, "too many attempts; wait 5 minutes")
        if not accounts.verify(body.username, body.password):
            accounts.record_failure(client)
            raise HTTPException(401, "wrong username or password")
        accounts.clear_failures(client)
        username = body.username.strip().lower()
        token = accounts.create_session(username)
        if body.want_token:
            return {"username": username, "token": token}
        response.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/")
        return {"username": username}

    @app.post("/auth/logout")
    def logout(request: Request, response: Response):
        accounts.end_session(_bearer(request) or request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/auth/me")
    def me(request: Request):
        user = accounts.session_user(_bearer(request) or request.cookies.get(COOKIE))
        if not user:
            raise HTTPException(401, "sign in required" if accounts.has_users()
                                else "no accounts yet: run `ticket user add <name>` on the desktop")
        return {"username": user}

    def run(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/status", dependencies=[Depends(auth)])
    def status():
        return {
            "printer_reachable": printer.is_reachable(ctx.settings.printer_ip),
            "dry_run_forced": ctx.settings.dry_run,
            "config": ctx.settings.redacted(),
        }

    @app.get("/tasks", dependencies=[Depends(auth)])
    def tasks(query: str = jobs.TODAY_QUERY):
        return run(ctx.todoist.filter_tasks, query)

    @app.post("/print/today", dependencies=[Depends(auth)])
    def p_today(body: TodayBody):
        if body.source not in {"manual", "auto"}:
            raise HTTPException(400, "source must be manual or auto")
        return run(jobs.print_today, ctx, source=body.source, dry_run=body.dry_run)

    @app.post("/print/list", dependencies=[Depends(auth)])
    def p_list(body: ListBody):
        return run(jobs.print_filter, ctx, body.query, body.title, dry_run=body.dry_run)

    @app.post("/print/task", dependencies=[Depends(auth)])
    def p_task(body: TaskBody):
        return run(jobs.print_task, ctx, body.task_id, dry_run=body.dry_run)

    @app.post("/print/text", dependencies=[Depends(auth)])
    def p_text(body: TextBody):
        if not body.text.strip():
            raise HTTPException(400, "text is empty")
        if body.todo:
            return run(jobs.add_and_print, ctx, body.text, dry_run=body.dry_run)
        return run(jobs.print_text, ctx, body.text, body.title, dry_run=body.dry_run)

    @app.get("/printed", dependencies=[Depends(auth)])
    def printed(limit: int = 50):
        return ctx.store.list_printed(limit)

    @app.get("/printed/{label_id}/png", dependencies=[Depends(auth)])
    def printed_png(label_id: str):
        label_id = normalize_id(label_id)
        path = ctx.store.png_path(label_id)
        if not re.fullmatch(r"[A-Z0-9-]{4,16}", label_id) or not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.post("/scan", dependencies=[Depends(auth)])
    async def do_scan(photo: UploadFile = File(...), label_id: str | None = Form(default=None)):
        data = await photo.read()
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(413, "photo too large")
        media_type = photo.content_type or "image/jpeg"
        if media_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            raise HTTPException(415, f"unsupported image type {media_type}")
        fn = vision or scan.claude_vision(ctx.settings)
        return run(scan.scan_photo, ctx, fn, data, media_type, label_id or None)

    @app.get("/scans", dependencies=[Depends(auth)])
    def scans(limit: int = 20):
        return ctx.store.list_scans(limit)

    @app.get("/scan/{scan_id}", dependencies=[Depends(auth)])
    def get_scan(scan_id: str):
        rec = ctx.store.get_scan(scan_id)
        if not rec:
            raise HTTPException(404)
        return rec

    @app.post("/scan/{scan_id}/confirm", dependencies=[Depends(auth)])
    def confirm_scan(scan_id: str, body: ConfirmBody):
        try:
            return scan.confirm(ctx, scan_id, body.decisions)
        except KeyError:
            raise HTTPException(404)

    if Path(WEB_DIR).is_dir():
        app.mount("/app", StaticFiles(directory=WEB_DIR, html=True), name="app")

        @app.get("/")
        def root():
            return RedirectResponse("/app/")

    return app


def build_default_app() -> FastAPI:
    from .config import load_settings
    from .store import Store

    settings = load_settings()
    if not settings.ticket_key:
        raise SystemExit("TICKET_KEY is not set in .env; refusing to start an unauthenticated server")
    return create_app(Context(settings=settings, store=Store(settings.data_dir)))
