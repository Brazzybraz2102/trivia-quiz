"""My list: the built-in to-do list, color tags, brain-dump capture, and the merged view.

Everyone has a built-in list, so Next Box works with no other app. A linked app (Todoist, CalDAV)
is optional: its tasks show up next to the built-in ones, and each task stays in the list it came
from. Nothing is copied between them, so there's never a sync conflict or a duplicate.
"""
from __future__ import annotations

import calendar
import re
import time
from contextlib import contextmanager
from datetime import date, timedelta

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from . import db
from .providers.base import NotConnected, ProviderError, TaskNotFound, TaskProvider

PREFIX = "nb:"  # ids of built-in tasks; linked apps' ids are used as they are
REPEATS = ("daily", "weekdays", "weekly", "monthly")
TAG_COLORS = ("red", "orange", "yellow", "green", "blue", "pink", "purple", "white")
DEFAULT_TAGS = [("urgent", "red"), ("errand", "yellow"), ("call", "blue"), ("home", "green"),
                ("work", "purple")]
MAX_STEPS = 12
GONE = "That task was already removed."  # the web app reloads the list when it sees this
STEP_CHANGED = "That task's steps changed on another device. Here they are now."


class StepChanged(ValueError):
    """The step isn't where the screen thought it was: edited on another device a moment ago."""


def _iso_day(value: str) -> str:
    """YYYY-MM-DD only (Python also reads 20261016 and 2026-W42-5, which then sort wrongly)."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date looks like 2026-10-16")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("date looks like 2026-10-16") from None


def is_local(task_id: str) -> bool:
    return str(task_id).startswith(PREFIX)


def tag_name(raw: str) -> str:
    """Lowercase letters (any language), numbers, - and _, at most 24: #Café → café."""
    name = re.sub(r"[^\w-]", "", raw.strip().lstrip("#@").lower().replace(" ", "-"))[:24]
    if not name:
        raise ValueError("a tag needs a name (letters, numbers, - or _)")
    return name


def _roll_to_weekday(d: date) -> date:
    """A weekday task never starts on a Saturday or Sunday: it moves to Monday."""
    return d + timedelta(days=(7 - d.weekday()) % 7) if d.weekday() >= 5 else d


def repeat_anchor(repeat: str | None, due_date: str | None) -> int | None:
    """The day a repeat belongs on, kept in tasks.repeat_day: the day of the month for a monthly
    repeat (the 31st stays the 31st after a short month), the weekday (0 = Monday) for a weekly one."""
    if not due_date or repeat not in ("weekly", "monthly"):
        return None
    d = date.fromisoformat(due_date[:10])
    return d.day if repeat == "monthly" else d.weekday()


def next_due(repeat: str, due: date, today: date, anchor_day: int | None = None) -> date:
    """The next occurrence after the current one and after today, so ticking a repeat early moves
    it on, and one that was missed never piles up as overdue. `anchor_day` (repeat_anchor): the
    day a weekly or monthly repeat belongs on, so one moved to another day (→ Tomorrow) comes back
    to its own day: "every Friday" done on Saturday is next due on Friday, not Saturday."""
    after = max(due, today)
    if repeat == "daily":
        return after + timedelta(days=1)
    if repeat == "weekdays":
        d = after + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d
    if repeat == "weekly":
        weekday = due.weekday() if anchor_day is None else anchor_day % 7
        return after + timedelta(days=(weekday - after.weekday()) % 7 or 7)
    day = due.day if anchor_day is None else anchor_day
    y, m = after.year, after.month
    while True:  # monthly, on its day where the month has it, else the month's last day
        d = date(y, m, min(day, calendar.monthrange(y, m)[1]))
        if d > after:
            return d
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# --- brain dump ------------------------------------------------------------------------------
_WEEKDAYS = {name: i for i, names in enumerate(
    [("mon", "monday"), ("tue", "tues", "tuesday"), ("wed", "wednesday"), ("thu", "thur", "thurs", "thursday"),
     ("fri", "friday"), ("sat", "saturday"), ("sun", "sunday")]) for name in names}


_DAYWORDS = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_SHORT_DAYS = {"mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun"}
_MARK = "\x00"                     # where a shortcut was taken out of the line
_START = r"(?<![^\s(\x00])"        # a shortcut starts the line, or follows a space, "(" or another shortcut
_END = r"(?=[\s,.;:!?)\x00]|$)"    # ...and ends the line, or comes before a space or punctuation
_AMPM = r"(?:\s*(am|pm|a\.m\.|p\.m\.)|(?<=:\d\d)(a|p)|(p))"  # 3pm 3 pm 3:30p 3p (3a reads as a flat number)
_RANGE_SEP = r"\s*(?:-|–|to)\s*"
MAX_DUMP_LINES = 200


def _clock(hour: str, minute: str | None, ampm: str | None) -> tuple[int, int] | None:
    h, m, ap = int(hour), int(minute or 0), (ampm or "").lower()[:1]
    if ap:
        if not 1 <= h <= 12:
            return None  # 13pm, 0am
        h = h % 12 + (12 if ap == "p" else 0)
    return (h, m) if h < 24 and m < 60 else None


def _at_end(after: str) -> bool:
    return re.fullmatch(r"[\s,.;:!?)\x00]*", after) is not None


