"""The journal, wins and slugs.

The daily routine: paste today's label on the left page of a paper journal, work the list, write
notes on the right page, then take one photo of both pages. Read-back applies the marks, reads the
handwritten notes, and keeps the photo here, so a lost journal is never a lost record.

- **Journal pages** (`journal` table): one per photo or typed note, grouped by day. Private to the
  person who wrote them; admins can't read them. Snapping the same ticket again updates its page.
- **Wins** (`wins` table): what you finished each day, one row per task, filed under the day it was
  done (the ticket's day for a photo read-back). Kept like the journal, until the account goes.
- **Slugs**: tasks that keep sliding to another day (`task_slides` counts the moves) or have waited
  a long time. They're shown gently, with a way to make them smaller.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from datetime import date

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.engine import Engine

from . import db

MAX_NOTES = 20_000
MAX_WIN = 500
SLUG_MOVES = 3       # moved this many times...
SLUG_WAITING = 7     # ...or waiting this many days
MAX_SLUGS = 10


def merge_notes(old: str, new: str) -> str:
    """Notes read from another photo of the same page: keep what's there, add what's new."""
    old, new = (old or "").strip(), (new or "").strip()

    def squash(t: str) -> str:
        return " ".join(t.split())
    if not new or squash(new) in squash(old):
        return old
    if not old or squash(old) in squash(new):
        return new
    return f"{old}\n\n{new}"


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
                "label_id": row["label_id"], "win": row["win"] or ""}

    def _cols(self):
        c = db.journal.c
        return [c.id, c.day, c.created, c.updated, c.notes, c.scan_id, c.label_id, c.win,
                (c.photo.isnot(None)).label("has_photo")]

    @staticmethod
    def _day(day: str) -> str:
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("day looks like 2026-10-16")
        try:
            date.fromisoformat(day)
        except ValueError:
            raise ValueError("day looks like 2026-10-16") from None
        return day

    def add(self, day: str, notes: str = "", photo: bytes | None = None, scan_id: str | None = None,
            label_id: str | None = None, win: str = "") -> dict:
        day = self._day(day)
        notes = (notes or "").strip()
        if len(notes) > MAX_NOTES:
            raise ValueError(f"notes can be up to {MAX_NOTES} characters")
        if not notes and photo is None:
            raise ValueError("write something first")
        page_id, now = secrets.token_hex(6), int(time.time())
        with self.db.begin() as conn:
            conn.execute(insert(db.journal).values(
                id=page_id, owner=self.owner, household_id=self.household, day=day, created=now,
                updated=now, notes=notes, photo=photo, scan_id=scan_id, label_id=label_id,
                win=(win or "").strip()[:MAX_WIN] or None,
                photo_sha=hashlib.sha256(photo).hexdigest() if photo is not None else None))
        return self.get(page_id)

    def save_snap(self, day: str, notes: str = "", photo: bytes | None = None, scan_id: str | None = None,
                  label_id: str | None = None, win: str = "", page_id: str | None = None) -> dict:
        """Keep a read-back photo as a page. Snapping the same ticket again on its day, or the very
        same photo again, updates that page (newer photo, notes merged) instead of adding another.
        `page_id` without a photo moves that page to this ticket and day (the ticket was picked
        afterwards, or the photo was read again after reading it failed)."""
        day = self._day(day)
        if photo is None and page_id:
            photo = self.photo(page_id)
        if photo is None:
            return self.add(day, notes, None, scan_id, label_id, win)
        sha = hashlib.sha256(photo).hexdigest()
        c = db.journal.c
        cols = (c.id, c.notes, c.win)
        with self.db.connect() as conn:
            by_page = conn.execute(select(*cols).where(self._mine(page_id))).mappings().all() if page_id else []
            by_label = conn.execute(select(*cols).where(c.owner == self.owner, c.day == day, c.label_id == label_id)
                                    .order_by(c.created)).mappings().all() if label_id else []
            by_photo = conn.execute(select(*cols).where(c.owner == self.owner, c.photo_sha == sha)
                                    .order_by(c.created)).mappings().all()
        pages = list({p["id"]: dict(p) for p in [*by_label, *by_page, *by_photo]}.values())
        if not pages:
            return self.add(day, notes, photo, scan_id, label_id, win)
        keep = pages[0]  # the ticket's page if it has one; the others (the same photo) fold into it
        merged = keep["notes"] or ""
        for p in pages[1:]:
            merged = merge_notes(merged, p["notes"])
        merged = merge_notes(merged, notes)[:MAX_NOTES]
        new_win = (win or "").strip()[:MAX_WIN] or next((p["win"] for p in pages if p["win"]), None)
        values = {"day": day, "photo": photo, "photo_sha": sha, "notes": merged, "win": new_win or None,
                  "updated": int(time.time())}
        if scan_id:
            values["scan_id"] = scan_id
        if label_id:
            values["label_id"] = label_id
        with self.db.begin() as conn:
            conn.execute(update(db.journal).where(self._mine(keep["id"])).values(**values))
            for p in pages[1:]:
                conn.execute(delete(db.journal).where(self._mine(p["id"])))
        return self.get(keep["id"])

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
        page = self.get(page_id)
        if page is None:
            raise KeyError(page_id)
        if not notes and not page["has_photo"]:
            raise ValueError("This page would be empty. Delete it instead?")
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
        with self.db.connect() as conn:
            return conn.execute(select(func.count()).select_from(db.journal).where(
                db.journal.c.owner == self.owner)).scalar() or 0

    def newest_day_before(self, day: str) -> str | None:
        """The latest day with a page before `day`, so "Older days" can jump over empty weeks."""
        c = db.journal.c
        with self.db.connect() as conn:
            return conn.execute(select(func.max(c.day)).where(c.owner == self.owner, c.day < day)).scalar()

    def delete_for_user(self) -> int:
        with self.db.begin() as conn:
            return conn.execute(delete(db.journal).where(db.journal.c.owner == self.owner)).rowcount


class Wins:
    """What one person finished, by day. My list tasks keep their name; a linked app's don't."""

    def __init__(self, engine: Engine, owner: str, household: str = "home"):
        self.db, self.owner, self.household = engine, owner, household

    def add(self, task_id: str | None, day: str, source: str, content: str | None = None,
            scan_id: str | None = None, once_a_day: bool = False) -> bool:
        """Record a finished task. `once_a_day`: not again if this task already counted that day
        (a linked task ticked in the app and also ✓-marked on paper)."""
        w = db.wins.c
        with self.db.begin() as conn:
            if once_a_day and task_id and conn.execute(select(w.id).where(
                    w.owner == self.owner, w.task_id == str(task_id)[:200], w.day == day).limit(1)).first():
                return False
            conn.execute(insert(db.wins).values(
                owner=self.owner, household_id=self.household, day=day, ts=int(time.time()),
                task_id=str(task_id)[:200] if task_id else None, source=source,
                content=(content or "")[:500] or None, scan_id=scan_id))
        return True

    def remove_last(self, task_id: str, scan_id: str | None = None) -> bool:
        """Undo: forget the newest win for this task (from this read-back, when given)."""
        w = db.wins.c
        conds = [w.owner == self.owner, w.task_id == str(task_id)[:200]]
        if scan_id:
            conds.append(w.scan_id == scan_id)
        with self.db.begin() as conn:
            newest = conn.execute(select(w.id).where(*conds).order_by(w.ts.desc(), w.id.desc()).limit(1)).scalar()
            if newest is None:
                return False
            conn.execute(delete(db.wins).where(w.id == newest))
        return True

    def between(self, first: str, last: str) -> list[dict]:
        w = db.wins.c
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                select(w.day, w.task_id, w.source, w.content, w.scan_id)
                .where(w.owner == self.owner, w.day >= first, w.day <= last).order_by(w.ts, w.id)).mappings()]

    def newest_day_before(self, day: str) -> str | None:
        w = db.wins.c
        with self.db.connect() as conn:
            return conn.execute(select(func.max(w.day)).where(w.owner == self.owner, w.day < day)).scalar()

    def delete_for_user(self) -> int:
        with self.db.begin() as conn:
            return conn.execute(delete(db.wins).where(db.wins.c.owner == self.owner)).rowcount


class Slides:
    """How often each of a person's tasks was moved to a later day. -1 means "not a slug": the
    person said so, and it stays off the slugs list until it's moved again."""

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
            moves = max(have, 0) + 1
            conn.execute(update(db.task_slides).where(self._key(task_id)).values(moves=moves, last_moved=now))
            return moves

    def unbump(self, task_id: str) -> None:
        """A move that was undone doesn't count."""
        with self.db.begin() as conn:
            have = conn.execute(select(db.task_slides.c.moves).where(self._key(task_id))).scalar()
            if have is None or have < 0:
                return
            if have <= 1:
                conn.execute(delete(db.task_slides).where(self._key(task_id)))
            else:
                conn.execute(update(db.task_slides).where(self._key(task_id)).values(moves=have - 1))

    def dismiss(self, task_id: str) -> None:
        """"Not a slug": off the list until it's moved again."""
        with self.db.begin() as conn:
            conn.execute(delete(db.task_slides).where(self._key(task_id)))
            conn.execute(insert(db.task_slides).values(owner=self.owner, task_id=str(task_id)[:200],
                                                       moves=-1, last_moved=int(time.time())))

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
        if n < 0:
            continue  # they said it isn't one
        if n >= min_moves or waited >= SLUG_WAITING:
            out.append({"id": str(t["id"]), "content": t["content"], "moves": n, "waiting_days": waited,
                        "has_steps": bool(t.get("steps"))})
    out.sort(key=lambda s: (-s["moves"], -s["waiting_days"]))
    return out[:MAX_SLUGS]
