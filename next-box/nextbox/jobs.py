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
from .render import (DayRow, Row, Sticker, finalize, reading_font, render_day, render_focus, render_label,
                     render_sticker, render_strips, stack_with_cuts)
from .journal import SLUG_MOVES
from .mylist import MAX_STEPS, LocalTasks, Tags, parse_line
from .store import Store
from .providers import TaskProvider

TODAY_QUERY = "today | overdue"
MAX_ROWS = 5  # the rest are listed at the bottom (not markable): a short list is one you start
LIST_MAX_ROWS = 20  # a list from a filter (groceries, packing) gets a box for everything up to this
MAX_STICKERS = 30
MAX_BODY = 1500  # characters of a note or task notes that go on a label; the rest stays in Next Box
MAX_TEXT = 4000  # longest note Next Box takes in (the web app and the API stop there too)


def _short(text: str, limit: int, tail: str = "…") -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + tail


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
    events: Callable[[date], tuple[list, list]] | None = None  # (today's calendar events, problems)
    slides: Callable[[], dict] | None = None     # task id -> times moved to a later day
    on_move: Callable[[str], None] | None = None  # a task was moved to a later day
    on_done: Callable[[str], None] | None = None  # a task was finished
    save_page: Callable[..., dict] | None = None  # keep a journal page (read-back)
    can_add_printer: bool = False     # an admin: the no-printer note says where to add one
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


def list_order(task: dict, today: date):
    """My list on screen: today and overdue in the order today's ticket prints them, then what's
    coming up by day and time, then tasks with no day (urgent first). Not _sort_key alone for
    later days: its days_late would put the furthest date first."""
    due = task.get("due") or {}
    day = (due.get("date") or "")[:10]
    tid = str(task.get("id", ""))
    num = int(tid[3:]) if tid.startswith("nb:") and tid[3:].isdigit() else 0
    pri = -int(task.get("priority", 1) or 1)
    if day and day <= today.isoformat():
        return (0, _sort_key(task, today), num)
    if day:
        timed = len(due.get("datetime") or due.get("date") or "") >= 16
        return (1, (day, _time_minutes(task) if timed else 24 * 60, pri, num))
    return (2, (pri, num))


def _time_minutes(task: dict) -> int:
    due = task.get("due") or {}
    raw = due.get("datetime") or due.get("date") or ""
    return int(raw[11:13]) * 60 + int(raw[14:16]) if len(raw) >= 16 else 0


def _is_late(task: dict, today: date) -> bool:
    due = (task.get("due") or {}).get("date")
    return bool(due) and due[:10] < today.isoformat()


def _made(task: dict) -> dict:
    """A My list task's row on a ticket keeps when the task was made, so read-back can tell it from a
    newer task that got the id of a deleted one (scan._not_on_ticket)."""
    return {"created": task["created"]} if str(task.get("id")).startswith("nb:") and task.get("created") else {}


NO_PRINTER = "No label printer in your household yet. Tap Print on paper to print it on any printer."
NO_PRINTER_ADMIN = ("No label printer in your household yet. Add one under Admin → Printers, "
                    "or tap Print on paper to print it on any printer.")
# Next Box runs on one computer, so it can only reach printers on that home's network. A household
# other than Home with no printer of its own prints from its own device for now (SPEC §19). The web
# app shows the same words (app.js PRINT_AWAY); change both when remote printing arrives.
PRINT_AWAY = ("In this test version, labels print straight to a printer only at the Next Box computer's home. "
              "To print yours, tap Print on paper, or Save label image and open it in your label printer's app "
              "(like Brother iPrint&Label).")


def prints_here(household: str, printers: list) -> bool:
    """Can this household's labels go straight to a label printer? Home always can (its messages stay
    as they are); another household only once it has a printer of its own (one living at that house)."""
    from .auth import DEFAULT_HOUSEHOLD
    return household == DEFAULT_HOUSEHOLD or bool(printers)


