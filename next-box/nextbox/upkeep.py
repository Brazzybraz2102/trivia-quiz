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
- tasks-autoincrement: SQLite reused a deleted task's id; older tasks tables are rebuilt so it can't
- vision-errors: a failed read-back's error could quote the journal notes it read; only its type stays
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, insert, select, update
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
    with engine.begin() as conn:  # the invite they used stays used, but no longer names them
        conn.execute(update(db.invites).where(db.invites.c.used_by == username).values(used_by=DELETED_ACCOUNT))


DELETED_ACCOUNT = "(deleted account)"  # in place of a deleted person's name on the invite they used


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


def _type_only(error) -> str | None:
    """A read-back error that quotes what the model read ("...input_value='{"notes": ...'"), cut to
    its type name. Anything else is left as it is (None)."""
    if isinstance(error, str) and "input_value=" in error:
        return error.split(":", 1)[0].strip()[:80] or "Error"
    return None


def scrub_vision_errors(engine: Engine) -> int:
    """A failed photo read-back used to keep the error's whole message, which could quote the end of
    the journal notes it read, in the scan, the event log and the errors a feedback post attached.
    Keep only the error's type."""
    changed = 0
    with engine.begin() as conn:
        for scan_id, record in conn.execute(select(db.scans.c.id, db.scans.c.record)).all():
            fixed = _type_only((record or {}).get("vision_error"))
            if fixed:
                conn.execute(update(db.scans).where(db.scans.c.id == scan_id).values(
                    record={**record, "vision_error": fixed}))
                changed += 1
        for event_id, error in conn.execute(select(db.events.c.id, db.events.c.error)
                                            .where(db.events.c.error.like("%input_value=%"))).all():
            conn.execute(update(db.events).where(db.events.c.id == event_id).values(error=_type_only(error)))
            changed += 1
        for fid, debug in conn.execute(select(db.feedback_identities.c.id, db.feedback_identities.c.debug)
                                       .where(db.feedback_identities.c.debug.is_not(None))).all():
            errors = (debug or {}).get("recent_errors") if isinstance(debug, dict) else None
            if errors and any(_type_only(e.get("error")) for e in errors if isinstance(e, dict)):
                kept = [{**e, "error": _type_only(e.get("error")) or e.get("error")} if isinstance(e, dict) else e
                        for e in errors]
                conn.execute(update(db.feedback_identities).where(db.feedback_identities.c.id == fid)
                             .values(debug={**debug, "recent_errors": kept}))
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


def _local_num(task_id) -> int:
    tid = str(task_id or "")
    return int(tid[3:]) if is_local(tid) and tid[3:].isdigit() and len(tid) <= 18 else 0


def tasks_autoincrement(engine: Engine, store=None) -> bool:
    """SQLite gave a new task the id of the newest one deleted, so an old ticket's row (or its slide
    count) could land on the new task. Rebuild an older tasks table with AUTOINCREMENT, which never
    hands an id out twice, and start the count past every task id a kept ticket, a win or a slide
    count still names. PostgreSQL never reused ids. Returns whether the table was rebuilt."""
    def rebuilt(conn) -> bool:
        made = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'").scalar()
        return not made or "AUTOINCREMENT" in made.upper()

    if engine.dialect.name != "sqlite":
        return False
    with engine.connect() as conn:
        if rebuilt(conn):  # made by 0.14.1 or later
            return False
    on_tickets = 0  # read before the rebuild, which holds the write lock
    for rec in (store.list_printed(1_000_000) if store is not None else []):
        on_tickets = max([on_tickets, *(_local_num(m.get("task_id")) for m in rec.get("manifest") or [])])
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")  # one process at a time; the next one finds it done
        try:
            if rebuilt(conn):
                conn.rollback()
                return False
            have = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(tasks)")}
            cols = ", ".join(f'"{c.name}"' for c in db.tasks.columns if c.name in have)
            conn.exec_driver_sql("ALTER TABLE tasks RENAME TO tasks_before_v0141")
            conn.exec_driver_sql("DROP INDEX IF EXISTS ix_tasks_owner_open")
            db.tasks.create(conn)
            conn.exec_driver_sql(f"INSERT INTO tasks ({cols}) SELECT {cols} FROM tasks_before_v0141")
            conn.exec_driver_sql("DROP TABLE tasks_before_v0141")
            top = max(conn.execute(select(func.max(db.tasks.c.id))).scalar() or 0, on_tickets)
            for col in (db.wins.c.task_id, db.task_slides.c.task_id):
                for (tid,) in conn.execute(select(col).where(col.like("nb:%")).distinct()):
                    top = max(top, _local_num(tid))
            conn.exec_driver_sql("DELETE FROM sqlite_sequence WHERE name = 'tasks'")
            if top:
                conn.exec_driver_sql("INSERT INTO sqlite_sequence (name, seq) VALUES ('tasks', ?)", (top,))
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    return True


def run_startup_fixes(engine: Engine, store) -> None:
    _once(engine, "orphans-v1", lambda: sweep_orphans(engine, store))
    _once(engine, "debug-details-v1", lambda: scrub_debug_details(engine))
    _once(engine, "dates-v1", lambda: normalise_dates(engine))
    _once(engine, "wins-v1", lambda: backfill_wins(engine, store))
    _once(engine, "tasks-autoincrement-v1", lambda: tasks_autoincrement(engine, store))
    _once(engine, "vision-errors-v1", lambda: scrub_vision_errors(engine))
