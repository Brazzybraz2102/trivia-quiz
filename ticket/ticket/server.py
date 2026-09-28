"""HTTP API for the web app, phone app and Home Assistant. LAN only."""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import jobs, printer, scan
from .config import PROJECT_DIR
from .jobs import Context

WEB_DIR = PROJECT_DIR / "web"
MAX_PHOTO_BYTES = 15 * 1024 * 1024


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


def create_app(ctx: Context, vision: scan.VisionFn | None = None) -> FastAPI:
    app = FastAPI(title="ticket", docs_url="/docs")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])
    key = ctx.settings.ticket_key

    def auth(x_ticket_key: str | None = Header(default=None), k: str | None = Query(default=None)):
        supplied = x_ticket_key or k or ""
        if not key or not secrets.compare_digest(supplied.encode(), key.encode()):
            raise HTTPException(401, "missing or wrong X-Ticket-Key")

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
        path = ctx.store.png_path(label_id.lower())
        if not label_id.isalnum() or not path.exists():
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