def _no_printer_note(ctx: Context) -> str:
    """What a ticket says when no printer could take it (so the household has none at all)."""
    if not prints_here(ctx.household, []):
        return PRINT_AWAY
    return NO_PRINTER_ADMIN if ctx.can_add_printer else NO_PRINTER
# Always preview only, as the setup questions set it (prefs.print_with): say what to do next.
PREVIEW_NOTES = {"paper": "You print on paper: tap Print on paper to print this on any printer.",
                 "none": "No printer yet, so this is a preview. Once you have one, Print on paper works with any printer."}


def _available(ctx: Context) -> list[PrinterConfig]:
    """This household's printers. The .env printer only ever serves the original Home household,
    so someone in another household can never print on it by accident."""
    if ctx.printers is None:
        fallback = _fallback(ctx)
        return [fallback] if fallback else []
    return ctx.printers.all(ctx.household)


def _fallback(ctx: Context) -> PrinterConfig | None:
    """The .env printer, only for callers with no printer registry (the registry already holds it,
    so a printer an admin removed stays removed), only for Home, and only if it has an address."""
    from .auth import DEFAULT_HOUSEHOLD
    if ctx.printers is not None or not ctx.settings.printer_ip or ctx.household != DEFAULT_HOUSEHOLD:
        return None
    return legacy_config(ctx.settings)


def _target(ctx: Context, reason: str) -> tuple[PrinterConfig | None, str]:
    """Which printer this ticket goes to, from the person's label-color rules."""
    cfg, note = choose(_available(ctx), ctx.prefs, reason)
    return (cfg or _fallback(ctx)), note


def _finish(ctx: Context, *, reason: str, kind: str, title: str, draw: Callable[[bool], Image.Image | list],
            label_id: str, manifest: list[dict], source: str, dry_run: bool | None, text: str = "",
            color: str | None = None) -> dict:
    """Fit, print and record a ticket. `draw` may return several images (stickers): each is
    printed and cut on its own, and the record keeps one preview of them all.

    The record is saved before anything is sent, so every #code that comes out of the printer can
    be read back. If the printer doesn't answer, the result says so (status "failed") with the
    preview, so Print on paper still works. It's never retried automatically."""
    dry = ctx.is_dry(dry_run)
    if color is None:
        cfg, note = _target(ctx, reason)
    else:
        found, note = choose_color(_available(ctx), ctx.prefs, color)
        cfg = found or _fallback(ctx)
    no_printer = cfg is None
    if no_printer:  # nothing this person may print on: draw it at the usual size, as a preview
        cfg, note, dry = legacy_config(ctx.settings), _no_printer_note(ctx), True
    elif dry and not dry_run and not ctx.settings.dry_run:  # they asked for paper: say why it's a preview
        if ctx.printing_paused:
            note = "Printing is paused by an admin, so this is a preview."
        elif ctx.prefs.get("always_dry_run"):
            note = PREVIEW_NOTES.get(ctx.prefs.get("print_with"),
                                     "Always preview only is on in Settings, so this is a preview.")
    red = cfg.ink == "black_red"
    with reading_font(bool(ctx.prefs.get("reading_mode"))):  # Easy-to-read text: the same font on the label
        drawn = draw(red)
    parts = [finalize(i, cfg.width_px, red=red) for i in (drawn if isinstance(drawn, list) else [drawn])]
    for part in parts:  # a ticket split over die-cut labels prints its #code on every label
        printer.with_code(part, label_id)
    shown = [page for p in parts for page in printer.label_pages(p, cfg)]  # die-cut rolls: the real labels
    img = shown[0] if len(shown) == 1 else stack_with_cuts([p.convert("RGB") for p in shown])
    record = {
        "id": label_id,
        "kind": kind,
        "reason": reason,
        "title": title,
        "source": source,
        "dry_run": dry,
        "by": ctx.user,
        "household_id": ctx.household,
        "printer": None if no_printer else {"id": cfg.id, "name": cfg.name, "color": cfg.stock_color},
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "manifest": manifest,
    }
    if color is not None:
        record["color"] = color
    linked_error = getattr(ctx._tasks, "linked_error", "") if ctx._tasks is not None else ""
    if linked_error:  # the linked app was down: this has My list only
        note = f"{note} {linked_error}".strip()
    if note:
        record["note"] = note
    if text:
        record["text"] = text
    # Stickers keep their separate images (even just one), so Print on paper can lay them out real size.
    keep = drawn if isinstance(drawn, list) else None
    ctx.store.save_printed(record, png=img, parts=keep)
    sent_parts = 0
    try:
        for part in parts:
            if printer.print_image(part, ctx.settings, dry_run=dry, cfg=cfg):
                sent_parts += 1
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:  # off, unplugged, out of reach
        failed = {**record, "dry_run": sent_parts == 0, "failed": True, "sent_parts": sent_parts,
                  "error": type(exc).__name__,  # the kind of problem only: messages can hold addresses
                  "note": (f"{cfg.name} didn't answer"
                           + (f" after {sent_parts} of {len(parts)} stickers" if sent_parts else "")
                           + ". Is it on? Tap Print on paper instead." + printer.trouble_hint(cfg))}
        ctx.store.save_printed(failed, png=img, parts=keep)
        return {"status": "failed", **failed}
    if record["dry_run"] == bool(sent_parts):  # the printer module had the last word on dry runs
        record["dry_run"] = not sent_parts
        ctx.store.save_printed(record, png=img, parts=keep)
    return {"status": "printed" if sent_parts else "dry_run", **record}


