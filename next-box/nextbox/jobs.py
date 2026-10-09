"""Every print type lives here so the CLI, web app, hotkey and Home Assistant share one path."""
from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from PIL import Image

from . import printer
from .auth import default_prefs
from .config import Settings
from .printer import PrinterConfig, legacy_config
from .printers import Printers, choose, choose_color
from .render import (DayRow, Sticker, finalize, render_day, render_focus, render_label, render_sticker,
                     render_strips, stack_with_cuts)
from .store import Store
from .providers import TaskProvider

TODAY_QUERY = "today | overdue"
MAX_ROWS = 5  # the rest are listed at the bottom (not markable): a short list is one you start
MAX_STICKERS = 30


@dataclass
class Context:
    settings: Settings
    store: Store
    tasks_factory: Callable[[], TaskProvider] | None = None  # this person's to-do app
    today: Callable[[], date] = date.today
    user: str = "cli"                 # whose tickets these are (a username)
    household: str = "home"           # their household: printers and tickets belong to it
    prefs: dict = field(default_factory=default_prefs)
    printing_paused: bool = False     # server-wide kill switch: everything becomes a dry run
    auto_print_enabled: bool = True
    printers: Printers | None = None  # None: the single .env printer (older setups, tests)
    tag_colors: dict = field(default_factory=dict)  # this person's tags: name -> label color
    wins: Callable[[], int] | None = None  # how many tasks they finished yesterday
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


def _tag(task: dict, today: date, gentle: bool = False) -> str:
    due = task.get("due") or {}
    bits = []
    if due.get("date"):
        late = (today - date.fromisoformat(due["date"][:10])).days
        if late > 0:
            bits.append(f"{'waiting' if gentle else 'overdue'} {late}d")
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


def _is_late(task: dict, today: date) -> bool:
    due = (task.get("due") or {}).get("date")
    return bool(due) and due[:10] < today.isoformat()


NO_PRINTER = "Your household has no printer yet, so this is a preview. An admin can add one under Admin → Printers."


def _available(ctx: Context) -> list[PrinterConfig]:
    """This household's printers. The .env printer only ever serves the original Home household,
    so someone in another household can never print on it by accident."""
    if ctx.printers is None:
        return [legacy_config(ctx.settings)]
    return ctx.printers.all(ctx.household)


def _fallback(ctx: Context) -> PrinterConfig | None:
    from .auth import DEFAULT_HOUSEHOLD
    return legacy_config(ctx.settings) if ctx.household == DEFAULT_HOUSEHOLD else None


def _target(ctx: Context, reason: str) -> tuple[PrinterConfig | None, str]:
    """Which printer this ticket goes to, from the person's label-color rules."""
    cfg, note = choose(_available(ctx), ctx.prefs, reason)
    return (cfg or _fallback(ctx)), note


def _finish(ctx: Context, *, reason: str, kind: str, title: str, draw: Callable[[bool], Image.Image | list],
            label_id: str, manifest: list[dict], source: str, dry_run: bool | None, text: str = "",
            color: str | None = None) -> dict:
    """Fit, print and record a ticket. `draw` may return several images (stickers): each is
    printed and cut on its own, and the record keeps one preview of them all."""
    dry = ctx.is_dry(dry_run)
    if color is None:
        cfg, note = _target(ctx, reason)
    else:
        found, note = choose_color(_available(ctx), ctx.prefs, color)
        cfg = found or _fallback(ctx)
    if cfg is None:  # nothing this person may print on: draw it at the usual size, as a preview
        cfg, note, dry = legacy_config(ctx.settings), NO_PRINTER, True
    red = cfg.ink == "black_red"
    drawn = draw(red)
    parts = [finalize(i, cfg.width_px, red=red) for i in (drawn if isinstance(drawn, list) else [drawn])]
    png = ctx.store.png_path(label_id)
    sent = False
    for part in parts:
        sent = printer.print_image(part, ctx.settings, dry_run=dry, cfg=cfg)
    img = parts[0] if len(parts) == 1 else stack_with_cuts([p.convert("RGB") for p in parts])
    record = {
        "id": label_id,
        "kind": kind,
        "reason": reason,
        "title": title,
        "source": source,
        "dry_run": not sent,
        "by": ctx.user,
        "household_id": ctx.household,
        "printer": {"id": cfg.id, "name": cfg.name, "color": cfg.stock_color},
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "png": str(png),
        "manifest": manifest,
    }
    if color is not None:
        record["color"] = color
    if note:
        record["note"] = note
    if text:
        record["text"] = text
    ctx.store.save_printed(record, png=img)
    return {"status": "printed" if sent else "dry_run", **record}