def _next_to_shortcut(before: str, after: str) -> bool:
    return bool(re.search(r"\x00\s*$", before) or re.match(r"\s*\x00", after))


def _in_a_list(before: str, after: str) -> bool:
    """"sat and sun", "mon or tue", "move it from monday to friday": days named in the words,
    not the task's due date."""
    return bool(re.search(r"(?:\b(?:and|or|from|to|until|till|since|after|before)|&|/)\s*$", before, re.I)
                or re.match(r"\s*(?:and\b|or\b|&|/)", after, re.I))


def strip_markers(line: str) -> tuple[str, bool]:
    """A pasted list's markers off the front ("- ", "* ", "1. ", "[ ]", "- [ ] ", "[x] "), and
    whether it was ticked already ([x])."""
    text, ticked = line, False
    while m := re.match(r"\s*(?:[-*•+](?=\s)|\d{1,3}[.)](?=\s)|\[([ xX✓]?)\])\s*", text):
        ticked = ticked or bool((m[1] or "").strip())
        text = text[m.end():]
    return text.strip(), ticked


def parse_line(line: str, today: date) -> dict | None:
    """One line of a brain dump → task fields. Understands (SPEC §17):
    #tag  ! !! !1..!4  today tonight tomorrow  fri / friday (on fri, next fri)  2026-10-09
    3pm 3:30p 15:30 9am-10am  ~15m ~1h ~1.5h ~2 hours ~1h30m
    every day|weekday|week|month|monday  daily weekly monthly weekdays.
    Words that only look like dates stay words ("Buy sun hat", "Fix the chair I sat on"): a short
    day name, today, tonight and daily/weekly/monthly count only at the end of the line, after
    on/by/due/next/this, or next to a time or another shortcut. One date per line (the last).
    A date or time that can't be real ("2026-02-30", "13pm") stays in the words and
    out["problem"] says why, so a brain dump can stop before adding anything."""
    text, ticked = strip_markers(line)
    typed = text
    if not text:
        return None
    out: dict = {"tags": [], "priority": 1}
    problems: list[str] = []

    def take(pattern: str, fn) -> None:
        """Take each match out of the text, unless fn says False (leave it as words)."""
        nonlocal text
        def repl(m):
            return m[0] if fn(m) is False else f" {_MARK} "
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)

    def tag(m):
        raw = m[1]
        if not raw[0].isalpha() or not re.fullmatch(r"[\w-]+", raw):
            return False  # #123, #c++: words, not tags
        if len(raw) > 24:
            problems.append(f"#{raw[:30]} is too long for a tag (24 letters at most)")
            return False
        out["tags"].append(tag_name(raw))
    take(_START + r"#([^\s#]+?)(?=[,.;:!?)]*(?:\s|$))", tag)
    take(_START + r"!(?:!{1,2}|([1-4]))?" + _END,
         lambda m: out.update(priority={None: 4, "1": 4, "2": 3, "3": 2, "4": 1}[m[1]]))

    def duration(m):
        if m[1]:  # ~1h30m
            mins = int(m[1]) * 60 + int(m[2])
        else:
            n = float(m[3].replace(",", "."))
            mins = round(n * 60) if m[4].lower().startswith("h") else round(n)
        if mins < 1:
            return False
        out["minutes"] = min(mins, 24 * 60)
    take(_START + r"~\s?(?:(\d{1,2})\s*h(?:rs?|ours?)?\s*(\d{1,2})(?:\s*m(?:ins?|inutes?)?)?"
         r"|(\d{1,4}(?:[.,]\d{1,2})?)\s*(m|mins?|minutes?|h|hrs?|hours?))\.?" + _END, duration)

    def every(m):
        word = m[1].lower().replace(" ", "")
        if word in _WEEKDAYS:  # every monday: weekly, starting on the next monday (today if it's monday)
            out.update(repeat="weekly", due_date=(today + timedelta(
                days=(_WEEKDAYS[word] - today.weekday()) % 7)).isoformat())
        else:
            out["repeat"] = {"day": "daily", "weekday": "weekdays", "workday": "weekdays",
                             "week": "weekly", "month": "monthly"}[word]
    take(_START + r"every\s+(day|week\s?day|workday|week|month|" + _DAYWORDS + r")" + _END, every)

    def clock_range(m):
        end = _clock(m[5], m[6], m[7] or m[8] or m[9])
        start_ap = m[3] or m[4] or None
        if end is None or not (start_ap or m[7] or m[8] or m[9] or (m[2] and m[6])):
            return False  # "2-3 apples": no am/pm and no colons, so not times
        start = _clock(m[1], m[2], start_ap or m[7] or m[8] or m[9])
        if start and not start_ap and (m[7] or m[8] or m[9]) and start > end:  # 11-1pm: 11am to 1pm
            start = _clock(m[1], m[2], "am" if (m[7] or m[8] or m[9]).lower().startswith("p") else "pm")
        if start is None:
            problems.append(f"{m[0].strip()} isn't a real time")
            return False
        out["due_time"] = f"{start[0]:02d}:{start[1]:02d}"
        length = (end[0] * 60 + end[1]) - (start[0] * 60 + start[1])
        if length > 0 and "minutes" not in out:
            out["minutes"] = length
    take(_START + r"(?:(?:at|from)\s+)?(\d{1,2})(?::([0-5]\d))?(?:\s*(am|pm|a\.m\.|p\.m\.)|(a|p))?"
         + _RANGE_SEP + r"(\d{1,2})(?::([0-5]\d))?" + _AMPM + "?" + _END, clock_range)

    def clock(m):
        ampm = (m[3] or m[4] or m[5]) if len(m.groups()) > 2 else None
        hhmm = _clock(m[1], m[2], ampm)
        if hhmm is None:
            problems.append(f"{m[0].strip()} isn't a real time")
            return False
        out["due_time"] = f"{hhmm[0]:02d}:{hhmm[1]:02d}"
    take(_START + r"(?:at\s+|@\s?)?(\d{1,2})(?::([0-5]\d))?" + _AMPM + _END, clock)
    take(_START + r"(?:at\s+|@\s?)?([01]?\d|2[0-3]):([0-5]\d)" + _END, clock)

    def bare_repeat(m):
        if "repeat" in out or not (_at_end(m.string[m.end():]) or _next_to_shortcut(m.string[:m.start()], m.string[m.end():])):
            return False  # "Write the weekly report": a word
        out["repeat"] = {"daily": "daily", "weekdays": "weekdays", "weekly": "weekly", "monthly": "monthly"}[m[1].lower()]

    def pick_date() -> None:
        """The last date word that's really a date: everything else stays in the words."""
        nonlocal text
        if "due_date" in out:
            return
        chosen = None
        for m in re.finditer(_START + r"(?:(on|by|due|next|this)\s+)?(\d{4}-\d{2}-\d{2}|today|tonight|tomorrow|tmrw|"
                             + _DAYWORDS + r")" + _END, text, re.IGNORECASE):
            word, before, after = m[2].lower(), text[:m.start()], text[m.end():]
            if word[:1].isdigit():
                try:
                    date.fromisoformat(word)
                except ValueError:
                    problems.append(f"{word} isn't a real date")
                    continue
            elif word in _SHORT_DAYS or word in ("today", "tonight"):
                # often plain words ("Buy sun hat", "Tell Sam today was great"), so only taken at
                # the end, after on/by/due/next/this, next to a time, or (today) starting the line
                if not (m[1] or _at_end(after) or _next_to_shortcut(before, after)
                        or (word in ("today", "tonight") and not before.strip())):
                    continue
            if not word[:1].isdigit() and not m[1] and _in_a_list(before, after):
                continue
            chosen = m
        if chosen is None:
            return
        word, prefix = chosen[2].lower(), (chosen[1] or "").lower()
        if word[:1].isdigit():
            due = date.fromisoformat(word)
        elif word in ("today", "tonight", "tomorrow", "tmrw"):
            due = today + timedelta(days=0 if word in ("today", "tonight") else 1)
        else:
            ahead = (_WEEKDAYS[word] - today.weekday()) % 7 or 7  # a thursday's "thu" means next week
            if prefix == "next" and ahead < 7:
                ahead += 7  # "next fri" on a thursday: the friday of next week
            due = today + timedelta(days=ahead)
        out["due_date"] = due.isoformat()
        start = chosen.start(2) if prefix in ("next", "this") and word not in _WEEKDAYS else chosen.start()
        text = text[:start] + f" {_MARK} " + text[chosen.end():]  # "Do this today" keeps its "this"

    for _ in range(2):  # a date can make "daily" next to a shortcut, and the other way round
        take(_START + r"(daily|weekdays|weekly|monthly)" + _END, bare_repeat)
        pick_date()
    if (out.get("repeat") or out.get("due_time")) and not out.get("due_date"):
        out["due_date"] = today.isoformat()
    if out.get("repeat") == "weekdays":
        out["due_date"] = _roll_to_weekday(date.fromisoformat(out["due_date"])).isoformat()
    took = _MARK in text
    text = re.sub(r"\(\s*\)", " ", text.replace(_MARK, " "))  # "(3pm)"
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)               # "Call mom , 3pm" → "Call mom,"
    text = re.sub(r"\s{2,}", " ", text).strip(" ,;:" + ("." if took else ""))
    if not text:
        return None
    out["content"] = text[:500]
    if len(text) > 500:  # nothing typed is lost: the whole line goes in the task's notes
        out["description"] = typed[:4000]
    out["tags"] = list(dict.fromkeys(out["tags"]))
    if "urgent" in out["tags"]:  # the urgent tag and ! mean the same thing
        out["priority"] = max(out["priority"], 3)
    if ticked:
        out["ticked"] = True
    if problems:
        out["problem"] = "; ".join(dict.fromkeys(problems))
    return out