MAX_EVENTS = 8


def _schedule(ctx: Context) -> tuple[list[tuple[str, str]], str]:
    """Today's calendar for the top of the daily ticket: [(time, title)], plus a note if a calendar failed."""
    if ctx.events is None or not ctx.prefs.get("show_calendar", True):
        return [], ""
    try:
        found, problems = ctx.events(ctx.today())
    except Exception as exc:  # the to-do part still prints
        return [], f"Calendar didn't work: {str(exc)[:120]}"
    h24 = bool(ctx.prefs.get("time_24h"))

    def clock(hhmm: str) -> str:
        h, m = int(hhmm[:2]), hhmm[3:5]
        return f"{h:02d}:{m}" if h24 else f"{h % 12 or 12}:{m}{'a' if h < 12 else 'p'}"
    lines = [("all day" if e["all_day"] else clock(e["start"]), _clean(e["title"])) for e in found[:MAX_EVENTS]]
    if len(found) > MAX_EVENTS:
        lines.append(("", f"+{len(found) - MAX_EVENTS} more"))
    return lines, "; ".join(problems)[:300]


def _task_label(ctx: Context, *, kind: str, title: str, subtitle: str, tasks: list[dict],
                source: str, dry_run: bool | None, reason: str,
                schedule: list[tuple[str, str]] | None = None, calendar_note: str = "",
                max_rows: int | None = None, extras: bool = True) -> dict:
    """extras: the wins and journal lines (only on the main daily ticket, not its overdue half)."""
    today = ctx.today()
    tasks = sorted(tasks, key=lambda t: _sort_key(t, today))
    max_rows = max_rows or int(ctx.prefs.get("max_rows", MAX_ROWS))
    shown, waiting = tasks[:max_rows], tasks[max_rows:]
    h24 = bool(ctx.prefs.get("time_24h"))
    gentle = bool(ctx.prefs.get("gentle_words", True))
    label_id = ctx.store.new_id(today)
    rows, manifest = [], []
    moves = ctx.slides() if ctx.slides else {}
    slug_after = int(ctx.prefs.get("slug_after", SLUG_MOVES))
    for n, task in enumerate(shown, start=1):
        due = task.get("due") or {}
        tag, color = _first_tag(ctx, task)
        state = _tag(task, today, gentle)
        day = (due.get("date") or "")[:10]
        if day > today.isoformat():  # not due today: its day goes with its time, so "3:00p" never reads as today
            state = f"{state} {_day_words(date.fromisoformat(day), today)} {_time(task, h24)}".strip()
        slid = moves.get(str(task["id"]), 0)
        if slid >= slug_after:  # it keeps sliding: say so kindly, so it can be made smaller
            state = f"{state} · slid {slid}×" if state else f"slid {slid}×"
        rows.append(DayRow(_clean(task["content"]), _time(task, h24) if day == today.isoformat() else "",
                           int(task.get("priority", 1)) >= 3,
                           state, late=_is_late(task, today), color=color if tag else "",
                           note=_next_step(task) if ctx.prefs.get("show_next_step", True) else ""))
        manifest.append({
            "row": n,
            "task_id": str(task["id"]),
            "content": task["content"],
            "due_date": (due.get("date") or "")[:10] or None,
            "is_recurring": bool(due.get("is_recurring")),
            **_made(task),
        })
    now = datetime.now()
    made = f"{now:%a} {now:%H:%M}" if h24 else f"{now:%a} {now.hour % 12 or 12}:{now:%M}{'a' if now.hour < 12 else 'p'}"
    footer = f"{kind}  {today.isoformat()}  made {made}"
    show_waiting = ctx.prefs.get("show_waiting", True)

    def waiting_name(t: dict) -> str:
        """Short, so one long name can't hide the rest; a slug keeps its "slid N×" even down here."""
        slid = moves.get(str(t["id"]), 0)
        return _short(_clean(t["content"]), 28) + (f" (slid {slid}×)" if slid >= slug_after else "")
    waiting_names = [waiting_name(t) for t in waiting] if show_waiting else None
    footer_win = ""
    if kind == "today" and extras and ctx.prefs.get("show_wins", True) and ctx.wins is not None:
        done = ctx.wins()
        footer_win = f"Yesterday you finished {done}. ✓" if done else ""
    head = "More" if kind == "list" else "Not today" if gentle else "Also waiting"

    def draw(accent: bool) -> Image.Image:
        return render_day(title, subtitle, rows, code=label_id, footer=footer, waiting=waiting_names,
                          accent=accent, waiting_head=head,
                          win=footer_win, schedule=schedule,
                          journal=kind == "today" and extras and bool(ctx.prefs.get("journal_mode", True)))
    result = _finish(ctx, reason=reason, kind=kind, title=title, draw=draw, label_id=label_id,
                     manifest=manifest, source=source, dry_run=dry_run)
    if calendar_note:
        result["note"] = (result.get("note", "") + " " + calendar_note).strip()
    return result


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


