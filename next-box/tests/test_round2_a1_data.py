"""Round 2, batch A1 (server-side data integrity): db sql on PostgreSQL, invites that still named deleted
accounts, Done from a device that still shows a repeat's old day, the starter tags seeded twice at once,
CalDAV moves of a DUE stored in another zone, and the join messages.

The browser half of the stale-device Done (every Done sends the day it showed, and a Done that changed
nothing says so instead of "Done ✓") was checked in a real browser; the last tests pin it. db sql's
one-statement guard holds whatever psycopg's prepare_threshold is (None for PgBouncer)."""
import dataclasses
import datetime as dt
import os
import threading
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, inspect, select, text

from nextbox import auth, cli, db, dbtools, server, upkeep
from nextbox.auth import Accounts
from nextbox.journal import Journal, Slides
from nextbox.mylist import DEFAULT_TAGS, LocalTasks, Tags
from nextbox.providers import CalDAVTasks
from nextbox.server import create_app
from nextbox.store import Store

from conftest import rows
from test_providers import cal, radicale  # noqa: F401  (the in-process Radicale server)

WEB = Path(server.WEB_DIR)
FRI = date(2026, 10, 16)
POSTGRES = bool(os.environ.get("DATABASE_URL"))


def site(ctx, today=date.today):
    """boss (superadmin), hal (admin) and sam, My list only."""
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("hal", "hal password", role="admin")
    a.create("sam", "sam password", email="sam@example.com")
    app = create_app(ctx)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, app, login


# ------------------------------------------------------------------ R1-01: db sql runs one statement
SNEAKY = ("SELECT 1; COMMIT; UPDATE users SET role = 'superadmin'",
          "SELECT 1; COMMIT; CREATE TABLE leak AS SELECT notes AS n FROM journal",
          "SELECT 1; COMMIT; DELETE FROM sessions",
          "EXPLAIN SELECT 1; COMMIT; UPDATE users SET role = 'superadmin'",
          "SELECT 1; SELECT notes AS n FROM journal")


def _state(data_dir):
    with db.database(data_dir).connect() as conn:
        return (sorted(conn.execute(text("SELECT username, role FROM users")).all()),
                conn.execute(text("SELECT COUNT(*) FROM sessions")).scalar(),
                "leak" in inspect(conn).get_table_names())


def test_db_sql_never_runs_a_second_statement(tmp_path, monkeypatch, capsys):
    """On PostgreSQL a plain query string runs every statement in it: COMMIT ended the read-only
    transaction and the UPDATE after it was committed before the check saw the `;`. SQLite always
    refused a second statement before running any."""
    a = Accounts(tmp_path)
    a.create("sam", "sam password")
    a.create_session("sam")
    Journal(db.database(tmp_path), "sam").add(FRI.isoformat(), "PGSECRETNOTE saw my therapist")
    before = _state(tmp_path)
    assert before[1] == 1 and not before[2]
    for sql in SNEAKY:
        with pytest.raises(PermissionError if POSTGRES else Exception) as caught:
            dbtools.run_sql(tmp_path, sql)
        if POSTGRES:
            assert "one query at a time" in str(caught.value), sql
        assert _state(tmp_path) == before, sql
    # The CLI says "refused" and nothing changed.
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(tmp_path / "none.env"))
    assert cli.main(["db", "sql", SNEAKY[0]]) != 0
    assert _state(tmp_path) == before and "PGSECRET" not in capsys.readouterr().out
    # One statement still works, a trailing ; and a % included.
    assert dbtools.run_sql(tmp_path, "SELECT username FROM users;")[1] == [["sam"]]
    assert dbtools.run_sql(tmp_path, "SELECT COUNT(*) FROM users WHERE username LIKE 's%'")[1] == [[1]]
    assert dbtools.run_sql(tmp_path, "SELECT notes FROM journal")[1] == [["•••• (hidden)"]]
    with pytest.raises(Exception):
        dbtools.run_sql(tmp_path, "UPDATE users SET role = 'superadmin'")  # read-only, as before
    assert _state(tmp_path) == before