def split_dump(text: str, limit: int = MAX_DUMP_LINES) -> tuple[str, str]:
    """The first `limit` lines with something on them, and the rest (kept for next time, never lost)."""
    lines, seen = text.splitlines(), 0
    for i, line in enumerate(lines):
        if line.strip():
            seen += 1
            if seen > limit:
                return "\n".join(lines[:i]), "\n".join(lines[i:])
    return text, ""


def parse_dump(text: str, today: date) -> list[dict]:
    """Each line with a task on it → its fields, plus "line" (its number) and "text" (the words as
    typed, without a list marker). Only the first MAX_DUMP_LINES lines: split_dump says what's left."""
    head, _ = split_dump(text)
    return [{**t, "line": n, "text": strip_markers(line)[0][:500]}
            for n, line in enumerate(head.splitlines(), 1) if (t := parse_line(line, today))]


def preview_dump(text: str, today: date) -> dict:
    """What Brain dump would make of `text`, without adding anything: `lines` (each task as
    parse_dump makes it, problems and ticked lines included), `dropped` (lines with no task words
    left, like "fri" alone or "#tag" alone) and `more` (lines past the first MAX_DUMP_LINES)."""
    head, rest = split_dump(text)
    dropped = [{"line": n, "text": line.strip()[:120]} for n, line in enumerate(head.splitlines(), 1)
               if line.strip() and parse_line(line, today) is None]
    return {"lines": parse_dump(head, today), "dropped": dropped,
            "more": sum(1 for line in rest.splitlines() if line.strip())}