def _day_words(day: date, today: date) -> str:
    """A later day, briefly: "tomorrow", "Sat" this week, "Oct 24" after that."""
    ahead = (day - today).days
    return "tomorrow" if ahead == 1 else day.strftime("%a") if ahead < 7 else day.strftime("%b %-d")


def _when(task: dict, today: date, h24: bool, gentle: bool) -> str:
    due = task.get("due") or {}
    if not due.get("date"):
        return ""
    day = date.fromisoformat(due["date"][:10])
    late = (today - day).days
    repeats = " ↻" if due.get("is_recurring") else ""  # as on the daily label: this one comes back
    if late > 0:
        return f"{'waiting' if gentle else 'overdue'} {late}d{repeats}"
    words = "today" if late == 0 else "tomorrow" if late == -1 else day.strftime("%a %b %-d")
    t = _time(task, h24)
    return f"{words} {t}".strip() + repeats


def _sticker_rows(ctx: Context, tasks: list[dict]) -> tuple[list[Sticker], list[dict]]:
    today = ctx.today()
    h24, gentle = bool(ctx.prefs.get("time_24h")), bool(ctx.prefs.get("gentle_words", True))
    steps_on, big = ctx.prefs.get("show_next_step", True), ctx.prefs.get("sticker_size") == "big"
    stickers, manifest = [], []
    for n, task in enumerate(tasks, start=1):
        tag, color = _first_tag(ctx, task)
        due = task.get("due") or {}
        stickers.append(Sticker(n, _clean(task["content"]), tag, color, _when(task, today, h24, gentle),
                                task.get("minutes"), _next_step(task) if steps_on else "",
                                int(task.get("priority", 1)) >= 3, big=big))
        manifest.append({"row": n, "task_id": str(task["id"]), "content": task["content"],
                         "due_date": (due.get("date") or "")[:10] or None,
                         "is_recurring": bool(due.get("is_recurring")), **_made(task)})
    return stickers, manifest


