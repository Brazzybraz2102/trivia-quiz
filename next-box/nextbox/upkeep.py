"""Removing an account with everything Next Box keeps about it, and one-time clean-ups of older data.

`purge_user` is the one way an account is deleted (yourself, an admin, or `nextbox user remove`), so
a new account that later gets the same username never inherits anyone's tasks, tickets, read-backs,
journal or activity.

`run_startup_fixes` runs each clean-up once per database (remembered in server_settings):
- orphans: rows left behind by accounts deleted before purge_user existed
- debug-details: debug mode used to copy task text and journal notes into events
- dates: due dates and journal days stored as 20261016 or 2026-W42-5 instead of 2026-10-16
- wins: wins used to be worked out from events (deleted after 180 days) and read-backs (90 days);
  they're kept in their own table now, filled once from what older versions had
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from . import db
from .journal import Journal, Slides, Wins
from .mylist import LocalTasks, Tags, is_local

# Not people: what the CLI and Home Assistant print as before any account exists. Left alone.
NOT_ACCOUNTS = frozenset({"", "cli", "key"})


def forget_data(engine: Engine, store, username: str) -> None:
    """Delete a person's built-in list, tags, journal, slides, tickets and read-backs."""
    LocalTasks(engine, username).delete_for_user()
    Tags(engine, username).delete_for_user()
    Journal(engine, username).delete_for_user()
    Wins(engine, username).delete_for_user()
    Slides(engine, username).delete_for_user()
    store.delete_for_user(username)


def purge_user(accounts, store, events, feedback, username: str) -> str:
    """Delete the account, then everything kept about it. Their linked apps aren't touched and
    their feedback stays on the board, unlinked. Raises AuthError (nothing deleted) when the account
    can't be removed, e.g. the protected admin or the last superadmin. Returns the username."""
    username = username.strip().lower()
    accounts.remove_user(username)
    feedback.forget_user(username)
    forget_data(accounts.db, store, username)
    events.delete_for_user(username)
    return username


# --- one-time clean-ups -------------------------------------------------------------------------
def _once(engine: Engine, key: str, fix) -> None:
    flag = f"_upkeep:{key}"
    with engine.connect() as conn:
        if conn.execute(select(db.server_settings.c.key).where(db.server_settings.c.key == flag)).first():
            return
    fix()
    try:
        with engine.begin() as conn:
            conn.execute(insert(db.server_settings).values(key=flag, value=True))
    except IntegrityError:
        pass  # another process finished the same clean-up a moment ago


def sweep_orphans(engine: Engine, store) -> list[str]:
    """Data whose owner no longer has an account. Returns the names cleaned up."""
    with engine.connect() as conn:
        people = {r[0] for r in conn.execute(select(db.users.c.username))}
        owners: set[str] = set()
        for col in (db.tickets.c.by, db.scans.c.by, db.tasks.c.owner, db.tags.c.owner, db.journal.c.owner,
                    db.task_slides.c.owner, db.wins.c.owner):
            owners |= {r[0] for r in conn.execute(select(col).distinct())}
    gone = sorted(owners - people - NOT_ACCOUNTS)
    for name in gone:
        forget_data(engine, store, name)
    return gone


def scrub_debug_details(engine: Engine) -> int:
    """Debug mode used to keep each action's arguments and result (task text, journal notes) in
    events. Drop them; the event itself (who, what, when, how long) stays."""
    changed = 0
    with engine.begin() as conn:
        rows = conn.execute(select(db.events.c.id, db.events.c.detail)).all()
        for event_id, detail in rows:
            if isinstance(detail, dict) and ("args" in detail or "result" in detail):
                kept = {k: v for k, v in detail.items() if k not in ("args", "result")}
                conn.execute(update(db.events).where(db.events.c.id == event_id).values(detail=kept or None))
                changed += 1
    return changed