# --- tags ------------------------------------------------------------------------------------
class TagExists(ValueError):
    pass


class Tags:
    def __init__(self, root_or_url, owner: str):
        self.db = root_or_url if isinstance(root_or_url, Engine) else db.database(root_or_url)
        self.owner = owner

    def all(self) -> list[dict]:
        q = select(db.tags).where(db.tags.c.owner == self.owner).order_by(db.tags.c.position, db.tags.c.name)
        flag = f"_tags_seeded:{self.owner}"
        try:
            with self.db.begin() as conn:
                rows = conn.execute(q).mappings().all()
                if not rows and not conn.execute(select(db.server_settings.c.key).where(
                        db.server_settings.c.key == flag)).first():
                    # First visit: a few starter tags. Seeded once, so deleting them all sticks.
                    for i, (name, color) in enumerate(DEFAULT_TAGS):
                        conn.execute(insert(db.tags).values(owner=self.owner, name=name, color=color, position=i))
                    conn.execute(insert(db.server_settings).values(key=flag, value=True))
                if not rows:  # just seeded, or by another request since the first look (or all removed)
                    rows = conn.execute(q).mappings().all()
        except IntegrityError:  # another request (a second tab, Home Assistant) seeded them at the same moment
            with self.db.connect() as conn:
                rows = conn.execute(q).mappings().all()
        return [{"name": r["name"], "color": r["color"]} for r in rows]

    def colors(self) -> dict[str, str]:
        return {t["name"]: t["color"] for t in self.all()}

    def set(self, name: str, color: str) -> dict:
        name = tag_name(name)
        if color not in TAG_COLORS:
            raise ValueError(f"color must be one of {', '.join(TAG_COLORS)}")
        existing = self.all()
        with self.db.begin() as conn:
            if any(t["name"] == name for t in existing):
                conn.execute(update(db.tags).where(and_(db.tags.c.owner == self.owner, db.tags.c.name == name))
                             .values(color=color))
            else:
                if len(existing) >= 30:
                    raise ValueError("30 tags is the limit; fewer tags are easier to use anyway")
                conn.execute(insert(db.tags).values(owner=self.owner, name=name, color=color, position=len(existing)))
        return {"name": name, "color": color}

    def ensure(self, names: list[str]) -> None:
        """Make sure these tags exist (a new one starts white), e.g. after typing #tag."""
        known = {t["name"] for t in self.all()}
        for name in names:
            if tag_name(name) not in known:
                self.set(name, "white")
                known.add(tag_name(name))

    def add(self, name: str, color: str) -> dict:
        """A new tag only: an existing one keeps its color (change that with set)."""
        name = tag_name(name)
        mine = next((t for t in self.all() if t["name"] == name), None)
        if mine:
            raise TagExists(f"You already have #{name} ({mine['color']}). Change its color in the list above.")
        return self.set(name, color)

    def add_new(self, conn, names: list[str]) -> None:
        """Inside someone else's transaction: these tags, white, unless they're there already."""
        have = {r[0] for r in conn.execute(select(db.tags.c.name).where(db.tags.c.owner == self.owner))}
        for name in dict.fromkeys(tag_name(n) for n in names):
            if name not in have:
                conn.execute(insert(db.tags).values(owner=self.owner, name=name, color="white", position=len(have)))
                have.add(name)

    def usage(self) -> dict[str, int]:
        """How many open tasks carry each tag name."""
        counts: dict[str, int] = {}
        with self.db.connect() as conn:
            for (names,) in conn.execute(select(db.tasks.c.tags).where(
                    db.tasks.c.owner == self.owner, db.tasks.c.done_at.is_(None))):
                for n in set(names or []):
                    counts[n] = counts.get(n, 0) + 1
        return counts

    def remove(self, name: str, strip: bool = False) -> int:
        """Remove the tag. strip: take it off this person's tasks too, in the same go (otherwise
        they keep it as a plain, colorless word). Returns how many tasks changed."""
        self.all()
        name = tag_name(name)
        changed = 0
        with self.db.begin() as conn:
            conn.execute(delete(db.tags).where(and_(db.tags.c.owner == self.owner, db.tags.c.name == name)))
            if strip:
                for tid, names in conn.execute(select(db.tasks.c.id, db.tasks.c.tags).where(
                        db.tasks.c.owner == self.owner)).all():
                    if name in (names or []):
                        conn.execute(update(db.tasks).where(db.tasks.c.id == tid)
                                     .values(tags=[n for n in names if n != name]))
                        changed += 1
        return changed

    def delete_for_user(self) -> None:
        with self.db.begin() as conn:
            conn.execute(delete(db.tags).where(db.tags.c.owner == self.owner))
            conn.execute(delete(db.server_settings).where(db.server_settings.c.key == f"_tags_seeded:{self.owner}"))