def _task_label(ctx: Context, *, kind: str, title: str, subtitle: str, tasks: list[dict],
                source: str, dry_run: bool | None, reason: str) -> dict:
    today = ctx.today()
    tasks = sorted(tasks, key=lambda t: _sort_key(t, today))
    max_rows = int(ctx.prefs.get("max_rows", MAX_ROWS))
    shown, waiting = tasks[:max_rows], tasks[max_rows:]
    h24 = bool(ctx.prefs.get("time_24h"))
    gentle = bool(ctx.prefs.get("gentle_words", True))
    label_id = ctx.store.new_id(today)
    rows, manifest = [], []
    for n, task in enumerate(shown, start=1):
        due = task.get("due") or {}
        tag, color = _first_tag(ctx, task)
        rows.append(DayRow(_clean(task["content"]), _time(task, h24), int(task.get("priority", 1)) >= 3,
                           _tag(task, today, gentle), late=_is_late(task, today), color=color if tag else "",
                           note=_next_step(task) if ctx.prefs.get("show_next_step", True) else ""))
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
    waiting_names = [_clean(t["content"]) for t in waiting] if show_waiting else None
    footer_win = ""
    if kind == "today" and ctx.prefs.get("show_wins", True) and ctx.wins is not None:
        done = ctx.wins()
        footer_win = f"Yesterday you finished {done}. ✓" if done else ""

    def draw(accent: bool) -> Image.Image:
        return render_day(title, subtitle, rows, code=label_id, footer=footer, waiting=waiting_names,
                          accent=accent, waiting_head="Not today" if gentle else "Also waiting",
                          win=footer_win)
    return _finish(ctx, reason=reason, kind=kind, title=title, draw=draw, label_id=label_id,
                   manifest=manifest, source=source, dry_run=dry_run)


def _first_tag(ctx: Context, task: dict) -> tuple[str, str]:
    """The task's first tag that has a color; else, for a late task, the overdue color rule."""
    for name in task.get("labels") or []:
        color = ctx.tag_colors.get(str(name).lower())
        if color:
            return str(name).lower(), color
    if _is_late(task, ctx.today()):
        rules = ctx.prefs.get("color_rules") or {}
        word = "waiting" if ctx.prefs.get("gentle_words", True) else "overdue"
        return word, rules.get("overdue", "red") if rules.get("overdue", "red") != "any" else "red"
    if task.get("labels"):
        return str(task["labels"][0]).lower(), "white"
    return "", "white"


def _next_step(task: dict) -> str:
    return next((s["text"] for s in task.get("steps") or [] if not s.get("done")), "")


def _when(task: dict, today: date, h24: bool, gentle: bool) -> str:
    due = task.get("due") or {}
    if not due.get("date"):
        return ""
    day = date.fromisoformat(due["date"][:10])
    late = (today - day).days
    if late > 0:
        return f"{'waiting' if gentle else 'overdue'} {late}d"
    words = "today" if late == 0 else "tomorrow" if late == -1 else day.strftime("%a %b %-d")
    t = _time(task, h24)
    return f"{words} {t}".strip()


def _sticker_rows(ctx: Context, tasks: list[dict]) -> tuple[list[Sticker], list[dict]]:
    today = ctx.today()
    h24, gentle = bool(ctx.prefs.get("time_24h")), bool(ctx.prefs.get("gentle_words", True))
    stickers, manifest = [], []
    for n, task in enumerate(tasks, start=1):
        tag, color = _first_tag(ctx, task)
        due = task.get("due") or {}
        stickers.append(Sticker(n, _clean(task["content"]), tag, color, _when(task, today, h24, gentle),
                                task.get("minutes"), _next_step(task), int(task.get("priority", 1)) >= 3))
        manifest.append({"row": n, "task_id": str(task["id"]), "content": task["content"],
                         "due_date": (due.get("date") or "")[:10] or None,
                         "is_recurring": bool(due.get("is_recurring"))})
    return stickers, manifest


def _pick(ctx: Context, task_ids: list[str] | None, query: str | None) -> list[dict]:
    if task_ids:
        if len(task_ids) > MAX_STICKERS:
            raise ValueError(f"{MAX_STICKERS} at a time, so the printer isn't tied up")
        return [ctx.tasks.get_task(str(t)) for t in dict.fromkeys(task_ids)]
    today = ctx.today()
    tasks = ctx.tasks.filter_tasks(query) if query else ctx.tasks.today()
    return sorted(tasks, key=lambda t: _sort_key(t, today))[:MAX_STICKERS]


