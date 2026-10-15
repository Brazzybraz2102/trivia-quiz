"""The journal, wins and slugs.

The daily routine: paste today's label on the left page of a paper journal, work the list, write
notes on the right page, then take one photo of both pages. Read-back applies the marks, reads the
handwritten notes, and keeps the photo here, so a lost journal is never a lost record.

- **Journal pages** (`journal` table): one per photo or typed note, grouped by day. Private to the
  person who wrote them; admins can't read them.
- **Wins**: what you finished each day, worked out from your list, events and read-backs.
- **Slugs**: tasks that keep sliding to another day (`task_slides` counts the moves) or have waited
  a long time. They're shown gently, with a way to make them smaller.
"""
from __future__ import annotations

import secrets
import time
from datetime import date

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.engine import Engine

from . import db

MAX_NOTES = 20_000
SLUG_MOVES = 3       # moved this many times...
SLUG_WAITING = 7     # ...or waiting this many days
MAX_SLUGS = 10


class Journal:
    """One person's journal pages."""

    def __init__(self, engine: Engine, owner: str, household: str = "home"):
        self.db, self.owner, self.household = engine, owner, household

    def _mine(self, page_id: str):
        return and_(db.journal.c.id == page_id, db.journal.c.owner == self.owner)

    @staticmethod
    def _page(row) -> dict:
        return {"id": row["id"], "day": row["day"], "created": row["created"], "updated": row["updated"],
                "notes": row["notes"], "has_photo": bool(row["has_photo"]), "scan_id": row["scan_id"],
                "label_id": row["label_id"]}

    def _cols(self):
        c = db.journal.c
        return [c.id, c.day, c.created, c.updated, c.notes, c.scan_id, c.label_id,
                (c.photo.isnot(None)).label("has_photo")]

    def add(self, day: str, notes: str = "", photo: bytes | None = None, scan_id: str | None = None,
            label_id: str | None = None) -> dict:
        date.fromisoformat(day)  # ValueError if it isn't a date
        notes = (notes or "").strip()
        if len(notes) > MAX_NOTES:
            raise ValueError(f"notes can be up to {MAX_NOTES} characters")
        if not notes and photo is None:
            raise ValueError("write something first")
        page_id, now = secrets.token_hex(6), int(time.time())
        with self.db.begin() as conn:
            conn.execute(insert(db.journal).values(
                id=page_id, owner=self.owner, household_id=self.household, day=day, created=now,
                updated=now, notes=notes, photo=photo, scan_id=scan_id, label_id=label_id))
        return self.get(page_id)

    def get(self, page_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(select(*self._cols()).where(self._mine(page_id))).mappings().first()
        return self._page(row) if row else None

    def photo(self, page_id: str) -> bytes | None:
        with self.db.connect() as conn:
            return conn.execute(select(db.journal.c.photo).where(self._mine(page_id))).scalar()

    def update(self, page_id: str, notes: str) -> dict:
        notes = (notes or "").strip()
        if len(notes) > MAX_NOTES:
            raise ValueError(f"notes can be up to {MAX_NOTES} characters")
        with self.db.begin() as conn:
            done = conn.execute(update(db.journal).where(self._mine(page_id)).values(
                notes=notes, updated=int(time.time()))).rowcount
        if not done:
            raise KeyError(page_id)
        return self.get(page_id)

    def delete(self, page_id: str) -> None:
        with self.db.begin() as conn:
            if not conn.execute(delete(db.journal).where(self._mine(page_id))).rowcount:
                raise KeyError(page_id)

    def pages(self, since: str, until: str) -> list[dict]:
        """Pages from `since` to `until` (inclusive), newest day first, without the photos."""
        c = db.journal.c
        q = (select(*self._cols()).where(and_(c.owner == self.owner, c.day >= since, c.day <= until))
             .order_by(c.day.desc(), c.created))
        with self.db.connect() as conn:
            return [self._page(r) for r in conn.execute(q).mappings()]

    def count(self) -> int:
        from sqlalchemy import func
        with self.db.connect() as conn:
            return conn.execute(select(func.count()).select_from(db.journal).where(
                db.journal.c.owner == self.owner)).scalar() or 0

    def delete_for_user(self) -> int:
        with self.db.begin() as conn:
            return conn.execute(delete(db.journal).where(db.journal.c.owner == self.owner)).rowcount


class Slides:
    """How often each of a person's tasks was moved to a later day."""

    def __init__(self, engine: Engine, owner: str):
        self.db, self.owner = engine, owner

    def _key(self, task_id: str):
        return and_(db.task_slides.c.owner == self.owner, db.task_slides.c.task_id == str(task_id)[:200])

    def bump(self, task_id: str) -> int:
        now = int(time.time())
        with self.db.begin() as conn:
            have = conn.execute(select(db.task_slides.c.moves).where(self._key(task_id))).scalar()
            if have is None:
                conn.execute(insert(db.task_slides).values(owner=self.owner, task_id=str(task_id)[:200],
                                                           moves=1, last_moved=now))
                return 1
            conn.execute(update(db.task_slides).where(self._key(task_id)).values(moves=have + 1, last_moved=now))
            return have + 1

    def clear(self, task_id: str) -> None:
        with self.db.begin() as conn:
            conn.execute(delete(db.task_slides).where(self._key(task_id)))

    def counts(self) -> dict[str, int]:
        with self.db.connect() as conn:
            return {r[0]: r[1] for r in conn.execute(select(db.task_slides.c.task_id, db.task_slides.c.moves)
                                                     .where(db.task_slides.c.owner == self.owner))}

    def delete_for_user(self) -> int:
        with self.db.begin() as conn:
            return conn.execute(delete(db.task_slides).where(db.task_slides.c.owner == self.owner)).rowcount


def waiting_days(task: dict, today: date) -> int:
    due = (task.get("due") or {}).get("date")
    return max(0, (today - date.fromisoformat(due[:10])).days) if due else 0


def slugs(tasks: list[dict], moves: dict[str, int], today: date, min_moves: int = SLUG_MOVES) -> list[dict]:
    """Tasks that keep sliding: moved `min_moves`+ times, or waiting SLUG_WAITING+ days. Most stuck first."""
    out = []
    for t in tasks:
        n, waited = moves.get(str(t["id"]), 0), waiting_days(t, today)
        if n >= min_moves or waited >= SLUG_WAITING:
            out.append({"id": str(t["id"]), "content": t["content"], "moves": n, "waiting_days": waited,
                        "has_steps": bool(t.get("steps"))})
    out.sort(key=lambda s: (-s["moves"], -s["waiting_days"]))
    return out[:MAX_SLUGS]
