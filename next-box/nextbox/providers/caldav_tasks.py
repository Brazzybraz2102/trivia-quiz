"""CalDAV task lists (VTODO): Nextcloud Tasks, Fastmail, Synology, Zoho, Radicale, DAVx⁵ and
older iCloud Reminders lists. Each person connects with their server address and an app password.
"""
from __future__ import annotations

import datetime as dt

from .base import ProviderError, TaskProvider


def _priority(value) -> int:
    """iCalendar 1 (highest)..9 (lowest), 0 = none  ->  Todoist-style 4 (urgent)..1 (none)."""
    try:
        p = int(value or 0)
    except (TypeError, ValueError):
        return 1
    if p == 0:
        return 1
    return 4 if p <= 3 else 3 if p <= 5 else 2


def _local(value) -> dt.date | dt.datetime:
    if isinstance(value, dt.datetime) and value.tzinfo:
        return value.astimezone().replace(tzinfo=None)
    return value


def _due_string(value) -> str | None:
    if value is None:
        return None
    value = _local(value)
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return value.isoformat()


def _next_occurrence(component, due):
    """Next due after `due` per the task's RRULE, keeping date vs date-time."""
    from dateutil.rrule import rrulestr

    rule = component.get("rrule")
    if not rule:
        return None
    start = component.get("dtstart").dt if component.get("dtstart") else due
    is_date = not isinstance(due, dt.datetime)
    as_dt = (lambda d: dt.datetime.combine(d, dt.time())) if is_date else (lambda d: d)
    rr = rrulestr(rule.to_ical().decode(), dtstart=as_dt(start))
    nxt = rr.after(as_dt(due))
    if nxt is None:
        return None
    return nxt.date() if is_date else nxt


def _categories(component) -> list[str]:
    """CATEGORIES may appear once or several times, each holding one or more names."""
    raw = component.get("categories")
    if raw is None:
        return []
    out = []
    for item in raw if isinstance(raw, list) else [raw]:
        cats = getattr(item, "cats", None)
        out += [str(c) for c in cats] if cats is not None else [s.strip() for s in str(item).split(",")]
    return [c for c in out if c]


class CalDAVTasks(TaskProvider):
    key = "caldav"

    def __init__(self, url: str, username: str, password: str, list_name: str = "", client=None):
        if not url or not username or not password:
            raise ProviderError("CalDAV needs a server address, username and app password.")
        import caldav

        self._client = client or caldav.DAVClient(url=url, username=username, password=password,
                                                  timeout=20)
        self._list_name = list_name.strip()
        self._username = username
        self._calendars = None

    # --- lists -------------------------------------------------------------
    def _task_lists(self):
        if self._calendars is None:
            try:
                cals = self._client.principal().calendars()
            except Exception as exc:  # caldav raises many types (auth, DAV, connection)
                raise ProviderError(f"Couldn't reach the CalDAV server: {exc}") from exc
            lists = []
            for c in cals:
                try:
                    comps = c.get_supported_components()
                except Exception:
                    comps = ["VTODO"]  # some servers don't say; try anyway
                if "VTODO" in comps:
                    lists.append(c)
            self._calendars = lists
        return self._calendars

    def _selected(self):
        lists = self._task_lists()
        if self._list_name:
            lists = [c for c in lists if (c.get_display_name() or "") == self._list_name]
            if not lists:
                raise ProviderError(f"No task list called {self._list_name!r} on the server.")
        return lists

    def _find(self, task_id: str):
        for cal in self._task_lists():
            try:
                return cal.todo_by_uid(task_id)
            except Exception:
                continue
        raise ProviderError("That task isn't on the server any more.")

    # --- shape -------------------------------------------------------------
    @staticmethod
    def _to_task(todo) -> dict:
        c = todo.icalendar_component
        due = c.get("due").dt if c.get("due") else None
        return {
            "id": str(c.get("uid")),
            "content": str(c.get("summary") or "(untitled)"),
            "description": str(c.get("description") or ""),
            "priority": _priority(c.get("priority")),
            "due": {"date": _due_string(due), "is_recurring": "rrule" in c} if due else None,
            "labels": _categories(c),
        }

    def _pending(self, cals) -> list[dict]:
        out = []
        for cal in cals:
            for todo in cal.todos(include_completed=False):
                out.append(self._to_task(todo))
        return out

    # --- TaskProvider ------------------------------------------------------
    def check(self) -> dict:
        lists = self._task_lists()
        if not lists:
            raise ProviderError("Connected, but this account has no task lists.")
        self._selected()
        return {"account": self._username, "lists": sorted(c.get_display_name() or "Tasks" for c in lists)}

    def today(self) -> list[dict]:
        today = dt.date.today().isoformat()
        return [t for t in self._pending(self._selected())
                if t["due"] and t["due"]["date"][:10] <= today]

    def filter_tasks(self, query: str) -> list[dict]:
        """A list name prints that list; "all" prints every pending task."""
        q = query.strip()
        if q.lower() in {"", "all"}:
            return self._pending(self._selected())
        if q.lower() in {"today", "today | overdue"}:
            return self.today()
        lists = [c for c in self._task_lists() if (c.get_display_name() or "").lower() == q.lower()]
        if not lists:
            names = ", ".join(sorted(c.get_display_name() or "?" for c in self._task_lists()))
            raise ProviderError(f"No list called {q!r}. Your lists: {names}")
        return self._pending(lists)

    def get_task(self, task_id: str) -> dict:
        return self._to_task(self._find(task_id))

    def add_task(self, content: str, description: str = "") -> dict:
        lists = self._selected()
        todo = lists[0].save_todo(summary=content, description=description or None)
        return self._to_task(todo)

    def close_task(self, task_id: str) -> None:
        todo = self._find(task_id)
        c = todo.icalendar_component
        due = c.get("due").dt if c.get("due") else None
        nxt = _next_occurrence(c, due) if (due is not None and "rrule" in c) else None
        if nxt is None:
            todo.complete()
            return
        # Repeating: advance this task to its next occurrence instead of finishing the series.
        shift = nxt - due
        start = c.get("dtstart").dt if c.get("dtstart") else None
        del c["due"]
        c.add("due", nxt)
        if start is not None:
            del c["dtstart"]
            c.add("dtstart", start + shift)
        todo.save()

    def set_due_date(self, task_id: str, date_iso: str) -> None:
        todo = self._find(task_id)
        c = todo.icalendar_component
        if "rrule" in c:
            # Moving one occurrence needs a per-occurrence override most apps handle differently.
            raise ProviderError("Can't move one day of a repeating task in this app. Skip it, "
                                "or move it in your to-do app.")
        new = dt.date.fromisoformat(date_iso)
        old = c.get("due").dt if c.get("due") else None
        if isinstance(old, dt.datetime):
            new = dt.datetime.combine(new, old.timetz())  # keep the time of day
        todo.set_due(new, move_dtstart=True)
        todo.save()

    def delete_task(self, task_id: str) -> None:
        self._find(task_id).delete()
