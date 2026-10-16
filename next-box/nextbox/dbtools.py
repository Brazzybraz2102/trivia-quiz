"""`nextbox db ...`: look inside the database, safely, and practice SQL on a sandbox copy.

- The real database is only ever opened read-only here, and password hashes, salts, session
  hashes and connection secrets are masked in every result.
- `nextbox db demo` builds a separate practice database full of realistic fake data. You can
  run INSERT / UPDATE / DELETE there as much as you like.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, insert, inspect, select, text
from sqlalchemy.engine import make_url

from . import db

# Plain-English meaning of every table, shown by `nextbox db tables` and `schema`.
TABLE_NOTES = {
    "households": "A home (or family) using Next Box. Everyone belongs to one.",
    "users": "People who can sign in. Passwords are stored only as scrypt hashes.",
    "sessions": "Signed-in devices. Only a SHA-256 hash of each sign-in token is stored.",
    "invites": "Codes a household manager hands out so someone can join their household.",
    "tickets": "Every ticket printed or previewed: what was on it (record) and its image (png).",
    "scans": "Photo read-backs: what the photo said and what changed in the to-do app.",
    "auto_guard": "Remembers the day the automatic print last ran, so it never runs twice.",
    "events": "The activity, audit and error log.",
    "server_settings": "Switches for the whole server (pause printing, banner, ...).",
    "printers": "Each household's printers and the label color loaded in each.",
    "feedback": "The public feedback board: anonymous, dated by day only.",
    "feedback_identities": "Who sent each piece of feedback. The only place that link exists.",
    "tasks": "Everyone's built-in to-do list (My list). Linked apps' tasks are never stored here.",
    "tags": "Each person's tags and the label color for each.",
    "wins": "What each person finished, by day (My list tasks by name; a linked app's by id only).",
    "journal": "Each person's private journal pages: photo and notes (both hidden here), by day.",
    "task_slides": "How many times each task moved to a later day (task ids and counts only, never text).",
}
MASKED_COLUMNS = {"hash", "salt", "token_hash", "notes", "photo", "win"}  # notes/photo/win: private journal pages
# The same columns by table. On SQLite they read as NULL whatever the query calls them
# (`SELECT notes AS n`, `hash || ''`, `length(notes)`), so an alias can't get past the masking.
UNREADABLE = {("journal", "notes"), ("journal", "photo"), ("journal", "win"), ("users", "hash"), ("users", "salt"),
              ("sessions", "token_hash")}


def _authorizer(action, table, column, _db, _source):
    if action == sqlite3.SQLITE_READ and (table, column) in UNREADABLE:
        return sqlite3.SQLITE_IGNORE  # the value reads as NULL
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY  # read-only means no new database files either
    return sqlite3.SQLITE_OK


# Read-only still lets these make files (ATTACH a new name, VACUUM INTO), so they're refused up front.
FILE_MAKERS = re.compile(r"^\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*(ATTACH|DETACH|VACUUM)\b", re.I | re.S)
READ_ONLY_REFUSED = ("db sql is read-only here: ATTACH, DETACH and VACUUM aren't allowed "
                     "(add --practice --write to try them on the practice database).")


# PostgreSQL has no authorizer, so before a query runs on the real database its plan (EXPLAIN VERBOSE)
# is checked: the plan names every column the query reads, under the table it reads it from. Those
# columns may only pass straight through to a result column of their own name (which _mask hides);
# an alias, a WHERE, a sort, a join, a function or a subquery over them is refused.
PG_REFUSED = ("On PostgreSQL, db sql can't read journal text or password columns (only show them hidden, "
              "as in SELECT * FROM journal). Leave them out of WHERE, ORDER BY, functions and aliases.")
PG_ONE_QUERY = "On PostgreSQL, db sql runs one query at a time: a SELECT (WITH, VALUES, TABLE and EXPLAIN work too)."
_PROTECTED: dict[str, set[str]] = {}
for _table, _column in UNREADABLE:
    _PROTECTED.setdefault(_table, set()).add(_column)
# A node hands its columns to its parent under the same names (Outer, Inner), unless the parent gives
# them new ones: a subquery, a CTE, a UNION or a sub-select (InitPlan, SubPlan) does.
_PASS_LINKS = {"Outer", "Inner"}
_RENAMING = {"Subquery Scan", "CTE Scan", "WorkTable Scan", "Named Tuplestore Scan", "Append", "Merge Append",
             "Recursive Union", "SetOp", "Function Scan", "Table Function Scan", "Values Scan", "Foreign Scan",
             "Custom Scan", "ModifyTable"}
_NAME_KEYS = {"Node Type", "Parent Relationship", "Relation Name", "Schema", "Alias", "Index Name", "CTE Name",
              "Subplan Name", "Function Name", "Join Type", "Strategy", "Partial Mode", "Scan Direction",
              "Operation", "Command", "Sort Method", "Plans"}
# These run SQL from a string, which a plan can't see into; the planner's statistics hold sample values.
_RUNS_SQL = re.compile(r"(?<![\w.])(?:pg_catalog\.)?(?:\w+_to_xml\w*|ts_stat|ts_rewrite|dblink\w*)\s*\(", re.I)
_STATISTICS = {"pg_statistic", "pg_statistic_ext_data"}
# A quoted name ("j'k") is read whole first, so a quote inside it doesn't start a string.
_TOKENS = re.compile(r'"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'')
_EXPLAIN = re.compile(r"^\s*explain\b(?:\s*\((?:[^()]|\([^()]*\))*\))?(?:\s+(?:analy[sz]e|verbose)\b)*", re.I)


def _no_strings(expr: str) -> str:
    """An expression with its string constants emptied: what they say isn't a column."""
    return _TOKENS.sub(lambda m: m[0] if m[0].startswith('"') else "''", expr)