# --- the built-in list -----------------------------------------------------------------------
class LocalTasks(TaskProvider):
    key = "nextbox"

    def __init__(self, root_or_url, owner: str, household: str = "home", today=date.today):
        self.db = root_or_url if isinstance(root_or_url, Engine) else db.database(root_or_url)
        self.owner = owner
        self.household = household
        self._today = today

    # shape
    def _task(self, r) -> dict:
        due = None
        if r["due_date"]:
            due = {"date": r["due_date"] + (f"T{r['due_time']}:00" if r["due_time"] else ""),
                   "is_recurring": bool(r["repeat"])}
        return {"id": f"{PREFIX}{r['id']}", "content": r["content"], "description": r["description"] or "",
                "priority": r["priority"], "due": due, "labels": list(r["tags"] or []),
                "steps": list(r["steps"] or []), "minutes": r["minutes"],
                "repeat": r["repeat"], "source": "nextbox", "done": r["done_at"] is not None,
                "done_at": r["done_at"], "created": r["created"]}

    def _num(self, task_id: str) -> int:
        digits = str(task_id)[len(PREFIX):]
        if not is_local(task_id) or not digits.isdigit() or len(digits) > 15:
            raise TaskNotFound(GONE)
        return int(digits)

    def _mine(self, num: int):
        return and_(db.tasks.c.id == num, db.tasks.c.owner == self.owner)

    @contextmanager
    def _locked(self, num: int):
        """Read, change and write one task as a single step (as Store.edit_scan does): two quick
        taps, or two devices, wait for each other, so neither change is lost. Yields (conn, row)."""
        with self.db.connect() as conn:
            if conn.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")  # take the write lock before reading
            q = select(db.tasks).where(self._mine(num))
            if conn.dialect.name == "postgresql":
                q = q.with_for_update()
            try:
                row = conn.execute(q).mappings().first()
                if not row:
                    raise TaskNotFound(GONE)
                yield conn, dict(row)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()

    def _open(self, *conds) -> list[dict]:
        q = select(db.tasks).where(db.tasks.c.owner == self.owner, db.tasks.c.done_at.is_(None), *conds)
        with self.db.connect() as conn:
            return [self._task(r) for r in conn.execute(q.order_by(db.tasks.c.id)).mappings()]

    # TaskProvider
    def check(self) -> dict:
        return {"account": "Next Box", "lists": ["My list"]}

    def today(self) -> list[dict]:
        return self._open(db.tasks.c.due_date <= self._today().isoformat())

    def all_open(self) -> list[dict]:
        return self._open()

    def filter_tasks(self, query: str) -> list[dict]:
        q = query.strip().lower()
        today = self._today()
        if q in ("", "all", "everything", "my list"):
            return self.all_open()
        if q in ("today | overdue", "overdue | today", "today"):
            return self.today() if q != "today" else self._open(db.tasks.c.due_date == today.isoformat())
        if q == "overdue":
            return self._open(db.tasks.c.due_date < today.isoformat())
        if q == "tomorrow":
            return self._open(db.tasks.c.due_date == (today + timedelta(days=1)).isoformat())
        if q in ("no date", "nodate", "someday"):
            return self._open(db.tasks.c.due_date.is_(None))
        if re.fullmatch(r"p[1-4]", q):
            return self._open(db.tasks.c.priority == 5 - int(q[1]))
        if q[:1] in "#@" and len(q) > 1:
            name = tag_name(q)
            return [t for t in self.all_open() if name in t["labels"]]
        return [t for t in self.all_open() if q in t["content"].lower()]

    def get_task(self, task_id: str) -> dict:
        with self.db.connect() as conn:
            r = conn.execute(select(db.tasks).where(self._mine(self._num(task_id)))).mappings().first()
        if not r:
            raise TaskNotFound(GONE)
        return self._task(r)

    def add_task(self, content: str, description: str = "", **fields) -> dict:
        return self.create({"content": content, "description": description, **fields})

    def create(self, fields: dict) -> dict:
        return self.create_many([fields])[0]

    def create_many(self, items: list[dict], new_tags: list[str] = ()) -> list[dict]:
        """All of them or none: every one is checked first, then the new tags (white) and the
        tasks go in together, so a bad line or a full list never leaves half a brain dump behind."""
        rows = [self._settle(self._validate({"priority": 1, "tags": [], "steps": [], **f}, creating=True))
                for f in items]
        now = int(time.time())
        with self.db.begin() as conn:
            count = conn.execute(select(func.count()).select_from(db.tasks).where(
                db.tasks.c.owner == self.owner, db.tasks.c.done_at.is_(None))).scalar()
            if count + len(rows) > 2000:
                raise ValueError("Your list has 2,000 open tasks. Finish or drop some first." if count >= 2000 else
                                 f"That's {len(rows)} more, and your list already has {count:,} open tasks "
                                 "(2,000 is the most). Finish or drop some first, or add fewer.")
            if new_tags:
                Tags(self.db, self.owner).add_new(conn, list(new_tags))
            ids = [conn.execute(insert(db.tasks).values(owner=self.owner, household_id=self.household,
                                                        created=now, **values)).inserted_primary_key[0]
                   for values in rows]
            found = {r["id"]: self._task(r) for r in conn.execute(
                select(db.tasks).where(db.tasks.c.id.in_(ids))).mappings()}
        return [found[i] for i in ids]

    def update(self, task_id: str, fields: dict, new_tags: list[str] = ()) -> dict:
        """Change the task (the Edit dialog): a new day here changes a repeat's day too. `new_tags`
        are made (white) in the same go, so a change that fails never leaves tags behind."""
        num = self._num(task_id)
        checked = self._validate(fields, creating=False)
        with self._locked(num) as (conn, had):  # read and write together: a step tick can't slip between
            values = self._settle(checked, had)
            if new_tags:
                Tags(self.db, self.owner).add_new(conn, list(new_tags))
            if values:
                conn.execute(update(db.tasks).where(self._mine(num)).values(**values))
        return self.get_task(task_id)

    def move(self, task_id: str, date_iso: str | None) -> dict:
        """Move this time only to another day (→ Tomorrow, a → on paper, Undo): it keeps its time, a
        weekday repeat skips the weekend, and a repeat keeps the day it belongs on, so "every Friday"
        moved to Saturday is back on Friday once it's done. (No day at all ends a repeat, as Edit does.)"""
        num = self._num(task_id)
        checked = self._validate({"due_date": date_iso}, creating=False)
        with self._locked(num) as (conn, had):
            values = self._settle(checked, had, occurrence=True)
            conn.execute(update(db.tasks).where(self._mine(num)).values(**values))
        return self.get_task(task_id)

    def _settle(self, values: dict, had: dict | None = None, occurrence: bool = False) -> dict:
        """Rules about the task as a whole, once a change is applied: a time or a repeat needs a
        day (today, as Brain dump does, when none was picked), a weekday repeat starts on a weekday,
        a weekly or monthly repeat remembers the day it belongs on (a new day or repeat sets it; moving
        one time only, `occurrence`, doesn't), and the urgent tag means urgent: taking it off takes back
        the urgency it gave (not a "most urgent" set on purpose)."""
        merged = {**(had or {}), **values}
        if (merged.get("due_time") or merged.get("repeat")) and not merged.get("due_date"):
            values["due_date"] = merged["due_date"] = self._today().isoformat()
        if merged.get("repeat") == "weekdays" and merged.get("due_date"):
            rolled = _roll_to_weekday(date.fromisoformat(merged["due_date"])).isoformat()
            if rolled != merged["due_date"]:
                values["due_date"] = merged["due_date"] = rolled

        def changed(key: str) -> bool:
            return key in values and (had is None or values[key] != had.get(key))
        if occurrence and merged.get("repeat") and merged.get("due_date"):
            if had and had.get("repeat_day") is None:  # set before 0.14.1 (weekly) or never: its day until now
                values["repeat_day"] = repeat_anchor(merged["repeat"], had.get("due_date"))
        elif changed("repeat") or changed("due_date"):
            values["repeat_day"] = repeat_anchor(merged.get("repeat"), merged.get("due_date"))
        if had and "tags" in values and "priority" not in values and "urgent" in (had.get("tags") or []) \
                and "urgent" not in (values["tags"] or []) and int(had.get("priority") or 1) == 3:
            values["priority"] = merged["priority"] = 1  # the tag made it urgent: untagging undoes that
        if ("tags" in values or "priority" in values) and "urgent" in (merged.get("tags") or []) \
                and int(merged.get("priority") or 1) < 3:
            values["priority"] = 3
        return values

    def close_task(self, task_id: str, on_day: date | None = None, seen_due: str | None = None) -> dict | None:
        """Finish it (a repeat moves to its next day) and count it as a win on `on_day` (default
        today; a photo read-back passes the ticket's day). Already finished: nothing changes and
        None comes back, so a double tap or a second read-back never counts twice. `seen_due` is
        the day the screen showed: a repeat that has moved on from it since (Done on another device,
        or a second Done at the same moment) isn't finished again. Otherwise returns what undoing it needs."""
        task = self.get_task(task_id)
        num = self._num(task_id)
        day = on_day or self._today()
        now = int(time.time())
        with self.db.begin() as conn:
            if task["done"]:
                return None
            if task["repeat"] and task["due"]:
                prev = _iso_day(seen_due[:10]) if seen_due else task["due"]["date"][:10]  # WHERE due_date == prev
                anchor = conn.execute(select(db.tasks.c.repeat_day).where(self._mine(num))).scalar()
                nxt = next_due(task["repeat"], date.fromisoformat(prev), day, anchor_day=anchor)
                changed = conn.execute(update(db.tasks).where(self._mine(num), db.tasks.c.due_date == prev).values(
                    due_date=nxt.isoformat(), steps=[{**s, "done": False} for s in task["steps"]])).rowcount
                undo = {"prev_due": prev, "prev_steps": [bool(s.get("done")) for s in task["steps"]]}
            else:
                changed = conn.execute(update(db.tasks).where(self._mine(num), db.tasks.c.done_at.is_(None))
                                       .values(done_at=now)).rowcount
                undo = {"prev_due": (task["due"] or {}).get("date")}
            if not changed:
                return None  # finished on another device a moment ago
            conn.execute(insert(db.events).values(  # ids only: admins read events
                ts=time.time(), user=self.owner, household_id=self.household, kind="activity",
                action="task_done", ok=True, detail={"source": "nextbox", "task_id": f"{PREFIX}{num}"}))
            conn.execute(insert(db.wins).values(  # counts toward "done yesterday" and the journal's wins
                owner=self.owner, household_id=self.household, day=day.isoformat(), ts=now,
                task_id=f"{PREFIX}{num}", source="nextbox", content=task["content"][:500]))
        return undo

    def reopen(self, task_id: str, prev_due: str | None = None, prev_steps: list | None = None) -> dict:
        """Not done after all. A repeat goes back to `prev_due` (and its ticked steps). Either way
        the win it counted is taken back."""
        task = self.get_task(task_id)
        num = self._num(task_id)
        values: dict = {}
        if task["done"]:
            values["done_at"] = None
        elif task["repeat"] and prev_due:
            values["due_date"] = _iso_day(str(prev_due)[:10])
            if prev_steps is not None and len(prev_steps) == len(task["steps"]):
                values["steps"] = [{**s, "done": bool(d)} for s, d in zip(task["steps"], prev_steps)]
        if not values:
            return task
        w = db.wins.c
        with self.db.begin() as conn:
            conn.execute(update(db.tasks).where(self._mine(num)).values(**values))
            newest = conn.execute(select(w.id).where(w.owner == self.owner, w.task_id == f"{PREFIX}{num}")
                                  .order_by(w.ts.desc(), w.id.desc()).limit(1)).scalar()
            if newest is not None:
                conn.execute(delete(db.wins).where(w.id == newest))
        return self.get_task(task_id)

    def tick_step(self, task_id: str, index: int, done: bool, text: str | None = None) -> dict:
        """Tick (or untick) one tiny step. `text` is the step as the screen showed it: when the steps
        changed on another device since, nothing is ticked and StepChanged says so."""
        num = self._num(task_id)
        with self._locked(num) as (conn, r):  # two quick ticks wait for each other: neither is lost
            steps = [dict(s) for s in (r["steps"] or [])]
            if r["done_at"] is not None or not 0 <= index < len(steps) \
                    or (text is not None and steps[index].get("text") != text):
                raise StepChanged(STEP_CHANGED)
            steps[index]["done"] = bool(done)
            conn.execute(update(db.tasks).where(self._mine(num)).values(steps=steps))
        return self.get_task(task_id)

    def skip(self, task_id: str) -> dict | None:
        """Skip this time: a repeating task moves on to its next day without counting as done (no
        win) and its steps start fresh. None when it had moved on already (a double tap). Otherwise
        returns what unskip needs to put it back."""
        task = self.get_task(task_id)
        if task["done"] or not task["repeat"] or not task["due"]:
            raise ValueError("Only a repeating task can be skipped. Move it to another day instead.")
        num = self._num(task_id)
        prev = task["due"]["date"][:10]
        with self.db.begin() as conn:
            anchor = conn.execute(select(db.tasks.c.repeat_day).where(self._mine(num))).scalar()
            nxt = next_due(task["repeat"], date.fromisoformat(prev), self._today(), anchor_day=anchor)
            changed = conn.execute(update(db.tasks).where(self._mine(num), db.tasks.c.due_date == prev).values(
                due_date=nxt.isoformat(), steps=[{**s, "done": False} for s in task["steps"]])).rowcount
        if not changed:
            return None
        return {"prev_due": prev, "prev_steps": [bool(s.get("done")) for s in task["steps"]],
                "next_due": nxt.isoformat()}

    def unskip(self, task_id: str, prev_due: str, prev_steps: list | None = None) -> dict:
        """Undo a skip: back to `prev_due`, with the steps it had ticked. No win to take back."""
        task = self.get_task(task_id)
        num = self._num(task_id)
        values: dict = {"due_date": _iso_day(str(prev_due)[:10])}
        if prev_steps is not None and len(prev_steps) == len(task["steps"]):
            values["steps"] = [{**s, "done": bool(d)} for s, d in zip(task["steps"], prev_steps)]
        with self.db.begin() as conn:
            conn.execute(update(db.tasks).where(self._mine(num)).values(**values))
        return self.get_task(task_id)

    def set_due_date(self, task_id: str, date_iso: str) -> None:
        self.move(task_id, date_iso)  # a read-back → or its undo: this time only, the repeat keeps its day

    def delete_task(self, task_id: str) -> None:
        num = self._num(task_id)
        self.get_task(task_id)
        with self.db.begin() as conn:
            conn.execute(delete(db.tasks).where(self._mine(num)))
            # Its slide count goes too, so nothing about it is left for a later task to inherit.
            conn.execute(delete(db.task_slides).where(db.task_slides.c.owner == self.owner,
                                                      db.task_slides.c.task_id == f"{PREFIX}{num}"))

    def delete_for_user(self) -> int:
        with self.db.begin() as conn:
            return conn.execute(delete(db.tasks).where(db.tasks.c.owner == self.owner)).rowcount

    # checks
    def _validate(self, f: dict, creating: bool) -> dict:
        out: dict = {}
        if "content" in f or creating:
            content = str(f.get("content") or "").strip()
            if not content:
                raise ValueError("Type a few words for the task first.")
            out["content"] = content[:500]
        if "description" in f:
            out["description"] = str(f["description"] or "")[:4000]
        if "priority" in f:
            p = int(f["priority"] or 1)
            if p not in (1, 2, 3, 4):
                raise ValueError("priority is 1 to 4 (4 = most urgent)")
            out["priority"] = p
        if "due_date" in f:
            d = f["due_date"] or None
            if d is not None:
                d = _iso_day(d)
            out["due_date"] = d
            if d is None:
                out["due_time"] = None
                out["repeat"] = None
        if "due_time" in f:
            t = f["due_time"] or None
            if t is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                raise ValueError("time looks like 09:30")
            out["due_time"] = t
        if "repeat" in f:
            r = f["repeat"] or None
            if r is not None and r not in REPEATS:
                raise ValueError(f"repeat is one of {', '.join(REPEATS)}")
            out["repeat"] = r
        if "tags" in f:
            out["tags"] = list(dict.fromkeys(tag_name(t) for t in (f["tags"] or [])))[:8]
        if "steps" in f:
            steps = []
            for s in (f["steps"] or [])[:MAX_STEPS]:
                s = {"text": s, "done": False} if isinstance(s, str) else s
                if not isinstance(s, dict):
                    raise ValueError("each step is a short line of text")
                text = str(s.get("text", "")).strip()[:200]
                if text:
                    steps.append({"text": text, "done": bool(s.get("done"))})
            out["steps"] = steps
        if "minutes" in f:
            m = f["minutes"]
            out["minutes"] = None if m in (None, "", 0) else max(1, min(int(m), 24 * 60))
        return out  # _settle adds what depends on the whole task (a day for a time or a repeat)