def print_stickers(ctx: Context, task_ids: list[str] | None = None, query: str | None = None,
                   dry_run: bool | None = None, source: str = "manual") -> dict:
    """One sticker per task, cut apart. Stickers go to the printer loaded with their tag's color
    (one ticket per color); on plain labels the tag's pattern shows the color."""
    tasks = _pick(ctx, task_ids, query)
    if not tasks:
        raise ValueError("No tasks to print. Pick some on My list, or add a few first.")
    stickers, manifest = _sticker_rows(ctx, tasks)
    groups: dict[str, list[int]] = {}
    available = _available(ctx)
    for i, s in enumerate(stickers):
        cfg, _ = choose_color(available, ctx.prefs, s.color)
        groups.setdefault(cfg.id if cfg else "", []).append(i)
    results = []
    for idx in groups.values():
        label_id = ctx.store.new_id(ctx.today())
        part = [Sticker(**{**stickers[i].__dict__, "number": n}) for n, i in enumerate(idx, start=1)]
        rows = [{**manifest[i], "row": n} for n, i in enumerate(idx, start=1)]
        color = part[0].color if len({s.color for s in part}) == 1 else "white"

        def draw(accent: bool, part=part, label_id=label_id) -> list[Image.Image]:
            return [render_sticker(s, label_id) for s in part]
        title = f"{len(part)} sticker{'s' * (len(part) != 1)}"
        results.append(_finish(ctx, reason="task", kind="stickers", title=title, draw=draw, label_id=label_id,
                               manifest=rows, source=source, dry_run=dry_run, color=color))
    return {**results[0], "also": results[1:]} if len(results) > 1 else results[0]


def print_strips(ctx: Context, task_ids: list[str] | None = None, query: str | None = None,
                 dry_run: bool | None = None, source: str = "manual") -> dict:
    """One label of tear-off strips: tear along the dashes and stick each task where it happens."""
    tasks = _pick(ctx, task_ids, query)
    stickers, manifest = _sticker_rows(ctx, tasks)
    today = ctx.today()
    label_id = ctx.store.new_id(today)

    def draw(accent: bool) -> Image.Image:
        return render_strips("Tear-off", today.strftime("%a %b %-d"), stickers, label_id)
    return _finish(ctx, reason="strips", kind="strips", title="Tear-off strips", draw=draw, label_id=label_id,
                   manifest=manifest, source=source, dry_run=dry_run)


def one_thing(ctx: Context) -> dict | None:
    """What to do now: the first task today's ticket would list."""
    today = ctx.today()
    tasks = sorted(ctx.tasks.today(), key=lambda t: _sort_key(t, today))
    return tasks[0] if tasks else None


def print_focus(ctx: Context, task_id: str | None = None, dry_run: bool | None = None,
                source: str = "manual") -> dict:
    """Just one thing: one task, big, with its tiny steps."""
    task = ctx.tasks.get_task(task_id) if task_id else one_thing(ctx)
    if task is None:
        raise ValueError("Nothing due today. Pick a task on My list to focus on.")
    today = ctx.today()
    h24, gentle = bool(ctx.prefs.get("time_24h")), bool(ctx.prefs.get("gentle_words", True))
    tag, color = _first_tag(ctx, task)
    steps = [s["text"] for s in task.get("steps") or [] if not s.get("done")]
    minutes = task.get("minutes")
    start_by = ""
    if minutes and len(((task.get("due") or {}).get("date") or "")) >= 16:
        due = datetime.fromisoformat(task["due"]["date"][:19]) - timedelta(minutes=int(minutes))
        start_by = f"{due:%H:%M}" if h24 else f"{due.hour % 12 or 12}:{due:%M}{'a' if due.hour < 12 else 'p'}"
    label_id = ctx.store.new_id(today)
    due = task.get("due") or {}
    manifest = [{"row": 1, "task_id": str(task["id"]), "content": task["content"],
                 "due_date": (due.get("date") or "")[:10] or None, "is_recurring": bool(due.get("is_recurring"))}]

    def draw(accent: bool) -> Image.Image:
        return render_focus(_clean(task["content"]), steps, label_id, tag, color,
                            _when(task, today, h24, gentle), minutes, start_by)
    return _finish(ctx, reason="focus", kind="focus", title=task["content"], draw=draw, label_id=label_id,
                   manifest=manifest, source=source, dry_run=dry_run)


