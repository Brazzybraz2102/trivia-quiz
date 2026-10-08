"""My list: the built-in to-do list, color tags, brain-dump capture, and the merged view.

Everyone has a built-in list, so Next Box works with no other app. A linked app (Todoist, CalDAV)
is optional: its tasks show up next to the built-in ones, and each task stays in the list it came
from. Nothing is copied between them, so there's never a sync conflict or a duplicate.
"""
from __future__ import annotations

import calendar
import re
import time
from datetime import date, timedelta

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.engine import Engine

from . import db
from .providers.base import ProviderError, TaskProvider

PREFIX = "nb:"  # ids of built-in tasks; linked apps' ids are used as they are
REPEATS = ("daily", "weekdays", "weekly", "monthly")
TAG_COLORS = ("red", "orange", "yellow", "green", "blue", "pink", "purple", "white")
DEFAULT_TAGS = [("urgent", "red"), ("errand", "yellow"), ("call", "blue"), ("home", "green"),
                ("work", "purple")]
MAX_STEPS = 12


def is_local(task_id: str) -> bool:
    return str(task_id).startswith(PREFIX)


def tag_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9_-]", "", raw.strip().lstrip("#@").lower().replace(" ", "-"))[:24]
    if not name:
        raise ValueError("a tag needs a name (letters, numbers, - or _)")
    return name


def next_due(repeat: str, due: date, today: date) -> date:
    """The next occurrence after today, so a repeating task never piles up as overdue."""
    if repeat == "daily":
        return today + timedelta(days=1)
    if repeat == "weekdays":
        d = today + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d
    step_days = 7 if repeat == "weekly" else None
    d = due
    while d <= today:
        if step_days:
            d += timedelta(days=step_days)
        else:  # monthly, keeping the day of month where the month allows it
            y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
            d = date(y, m, min(due.day, calendar.monthrange(y, m)[1]))
    return d


# --- brain dump ------------------------------------------------------------------------------
_WEEKDAYS = {name: i for i, names in enumerate(
    [("mon", "monday"), ("tue", "tues", "tuesday"), ("wed", "wednesday"), ("thu", "thur", "thurs", "thursday"),
     ("fri", "friday"), ("sat", "saturday"), ("sun", "sunday")]) for name in names}