def _pick(ctx: Context, task_ids: list[str] | None, query: str | None,
          missing: list[str] | None = None) -> list[dict]:
    """The chosen tasks. Ids already removed (on another device, say) go in `missing` instead of
    failing the whole batch; when every one is gone, that's TaskNotFound."""
    if task_ids:
        if len(task_ids) > MAX_STICKERS:
            raise ValueError(f"{MAX_STICKERS} at a time, so the printer isn't tied up")
        from .providers.base import TaskNotFound
        found, gone = [], []
        for t in dict.fromkeys(task_ids):
            try:
                found.append(ctx.tasks.get_task(str(t)))
            except TaskNotFound:
                gone.append(str(t))
        if gone and (missing is None or not found):
            raise TaskNotFound("That task was already removed." if len(gone) == 1
                               else "Those tasks were already removed.")
        if missing is not None:
            missing += gone
        return found
    today = ctx.today()
    tasks = ctx.tasks.filter_tasks(query) if query else ctx.tasks.today()
    return sorted(tasks, key=lambda t: _sort_key(t, today))[:MAX_STICKERS]


def print_stickers(ctx: Context, task_ids: list[str] | None = None, query: str | None = None,
                   dry_run: bool | None = None, source: str = "manual") -> dict:
    """One sticker per task, cut apart. Stickers go to the printer loaded with their tag's color
    (one ticket per color); on plain labels the tag's pattern shows the color."""
    missing: list[str] = []
    tasks = _pick(ctx, task_ids, query, missing)
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
    out = {**results[0], "also": results[1:]} if len(results) > 1 else results[0]
    return {**out, "missing": missing} if missing else out


def print_strips(ctx: Context, task_ids: list[str] | None = None, query: str | None = None,
                 dry_run: bool | None = None, source: str = "manual") -> dict:
    """One label of tear-off strips: tear along the dashes and stick each task where it happens."""
    missing: list[str] = []
    tasks = _pick(ctx, task_ids, query, missing)
    if not tasks:
        raise ValueError("No tasks to print. Pick some on My list, or add a few first.")
    stickers, manifest = _sticker_rows(ctx, tasks)
    today = ctx.today()
    label_id = ctx.store.new_id(today)

    def draw(accent: bool) -> Image.Image:
        return render_strips("Tear-off", today.strftime("%a %b %-d"), stickers, label_id)
    out = _finish(ctx, reason="strips", kind="strips", title="Tear-off strips", draw=draw, label_id=label_id,
                  manifest=manifest, source=source, dry_run=dry_run)
    return {**out, "missing": missing} if missing else out


def one_thing(ctx: Context) -> dict | None:
    """What to do now: the first task today's ticket would list."""
    today = ctx.today()
    tasks = sorted(ctx.tasks.today(), key=lambda t: _sort_key(t, today))
    return tasks[0] if tasks else None


