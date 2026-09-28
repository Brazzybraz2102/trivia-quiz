"""Every print type lives here so the CLI, web app, hotkey and Home Assistant share one path."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from PIL import Image

from . import printer
from .config import Settings
from .render import Row, render_label
from .store import Store
from .todoist import Todoist

TODAY_QUERY = "today | overdue"


@dataclass
class Context:
    settings: Settings
    store: Store
    todoist_factory: Callable[[], Todoist] | None = None
    today: Callable[[], date] = date.today
    _todoist: Todoist | None = field(default=None, repr=False)

    @property
    def todoist(self) -> Todoist:
        if self._todoist is None:
            factory = self.todoist_factory or (lambda: Todoist(self.settings.todoist_token))
            self._todoist = factory()
        return self._todoist

    def is_dry(self, dry_run: bool | None) -> bool:
        return bool(dry_run) or self.settings.dry_run


def _clean(content: str) -> str:
    """Strip Todoist markdown links/emphasis so labels stay readable."""
    content = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content)
    return re.sub(r"[*_`]{1,2}", "", content).strip()


def _meta(task: dict, today: date) -> str:
    bits = []
    due = task.get("due") or {}
    if due.get("date") and due["date"][:10] < today.isoformat():
        bits.append("late")
    if task.get("priority") == 4:
        bits.append("p1")
    elif task.get("priority") == 3:
        bits.append("p2")
    if due.get("is_recurring"):
        bits.append("↻")
    return " ".join(bits)


def _sort_key(task: dict):
    due = task.get("due") or {}
    return (due.get("date", "9999")[:10], -int(task.get("priority", 1)), task.get("child_order", 0))


def _finish(ctx: Context, *, kind: str, title: str, img: Image.Image, label_id: str,
            manifest: list[dict], source: str, dry_run: bool | None, text: str = "") -> dict:
    dry = ctx.is_dry(dry_run)
    png = ctx.store.png_path(label_id)
    img.save(png)
    sent = printer.print_image(img, ctx.settings, dry_run=dry)
    record = {
        "id": label_id,
        "kind": kind,
        "title": title,
        "source": source,
        "dry_run": not sent,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "png": str(png),
        "manifest": manifest,
    }
    if text:
        record["text"] = text
    ctx.store.save_printed(record)
    return {"status": "printed" if sent else "dry_run", **record}


def _task_label(ctx: Context, *, kind: str, title: str, subtitle: str, tasks: list[dict],
                source: str, dry_run: bool | None) -> dict:
    today = ctx.today()
    tasks = sorted(tasks, key=_sort_key)
    label_id = ctx.store.new_id()
    rows, manifest = [], []
    for n, task in enumerate(tasks, start=1):
        due = task.get("due") or {}
        rows.append(Row(n, _clean(task["content"]), _meta(task, today)))
        manifest.append({
            "row": n,
            "task_id": str(task["id"]),
            "content": task["content"],
            "due_date": (due.get("date") or "")[:10] or None,
            "is_recurring": bool(due.get("is_recurring")),
        })
    img = render_label(title, subtitle, rows=rows, code=label_id)
    return _finish(ctx, kind=kind, title=title, img=img, label_id=label_id,
                   manifest=manifest, source=source, dry_run=dry_run)


def print_today(ctx: Context, source: str = "manual", dry_run: bool | None = None) -> dict:
    """source="auto" is Home Assistant's once-a-day print. It is guarded server-side:
    the second auto call on the same day returns skipped, whatever happened to the first."""
    today = ctx.today()
    dry = ctx.is_dry(dry_run)
    if source == "auto" and not ctx.store.claim_auto(today.isoformat(), dry):
        return {"status": "skipped", "reason": f"auto print already ran on {today.isoformat()}"}
    try:
        tasks = ctx.todoist.filter_tasks(TODAY_QUERY)
    except Exception:
        if source == "auto":
            ctx.store.release_auto(today.isoformat(), dry)  # nothing printed, so no reprint risk
        raise
    title = "TODAY"
    subtitle = today.strftime("%A %b %-d")
    return _task_label(ctx, kind="today", title=title, subtitle=subtitle, tasks=tasks,
                       source=source, dry_run=dry_run)


def print_filter(ctx: Context, query: str, title: str | None = None,
                 dry_run: bool | None = None, source: str = "manual") -> dict:
    tasks = ctx.todoist.filter_tasks(query)
    return _task_label(ctx, kind="list", title=title or query, subtitle=ctx.today().strftime("%a %b %-d"),
                       tasks=tasks, source=source, dry_run=dry_run)


def print_task(ctx: Context, task_id: str, dry_run: bool | None = None, source: str = "manual") -> dict:
    task = ctx.todoist.get_task(task_id)
    label_id = ctx.store.new_id()
    due = task.get("due") or {}
    subtitle = f"due {due['date'][:10]}" if due.get("date") else ""
    img = render_label(_clean(task["content"]), subtitle, body=task.get("description", ""), code=label_id)
    manifest = [{"row": 1, "task_id": str(task["id"]), "content": task["content"],
                 "due_date": (due.get("date") or "")[:10] or None,
                 "is_recurring": bool(due.get("is_recurring"))}]
    return _finish(ctx, kind="task", title=task["content"], img=img, label_id=label_id,
                   manifest=manifest, source=source, dry_run=dry_run)


def print_text(ctx: Context, text: str, title: str = "NOTE", dry_run: bool | None = None,
               source: str = "manual") -> dict:
    label_id = ctx.store.new_id()
    img = render_label(title, ctx.today().strftime("%a %b %-d"), body=text, code=label_id)
    return _finish(ctx, kind="text", title=title, img=img, label_id=label_id, manifest=[],
                   source=source, dry_run=dry_run, text=text)


def add_and_print(ctx: Context, text: str, dry_run: bool | None = None, source: str = "manual") -> dict:
    """Create a Todoist task (first line = content, rest = description) and print its ticket."""
    first, _, rest = text.strip().partition("\n")
    task = ctx.todoist.add_task(first.strip()[:500], description=rest.strip())
    return print_task(ctx, str(task["id"]), dry_run=dry_run, source=source)


def read_clipboard() -> str:
    for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"]):
        if shutil.which(cmd[0]):
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                return out.stdout
    raise RuntimeError("no clipboard tool found (install wl-clipboard or xclip)")
