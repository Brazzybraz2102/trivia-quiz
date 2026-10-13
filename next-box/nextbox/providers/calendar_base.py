"""The contract every calendar connection implements, and helpers they share."""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod


class CalendarSource(ABC):
    key = ""

    @abstractmethod
    def check(self) -> dict:
        """Prove it works. Returns {"account": label, "calendars": [names]}."""

    @abstractmethod
    def events(self, day: dt.date) -> list[dict]:
        """That day's events: {"title", "start" "HH:MM"|None, "end", "all_day", "calendar", "location"}."""


def _local(value) -> dt.datetime | dt.date:
    if isinstance(value, dt.datetime):
        return value.astimezone().replace(tzinfo=None) if value.tzinfo else value
    return value


def event_from_component(c, day: dt.date, calendar: str = "") -> dict | None:
    """One VEVENT (already expanded to a single occurrence) as it looks on `day`."""
    if str(c.get("status", "")).upper() == "CANCELLED":
        return None
    start = _local(c.get("dtstart").dt) if c.get("dtstart") else None
    if start is None:
        return None
    end = _local(c.get("dtend").dt) if c.get("dtend") else None
    title = str(c.get("summary") or "(no title)").strip()[:200]
    all_day = not isinstance(start, dt.datetime)
    starts_today = (start if all_day else start.date()) == day
    if not all_day and not starts_today:
        all_day = True  # began before today and runs into it: show it as "all day"
    return {"title": title, "all_day": all_day,
            "start": None if all_day else start.strftime("%H:%M"),
            "end": end.strftime("%H:%M") if (not all_day and isinstance(end, dt.datetime) and end.date() == day) else None,
            "calendar": calendar, "location": str(c.get("location") or "").strip()[:120]}


def sort_events(events: list[dict]) -> list[dict]:
    return sorted(events, key=lambda e: (not e["all_day"], e["start"] or "", e["title"].lower()))