def print_focus(ctx: Context, task_id: str | None = None, dry_run: bool | None = None,
                source: str = "manual", steps: list[str] | None = None) -> dict:
    """Just one thing: one task, big, with its tiny steps. `steps`: steps for a linked app's task,
    printed on this card only (they aren't written to the app or kept)."""
    task = ctx.tasks.get_task(task_id) if task_id else one_thing(ctx)
    if task is None:
        raise ValueError("Nothing due today. Pick a task on My list to focus on.")
    today = ctx.today()
    h24, gentle = bool(ctx.prefs.get("time_24h")), bool(ctx.prefs.get("gentle_words", True))
    tag, color = _first_tag(ctx, task)
    if steps is not None:
        steps = [" ".join(str(x).split())[:200] for x in steps if str(x).strip()][:12]
    else:
        steps = [s["text"] for s in task.get("steps") or [] if not s.get("done")]
    minutes = task.get("minutes")
    start_by = ""
    if minutes and len(((task.get("due") or {}).get("date") or "")) >= 16:
        due = datetime.fromisoformat(task["due"]["date"][:19]) - timedelta(minutes=int(minutes))
        start_by = f"{due:%H:%M}" if h24 else f"{due.hour % 12 or 12}:{due:%M}{'a' if due.hour < 12 else 'p'}"
    label_id = ctx.store.new_id(today)
    due = task.get("due") or {}
    manifest = [{"row": 1, "task_id": str(task["id"]), "content": task["content"],
                 "due_date": (due.get("date") or "")[:10] or None, "is_recurring": bool(due.get("is_recurring")),
                 **_made(task)}]

    def draw(accent: bool) -> Image.Image:
        return render_focus(_clean(task["content"]), steps, label_id, tag, color,
                            _when(task, today, h24, gentle), minutes, start_by,
                            urgent=int(task.get("priority", 1)) >= 3)
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
    schedule, cal_note = _schedule(ctx)
    late = [t for t in tasks if _is_late(t, today)]
    if ctx.prefs.get("split_overdue") and late and len(late) < len(tasks):
        # Overdue tasks get their own ticket, so they can come out on (say) red labels.
        # The wins and journal lines stay on the main ticket only.
        extra = _task_label(ctx, kind="today", title="Waiting" if ctx.prefs.get("gentle_words", True) else "Overdue",
                            subtitle=subtitle, tasks=late, source=source, dry_run=dry_run, reason="overdue",
                            extras=False)
        main = _task_label(ctx, kind="today", title=title, subtitle=subtitle,
                           tasks=[t for t in tasks if not _is_late(t, today)],
                           source=source, dry_run=dry_run, reason="today", schedule=schedule,
                           calendar_note=cal_note)
        return {**main, "also": [extra]}
    all_late = bool(late) and len(late) == len(tasks) and ctx.prefs.get("split_overdue")
    return _task_label(ctx, kind="today", title=title, subtitle=subtitle, tasks=tasks,
                       source=source, dry_run=dry_run, reason="overdue" if all_late else "today",
                       schedule=schedule, calendar_note=cal_note)


def print_filter(ctx: Context, query: str, title: str | None = None,
                 dry_run: bool | None = None, source: str = "manual") -> dict:
    tasks = ctx.tasks.filter_tasks(query)
    if not tasks:  # a typo shouldn't cost a label that says "Nothing due"
        raise ValueError(f"Nothing matches “{query.strip()}”. Try today, overdue, #tag or a word from a task."
                         if query.strip() else "Your list is empty. Add a few tasks first.")
    # A list (groceries, packing) gets a box for every item, up to LIST_MAX_ROWS.
    return _task_label(ctx, kind="list", title=(title or query).strip() or "My list",
                       subtitle=ctx.today().strftime("%b %-d"), tasks=tasks, source=source, dry_run=dry_run,
                       reason="list", max_rows=LIST_MAX_ROWS)