def _strings(value) -> list[str]:
    """Every expression in a plan node's fields (not its children or names)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        return [s for k, v in value.items() if k not in _NAME_KEYS for s in _strings(v)]
    return []


def _pg_plan_check(plan: dict) -> list[tuple[int, str]]:
    """Raise PermissionError when the plan reads a protected column other than straight through.
    Returns [(result position, column)] for the ones passed straight to the result, whose result
    column must keep the column's own name (checked once the result's names are known)."""
    # (node, is the root, its parent sees its columns under their own names). A scan often lists every
    # column of its table even when the parent uses none (SELECT count(*)): that's fine as long as
    # the parent doesn't rename them, and anything the parent does with them shows in its own fields.
    nodes: list[tuple[dict, bool, bool]] = []

    def walk(node: dict, parent_type: str | None) -> None:
        same_names = parent_type is None or (node.get("Parent Relationship") in _PASS_LINKS
                                             and parent_type not in _RENAMING)
        nodes.append((node, parent_type is None, same_names))
        for child in node.get("Plans") or []:
            walk(child, node.get("Node Type") or "?")
    walk(plan, None)
    aliases: dict[str, set[str]] = {}
    for node, _, _ in nodes:
        rel = node.get("Relation Name")
        if rel in _STATISTICS:
            raise PermissionError(PG_REFUSED)
        if rel in _PROTECTED:
            aliases.setdefault(node.get("Alias") or rel, set()).update(_PROTECTED[rel])
    if any(_RUNS_SQL.search(_no_strings(s)) for node, _, _ in nodes for s in _strings(node)):
        raise PermissionError(PG_REFUSED)
    if not aliases:
        return []
    cols = sorted(set().union(*aliases.values()))
    quals = "|".join(sorted({re.escape(a) for a in aliases} | {re.escape('"' + a.replace('"', '""') + '"')
                                                                for a in aliases}, key=len, reverse=True))
    col = "|".join(cols)
    # A protected column, qualified by a protected table's alias or (one table only) on its own, or a whole row.
    ref = re.compile(rf'(?<![\w."])(?:(?:{quals})\.)?"?({col})"?(?![\w"(])|(?<![\w."])(?:{quals})\.\*')
    bare = re.compile(rf'(?:(?:{quals})\.)?"?({col})"?')
    passed: list[tuple[int, str]] = []
    for node, root, same_names in nodes:
        for key, value in node.items():
            if key in _NAME_KEYS:
                continue
            if key == "Output" and isinstance(value, list):
                for i, item in enumerate(value):
                    text_ = _no_strings(str(item))
                    if not ref.search(text_):
                        continue
                    straight = bare.fullmatch(text_)
                    if not (straight and same_names):
                        raise PermissionError(PG_REFUSED)
                    if root:
                        passed.append((i, straight[1]))
                continue
            if any(ref.search(_no_strings(s)) for s in _strings(value)):
                raise PermissionError(PG_REFUSED)
    return passed


def _pg_one(conn, sql: str):
    """Run `sql` on PostgreSQL as ONE prepared statement and return the psycopg cursor. A plain query
    string (the simple protocol) runs every statement in it, so `SELECT 1; COMMIT; UPDATE users ...`
    would end the read-only transaction and commit the UPDATE before anything noticed the `;`. A
    prepared statement (the extended protocol) can't hold a second command: it fails whole, and
    nothing in it runs."""
    cur = conn.connection.driver_connection.cursor()
    cur.execute(sql, prepare=True)
    return cur


def _pg_guard(conn, sql: str) -> list[tuple[int, str]]:
    """Check one query against the real PostgreSQL database before it runs (see _pg_plan_check)."""
    lead = _EXPLAIN.match(sql)
    inner = sql[lead.end():] if lead else sql  # their own EXPLAIN: check what it explains
    try:
        plan = _pg_one(conn, "EXPLAIN (VERBOSE, FORMAT JSON) " + inner).fetchone()[0]
    except Exception as exc:  # not a query EXPLAIN takes (COPY, SHOW, two statements, a typo)
        why = str(getattr(exc, "orig", exc)).strip().splitlines()
        raise PermissionError(PG_ONE_QUERY + (f" The database said: {why[0]}" if why else "")) from None
    if isinstance(plan, str):
        plan = json.loads(plan)
    passed = _pg_plan_check(plan[0]["Plan"])
    return [] if lead else passed  # EXPLAIN's own result is the plan, never the values

DIAGRAM = """
  households ─┬─< users ─┬─< sessions          ( ─< means "one to many" )
              │          ├─< tickets
              │          ├─< scans
              │          └─< events
              ├─< printers
              └─< invites
  users ─┬─< tasks (owner)
         └─< tags  (owner)

  feedback ── feedback_identities    (one to one, kept apart on purpose: the board is anonymous)
  auto_guard, server_settings        (stand-alone settings tables)

  The links are by value: users.household_id = households.id, tickets.by = users.username, ...