# --- built-in list + linked app ----------------------------------------------------------------
class Merged(TaskProvider):
    """The built-in list plus, optionally, a linked app. Each task acts in the list it came from."""
    key = "merged"

    def __init__(self, local: LocalTasks, linked: TaskProvider | None = None, new_tasks_to: str = "nextbox"):
        self.local = local
        self.linked = linked
        self.new_tasks_to = new_tasks_to if linked is not None else "nextbox"

    def _route(self, task_id: str) -> TaskProvider:
        if is_local(task_id):
            return self.local
        if not str(task_id or "").strip():
            raise TaskNotFound(GONE)
        if self.linked is None:
            raise NotConnected("That task is in a linked app that isn't connected any more.")
        return self.linked

    def _linked_key(self) -> str:
        return getattr(self.linked, "key", "") or "linked"

    @staticmethod
    def _mark(tasks: list[dict], source: str) -> list[dict]:
        return [{**t, "source": t.get("source") or source, "labels": list(t.get("labels") or [])} for t in tasks]

    def check(self) -> dict:
        info = self.local.check()
        if self.linked is not None:
            other = self.linked.check()
            info = {"account": other.get("account", ""), "lists": ["My list", *other.get("lists", [])]}
        return info

    linked_error = ""  # set when the linked app didn't answer and only My list was used

    def today(self) -> list[dict]:
        out = self.local.today()
        if self.linked is not None:
            try:
                out += self._mark(self.linked.today(), self._linked_key())
            except (ProviderError, OSError):
                if not out:
                    raise  # nothing of My list to show either: say what went wrong, don't print "Nothing due"
                from .providers import display_name
                name = display_name(self._linked_key()) if self._linked_key() != "linked" else "Your linked app"
                self.linked_error = f"{name} didn't answer, so this has My list only."
        return out

    def filter_tasks(self, query: str) -> list[dict]:
        out = self.local.filter_tasks(query)
        if self.linked is not None:
            try:
                out += self._mark(self.linked.filter_tasks(query), self._linked_key())
            except ProviderError:
                if not out:
                    raise  # the query only made sense to the linked app, and it said no
        return out

    def get_task(self, task_id: str) -> dict:
        task = self._route(task_id).get_task(task_id)
        return task if is_local(task_id) else self._mark([task], self._linked_key())[0]

    def add_task(self, content: str, description: str = "") -> dict:
        if self.new_tasks_to == "linked":
            return self.linked.add_task(content, description=description)
        return self.local.add_task(content, description=description)

    def close_task(self, task_id: str, on_day: date | None = None, seen_due: str | None = None):
        if is_local(task_id):
            return self.local.close_task(task_id, on_day=on_day, seen_due=seen_due)
        return self._route(task_id).close_task(task_id)

    def reopen_task(self, task_id: str, prev_due: str | None = None, prev_steps: list | None = None):
        """Undo a finish. Linked apps without a reopen raise NotImplementedError."""
        if is_local(task_id):
            return self.local.reopen(task_id, prev_due=prev_due, prev_steps=prev_steps)
        reopen = getattr(self._route(task_id), "reopen_task", None)
        if reopen is None:
            raise NotImplementedError
        return reopen(task_id)

    def linked_name(self) -> str:
        from .providers import display_name
        return display_name(self._linked_key()) if self.linked is not None else "your linked app"

    def set_due_date(self, task_id: str, date_iso: str) -> None:
        self._route(task_id).set_due_date(task_id, date_iso)

    def delete_task(self, task_id: str) -> None:
        self._route(task_id).delete_task(task_id)