@pytest.fixture
def psycopg_set_to(monkeypatch):
    """Connections opened from here on use this prepare_threshold (None is the usual PgBouncer
    setting), with or without pipelines (libpq before 14 has none)."""
    hooked = []

    def use(engine, threshold, pipeline=True):
        import psycopg
        monkeypatch.setattr(psycopg.Pipeline, "is_supported", classmethod(lambda cls: pipeline))

        def _connect(dbapi, _record):
            dbapi.prepare_threshold = threshold
        event.listen(engine, "connect", _connect)
        hooked.append((engine, _connect))
        engine.dispose()  # pooled connections were opened without it
    yield use
    for engine, hook in hooked:
        event.remove(engine, "connect", hook)
        engine.dispose()
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS leak"))


@pytest.mark.skipif(not POSTGRES, reason="PostgreSQL only")
@pytest.mark.parametrize("threshold, pipeline", [(None, True), (None, False), (5, False), (0, True)])
def test_db_sql_runs_one_statement_whatever_psycopg_is_set_to(tmp_path, psycopg_set_to, threshold, pipeline):
    """psycopg ignores prepare=True when the connection's prepare_threshold is None and sends the
    simple protocol: the guard's EXPLAIN of `SELECT 1; COMMIT; CREATE TABLE leak ...` ran all of it.
    A pipeline always sends the extended protocol; without one (and without preparing) nothing is sent."""
    a = Accounts(tmp_path)
    a.create("sam", "sam password")
    a.create_session("sam")
    Journal(db.database(tmp_path), "sam").add(FRI.isoformat(), "PGSECRETNOTE saw my therapist")
    before = _state(tmp_path)
    psycopg_set_to(db.database(tmp_path), threshold, pipeline)
    for sql in SNEAKY + ("SELECT 1; COMMIT; CREATE TABLE leak AS SELECT 1 AS x",):
        with pytest.raises(PermissionError, match="one query at a time"):
            dbtools.run_sql(tmp_path, sql)
        assert _state(tmp_path) == before, sql
    if threshold is None and not pipeline:  # no safe way to send it: refused, not run
        with pytest.raises(PermissionError, match="one query at a time"):
            dbtools.run_sql(tmp_path, "SELECT username FROM users")
        return
    # One statement works as before: text results (pg_typeof reads "integer", not bytes), a %, masking.
    assert dbtools.run_sql(tmp_path, "SELECT pg_typeof(1) AS t, 'a%' LIKE 'a%' AS l")[1] == [["integer", True]]
    assert dbtools.run_sql(tmp_path, "SELECT username FROM users WHERE username LIKE 's%';")[1] == [["sam"]]
    assert dbtools.run_sql(tmp_path, "SELECT notes FROM journal")[1] == [["•••• (hidden)"]]
    with pytest.raises(PermissionError):
        dbtools.run_sql(tmp_path, "SELECT notes AS n FROM journal")
    assert _state(tmp_path) == before


@pytest.mark.skipif(not POSTGRES, reason="PostgreSQL only")
def test_db_sql_practice_runs_one_statement_with_preparing_off(tmp_path, psycopg_set_to):
    data_dir = tmp_path / "d"
    dbtools.build_practice(data_dir)
    url = dbtools.practice_url(data_dir)
    count = dbtools.run_sql(url, "SELECT COUNT(*) FROM users")[1]
    psycopg_set_to(db.database(url), None)
    with pytest.raises(PermissionError, match="one query at a time"):
        dbtools.run_sql(data_dir, "SELECT 1; COMMIT; DELETE FROM users", practice=True)
    assert dbtools.run_sql(data_dir, "SELECT COUNT(*) FROM users", practice=True)[1] == count
    assert dbtools.run_sql(data_dir, "SHOW TimeZone", practice=True)[0] == ["TimeZone"]


@pytest.mark.skipif(not POSTGRES, reason="PostgreSQL only")
def test_db_sql_practice_without_write_runs_one_statement_too(tmp_path):
    data_dir = tmp_path / "d"
    dbtools.build_practice(data_dir)
    url = dbtools.practice_url(data_dir)
    count = dbtools.run_sql(url, "SELECT COUNT(*) FROM users")[1]
    with pytest.raises(PermissionError, match="one query at a time"):
        dbtools.run_sql(data_dir, "SELECT 1; COMMIT; DELETE FROM users", practice=True)
    assert dbtools.run_sql(url, "SELECT COUNT(*) FROM users")[1] == count
    # --write is the sandbox: changes are allowed there, as before.
    dbtools.run_sql(data_dir, "DELETE FROM events", practice=True, write=True)
    assert dbtools.run_sql(url, "SELECT COUNT(*) FROM events")[1] == [[0]]