def _iso(value) -> str | None:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def normalise_dates(engine: Engine) -> int:
    """YYYY-MM-DD everywhere, so tasks sort and group right and journal pages show up by day."""
    changed = 0
    with engine.begin() as conn:
        for task_id, due in conn.execute(select(db.tasks.c.id, db.tasks.c.due_date)
                                         .where(db.tasks.c.due_date.is_not(None))).all():
            fixed = _iso(due)
            if fixed != due:
                conn.execute(update(db.tasks).where(db.tasks.c.id == task_id).values(
                    due_date=fixed, **({} if fixed else {"due_time": None, "repeat": None})))
                changed += 1
        for page_id, day, created in conn.execute(select(db.journal.c.id, db.journal.c.day, db.journal.c.created)).all():
            written = datetime.fromtimestamp(created).date()
            fixed = _iso(day)
            if not fixed or not "2000-01-01" <= fixed <= (written + timedelta(days=366)).isoformat():
                fixed = written.isoformat()  # unreadable, or a day the Journal tab can't reach
            if fixed != day:
                conn.execute(update(db.journal).where(db.journal.c.id == page_id).values(day=fixed))
                changed += 1
    return changed


def backfill_wins(engine: Engine, store) -> int:
    """Fill the wins table from what older versions kept: task_done events, finished My list tasks
    (their names), and read-backs that finished a linked app's task (by id only, never its text)."""
    with engine.connect() as conn:
        if conn.execute(select(db.wins.c.id).limit(1)).first():
            return 0
        homes = {r[0]: r[1] for r in conn.execute(select(db.users.c.username, db.users.c.household_id))}
        named: dict[tuple, list] = {}  # (owner, day) -> My list tasks finished that day
        for num, owner, content, done_at in conn.execute(select(
                db.tasks.c.id, db.tasks.c.owner, db.tasks.c.content, db.tasks.c.done_at)
                .where(db.tasks.c.done_at.is_not(None)).order_by(db.tasks.c.done_at)):
            named.setdefault((owner, date.fromtimestamp(done_at).isoformat()), []).append((f"nb:{num}", content, done_at))
        found = []  # (owner, day, ts, task_id, source, content, scan_id)
        for user, ts, detail in conn.execute(select(db.events.c.user, db.events.c.ts, db.events.c.detail)
                                             .where(db.events.c.action == "task_done").order_by(db.events.c.ts)):
            day, detail = date.fromtimestamp(ts).isoformat(), detail or {}
            if detail.get("source", "nextbox") == "nextbox":
                pool = named.get((user, day)) or []
                task_id, content, _ = pool.pop(0) if pool else (detail.get("task_id"), None, None)
                found.append((user, day, int(ts), task_id, "nextbox", content, None))
            else:
                found.append((user, day, int(ts), detail.get("task_id"), "linked", None, None))
        for (owner, day), pool in named.items():  # finished, but the event was already cleared out
            found += [(owner, day, int(ts), task_id, "nextbox", content, None) for task_id, content, ts in pool]
    for rec in store.list_scans(1_000_000):
        marks = rec.get("applied", []) + [p for p in rec.get("needs_confirmation", []) if p.get("status") == "applied"]
        for a in marks:
            if a.get("mark") == "done" and not is_local(str(a.get("task_id", ""))):
                ts = int(datetime.fromisoformat(rec["created_at"]).timestamp())
                found.append((rec.get("by", ""), rec["created_at"][:10], ts, str(a["task_id"]), "readback", None, rec["id"]))
    rows = [{"owner": o, "household_id": homes[o], "day": d, "ts": ts, "task_id": (t or None) and str(t)[:200],
             "source": s, "content": (c or "")[:500] or None, "scan_id": sid}
            for o, d, ts, t, s, c, sid in found if o in homes]
    if rows:
        with engine.begin() as conn:
            conn.execute(insert(db.wins), rows)
    return len(rows)


def run_startup_fixes(engine: Engine, store) -> None:
    _once(engine, "orphans-v1", lambda: sweep_orphans(engine, store))
    _once(engine, "debug-details-v1", lambda: scrub_debug_details(engine))
    _once(engine, "dates-v1", lambda: normalise_dates(engine))
    _once(engine, "wins-v1", lambda: backfill_wins(engine, store))