def print_today(ctx: Context, source: str = "manual", dry_run: bool | None = None) -> dict:
    """source="auto" is Home Assistant's once-a-day print. It is guarded server-side:
    the second auto call on the same day returns skipped, whatever happened to the first."""
    today = ctx.today()
    dry = ctx.is_dry(dry_run)
    if source == "auto" and not ctx.auto_print_enabled:
        return {"status": "skipped", "reason": "auto print is turned off in server settings"}
    target = _target(ctx, "today")[0] if source == "auto" and not dry else None
    if target is not None and not printer.is_reachable(target):
        # Don't use up today's print on a printer that's off; the next walk-in will print.
        return {"status": "skipped", "reason": "printer is offline; today's ticket will print next time"}
    if source == "auto" and not ctx.store.claim_auto(today.isoformat(), dry, scope=ctx.user):
        return {"status": "skipped", "reason": f"auto print already ran on {today.isoformat()}"}
    try:
        tasks = ctx.tasks.today()
    except Exception:
        if source == "auto":
            ctx.store.release_auto(today.isoformat(), dry, scope=ctx.user)  # nothing printed, so no reprint risk
        raise
    title = today.strftime("%A")
    subtitle = today.strftime("%B %-d")
    late = [t for t in tasks if _is_late(t, today)]
    if ctx.prefs.get("split_overdue") and late and len(late) < len(tasks):
        # Overdue tasks get their own ticket, so they can come out on (say) red labels.
        extra = _task_label(ctx, kind="today", title="Overdue", subtitle=subtitle, tasks=late,
                            source=source, dry_run=dry_run, reason="overdue")
        main = _task_label(ctx, kind="today", title=title, subtitle=subtitle,
                           tasks=[t for t in tasks if not _is_late(t, today)],
                           source=source, dry_run=dry_run, reason="today")
        return {**main, "also": [extra]}
    all_late = bool(late) and len(late) == len(tasks) and ctx.prefs.get("split_overdue")
    return _task_label(ctx, kind="today", title=title, subtitle=subtitle, tasks=tasks,
                       source=source, dry_run=dry_run, reason="overdue" if all_late else "today")


def print_filter(ctx: Context, query: str, title: str | None = None,
                 dry_run: bool | None = None, source: str = "manual") -> dict:
    tasks = ctx.tasks.filter_tasks(query)
    return _task_label(ctx, kind="list", title=title or query, subtitle=ctx.today().strftime("%b %-d"),
                       tasks=tasks, source=source, dry_run=dry_run, reason="list")


def print_task(ctx: Context, task_id: str, dry_run: bool | None = None, source: str = "manual",
               reason: str | None = None) -> dict:
    task = ctx.tasks.get_task(task_id)
    today = ctx.today()
    label_id = ctx.store.new_id(today)
    due = task.get("due") or {}
    late = _is_late(task, today)
    subtitle = (f"overdue · due {due['date'][:10]}" if late else f"due {due['date'][:10]}") if due.get("date") else ""
    reason = reason or ("overdue" if late else "urgent" if int(task.get("priority", 1)) >= 3 else "task")
    manifest = [{"row": 1, "task_id": str(task["id"]), "content": task["content"],
                 "due_date": (due.get("date") or "")[:10] or None,
                 "is_recurring": bool(due.get("is_recurring"))}]

    def draw(accent: bool) -> Image.Image:
        return render_label(_clean(task["content"]), subtitle, body=task.get("description", ""),
                            code=label_id, accent_subtitle=accent and late)
    return _finish(ctx, reason=reason, kind="task", title=task["content"], draw=draw, label_id=label_id,
                   manifest=manifest, source=source, dry_run=dry_run)


def print_text(ctx: Context, text: str, title: str = "NOTE", dry_run: bool | None = None,
               source: str = "manual") -> dict:
    label_id = ctx.store.new_id(ctx.today())

    def draw(accent: bool) -> Image.Image:
        return render_label(title, ctx.today().strftime("%a %b %-d"), body=text, code=label_id)
    return _finish(ctx, reason="note", kind="text", title=title, draw=draw, label_id=label_id,
                   manifest=[], source=source, dry_run=dry_run, text=text)


def add_and_print(ctx: Context, text: str, dry_run: bool | None = None, source: str = "manual") -> dict:
    """Create a task in the person's to-do app (first line = title, rest = notes), print its ticket."""
    first, _, rest = text.strip().partition("\n")
    task = ctx.tasks.add_task(first.strip()[:500], description=rest.strip())
    return print_task(ctx, str(task["id"]), dry_run=dry_run, source=source, reason="new_task")


def read_clipboard() -> str:
    for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"]):
        if shutil.which(cmd[0]):
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                return out.stdout
    raise RuntimeError("no clipboard tool found (install wl-clipboard or xclip)")