# ------------------------------------------------------------------ R1-02: invites stop naming deleted accounts
def test_a_deleted_admin_is_no_longer_named_on_the_invites_it_made(ctx):
    ctx, a, app, login = site(ctx)
    hal = login("hal", "hal password")
    code = hal.post("/admin/invites", json={"note": "for Kim"}).json()["code"]
    assert hal.post("/auth/delete-account", json={"password": "hal password", "confirm": "hal"}).status_code == 200
    boss = login("boss", "boss password")
    listed = {i["code"]: i for i in boss.get("/admin/invites").json()}
    assert listed[code]["created_by"] == upkeep.DELETED_ACCOUNT and listed[code]["status"] == "open"
    assert "hal" not in str(rows(ctx.settings.data_dir, "invites"))


def test_invites_from_before_the_upgrade_stop_naming_deleted_accounts(tmp_path):
    """Accounts deleted by an older version: their names stayed on invites (used_by before 0.14.3,
    created_by always). A one-time startup fix renames them; real people, the CLI and HA stay."""
    a = Accounts(tmp_path)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    engine = db.database(tmp_path)
    codes = {who: a.create_invite("home", by) for who, by in
             (("zed", "boss"), ("sam", "boss"), ("hal", "hal"), ("cli", "cli"), ("open", "boss"))}
    used = {codes["zed"]: "zed", codes["sam"]: "sam", codes["hal"]: "kim"}
    with engine.begin() as conn:
        for code, name in used.items():
            conn.execute(db.invites.update().where(db.invites.c.code == code).values(used_by=name))
    upkeep.run_startup_fixes(engine, Store(tmp_path))
    got = {r["code"]: (r["created_by"], r["used_by"]) for r in rows(tmp_path, "invites")}
    gone = upkeep.DELETED_ACCOUNT
    assert got == {codes["zed"]: ("boss", gone), codes["sam"]: ("boss", "sam"), codes["hal"]: (gone, gone),
                   codes["cli"]: ("cli", None), codes["open"]: ("boss", None)}
    with engine.connect() as conn:
        flags = {r[0] for r in conn.execute(select(db.server_settings.c.key))}
    assert "_upkeep:invites-unnamed-v1" in flags
    assert upkeep.unname_invites(engine) == 0  # nothing left to rename


# ------------------------------------------------------------------ W4-02, W5-01: Done from a stale device
def _wins(c):
    return c.get("/journal/wins", params={"days": 1}).json()["total"]


