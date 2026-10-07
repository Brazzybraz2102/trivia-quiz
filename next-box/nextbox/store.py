"""Print history, ticket manifests, scan results and the daily auto-print guard (in the database).

This is not a task database. It records what was printed (so a photo of a ticket can be matched
back to task IDs) and what read-back is still waiting for confirmation.
"""
from __future__ import annotations

import contextlib
import io
import os
import secrets
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from . import db

ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def normalize_id(label_id: str) -> str:
    return label_id.strip().lstrip("#").upper()


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "png").mkdir(exist_ok=True)  # local cache of ticket images; the database is the record
        os.chmod(self.root, 0o700)  # tickets hold people's task names
        self.db = db.database(self.root)

    def new_id(self, day: date | None = None) -> str:
        """e.g. 260930-X5C8: printed date + 4 unambiguous characters (no 0/O, 1/I)."""
        day = day or date.today()
        with self.db.connect() as conn:
            while True:
                label_id = f"{day:%y%m%d}-" + "".join(secrets.choice(ID_ALPHABET) for _ in range(4))
                taken = conn.execute(select(db.tickets.c.id).where(db.tickets.c.id == label_id)).first() or \
                    conn.execute(select(db.scans.c.id).where(db.scans.c.id == label_id)).first()
                if not taken:
                    return label_id

    # --- auto guard -------------------------------------------------------
    def claim_auto(self, day: str, dry_run: bool, scope: str = "default") -> bool:
        """True the first time it's called for `day` (per scope); False after. Real and dry runs are
        tracked separately so testing never uses up the real print for the day. Atomic."""
        key = f"{scope}:{'dry' if dry_run else 'real'}"
        with self.db.begin() as conn:
            if conn.execute(update(db.auto_guard).where(and_(db.auto_guard.c.key == key,
                                                             db.auto_guard.c.day != day)).values(day=day)).rowcount:
                return True
            if conn.execute(select(db.auto_guard.c.key).where(db.auto_guard.c.key == key)).first():
                return False
        try:
            with self.db.begin() as conn:
                conn.execute(insert(db.auto_guard).values(key=key, day=day))
            return True
        except IntegrityError:
            return False  # someone claimed it a moment ago

    def release_auto(self, day: str, dry_run: bool, scope: str = "default") -> None:
        """Only used when nothing was sent to the printer (e.g. the to-do app was down)."""
        key = f"{scope}:{'dry' if dry_run else 'real'}"
        with self.db.begin() as conn:
            conn.execute(delete(db.auto_guard).where(and_(db.auto_guard.c.key == key, db.auto_guard.c.day == day)))

    def auto_state(self) -> dict:
        with self.db.connect() as conn:
            return {r.key: r.day for r in conn.execute(select(db.auto_guard))}

    # --- printed tickets ----------------------------------------------------
    def png_path(self, label_id: str) -> Path:
        return self.root / "png" / f"{label_id}.png"

    def save_printed(self, record: dict, png=None) -> None:
        """`png` is a PIL image or PNG bytes; it's kept with the record."""
        data = None
        if png is not None:
            if hasattr(png, "save"):
                buf = io.BytesIO()
                png.save(buf, "PNG")
                data = buf.getvalue()
            else:
                data = bytes(png)
            self.png_path(record["id"]).write_bytes(data)
        with self.db.begin() as conn:
            conn.execute(delete(db.tickets).where(db.tickets.c.id == record["id"]))
            conn.execute(insert(db.tickets).values(
                id=record["id"], household_id=record.get("household_id") or "home", by=record.get("by", ""),
                created_at=record["created_at"][:19], record=record, png=data))

    def get_png(self, label_id: str) -> bytes | None:
        with self.db.connect() as conn:
            row = conn.execute(select(db.tickets.c.png).where(db.tickets.c.id == normalize_id(label_id))).first()
        if row and row[0]:
            return row[0]
        cached = self.png_path(normalize_id(label_id))
        return cached.read_bytes() if cached.exists() else None

    def get_printed(self, label_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(select(db.tickets.c.record).where(db.tickets.c.id == normalize_id(label_id))).first()
        return row[0] if row else None

    def list_printed(self, limit: int = 50, by: str | None = None, household: str | None = None,
                     since: str | None = None) -> list[dict]:
        q = select(db.tickets.c.record).order_by(db.tickets.c.created_at.desc(), db.tickets.c.id.desc()).limit(limit)
        if by is not None:
            q = q.where(db.tickets.c.by == by)
        if household is not None:
            q = q.where(db.tickets.c.household_id == household)
        if since is not None:
            q = q.where(db.tickets.c.created_at >= since)
        with self.db.connect() as conn:
            return [r[0] for r in conn.execute(q)]

    # --- scans ------------------------------------------------------------
    def save_scan(self, record: dict) -> None:
        with self.db.begin() as conn:
            conn.execute(delete(db.scans).where(db.scans.c.id == record["id"]))
            conn.execute(insert(db.scans).values(
                id=record["id"], household_id=record.get("household_id") or "home", by=record.get("by", ""),
                created_at=record["created_at"][:19], record=record))

    def get_scan(self, scan_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(select(db.scans.c.record).where(db.scans.c.id == scan_id)).first()
        return row[0] if row else None

    @contextlib.contextmanager
    def edit_scan(self, scan_id: str) -> Iterator[dict]:
        with self.db.begin() as conn:
            q = select(db.scans.c.record).where(db.scans.c.id == scan_id)
            if conn.dialect.name == "postgresql":
                q = q.with_for_update()  # two taps at once can't both apply a delete
            row = conn.execute(q).first()
            if not row:
                raise KeyError(scan_id)
            record = dict(row[0])
            yield record
            conn.execute(update(db.scans).where(db.scans.c.id == scan_id).values(record=record))

    def list_scans(self, limit: int = 20, by: str | None = None, household: str | None = None,
                   since: str | None = None) -> list[dict]:
        q = select(db.scans.c.record).order_by(db.scans.c.created_at.desc(), db.scans.c.id.desc()).limit(limit)
        if by is not None:
            q = q.where(db.scans.c.by == by)
        if household is not None:
            q = q.where(db.scans.c.household_id == household)
        if since is not None:
            q = q.where(db.scans.c.created_at >= since)
        with self.db.connect() as conn:
            return [r[0] for r in conn.execute(q)]

    # --- retention & deletion -----------------------------------------------
    def _drop(self, ticket_filter, scan_filter) -> dict:
        with self.db.begin() as conn:
            ids = [r[0] for r in conn.execute(select(db.tickets.c.id).where(ticket_filter))]
            conn.execute(delete(db.tickets).where(ticket_filter))
            scans_removed = conn.execute(delete(db.scans).where(scan_filter)).rowcount
        for label_id in ids:
            self.png_path(label_id).unlink(missing_ok=True)
        return {"printed": len(ids), "scans": scans_removed}

    def prune(self, days: int = 90) -> dict:
        """Delete tickets and scans older than `days`. People's to-do apps are untouched."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        return self._drop(db.tickets.c.created_at < cutoff, db.scans.c.created_at < cutoff)

    def delete_for_user(self, username: str) -> dict:
        return self._drop(db.tickets.c.by == username, db.scans.c.by == username)

    def delete_for_household(self, household_id: str) -> dict:
        return self._drop(db.tickets.c.household_id == household_id, db.scans.c.household_id == household_id)