"""


# --- opening databases ------------------------------------------------------------------
def practice_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "practice"


def is_postgres(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://", "postgresql+"))


def practice_url(data_dir: Path) -> str:
    """The practice database sits next to the real one: a SQLite file, or a second PostgreSQL
    database with "_practice" on the end of its name (NEXTBOX_PRACTICE_URL overrides)."""
    if os.environ.get("NEXTBOX_PRACTICE_URL"):
        return os.environ["NEXTBOX_PRACTICE_URL"]
    real = db.url_for(data_dir)
    if is_postgres(real):
        base, _, name = real.rpartition("/")
        name, q, query = name.partition("?")
        return f"{base}/{name}_practice{q}{query}"
    return f"sqlite:///{practice_dir(data_dir) / 'nextbox.db'}"


def practice_exists(data_dir: Path) -> bool:
    url = practice_url(data_dir)
    if not is_postgres(url):
        return Path(url.removeprefix("sqlite:///")).exists()
    try:
        with db.database(url).connect() as conn:
            return bool(conn.execute(text("SELECT COUNT(*) FROM users")).scalar())
    except Exception:
        return False


def describe_location(data_dir: Path) -> str:
    url = db.url_for(data_dir)
    if url.startswith("sqlite:///"):
        return url.removeprefix("sqlite:///")
    head, _, tail = url.partition("@")  # hide the password in postgres://user:pass@host/db
    return (head.split(":")[0] + "://…@" + tail) if tail else url


def _mask(columns: list[str], row: tuple) -> list:
    out = []
    for col, val in zip(columns, row, strict=False):
        if col in MASKED_COLUMNS and (val or val is None):  # None: the authorizer hid it
            val = "•••• (hidden)"
        elif col == "connection" and val:
            data = json.loads(val) if isinstance(val, str) else val  # 'null' for someone with no linked app
            if isinstance(data, dict):
                data = {k: v for k, v in data.items() if k != "secret"}
            val = json.dumps(data)
        elif col == "calendars" and val:
            data = json.loads(val) if isinstance(val, str) else val
            if isinstance(data, list):
                data = [{k: v for k, v in c.items() if k != "secret"} if isinstance(c, dict) else c for c in data]
            val = json.dumps(data)
        elif col in ("png", "parts") and val:
            val = f"<image, {len(val):,} bytes>"
        out.append(val)
    return out


def run_sql(data_dir: Path, sql: str, practice: bool = False, write: bool = False,
            limit: int = 200) -> tuple[list[str], list[list], str]:
    """Run one SQL statement. Read-only on the real database; writes allowed on the practice one."""
    if write and not practice:
        raise PermissionError("Changes are only allowed on the practice database (add --practice). "
                              "The real one is changed through the app, so nothing breaks.")
    engine = db.database(practice_url(data_dir) if practice else data_dir)
    with engine.connect() as conn:
        raw = conn.connection.driver_connection if engine.dialect.name == "sqlite" and not practice else None
        passed: list[tuple[int, str]] = []
        if not write and FILE_MAKERS.match(sql):
            raise PermissionError(READ_ONLY_REFUSED)
        if not write:
            if engine.dialect.name == "sqlite":
                conn.exec_driver_sql("PRAGMA query_only = ON")
            else:
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                if not practice:  # no authorizer here: the query's plan is checked first
                    passed = _pg_guard(conn, sql)
        if raw is not None:
            raw.set_authorizer(_authorizer)
        try:
            if write or engine.dialect.name == "sqlite":  # SQLite refuses a second statement before running any
                result = conn.execute(text(sql))
                cols = list(result.keys()) if result.returns_rows else None
            else:  # PostgreSQL, read-only: one prepared statement, so nothing after a `;` ever runs
                try:
                    result = _pg_one(conn, sql)
                except Exception as exc:  # the practice database has no _pg_guard to say it first
                    if "multiple commands" not in str(exc):
                        raise
                    raise PermissionError(PG_ONE_QUERY) from None
                cols = [d.name for d in result.description] if result.description else None
            if cols is not None:
                if any(i >= len(cols) or cols[i] != name for i, name in passed):  # SELECT notes AS n
                    raise PermissionError(PG_REFUSED)
                rows = [_mask(cols, tuple(r)) for r in result.fetchmany(limit)]
                note = f"{len(rows)} row(s)" + (f" (showing the first {limit})" if len(rows) == limit else "")
            else:
                cols, rows, note = [], [], f"{result.rowcount} row(s) changed"
            if write:
                conn.commit()
            return cols, rows, note
        finally:
            if raw is not None:
                raw.set_authorizer(None)  # the connection goes back to the pool
            if not write and engine.dialect.name == "sqlite":
                conn.exec_driver_sql("PRAGMA query_only = OFF")


def format_table(cols: list[str], rows: list[list], width: int = 40) -> str:
    def cell(v) -> str:
        s = "NULL" if v is None else (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
        s = s.replace("\n", " ")
        return s if len(s) <= width else s[:width - 1] + "…"
    if not cols:
        return ""
    cells = [[cell(v) for v in r] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(cols)]
    line = "─┼─".join("─" * w for w in widths)
    out = [" │ ".join(c.ljust(w) for c, w in zip(cols, widths, strict=False)), line]
    out += [" │ ".join(v.ljust(w) for v, w in zip(r, widths, strict=False)) for r in cells]
    return "\n".join(out)


# --- looking around -----------------------------------------------------------------------
def tables(data_dir: Path, practice: bool = False) -> list[tuple[str, int, str]]:
    engine = db.database(practice_url(data_dir) if practice else data_dir)
    out = []
    with engine.connect() as conn:
        for name in db.metadata.tables:
            count = conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
            out.append((name, count, TABLE_NOTES.get(name, "")))
    return out


def schema(data_dir: Path, table: str, practice: bool = False) -> str:
    if table not in db.metadata.tables:
        raise KeyError(f"no table {table!r}. Tables: {', '.join(db.metadata.tables)}")
    engine = db.database(practice_url(data_dir) if practice else data_dir)
    insp = inspect(engine)
    pk = set(insp.get_pk_constraint(table).get("constrained_columns") or [])
    lines = [f"{table}: {TABLE_NOTES.get(table, '')}", ""]
    for c in insp.get_columns(table):
        flags = ["primary key"] if c["name"] in pk else []
        if not c.get("nullable", True) and c["name"] not in pk:
            flags.append("required")
        lines.append(f"  {c['name']:<16} {c['type']!s:<14} {', '.join(flags)}")
    idx = insp.get_indexes(table)
    if idx:
        lines += ["", "  indexes (make lookups by these columns fast):"]
        lines += [f"    {i['name']}: {', '.join(i['column_names'])}" for i in idx]
    return "\n".join(lines)


def backup(data_dir: Path) -> Path:
    """Copy the database safely, even while the server is running: SQLite's online backup, or
    pg_dump (a plain .sql file you can read and restore with psql) for PostgreSQL."""
    url = db.url_for(data_dir)
    dest_dir = Path(data_dir) / "backups"
    dest_dir.mkdir(exist_ok=True)
    if is_postgres(url):
        if not shutil.which("pg_dump"):
            raise RuntimeError("pg_dump isn't installed (sudo apt install postgresql-client)")
        u = make_url(url)
        dest = dest_dir / f"nextbox-{datetime.now():%Y%m%d-%H%M%S}.sql"
        env = {**os.environ, "PGPASSWORD": u.password or ""}  # never on the command line
        with open(dest, "w") as fh:
            r = subprocess.run(["pg_dump", "--no-owner", "-h", u.host or "127.0.0.1", "-p", str(u.port or 5432),
                                "-U", u.username or "", u.database or ""], stdout=fh, stderr=subprocess.PIPE,
                               text=True, env=env, timeout=600)
        if r.returncode != 0:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"pg_dump failed: {r.stderr.strip()[:300]}")
        dest.chmod(0o600)
        return dest
    src = Path(url.removeprefix("sqlite:///"))
    dest = dest_dir / f"nextbox-{datetime.now():%Y%m%d-%H%M%S}.db"
    with sqlite3.connect(src) as s, sqlite3.connect(dest) as d:
        s.backup(d)  # SQLite's online backup: a consistent copy without stopping anything
    dest.chmod(0o600)
    return dest


# --- the practice database ---------------------------------------------------------------
TASKS = ["Water the plants", "Call the dentist", "Pay water bill", "Pick up prescription",
         "Renew car registration", "Take out recycling", "Email the landlord", "Return library books",
         "Order printer labels", "Book haircut", "Clean gutters", "Buy birthday card", "Fix squeaky door",
         "Schedule car inspection", "Send invoice", "Plan weekend trip", "Defrost freezer", "Back up laptop"]


def build_practice(data_dir: Path, seed: int = 7) -> dict:
    """A separate, throwaway database with realistic fake data to learn SQL on."""
    from .auth import Accounts
    from .config import Settings
    from .events import ServerSettings
    from .feedback import Feedback
    from .printers import Printers
    from .store import Store

    rnd = random.Random(seed)
    pdir = practice_dir(data_dir)
    url = practice_url(data_dir)
    if pdir.exists():
        shutil.rmtree(pdir)
    pdir.mkdir(parents=True)
    if is_postgres(url):  # start clean: drop every table in the practice database, then rebuild
        db.metadata.drop_all(db.database(url))
    db.reset_engines()
    accounts, store = Accounts(url), Store(pdir, url=url)
    printers, feedback = Printers(url, Settings(data_dir=pdir)), Feedback(url)
    ServerSettings(url).update({"announcement": "Welcome to the practice database!"})

    homes = {"The Rivera house": ["ana", "luis", "sofia"], "Chen family": ["mei", "wen"],
             "Pat's apartment": ["pat"], "Office crew": ["jordan", "sam", "taylor"]}
    people = []
    first = True
    for home, names in homes.items():
        hid = accounts.create_household(home)
        for i, name in enumerate(names):
            role = "superadmin" if first else ("admin" if i == 0 else "user")
            first = False
            accounts.create(name, "practice-password", role=role, household_id=hid,
                            email=f"{name}@example.com", beta=rnd.random() < 0.3)
            people.append((name, hid))
        printers.add({"name": "Kitchen", "driver": "escpos", "address": "192.168.1.60"}, hid)
        if rnd.random() < 0.6:
            printers.add({"name": "Red roll", "driver": "zpl", "address": "192.168.1.70", "stock_color": "red"}, hid)
        accounts.create_invite(hid, names[0])

    now = datetime.now()
    kinds = [("today", "today"), ("today", "overdue"), ("list", "list"), ("task", "task"), ("text", "note")]
    for name, hid in people:
        for day in range(30):
            if rnd.random() < 0.55:
                continue
            when = (now - timedelta(days=day, hours=rnd.randint(0, 10), minutes=rnd.randint(0, 59)))
            kind, reason = rnd.choice(kinds)
            label_id = store.new_id(when.date())
            picked = rnd.sample(TASKS, rnd.randint(1, 6)) if kind != "text" else []
            manifest = [{"row": n + 1, "task_id": f"t{rnd.randint(1000, 9999)}", "content": t,
                         "due_date": (when.date() - timedelta(days=rnd.randint(0, 4))).isoformat(),
                         "is_recurring": rnd.random() < 0.2} for n, t in enumerate(picked)]
            record = {"id": label_id, "kind": kind, "reason": reason, "title": kind.title(),
                      "source": rnd.choice(["manual", "manual", "auto"]), "dry_run": rnd.random() < 0.3,
                      "by": name, "household_id": hid,
                      "printer": {"id": "p", "name": "Kitchen", "color": "white"},
                      "created_at": when.isoformat(timespec="seconds"), "png": "", "manifest": manifest}
            store.save_printed(record)
            if manifest and rnd.random() < 0.35:
                applied = [{**m, "mark": rnd.choice(["done", "tomorrow"]), "confidence": round(rnd.uniform(.7, .99), 2)}
                           for m in manifest[:rnd.randint(0, len(manifest))]]
                store.save_scan({"id": store.new_id(when.date()), "by": name, "household_id": hid,
                                 "created_at": (when + timedelta(hours=3)).isoformat(timespec="seconds"),
                                 "label_id": label_id, "applied": applied, "needs_confirmation": [],
                                 "ignored": [], "errors": []})
    with db.database(url).begin() as conn:  # spread the activity log over the month too
        for name, hid in people:
            for _ in range(rnd.randint(10, 40)):
                ts = time.time() - rnd.uniform(0, 30 * 86400)
                action = rnd.choice(["login", "print_today_manual", "print_list", "scan", "settings", "connect"])
                ok = rnd.random() > 0.08
                conn.execute(db.events.insert().values(
                    ts=ts, user=name, household_id=hid, kind="auth" if action == "login" else ("activity" if ok else "error"),
                    action=action, ok=ok, detail={"ms": rnd.randint(40, 900)},
                    error=None if ok else "Todoist didn't answer"))
    from .mylist import LocalTasks, Tags
    for name, hid in people:  # everyone's built-in list, with tags
        Tags(url, name).all()
        mine = LocalTasks(url, name, hid)
        for task in rnd.sample(TASKS, rnd.randint(3, 8)):
            due = (datetime.now() + timedelta(days=rnd.randint(-4, 6))).date().isoformat() if rnd.random() < 0.75 else None
            t = mine.create({"content": task, "due_date": due, "priority": rnd.choice([1, 1, 1, 3, 4]),
                             "tags": rnd.sample(["urgent", "errand", "call", "home", "work"], rnd.randint(0, 2)),
                             "minutes": rnd.choice([None, 5, 15, 30, 60]),
                             "steps": ["Get started", "Finish it"] if rnd.random() < 0.3 else []})
            if rnd.random() < 0.25:
                mine.close_task(t["id"])
    for msg in ["Love the red labels for overdue stuff!", "Scanning missed a checkmark on row 3",
                "Can it print my grocery list sorted by aisle?", "The setup screen was confusing"]:
        feedback.submit(mode="open", kind="other", page="print", rating=rnd.randint(2, 5), fields={},
                        message=msg, who=rnd.choice(people)[0], ip="192.168.1.20", agent="Practice phone",
                        names=[], enforce_limit=False)
    counts = {name: count for name, count, _ in tables(data_dir, practice=True)}
    db.reset_engines()
    return counts


# --- moving to another database ----------------------------------------------------------------
def copy_database(source_url: str, target_url: str, batch: int = 500) -> dict:
    """Copy every row from one database to another (e.g. the SQLite file into a new, empty
    PostgreSQL database). The target must be empty, so nothing is ever overwritten."""
    src, dst = db.database(source_url), db.database(target_url)
    with dst.connect() as conn:
        busy = [t.name for t in db.metadata.sorted_tables
                if conn.execute(select(func.count()).select_from(t)).scalar()]
    if busy:
        raise RuntimeError(f"the target database isn't empty (it has {', '.join(busy)}); "
                           "nothing was copied")
    counts = {}
    with src.connect() as s_conn, dst.begin() as d_conn:  # all or nothing
        for table in db.metadata.sorted_tables:
            n = 0
            result = s_conn.execute(select(table)).mappings()
            while rows := result.fetchmany(batch):
                d_conn.execute(insert(table), [dict(r) for r in rows])
                n += len(rows)
            counts[table.name] = n
        if dst.dialect.name == "postgresql":
            # Rows came with their ids, so move each id counter past the highest id copied.
            for table in db.metadata.sorted_tables:
                pk = list(table.primary_key.columns)
                if len(pk) == 1 and isinstance(pk[0].type, db.Integer):
                    col = pk[0].name  # setval does nothing when the column has no counter (NULL)
                    d_conn.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('\"{table.name}\"', '{col}'), "
                        f"COALESCE((SELECT MAX(\"{col}\") FROM \"{table.name}\"), 0) + 1, false)"))
    return counts