def print_task(ctx: Context, task_id: str, dry_run: bool | None = None, source: str = "manual",
               reason: str | None = None) -> dict:
    """One task on its own ticket, with a numbered box and the ✓ → ✗ legend so it can be read back."""
    if not str(task_id or "").strip():
        raise ValueError("Pick a task from your list first.")
    task = ctx.tasks.get_task(task_id)
    today = ctx.today()
    label_id = ctx.store.new_id(today)
    due = task.get("due") or {}
    late = _is_late(task, today)
    h24, gentle = bool(ctx.prefs.get("time_24h")), bool(ctx.prefs.get("gentle_words", True))
    subtitle = _when(task, today, h24, gentle)
    reason = reason or ("overdue" if late else "urgent" if int(task.get("priority", 1)) >= 3 else "task")
    manifest = [{"row": 1, "task_id": str(task["id"]), "content": task["content"],
                 "due_date": (due.get("date") or "")[:10] or None,
                 "is_recurring": bool(due.get("is_recurring")), **_made(task)}]
    tag = str((task.get("labels") or [""])[0]).lower()
    text = ("! " if int(task.get("priority", 1)) >= 3 else "") + _clean(task["content"])
    body = _short(_clean(task.get("description") or ""), MAX_BODY, "… (the rest is in the task)")
    steps = [s["text"] for s in task.get("steps") or [] if not s.get("done")]  # all of them (12 at most)
    if steps and ctx.prefs.get("show_next_step", True):
        more = f"\n+{len(steps) - MAX_STEPS} more in Next Box" if len(steps) > MAX_STEPS else ""
        body = (body + "\n" if body else "") + "Tiny steps:\n" + "\n".join(f"• {s}" for s in steps[:MAX_STEPS]) + more

    def draw(accent: bool) -> Image.Image:
        return render_label("NEW TASK" if reason == "new_task" else "TASK", subtitle,
                            rows=[Row(1, text, meta=tag)], body=body, code=label_id,
                            accent_subtitle=accent and late)
    return _finish(ctx, reason=reason, kind="task", title=task["content"], draw=draw, label_id=label_id,
                   manifest=manifest, source=source, dry_run=dry_run)


def print_text(ctx: Context, text: str, title: str = "NOTE", dry_run: bool | None = None,
               source: str = "manual") -> dict:
    if not text.strip():
        raise ValueError("Type something to print.")
    text, title = text.strip()[:MAX_TEXT], _short(title, 80) or "NOTE"
    label_id = ctx.store.new_id(ctx.today())
    body = _short(text, MAX_BODY, "… (cut short to fit the label)")  # a long email can't run feet of tape

    def draw(accent: bool) -> Image.Image:
        return render_label(title, ctx.today().strftime("%a %b %-d"), body=body, code=label_id)
    return _finish(ctx, reason="note", kind="text", title=title, draw=draw, label_id=label_id,
                   manifest=[], source=source, dry_run=dry_run, text=text)


def add_and_print(ctx: Context, text: str, dry_run: bool | None = None, source: str = "manual") -> dict:
    """Create a task in the person's to-do app (first line = title, rest = notes), print its ticket.
    On My list the first line understands Brain dump's shortcuts: tomorrow 3pm #call !"""
    first, _, rest = text.strip().partition("\n")
    rest = rest.strip()[:MAX_TEXT]
    if not first.strip():
        raise ValueError("Type something to print.")
    tasks = ctx.tasks
    local = tasks if isinstance(tasks, LocalTasks) else getattr(tasks, "local", None)
    to_my_list = local is not None and getattr(tasks, "new_tasks_to", "nextbox") != "linked"
    fields = parse_line(first, ctx.today()) if to_my_list else None
    if fields and fields.get("problem"):  # "2026-02-30": say so, rather than file it with no date
        raise ValueError(f"{fields['problem'][:1].upper()}{fields['problem'][1:]}. Fix it and try again.")
    if fields:
        try:
            Tags(local.db, local.owner).ensure(fields.get("tags", []))
        except ValueError:
            pass  # 30 tags already: the task keeps its #tag words, just with no color
        task = local.create({**fields, "description": rest})
    else:
        task = tasks.add_task(first.strip()[:500], description=rest)
    return print_task(ctx, str(task["id"]), dry_run=dry_run, source=source, reason="new_task")


def read_clipboard() -> str:
    for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"]):
        if shutil.which(cmd[0]):
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                return out.stdout
    raise RuntimeError("no clipboard tool found (install wl-clipboard or xclip)")
