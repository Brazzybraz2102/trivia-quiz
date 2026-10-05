"""Every print type lives here so the CLI, web app, hotkey and Home Assistant share one path."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime
from collections.abc import Callable

from PIL import Image

from . import printer
from .auth import default_prefs
from .config import Settings
from .render import DayRow, render_day, render_label
from .store import Store
from .providers import TaskProvider

TODAY_QUERY = "today | overdue"
MAX_ROWS = 10  # the rest are listed under "Also waiting" (not markable)


@dataclass
class Context:
    settings: Settings
    store: Store
    tasks_factory: Callable[[], TaskProvider] | None = None  # this person's to-do app
    today: Callable[[], date] = date.today
    user: str = "cli"                 # whose tickets these are (a username)
    prefs: dict = field(default_factory=default_prefs)
    printing_paused: bool = False     # server-wide kill switch: everything becomes a dry run
    auto_print_enabled: bool = True
    _tasks: TaskProvider | None = field(default=None, repr=False)

    @property
    def tasks(self) -> TaskProvider:
        if self._tasks is None:
            if self.tasks_factory is None:
                from .providers import ProviderError
                raise ProviderError("Connect your to-do app in Settings first.")
            self._tasks = self.tasks_factory()
        return self._tasks

    def is_dry(self, dry_run: bool | None) -> bool:
        return (bool(dry_run) or self.settings.dry_run or self.printing_paused
                or bool(self.prefs.get("always_dry_run")))


def _clean(content: str) -> str:
    """Strip Todoist markdown links/emphasis so labels stay readable."""
    content = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content)
    return re.sub(r"[*_`]{1,2}", "", content).strip()


def _time(task: dict, h24: bool = False) -> str:
    """Todoist puts the time in due.date ("2026-09-30T19:00:00") or due.datetime."""
    due = task.get("due") or {}
    raw = due.get("datetime") or due.get("date") or ""
    if len(raw) < 16:
        return ""
    hour, minute = int(raw[11:13]), raw[14:16]
    if h24:
        return f"{hour:02d}:{minute}"
    return f"{hour % 12 or 12}:{minute}{'a' if hour < 12 else 'p'}"


def _tag(task: dict, today: date) -> str:
    due = task.get("due") or {}
    bits = []
    if due.get("date"):
        late = (today - date.fromisoformat(due["date"][:10])).days
        if late > 0:
            bits.append(f"overdue {late}d")
    if due.get("is_recurring"):
        bits.append("↻")
    return " ".join(bits)


def _sort_key(task: dict, today: date):
    """Today's timed items in time order, then urgent, then the rest of today, then overdue
    newest first, so long-stale tasks sink into "Also waiting" instead of crowding out today."""
    due = task.get("due") or {}
    day = (due.get("date") or "9999")[:10]
    is_today = day == today.isoformat()
    timed = bool(_time(task)) and is_today
    urgent = int(task.get("priority", 1)) >= 3
    group = 0 if timed else 1 if urgent else 2 if is_today else 3
    days_late = (today - date.fromisoformat(day)).days if day != "9999" else 0
    return (group, _time_minutes(task) if timed else 0, -int(task.get("priority", 1)), days_late,
            task.get("child_order", 0))


def _time_minutes(task: dict) -> int:
    due = task.get("due") or {}
    raw = due.get("datetime") or due.get("date") or ""
    return int(raw[11:13]) * 60 + int(raw[14:16]) if len(raw) >= 16 else 0


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
        "by": ctx.user,
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
    tasks = sorted(tasks, key=lambda t: _sort_key(t, today))
    max_rows = int(ctx.prefs.get("max_rows", MAX_ROWS))
    shown, waiting = tasks[:max_rows], tasks[max_rows:]
    h24 = bool(ctx.prefs.get("time_24h"))
    label_id = ctx.store.new_id(today)
    rows, manifest = [], []
    for n, task in enumerate(shown, start=1):
        due = task.get("due") or {}
        rows.append(DayRow(_clean(task["content"]), _time(task, h24), int(task.get("priority", 1)) >= 3,
                           _tag(task, today)))
        manifest.append({
            "row": n,
            "task_id": str(task["id"]),
            "content": task["content"],
            "due_date": (due.get("date") or "")[:10] or None,
            "is_recurring": bool(due.get("is_recurring")),
        })
    now = datetime.now()
    made = f"{now:%a} {now.hour % 12 or 12}:{now:%M}{'a' if now.hour < 12 else 'p'}"
    footer = f"{kind}  {today.isoformat()}  made {made}"
    show_waiting = ctx.prefs.get("show_waiting", True)
    img = render_day(title, subtitle, rows, code=label_id, footer=footer,
                     waiting=[_clean(t["content"]) for t in waiting] if show_waiting else None)
    return _finish(ctx, kind=kind, title=title, img=img, label_id=label_id,
                   manifest=manifest, source=source, dry_run=dry_run)


def print_today(ctx: Context, source: str = "manual", dry_run: bool | None = None) -> dict:
    """source="auto" is Home Assistant's once-a-day print. It is guarded server-side:
    the second auto call on the same day returns skipped, whatever happened to the first."""
    today = ctx.today()
    dry = ctx.is_dry(dry_run)
    if source == "auto" and not ctx.auto_print_enabled:
        return {"status": "skipped", "reason": "auto print is turned off in server settings"}
    if source == "auto" and not dry and not printer.is_reachable(ctx.settings.printer_ip):
        # Don't use up today's print on a printer that's off; the next walk-in will print.
        return {"status": "skipped", "reason": "printer is offline; today's ticket will print next time"}
    if source == "auto" and not ctx.store.claim_auto(today.isoformat(), dry):
        return {"status": "skipped", "reason": f"auto print already ran on {today.isoformat()}"}
    try:
        tasks = ctx.tasks.today()
    except Exception:
        if source == "auto":
            ctx.store.release_auto(today.isoformat(), dry)  # nothing printed, so no reprint risk
        raise
    title = today.strftime("%A")
    subtitle = today.strftime("%B %-d")
    return _task_label(ctx, kind="today", title=title, subtitle=subtitle, tasks=tasks,
                       source=source, dry_run=dry_run)


def print_filter(ctx: Context, query: str, title: str | None = None,
                 dry_run: bool | None = None, source: str = "manual") -> dict:
    tasks = ctx.tasks.filter_tasks(query)
    return _task_label(ctx, kind="list", title=title or query, subtitle=ctx.today().strftime("%b %-d"),
                       tasks=tasks, source=source, dry_run=dry_run)


def print_task(ctx: Context, task_id: str, dry_run: bool | None = None, source: str = "manual") -> dict:
    task = ctx.tasks.get_task(task_id)
    label_id = ctx.store.new_id(ctx.today())
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
    label_id = ctx.store.new_id(ctx.today())
    img = render_label(title, ctx.today().strftime("%a %b %-d"), body=text, code=label_id)
    return _finish(ctx, kind="text", title=title, img=img, label_id=label_id, manifest=[],
                   source=source, dry_run=dry_run, text=text)


def add_and_print(ctx: Context, text: str, dry_run: bool | None = None, source: str = "manual") -> dict:
    """Create a task in the person's to-do app (first line = title, rest = notes), print its ticket."""
    first, _, rest = text.strip().partition("\n")
    task = ctx.tasks.add_task(first.strip()[:500], description=rest.strip())
    return print_task(ctx, str(task["id"]), dry_run=dry_run, source=source)


def read_clipboard() -> str:
    for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"]):
        if shutil.which(cmd[0]):
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                return out.stdout
    raise RuntimeError("no clipboard tool found (install wl-clipboard or xclip)")