def test_done_from_a_device_that_still_shows_the_old_day_counts_once(ctx):
    ctx, a, app, login = site(ctx, today=lambda: FRI)
    phone, laptop = login("sam", "sam password"), login("sam", "sam password")
    water = LocalTasks(a.db, "sam", today=lambda: FRI).create(
        {"content": "Water the plants", "due_date": FRI.isoformat(), "repeat": "daily"})
    shown = FRI.isoformat()  # both screens show it due today
    r = phone.post(f"/list/{water['id']}/done", json={"due": shown}).json()
    assert r["undo"]["prev_due"] == shown
    stale = laptop.post(f"/list/{water['id']}/done", json={"due": shown})
    assert stale.status_code == 200 and stale.json()["undo"] is None  # done already: nothing to undo
    task = laptop.get("/list").json()["tasks"][0]
    assert task["due"]["date"] == "2026-10-17" and _wins(laptop) == 1  # tomorrow kept, one win
    # The screen shows tomorrow now: ticking it early still moves it on, as before.
    assert laptop.post(f"/list/{water['id']}/done", json={"due": "2026-10-17"}).json()["undo"]["prev_due"] == "2026-10-17"
    assert laptop.get("/list").json()["tasks"][0]["due"]["date"] == "2026-10-18" and _wins(laptop) == 2
    # No day sent (the CLI, Home Assistant, an older app): as before.
    assert laptop.post(f"/list/{water['id']}/done").json()["undo"]["prev_due"] == "2026-10-18"
    assert laptop.post(f"/list/{water['id']}/done", json={"due": "16 Oct"}).status_code == 400
    # A one-off ticked on both devices was already fine, and still is.
    once = LocalTasks(a.db, "sam", today=lambda: FRI).create({"content": "Post the letter", "due_date": shown})
    assert phone.post(f"/list/{once['id']}/done", json={"due": shown}).json()["undo"] is not None
    assert laptop.post(f"/list/{once['id']}/done", json={"due": shown}).json()["undo"] is None
    # Moved to tomorrow on the phone (a slide), then Done on the laptop that still shows today: nothing
    # changes, and the slide it counted stays.
    stretch = LocalTasks(a.db, "sam", today=lambda: FRI).create(
        {"content": "Stretch", "due_date": shown, "repeat": "daily"})
    assert phone.post(f"/list/{stretch['id']}/move", json={"to": "tomorrow"}).json()["moved"]
    wins = _wins(laptop)
    assert laptop.post(f"/list/{stretch['id']}/done", json={"due": shown}).json() == {"ok": True, "undo": None,
                                                                                       "stale": True}
    assert Slides(a.db, "sam").counts().get(stretch["id"]) == 1 and _wins(laptop) == wins
    assert LocalTasks(a.db, "sam").get_task(stretch["id"])["due"]["date"] == "2026-10-17"


def test_six_dones_at_once_on_a_repeat_move_it_one_day(ctx):
    ctx, a, app, login = site(ctx, today=lambda: FRI)
    feed = LocalTasks(a.db, "sam", today=lambda: FRI).create(
        {"content": "Feed the cat", "due_date": FRI.isoformat(), "repeat": "daily"})
    devices = [login("sam", "sam password") for _ in range(6)]
    start, got = threading.Barrier(6), []

    def tap(c):
        start.wait()
        got.append(c.post(f"/list/{feed['id']}/done", json={"due": FRI.isoformat()}).json())
    taps = [threading.Thread(target=tap, args=(c,)) for c in devices]
    [t.start() for t in taps]
    [t.join() for t in taps]
    assert sum(1 for r in got if r["undo"]) == 1
    assert devices[0].get("/list").json()["tasks"][0]["due"]["date"] == "2026-10-17" and _wins(devices[0]) == 1


def test_close_task_checks_the_day_it_was_shown(tmp_path):
    mine = LocalTasks(tmp_path, "sam", today=lambda: FRI)
    bins = mine.create({"content": "Bins", "due_date": "2026-10-13", "repeat": "weekly"})  # overdue, a Tuesday
    assert mine.close_task(bins["id"], seen_due="2026-10-12") is None  # not the day it's on: not done
    assert mine.get_task(bins["id"])["due"]["date"] == "2026-10-13"
    assert mine.close_task(bins["id"], seen_due="2026-10-13T09:00:00")["prev_due"] == "2026-10-13"
    assert mine.get_task(bins["id"])["due"]["date"] == "2026-10-20"
    with pytest.raises(ValueError, match="2026-10-16"):
        mine.close_task(bins["id"], seen_due="Tuesday")