def parse_line(line: str, today: date) -> dict | None:
    """One line of a brain dump → task fields. Understands:
    #tag  !/!1 (urgent) !2 !3  today tomorrow mon..sun 2026-10-09  3pm 15:30  ~15m ~1h
    every day|weekday|week|month."""
    text = re.sub(r"^\s*(?:[-*•]|\d+[.)]|\[ ?\])\s*", "", line).strip()
    if not text:
        return None
    out: dict = {"tags": [], "priority": 1}

    def take(pattern: str, fn) -> None:
        nonlocal text
        def repl(m):
            fn(m)
            return " "
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)

    take(r"(?<!\S)every\s+(day|weekday|week|month)\b", lambda m: out.update(
        repeat={"day": "daily", "weekday": "weekdays", "week": "weekly", "month": "monthly"}[m[1].lower()]))
    take(r"(?<!\S)#([\w-]{1,24})", lambda m: out["tags"].append(tag_name(m[1])))
    take(r"(?<!\S)!([1-3])?(?!\S)", lambda m: out.update(priority={None: 4, "1": 4, "2": 3, "3": 2}[m[1]]))
    take(r"(?<!\S)~(\d{1,3})\s*(m|min|h|hr)\b", lambda m: out.update(
        minutes=int(m[1]) * (60 if m[2].lower().startswith("h") else 1)))
    take(r"(?<!\S)(\d{4}-\d{2}-\d{2})(?!\S)", lambda m: out.update(due_date=m[1]))
    take(r"(?<!\S)(today|tonight|tomorrow|tmrw)(?!\S)", lambda m: out.update(
        due_date=(today + timedelta(days=0 if m[1].lower() in ("today", "tonight") else 1)).isoformat()))

    def weekday(m):
        ahead = (_WEEKDAYS[m[1].lower()] - today.weekday()) % 7 or 7
        out["due_date"] = (today + timedelta(days=ahead)).isoformat()
    take(r"(?<!\S)(?:on\s+)?(" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")(?!\S)", weekday)

    def clock(m):
        hour, minute, ampm = int(m[1]), int(m[2] or 0), (m[3] or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if hour < 24 and minute < 60:
            out["due_time"] = f"{hour:02d}:{minute:02d}"
    take(r"(?<!\S)(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?!\S)", clock)
    take(r"(?<!\S)(?:at\s+)?([01]?\d|2[0-3]):(\d{2})()(?!\S)", clock)
    if out.get("repeat") and not out.get("due_date"):
        out["due_date"] = today.isoformat()
    if out.get("due_time") and not out.get("due_date"):
        out["due_date"] = today.isoformat()
    text = re.sub(r"\s{2,}", " ", text).strip(" ,;")
    if not text:
        return None
    out["content"] = text[:500]
    out["tags"] = list(dict.fromkeys(out["tags"]))
    return out


def parse_dump(text: str, today: date) -> list[dict]:
    return [t for t in (parse_line(line, today) for line in text.splitlines()[:200]) if t]


# --- tags ------------------------------------------------------------------------------------
class Tags:
    def __init__(self, root_or_url, owner: str):
        self.db = root_or_url if isinstance(root_or_url, Engine) else db.database(root_or_url)
        self.owner = owner

    def all(self) -> list[dict]:
        q = select(db.tags).where(db.tags.c.owner == self.owner).order_by(db.tags.c.position, db.tags.c.name)
        flag = f"_tags_seeded:{self.owner}"
        with self.db.begin() as conn:
            rows = conn.execute(q).mappings().all()
            if not rows and not conn.execute(select(db.server_settings.c.key).where(
                    db.server_settings.c.key == flag)).first():
                # First visit: a few starter tags. Seeded once, so deleting them all sticks.
                for i, (name, color) in enumerate(DEFAULT_TAGS):
                    conn.execute(insert(db.tags).values(owner=self.owner, name=name, color=color, position=i))
                conn.execute(insert(db.server_settings).values(key=flag, value=True))
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

    def remove(self, name: str) -> None:
        self.all()
        with self.db.begin() as conn:
            conn.execute(delete(db.tags).where(and_(db.tags.c.owner == self.owner, db.tags.c.name == tag_name(name))))

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
                "repeat": r["repeat"], "source": "nextbox", "done": r["done_at"] is not None}

    def _num(self, task_id: str) -> int:
        if not is_local(task_id) or not task_id[len(PREFIX):].isdigit():
            raise ProviderError("That task isn't on your list.")
        return int(task_id[len(PREFIX):])

    def _mine(self, num: int):
        return and_(db.tasks.c.id == num, db.tasks.c.owner == self.owner)

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
            raise ProviderError("That task isn't on your list any more.")
        return self._task(r)

    def add_task(self, content: str, description: str = "", **fields) -> dict:
        return self.create({"content": content, "description": description, **fields})

    def create(self, fields: dict) -> dict:
        values = self._validate({"priority": 1, "tags": [], "steps": [], **fields}, creating=True)
        with self.db.begin() as conn:
            count = conn.execute(select(func.count()).select_from(db.tasks).where(
                db.tasks.c.owner == self.owner, db.tasks.c.done_at.is_(None))).scalar()
            if count >= 2000:
                raise ProviderError("Your list has 2,000 open tasks. Finish or drop some first.")
            new_id = conn.execute(insert(db.tasks).values(
                owner=self.owner, household_id=self.household, created=int(time.time()), **values)
            ).inserted_primary_key[0]
        return self.get_task(f"{PREFIX}{new_id}")

    def update(self, task_id: str, fields: dict) -> dict:
        num = self._num(task_id)
        self.get_task(task_id)
        values = self._validate(fields, creating=False)
        if values:
            with self.db.begin() as conn:
                conn.execute(update(db.tasks).where(self._mine(num)).values(**values))
        return self.get_task(task_id)

    def close_task(self, task_id: str) -> None:
        task = self.get_task(task_id)
        num = self._num(task_id)
        with self.db.begin() as conn:
            if task["repeat"] and task["due"]:
                nxt = next_due(task["repeat"], date.fromisoformat(task["due"]["date"][:10]), self._today())
                conn.execute(update(db.tasks).where(self._mine(num)).values(
                    due_date=nxt.isoformat(), steps=[{**s, "done": False} for s in task["steps"]]))
            else:
                conn.execute(update(db.tasks).where(self._mine(num)).values(done_at=int(time.time())))
            conn.execute(insert(db.events).values(  # counts toward "done yesterday" on the next ticket
                ts=time.time(), user=self.owner, household_id=self.household, kind="activity",
                action="task_done", ok=True, detail={"source": "nextbox"}))

    def reopen(self, task_id: str) -> dict:
        with self.db.begin() as conn:
            conn.execute(update(db.tasks).where(self._mine(self._num(task_id))).values(done_at=None))
        return self.get_task(task_id)

    def set_due_date(self, task_id: str, date_iso: str) -> None:
        self.update(task_id, {"due_date": date_iso})

    def delete_task(self, task_id: str) -> None:
        num = self._num(task_id)
        self.get_task(task_id)
        with self.db.begin() as conn:
            conn.execute(delete(db.tasks).where(self._mine(num)))

    def delete_for_user(self) -> int:
        with self.db.begin() as conn:
            return conn.execute(delete(db.tasks).where(db.tasks.c.owner == self.owner)).rowcount

    # checks
    def _validate(self, f: dict, creating: bool) -> dict:
        out: dict = {}
        if "content" in f or creating:
            content = str(f.get("content") or "").strip()
            if not content:
                raise ValueError("the task needs some words")
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
                date.fromisoformat(d)
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
                text = str(s.get("text", "")).strip()[:200]
                if text:
                    steps.append({"text": text, "done": bool(s.get("done"))})
            out["steps"] = steps
        if "minutes" in f:
            m = f["minutes"]
            out["minutes"] = None if m in (None, "", 0) else max(1, min(int(m), 24 * 60))
        if out.get("repeat") and not (out.get("due_date") or f.get("due_date")):
            if creating:
                out["due_date"] = self._today().isoformat()
        return out


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
        if self.linked is None:
            raise ProviderError("That task is in a linked app that isn't connected any more.")
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

    def today(self) -> list[dict]:
        out = self.local.today()
        if self.linked is not None:
            out += self._mark(self.linked.today(), self._linked_key())
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

    def close_task(self, task_id: str) -> None:
        self._route(task_id).close_task(task_id)

    def set_due_date(self, task_id: str, date_iso: str) -> None:
        self._route(task_id).set_due_date(task_id, date_iso)

    def delete_task(self, task_id: str) -> None:
        self._route(task_id).delete_task(task_id)
