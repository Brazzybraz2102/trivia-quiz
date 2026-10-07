"""Activity, audit and error log, plus server-wide switches (in the database).

Events never contain passwords, tokens or .env values. Full tracebacks are kept only for
errors, so staff can debug what a beta tester hit. Feedback never goes here: see feedback.py.
"""
from __future__ import annotations

import time

from sqlalchemy import and_, delete, insert, select

from . import db

_SERVER_DEFAULTS = {
    "printing_paused": False,     # every print becomes a dry run (kill switch)
    "auto_print_enabled": True,   # the once-a-day automatic print
    "announcement": "",           # banner shown to everyone who's signed in
}


def _row(r) -> dict:
    ev = {"ts": r["ts"], "user": r["user"], "kind": r["kind"], "action": r["action"], "ok": r["ok"]}
    for k in ("detail", "error", "trace"):
        if r[k]:
            ev[k] = r[k]
    if r["household_id"]:
        ev["household_id"] = r["household_id"]
    return ev


class Events:
    def __init__(self, root):
        self.db = db.database(root)

    def log(self, user: str, action: str, *, ok: bool = True, kind: str = "activity",
            detail: dict | None = None, error: str = "", trace: str = "",
            household: str | None = None) -> dict:
        """kind: activity | audit (admin actions) | error | auth."""
        ev = {"ts": round(time.time(), 3), "user": user, "kind": kind, "action": action, "ok": ok}
        with self.db.begin() as conn:
            conn.execute(insert(db.events).values(
                ts=ev["ts"], user=user[:64], household_id=household, kind=kind, action=action[:64], ok=ok,
                detail=detail or None, error=error[:500] or None, trace=trace[-6000:] or None))
        if detail:
            ev["detail"] = detail
        if error:
            ev["error"] = error[:500]
        return ev

    def take(self, kind: str) -> list[dict]:
        """Remove and return every event of one kind (used to move old feedback out of the log)."""
        with self.db.begin() as conn:
            rows = conn.execute(select(db.events).where(db.events.c.kind == kind).order_by(db.events.c.id)).mappings().all()
            conn.execute(delete(db.events).where(db.events.c.kind == kind))
        return [_row(r) for r in rows]

    def query(self, *, user: str | None = None, kind: str | None = None, ok: bool | None = None,
              limit: int = 200, household: str | None = None, since: float | None = None) -> list[dict]:
        conds = []
        if user:
            conds.append(db.events.c.user == user)
        if kind:
            conds.append(db.events.c.kind == kind)
        if ok is not None:
            conds.append(db.events.c.ok.is_(ok))
        if household:
            conds.append(db.events.c.household_id == household)
        if since:
            conds.append(db.events.c.ts >= since)
        q = select(db.events).order_by(db.events.c.id.desc()).limit(limit)
        if conds:
            q = q.where(and_(*conds))
        with self.db.connect() as conn:
            return [_row(r) for r in conn.execute(q).mappings()]

    def delete_for_user(self, username: str) -> int:
        with self.db.begin() as conn:
            return conn.execute(delete(db.events).where(db.events.c.user == username)).rowcount

    def prune(self, days: int = 180) -> int:
        with self.db.begin() as conn:
            return conn.execute(delete(db.events).where(db.events.c.ts < time.time() - days * 86400)).rowcount


class ServerSettings:
    def __init__(self, root):
        self.db = db.database(root)

    def get(self) -> dict:
        with self.db.connect() as conn:
            stored = {r.key: r.value for r in conn.execute(select(db.server_settings))}
        return {**_SERVER_DEFAULTS, **{k: v for k, v in stored.items() if k in _SERVER_DEFAULTS}}

    def update(self, changes: dict) -> dict:
        for k, v in changes.items():
            if k not in _SERVER_DEFAULTS:
                raise ValueError(f"unknown server setting {k}")
            if type(v) is not type(_SERVER_DEFAULTS[k]):
                raise ValueError(f"invalid value for {k}")
            if k == "announcement" and len(v) > 300:
                raise ValueError("announcement is limited to 300 characters")
        with self.db.begin() as conn:
            for k, v in changes.items():
                conn.execute(delete(db.server_settings).where(db.server_settings.c.key == k))
                conn.execute(insert(db.server_settings).values(key=k, value=v))
        return self.get()