def test_a_done_that_changed_nothing_says_the_screen_was_out_of_date(ctx):
    """The reply was {undo: None} whether the Done was a repeat ticked twice or a screen out of date,
    so the app said "Nice. ✓" for a chore nobody did. `stale` tells it nothing was done here."""
    ctx, a, app, login = site(ctx, today=lambda: FRI)
    phone, laptop = login("sam", "sam password"), login("sam", "sam password")
    mine = LocalTasks(a.db, "sam", today=lambda: FRI)
    water = mine.create({"content": "Water the plants", "due_date": FRI.isoformat(), "repeat": "daily"})
    done = lambda c, t, due: c.post(f"/list/{t['id']}/done", json={"due": due}).json()  # noqa: E731
    # → Tomorrow on the phone, the laptop loads it (tomorrow), then the phone puts it back on today.
    assert phone.post(f"/list/{water['id']}/move", json={"to": "tomorrow"}).json()["moved"]
    assert phone.post(f"/list/{water['id']}/move", json={"to": "today"}).json()["moved"]
    assert done(laptop, water, "2026-10-17") == {"ok": True, "undo": None, "stale": True}
    assert mine.get_task(water["id"])["due"]["date"] == FRI.isoformat() and _wins(laptop) == 0  # still to do
    # A skip (→ Tomorrow) on the phone, Done on the laptop that still shows today: the skip stays.
    assert phone.post(f"/list/{water['id']}/move", json={"to": "tomorrow"}).json()["moved"]
    assert done(laptop, water, FRI.isoformat())["stale"] is True
    assert mine.get_task(water["id"])["due"]["date"] == "2026-10-17" and _wins(laptop) == 0
    # A one-off finished on the phone.
    letter = mine.create({"content": "Post the letter", "due_date": FRI.isoformat()})
    assert "stale" not in done(phone, letter, FRI.isoformat())
    assert done(laptop, letter, FRI.isoformat())["stale"] is True and _wins(laptop) == 1
    # A Done that did something isn't stale: the screen showed tomorrow, and it's done early.
    r = done(laptop, water, "2026-10-17")
    assert "stale" not in r and r["undo"]["prev_due"] == "2026-10-17" and _wins(laptop) == 2


# ------------------------------------------------------------------ W5-03: starter tags seeded once
def _seed_from_another_request(engine, owner):
    with engine.begin() as other:
        for i, (name, color) in enumerate(DEFAULT_TAGS):
            other.execute(insert(db.tags).values(owner=owner, name=name, color=color, position=i))
        other.execute(insert(db.server_settings).values(key=f"_tags_seeded:{owner}", value=True))


@pytest.mark.parametrize("moment", ["INSERT INTO tags", "FROM server_settings"])
def test_starter_tags_seeded_by_another_request_at_the_same_moment(tmp_path, moment):
    """Another request (a second tab, Home Assistant) seeds the starter tags just before this one
    inserts them (it used to fail with a duplicate key: a 500), or just after this one found none
    (it used to answer with no tags at all)."""
    Accounts(tmp_path).create("sam", "sam password")
    engine = db.database(tmp_path)
    fired = []

    def meanwhile(conn, cursor, statement, params, context, executemany):
        if not fired and moment in statement:
            fired.append(statement)
            _seed_from_another_request(engine, "sam")
    event.listen(engine, "before_cursor_execute", meanwhile)
    try:
        got = Tags(engine, "sam").all()
    finally:
        event.remove(engine, "before_cursor_execute", meanwhile)
    assert fired and [t["name"] for t in got] == [n for n, _ in DEFAULT_TAGS]
    assert len(rows(tmp_path, "tags")) == len(DEFAULT_TAGS)


def test_first_requests_at_once_all_answer(ctx):
    """Home Assistant's burst before the owner ever opened the app, and a new person's tabs."""
    ctx, a, app, login = site(ctx)
    tabs = [login("sam", "sam password") for _ in range(8)]
    start, codes = threading.Barrier(18), []

    def ha():
        c = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)
        start.wait()
        codes.append(c.post("/print/today", json={"source": "auto"}, headers={"X-NextBox-Key": "k"}).status_code)

    def tab(c):
        start.wait()
        codes.append(c.get("/list").status_code)
    work = [threading.Thread(target=ha) for _ in range(10)] + [threading.Thread(target=tab, args=(c,)) for c in tabs]
    [t.start() for t in work]
    [t.join() for t in work]
    assert codes == [200] * 18
    assert len(Tags(a.db, "boss").all()) == len(Tags(a.db, "sam").all()) == len(DEFAULT_TAGS)


# ------------------------------------------------------------------ W4-01: CalDAV moves keep the day picked
@pytest.fixture
def zone(monkeypatch):
    def use(name):
        monkeypatch.setenv("TZ", name)
        time.tzset()
    yield use
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("where", ["America/New_York", "Asia/Tokyo"])
def test_caldav_move_of_a_due_stored_in_utc_lands_on_the_day_picked(cal, zone, where):  # noqa: F811
    """Many apps store DUE in UTC ("...Z"). The day is read in local time, so the move must be too:
    22:00 in New York is 02:00Z the next day, and 08:00 in Tokyo is 23:00Z the day before."""
    zone(where)
    url, user, chores = cal
    today = dt.date.today()
    tomorrow = today + dt.timedelta(days=1)
    uid = lambda t: str(t.icalendar_component["uid"])  # noqa: E731
    made = {}
    for hhmm in ("22:00", "08:00", "00:30", "23:30"):
        local = dt.datetime.combine(today, dt.time.fromisoformat(hhmm)).astimezone()
        made[hhmm] = uid(chores.save_todo(summary=f"At {hhmm}", due=local.astimezone(dt.timezone.utc)))
    p = CalDAVTasks(url, user, "pw")
    for hhmm, task_id in made.items():
        assert p.get_task(task_id)["due"]["date"] == f"{today}T{hhmm}:00"
        p.set_due_date(task_id, tomorrow.isoformat())  # → Tomorrow
        assert p.get_task(task_id)["due"]["date"] == f"{tomorrow}T{hhmm}:00", hhmm
        assert p._find(task_id).icalendar_component.get("due").to_ical().decode().endswith("Z")  # still UTC
        p.set_due_date(task_id, today.isoformat())  # its Undo
        assert p.get_task(task_id)["due"]["date"] == f"{today}T{hhmm}:00", hhmm


# ------------------------------------------------------------------ R1-05: join messages are sentences
def test_join_messages_are_sentences(ctx):
    ctx, a, app, login = site(ctx)
    code = a.create_invite("home", "boss")
    c = TestClient(app, base_url="http://testserver")
    r = c.post("/auth/signup", json={"username": "kim", "password": "kim password", "invite": code, "agree": True,
                                     "email": "sam@example.com"})
    assert r.status_code == 400 and r.json()["detail"] == auth.EMAIL_TAKEN and auth.EMAIL_TAKEN[0].isupper()
    r = c.post("/auth/signup", json={"username": "kim", "password": "kim password", "invite": "   ", "agree": True})
    assert r.status_code == 400 and r.json()["detail"] == "Type the invite code you were given."
    hal = login("hal", "hal password")
    r = hal.patch("/auth/profile", json={"email": "sam@example.com"})
    assert r.status_code == 400 and r.json()["detail"] == auth.EMAIL_TAKEN
    with pytest.raises(auth.AuthError, match="^That email already has an account. Use a different one.$"):
        a.create("kim", "kim password", email="sam@example.com")
    r = c.post("/auth/signup", json={"username": "kim", "password": "kim password", "invite": code, "agree": True})
    assert r.status_code == 200  # the code still works after the taken email


# ------------------------------------------------------------------ browser half, pinned
def test_every_done_sends_the_day_it_showed():
    js = (WEB / "app.js").read_text()
    sends = [line for line in js.splitlines() if "/done`, {" in line and "/list/" in line]
    assert len(sends) == 4, sends  # a row's ✓ (and Just one thing), bulk Done, the timer's Done, a reminder's Done
    assert all("json: { due: t" in line and "due?.date?.slice(0, 10) }" in line for line in sends), sends


def test_a_done_that_changed_nothing_never_says_done():
    """Checked in a real browser (row ✓, bulk Done, the timer's Done, a reminder's Done): a stale
    Done says DONE_STALE and draws the list again, and bulk Done counts only the ones done."""
    js = (WEB / "app.js").read_text()
    assert "const DONE_STALE = \"That changed on another device. Here's your list now.\";" in js
    lines = js.splitlines()
    sends = [i for i, line in enumerate(lines) if "/done`, {" in line and "/list/" in line]
    assert len(sends) == 4
    for i in sends:
        after = "\n".join(lines[i + 1:i + 6])
        assert "r.stale" in after and ("DONE_STALE" in after or "changed on another device" in after), after
    assert "snack(`Done ${done.length} ✓${stale ? ` · ${stale} changed on another device` : \"\"}" in js
    assert "Done ${ok.length}" not in js
    for done_path in ("toast(DONE_STALE); await loadList(); return;", "timerTell(DONE_STALE); await loadList(); return;",
                      "if (r.stale) remindNote(DONE_STALE, null, id);"):
        assert done_path in js, done_path
