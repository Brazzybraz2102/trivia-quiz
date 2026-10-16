"""Review batch RB: My list data integrity. A deleted task's id is never handed out again; moving one
day of a repeat keeps the day it belongs on; monday.com moves keep the local time; quick step ticks
are never lost; Undo puts slide counts back; taking #urgent off takes back what it gave; a failed
save leaves no tags; the setup questions name only what changed; one linked-app fetch per My list;
older unnamed wins aren't pinned on a linked app; tag bands in Easy-to-read text aren't capitals."""
import contextlib
import dataclasses
import json
import os
import threading
import time
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update
from sqlalchemy.schema import CreateTable

from conftest import FakeTodoist, rows, tiny_jpeg
from nextbox import coach, db, journal, paper, render, upkeep
from nextbox.auth import Accounts
from nextbox.journal import Slides, Wins
from nextbox.mylist import LocalTasks, Merged, next_due, repeat_anchor
from nextbox.providers.base import ProviderError
from nextbox.providers.monday import Monday
from nextbox.render import Sticker, reading_font, render_sticker
from nextbox.scan import NEWER_TASK, ReadBack, RowMark
from nextbox.server import WEB_DIR, create_app
from nextbox.store import Store

FRI = date(2026, 10, 16)
SQLITE = not os.environ.get("DATABASE_URL")


class Clock:
    """A today() the test can move on."""

    def __init__(self, day: date):
        self.day = day

    def __call__(self) -> date:
        return self.day


def site(ctx, linked=None, today=date.today):
    """boss and sam. Sam has My list (on `today`), plus `linked` as a linked app when given; `seen`
    is what the fake camera reads from the next photo."""
    a = Accounts(ctx.settings.data_dir)
    ctx = dataclasses.replace(ctx, tasks_factory=lambda: Merged(LocalTasks(a.db, "sam", today=today), linked),
                              today=today)
    ctx.settings.todoist_token = ""
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    seen: dict = {}

    def vision(image, media_type, manifest):
        return ReadBack(label_code=None, rows=[RowMark(**r) for r in seen.get("rows", [])], notes="", win="")
    app = create_app(ctx, vision=vision)

    def login(name="sam", pw="sam password"):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, login, seen


def add(c, **fields):
    r = c.post("/list", json=fields)
    assert r.status_code == 200, r.text
    return r.json()


def get(c, task_id):
    return next(t for t in c.get("/list?include_done=true").json()["tasks"] if t["id"] == task_id)


def due(task) -> str:
    return ((task.get("due") or {}).get("date") or "")[:10]


def snap(c, label_id):
    return c.post("/scan", data={"label_id": label_id}, files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")}).json()


def row_of(label, content):
    return next(m["row"] for m in label["manifest"] if m["content"] == content)


def reuse_id(a, task_id: str, content: str, created: int) -> None:
    """What SQLite did before 0.14.1: a new task got the id of the newest one deleted."""
    with a.db.begin() as conn:
        conn.execute(insert(db.tasks).values(id=int(task_id[3:]), owner="sam", household_id="home", content=content,
                                             description="", priority=1, due_date=date.today().isoformat(),
                                             tags=[], steps=[], created=created))


# --- R-B01: a deleted task's id is never handed out again -----------------------------------------------
def test_a_deleted_tasks_id_and_slides_are_never_handed_to_a_new_task(ctx):
    ctx, a, login, _ = site(ctx, today=Clock(FRI))
    sam = login()
    add(sam, content="Older task")
    newest = add(sam, content="Typo task", due_date="2026-10-16")
    for day in ("2026-10-17", "2026-10-18", "2026-10-19"):  # slid 3 times
        assert sam.post(f"/list/{newest['id']}/move", json={"to": day}).json()["slid"]
    assert Slides(a.db, "sam").counts()[newest["id"]] == 3
    assert sam.delete(f"/list/{newest['id']}?confirm=true").status_code == 200
    assert newest["id"] not in Slides(a.db, "sam").counts()  # its count went with it
    fresh = add(sam, content="Brand new task", due_date="2026-10-16")
    assert fresh["id"] != newest["id"] and int(fresh["id"][3:]) > int(newest["id"][3:])
    assert fresh["id"] not in sam.get("/list").json()["slides"]
    assert fresh["created"] and "created" in get(sam, fresh["id"])


def test_a_stale_ticket_row_never_finishes_or_deletes_a_newer_task(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    keep = add(sam, content="Water the plants", due_date=date.today().isoformat())
    typo = add(sam, content="Typo task", due_date=date.today().isoformat())
    label = sam.post("/print/today", json={"dry_run": True}).json()
    printed = {m["task_id"]: m for m in label["manifest"]}
    assert printed[typo["id"]]["created"] == typo["created"]  # each My list row says when its task was made
    assert sam.delete(f"/list/{typo['id']}?confirm=true").status_code == 200
    reuse_id(a, typo["id"], "Pay the electricity bill", typo["created"] + 60)
    seen["rows"] = [{"row": row_of(label, "Typo task"), "mark": "done", "confidence": 0.95},
                    {"row": row_of(label, "Water the plants"), "mark": "done", "confidence": 0.95}]
    rec = snap(sam, label["id"])
    assert [x["task_id"] for x in rec["applied"]] == [keep["id"]]
    assert {"row": row_of(label, "Typo task"), "mark": "done", "reason": NEWER_TASK,
            "content": "Typo task"} in rec["ignored"]
    bill = get(sam, typo["id"])
    assert bill["content"] == "Pay the electricity bill" and not bill["done"]
    assert [w["content"] for w in Wins(a.db, "sam").between("2000-01-01", "2100-01-01")] == ["Water the plants"]
    # A ✗ always waits for a yes, and even then it doesn't delete the newer task.
    seen["rows"] = [{"row": row_of(label, "Typo task"), "mark": "drop", "confidence": 0.99}]
    rec = snap(sam, label["id"])
    pending = rec["needs_confirmation"][0]
    assert pending["status"] == "pending" and pending["task_id"] == typo["id"]
    rec = sam.post(f"/scan/{rec['id']}/confirm", json={"decisions": {str(pending["row"]): "confirm"}}).json()
    assert rec["needs_confirmation"][0]["status"] == "already" and rec["needs_confirmation"][0]["reason"] == NEWER_TASK
    assert get(sam, typo["id"])["content"] == "Pay the electricity bill"


def test_an_older_ticket_without_task_times_goes_by_when_it_printed(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    keep = add(sam, content="Water the plants", due_date=date.today().isoformat())
    typo = add(sam, content="Typo task", due_date=date.today().isoformat())
    label = sam.post("/print/today", json={"dry_run": True}).json()
    rec = ctx.store.get_printed(label["id"])
    an_hour_ago = datetime.now() - timedelta(hours=1)
    ctx.store.save_printed({**rec, "created_at": an_hour_ago.isoformat(timespec="seconds"),  # as 0.14.0 printed it
                            "manifest": [{k: v for k, v in m.items() if k != "created"} for m in rec["manifest"]]})
    with a.db.begin() as conn:  # the plants were on the list before it printed
        conn.execute(update(db.tasks).where(db.tasks.c.id == int(keep["id"][3:]))
                     .values(created=int(an_hour_ago.timestamp()) - 3600))
    sam.delete(f"/list/{typo['id']}?confirm=true")
    reuse_id(a, typo["id"], "Pay the electricity bill", int(time.time()))
    seen["rows"] = [{"row": row_of(label, "Typo task"), "mark": "done", "confidence": 0.95},
                    {"row": row_of(label, "Water the plants"), "mark": "tomorrow", "confidence": 0.95}]
    out = snap(sam, label["id"])
    assert [x["task_id"] for x in out["applied"]] == [keep["id"]]
    assert [i["reason"] for i in out["ignored"]] == [NEWER_TASK]
    assert not get(sam, typo["id"])["done"]


@pytest.mark.skipif(not SQLITE, reason="PostgreSQL sequences never handed ids out twice")
def test_an_older_sqlite_tasks_table_is_rebuilt_so_ids_are_never_reused(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = db.database(url)
    store = Store(tmp_path, url)
    old_ddl = str(CreateTable(db.tasks).compile(engine)).replace(" AUTOINCREMENT", "")
    with engine.begin() as conn:  # a tasks table as 0.14.0 made it
        conn.exec_driver_sql("DROP TABLE tasks")
        conn.exec_driver_sql(old_ddl)
        conn.exec_driver_sql('CREATE INDEX ix_tasks_owner_open ON tasks (owner, done_at)')
    mine = LocalTasks(engine, "sam")
    made = [mine.create({"content": f"Task {n}", "tags": ["home"], "steps": ["one"], "repeat": "monthly",
                         "due_date": "2026-10-31"}) for n in range(1, 6)]
    mine.delete_task(made[-1]["id"])                                        # nb:5
    assert mine.create({"content": "Reused"})["id"] == "nb:5"               # what SQLite did
    mine.delete_task("nb:5")
    Wins(engine, "sam").add("nb:7", "2026-10-01", "nextbox", content="Finished long ago")
    Slides(engine, "sam").bump("nb:6")
    store.save_printed({"id": "261016-ABCD", "created_at": "2026-10-16T08:00:00", "by": "sam", "kind": "today",
                        "manifest": [{"row": 1, "task_id": "nb:9", "content": "Printed, then deleted"},
                                     {"row": 2, "task_id": "li-12", "content": "A linked task"}]})
    before = rows(url, "tasks")
    assert upkeep.tasks_autoincrement(engine, store) is True
    with engine.connect() as conn:
        made_sql = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name = 'tasks'").scalar()
        index = conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'tasks'").scalars().all()
        assert conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE name LIKE 'tasks_before%'").first() is None
    assert "AUTOINCREMENT" in made_sql and "ix_tasks_owner_open" in index
    assert rows(url, "tasks") == before                                     # every task and column kept
    assert mine.get_task("nb:2")["repeat"] == "monthly" and mine.get_task("nb:2")["labels"] == ["home"]
    assert mine.create({"content": "Brand new"})["id"] == "nb:10"            # past every id still named anywhere
    mine.delete_task("nb:10")
    assert mine.create({"content": "Next"})["id"] == "nb:11"
    assert upkeep.tasks_autoincrement(engine, store) is False               # once is enough
    engine.dispose()


def test_new_databases_never_reuse_ids_and_need_no_rebuild(ctx):
    engine = db.database(ctx.settings.data_dir)
    assert upkeep.tasks_autoincrement(engine, ctx.store) is False
    mine = LocalTasks(engine, "sam")
    first = mine.create({"content": "One"})
    newest = mine.create({"content": "Two"})
    mine.delete_task(newest["id"])
    assert int(mine.create({"content": "Three"})["id"][3:]) > int(newest["id"][3:]) > int(first["id"][3:])


# --- R-B02: moving one day of a repeat keeps its day ---------------------------------------------------
def test_next_due_brings_a_moved_repeat_back_to_its_own_day():
    sat, wed = date(2026, 10, 17), date(2026, 10, 21)
    assert next_due("weekly", sat, sat, anchor_day=4) == date(2026, 10, 23)      # every Friday, done Saturday
    assert next_due("weekly", sat, wed, anchor_day=4) == date(2026, 10, 23)      # done late: still Friday
    assert next_due("weekly", FRI, date(2026, 10, 15), anchor_day=4) == date(2026, 10, 23)  # early: the next one
    assert next_due("weekly", FRI, FRI) == date(2026, 10, 23)                    # no day kept: its own weekday
    assert next_due("monthly", date(2026, 10, 3), date(2026, 10, 3), anchor_day=1) == date(2026, 11, 1)
    assert next_due("monthly", date(2027, 1, 31), date(2027, 1, 31), anchor_day=31) == date(2027, 2, 28)
    assert next_due("monthly", date(2027, 2, 28), date(2027, 2, 28), anchor_day=31) == date(2027, 3, 31)
    assert next_due("monthly", date(2026, 10, 1), date(2026, 12, 15), anchor_day=1) == date(2027, 1, 1)
    assert repeat_anchor("weekly", "2026-10-16") == 4 and repeat_anchor("monthly", "2026-10-31") == 31
    assert repeat_anchor("daily", "2026-10-16") is None and repeat_anchor("weekly", None) is None


def test_tomorrow_on_a_weekly_or_monthly_task_moves_this_time_only(ctx):
    clock = Clock(FRI)
    ctx, a, login, _ = site(ctx, today=clock)
    sam = login()
    trash = add(sam, content="Take out the trash", due_date="2026-10-16", repeat="weekly")
    assert sam.post(f"/list/{trash['id']}/move", json={"to": "tomorrow"}).json()["to"] == "2026-10-17"
    clock.day = date(2026, 10, 17)
    assert sam.post(f"/list/{trash['id']}/done").status_code == 200
    assert due(get(sam, trash["id"])) == "2026-10-23"                            # Friday again, not Saturday
    clock.day = date(2026, 10, 23)
    sam.post(f"/list/{trash['id']}/done")
    assert due(get(sam, trash["id"])) == "2026-10-30"

    clock.day = date(2026, 10, 1)
    rent = add(sam, content="Pay rent", due_date="2026-10-01", repeat="monthly")
    sam.post(f"/list/{rent['id']}/move", json={"to": "2026-10-03"})
    clock.day = date(2026, 10, 3)
    sam.post(f"/list/{rent['id']}/done")
    assert due(get(sam, rent["id"])) == "2026-11-01"                             # the 1st, not the 3rd
    # Skip this time after a move: back to its own day too.
    clock.day = date(2026, 11, 1)
    sam.post(f"/list/{rent['id']}/move", json={"to": "tomorrow"})
    assert sam.post(f"/list/{rent['id']}/skip", json={"due": "2026-11-02"}).json()["undo"]["next_due"] == "2026-12-01"


def test_changing_the_day_in_edit_changes_the_series(ctx):
    clock = Clock(FRI)
    ctx, a, login, _ = site(ctx, today=clock)
    sam = login()
    yoga = add(sam, content="Yoga class", due_date="2026-10-16", repeat="weekly")
    assert sam.patch(f"/list/{yoga['id']}", json={"due_date": "2026-10-17"}).status_code == 200
    clock.day = date(2026, 10, 17)
    sam.post(f"/list/{yoga['id']}/done")
    assert due(get(sam, yoga["id"])) == "2026-10-24"                             # Saturdays from now on
    # Moved one time to Sunday, then saved in Edit with the day it shows: still Saturdays.
    sam.post(f"/list/{yoga['id']}/move", json={"to": "2026-10-25"})
    sam.patch(f"/list/{yoga['id']}", json={"content": "Yoga", "due_date": "2026-10-25"})
    clock.day = date(2026, 10, 25)
    sam.post(f"/list/{yoga['id']}/done")
    assert due(get(sam, yoga["id"])) == "2026-10-31"
    # A new day picked in Edit: Sundays from now on.
    sam.patch(f"/list/{yoga['id']}", json={"due_date": "2026-11-01"})
    clock.day = date(2026, 11, 1)
    sam.post(f"/list/{yoga['id']}/done")
    assert due(get(sam, yoga["id"])) == "2026-11-08"


def test_a_read_back_move_and_an_older_weekly_task_keep_their_day(ctx):
    clock = Clock(FRI)
    engine = db.database(ctx.settings.data_dir)
    mine = LocalTasks(engine, "sam", today=clock)
    bins = mine.create({"content": "Bins out", "due_date": "2026-10-16", "repeat": "weekly"})
    with engine.begin() as conn:  # made before weekly repeats kept their day
        conn.execute(update(db.tasks).where(db.tasks.c.id == int(bins["id"][3:])).values(repeat_day=None))
    Merged(mine, None).set_due_date(bins["id"], "2026-10-17")                     # a → on paper
    with engine.connect() as conn:
        assert conn.execute(select(db.tasks.c.repeat_day)).scalar() == 4          # Friday, from before the move
    clock.day = date(2026, 10, 17)
    mine.close_task(bins["id"])
    assert due(mine.get_task(bins["id"])) == "2026-10-23"
    moved = mine.move(bins["id"], None)                                            # no day at all ends the repeat
    assert moved["due"] is None and moved["repeat"] is None


# --- R-B03: monday.com keeps the local time of day ------------------------------------------------------
@contextlib.contextmanager
def zone(name: str):
    was = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if was is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = was
        time.tzset()


COLUMNS = [{"id": "person", "title": "Owner", "type": "people"},
           {"id": "status", "title": "Status", "type": "status",
            "settings_str": json.dumps({"labels": {"0": "Working on it", "1": "Done"}, "done_colors": [1]})},
           {"id": "date4", "title": "Due date", "type": "date"}]


class OneItemBoard:
    """monday.com with one item on one board; a date change is kept, as monday.com does."""

    def __init__(self, value: dict):
        self.value, self.sent = value, []

    def __call__(self, request):
        body = json.loads(request.content)
        q, v = body["query"], body.get("variables") or {}
        if "change_column_value" in q:
            self.sent.append(json.loads(v["value"]))
            self.value = self.sent[-1]
            data = {"change_column_value": {"id": v["item"]}}
        elif "items(ids:" in q:
            data = {"items": [{"id": "1001", "name": "Call the client", "state": "active", "board": {"id": "111"},
                               "column_values": [{"id": "date4", "type": "date", "text": "",
                                                  "value": json.dumps(self.value)}]}]}
        elif "boards(" in q:
            data = {"boards": [{"id": "111", "name": "My work", "type": "board", "columns": COLUMNS}]}
        else:
            raise AssertionError(q)
        return httpx.Response(200, json={"data": data})

    def monday(self) -> Monday:
        return Monday("tok", client=httpx.Client(transport=httpx.MockTransport(self), base_url="https://api.monday.test"))


@pytest.mark.parametrize("tz, stored, shown, moved_to, sent, now_shown", [
    # Los Angeles (UTC-7): 7pm on the 16th is 02:00 UTC on the 17th.
    ("America/Los_Angeles", {"date": "2026-10-17", "time": "02:00:00"}, "2026-10-16T19:00:00",
     "2026-10-17", {"date": "2026-10-18", "time": "02:00:00"}, "2026-10-17T19:00:00"),
    # Berlin (UTC+2): 1am on the 17th is 23:00 UTC on the 16th.
    ("Europe/Berlin", {"date": "2026-10-16", "time": "23:00:00"}, "2026-10-17T01:00:00",
     "2026-10-18", {"date": "2026-10-17", "time": "23:00:00"}, "2026-10-18T01:00:00"),
    # Across the end of summer time: 9am stays 9am.
    ("America/Los_Angeles", {"date": "2026-10-31", "time": "16:00:00"}, "2026-10-31T09:00:00",
     "2026-11-02", {"date": "2026-11-02", "time": "17:00:00"}, "2026-11-02T09:00:00"),
    # No time: just the day.
    ("Asia/Tokyo", {"date": "2026-10-16"}, "2026-10-16", "2026-10-17", {"date": "2026-10-17"}, "2026-10-17"),
])
def test_monday_moves_a_timed_item_to_the_local_day_at_the_same_local_time(tz, stored, shown, moved_to, sent, now_shown):
    with zone(tz):
        board = OneItemBoard(stored)
        m = board.monday()
        assert m.get_task("1001")["due"]["date"] == shown
        m.set_due_date("1001", moved_to)
        assert board.sent == [sent]
        assert m.get_task("1001")["due"]["date"] == now_shown


def test_a_linked_app_that_keeps_the_day_isnt_a_move_or_a_slide(ctx):
    stuck = FakeTodoist([{"id": "t1", "content": "Linked thing", "priority": 1, "due": {"date": "2026-10-16"}}])
    ctx, a, login, _ = site(ctx, linked=stuck, today=Clock(FRI))  # FakeTodoist records the move, nothing changes
    sam = login()
    r = sam.post("/list/t1/move", json={"to": "tomorrow"})
    assert r.status_code == 400 and "kept it on the same day" in r.json()["detail"]
    assert "t1" not in Slides(a.db, "sam").counts()


# --- R-B04: quick step ticks are never lost -------------------------------------------------------------
def test_quick_step_ticks_at_the_same_moment_all_stick(ctx):
    mine = LocalTasks(db.database(ctx.settings.data_dir), "sam")
    steps = [f"step {i}" for i in range(8)]
    t = mine.create({"content": "Pack for the trip", "steps": steps})
    for _ in range(4):
        go, errors = threading.Barrier(len(steps) + 1), []

        def tick(i):
            try:
                go.wait()
                mine.tick_step(t["id"], i, True, text=steps[i])
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
        threads = [threading.Thread(target=tick, args=(i,)) for i in range(len(steps))]
        threads.append(threading.Thread(target=lambda: (go.wait(), mine.update(t["id"], {"tags": ["home"]}))))
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert errors == []
        now = mine.get_task(t["id"])
        assert [s["done"] for s in now["steps"]] == [True] * len(steps) and now["labels"] == ["home"]
        mine.update(t["id"], {"steps": steps, "tags": []})


def test_the_web_app_sends_one_tasks_step_ticks_one_after_another():
    js = (WEB_DIR / "app.js").read_text()
    assert "const stepTicks = new Map();" in js and "stepTicks.get(id) || Promise.resolve()" in js


# --- R-B05: Undo puts the slide count back --------------------------------------------------------------
def test_done_then_undo_keeps_the_slide_count(ctx):
    ctx, a, login, _ = site(ctx, today=Clock(FRI))
    sam = login()
    garage = add(sam, content="Clean garage", due_date="2026-10-16")
    walk = add(sam, content="Walk the dog", due_date="2026-10-16", repeat="daily")
    slides = Slides(a.db, "sam")
    for _ in range(3):
        slides.bump(garage["id"])
    slides.dismiss(walk["id"])                                                    # "not a slug"
    for t, was in ((garage, 3), (walk, -1)):
        r = sam.post(f"/list/{t['id']}/done").json()
        assert r["undo"]["slides"] == was and t["id"] not in slides.counts()
        assert sam.post(f"/list/{t['id']}/reopen", json=r["undo"]).status_code == 200
        assert slides.counts()[t["id"]] == was
    assert not get(sam, garage["id"])["done"] and due(get(sam, walk["id"])) == "2026-10-16"
    plain = add(sam, content="Never moved", due_date="2026-10-16")
    r = sam.post(f"/list/{plain['id']}/done").json()
    assert "slides" not in r["undo"]
    sam.post(f"/list/{plain['id']}/reopen", json=r["undo"])
    assert plain["id"] not in slides.counts()


def test_a_linked_done_then_undo_keeps_the_slide_count(ctx):
    class Reopens(FakeTodoist):
        def reopen_task(self, task_id):
            self.calls.append(("reopen", task_id))
    app_tasks = Reopens([{"id": "t1", "content": "Linked thing", "priority": 1, "due": {"date": "2026-10-16"}}])
    ctx, a, login, _ = site(ctx, linked=app_tasks, today=Clock(FRI))
    sam = login()
    Slides(a.db, "sam").bump("t1")
    Slides(a.db, "sam").bump("t1")
    r = sam.post("/list/t1/done").json()
    assert r["undo"] == {"slides": 2}
    assert sam.post("/list/t1/reopen", json=r["undo"]).json() == {"ok": True}
    assert ("reopen", "t1") in app_tasks.calls and Slides(a.db, "sam").counts()["t1"] == 2


def test_undoing_a_misread_tick_keeps_the_slide_count(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    t = add(sam, content="Clean garage", due_date=date.today().isoformat())
    for _ in range(3):
        Slides(a.db, "sam").bump(t["id"])
    label = sam.post("/print/today", json={"dry_run": True}).json()
    seen["rows"] = [{"row": 1, "mark": "done", "confidence": 0.95}]
    rec = snap(sam, label["id"])
    assert rec["applied"][0]["slides"] == 3 and t["id"] not in Slides(a.db, "sam").counts()
    assert sam.post(f"/scan/{rec['id']}/undo", json={"row": 1}).status_code == 200
    assert Slides(a.db, "sam").counts()[t["id"]] == 3 and not get(sam, t["id"])["done"]


def test_undoing_new_steps_keeps_the_slide_count(ctx):
    ctx, a, login, _ = site(ctx, today=Clock(FRI))
    sam = login()
    t = add(sam, content="Do the taxes", due_date="2026-10-16", steps=["Find the forms"])
    for _ in range(4):
        Slides(a.db, "sam").bump(t["id"])
    r = sam.patch(f"/list/{t['id']}", json={"steps": ["Find the forms", "Open the envelope"]}).json()
    assert r["slides_was"] == 4 and t["id"] not in Slides(a.db, "sam").counts()   # made smaller: a fresh start
    back = sam.patch(f"/list/{t['id']}", json={"steps": [{"text": "Find the forms", "done": False}], "slides": 4})
    assert back.status_code == 200 and [s["text"] for s in back.json()["steps"]] == ["Find the forms"]
    assert Slides(a.db, "sam").counts()[t["id"]] == 4
    assert sam.patch(f"/list/{t['id']}", json={"slides": 10_001}).status_code == 422


# --- R-B06: taking #urgent off takes back the urgency it gave --------------------------------------------
def test_taking_the_urgent_tag_off_takes_its_urgency_back(ctx):
    ctx, a, login, _ = site(ctx, today=Clock(FRI))
    sam = login()
    rx = add(sam, content="Pick up prescription", tags=["errand"])
    assert sam.patch(f"/list/{rx['id']}", json={"tags": ["errand", "urgent"]}).json()["priority"] == 3
    assert sam.patch(f"/list/{rx['id']}", json={"tags": ["errand"]}).json()["priority"] == 1   # bulk Tag… ✕ urgent
    most = add(sam, content="Most urgent on purpose", priority=4, tags=["urgent"])
    assert sam.patch(f"/list/{most['id']}", json={"tags": []}).json()["priority"] == 4
    kept = add(sam, content="Still urgent", tags=["urgent"])
    assert kept["priority"] == 3
    assert sam.patch(f"/list/{kept['id']}", json={"tags": [], "priority": 3}).json()["priority"] == 3  # said so
    plain = add(sam, content="Urgent without the tag", priority=3)
    assert sam.patch(f"/list/{plain['id']}", json={"tags": ["home"]}).json()["priority"] == 3
    js = (WEB_DIR / "app.js").read_text()
    assert 'if (body.tags && (t.labels || []).includes("urgent") && !tags.includes("urgent")) body.priority' in js


# --- R-B07: a save that fails leaves no tags behind ------------------------------------------------------
def test_a_save_that_fails_makes_no_tags(ctx):
    ctx, a, login, _ = site(ctx, today=Clock(FRI))
    sam = login()

    def names():
        return {t["name"] for t in sam.get("/tags").json()["tags"]}
    start = names()
    r = sam.post("/list", json={"content": "t", "tags": [f"t{i}" for i in range(40)]})
    assert r.status_code == 400 and "8 tags" in r.json()["detail"]
    assert sam.post("/list", json={"content": "t", "tags": [f"t{i}" for i in range(9)]}).status_code == 400
    assert sam.post("/list", json={"content": "  ", "tags": ["brandnew"]}).status_code == 400   # no words
    assert sam.post("/list", json={"content": "t", "tags": ["brandnew"], "due_time": "25:00"}).status_code == 400
    assert sam.patch("/list/nb:99999", json={"tags": ["ghost"]}).status_code == 404
    assert sam.post("/list", json={"content": "t", "tags": ["#"]}).status_code == 400
    assert names() == start and sam.get("/list").json()["tasks"] == []
    # Room for 30 tags: one save that would go past it changes nothing at all.
    for i in range(30 - len(start) - 1):
        assert sam.post("/tags", json={"name": f"x{i}", "color": "white"}).status_code == 200
    r = sam.post("/list", json={"content": "Two more", "tags": ["new-a", "new-b"]})
    assert r.status_code == 400 and "30 tags" in r.json()["detail"] and len(names()) == 29
    ok = add(sam, content="One more", tags=["new-a", "New-A", "x0"])
    assert ok["labels"] == ["new-a", "x0"] and len(names()) == 30
    t = add(sam, content="Edit me")
    assert sam.patch(f"/list/{t['id']}", json={"tags": ["over-the-top"]}).status_code == 400
    assert len(names()) == 30 and get(sam, t["id"])["labels"] == []


# --- R-B08: the setup questions name only what changed ----------------------------------------------------
def test_the_setup_summary_lists_only_settings_that_changed(ctx):
    ctx, a, login, _ = site(ctx)
    sam = login()
    answers = {"rows": 5, "helps": ["gentle"], "plan_at": "08:00", "review_at": "18:00", "journal": "letter"}
    assert sam.post("/setup/quiz", json={"answers": answers}).json()["changed"] == {}  # all as they were
    again = sam.post("/setup/quiz", json={"answers": {**answers, "helps": ["basics"]}}).json()
    assert again["changed"] == {"gentle_words": False}                          # the rest were so already
    assert sam.post("/setup/quiz", json={"answers": {**answers, "helps": ["basics"]}}).json()["changed"] == {}
    logged = [e["detail"]["changed"] for e in rows(ctx.settings.data_dir, "events") if e["action"] == "setup_quiz"]
    assert logged == [[], ["gentle_words"], []]


# --- R-B09: one linked-app fetch per My list load ---------------------------------------------------------
def test_my_list_asks_the_linked_app_once(ctx):
    class Linked(FakeTodoist):
        key = "todoist"
    linked = Linked([{"id": "li-7", "content": "Send the report", "priority": 4, "due": {"date": date.today().isoformat()}}])
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("sam", "sam password", create=True)
    app = create_app(ctx, builder=lambda provider, config, secrets: linked)
    sam = TestClient(app, base_url="http://testserver")
    sam.post("/auth/login", json={"username": "sam", "password": "sam password"})
    assert sam.put("/connection", json={"provider": "todoist", "fields": {"token": "t"}}).status_code == 200
    add(sam, content="Water the plants", due_date=date.today().isoformat())
    linked.calls.clear()
    body = sam.get("/list").json()
    assert [c for c in linked.calls if c[0] == "filter"] == [("filter", "today | overdue")]
    assert body["one_thing"]["id"] == "li-7" and body["one_thing"]["source"] == "todoist"  # urgent first
    assert [t["id"] for t in body["linked"]] == ["li-7"]

    def down(query):
        raise ProviderError("Todoist is down")
    linked.filter_tasks = down
    body = sam.get("/list").json()
    assert body["one_thing"]["content"] == "Water the plants" and "didn't answer" in body["linked_error"]


# --- R-B10: older unnamed My list wins aren't a linked app's -----------------------------------------------
def test_unnamed_my_list_wins_from_an_older_version_arent_called_linked(ctx):
    ctx, a, login, _ = site(ctx)
    sam = login()
    today = date.today().isoformat()
    with a.db.begin() as conn:  # what 0.10.0 left: a repeat finished, no task id, no name
        conn.execute(insert(db.events).values(ts=time.time(), user="sam", household_id="home", kind="activity",
                                              action="task_done", ok=True, detail={"source": "nextbox"}))
    assert upkeep.backfill_wins(a.db, ctx.store) == 1
    Wins(a.db, "sam").add("nb:1", today, "nextbox", content="Answer the email")
    Wins(a.db, "sam").add("li-3", today, "linked")
    day = next(d for d in sam.get("/journal").json()["days"] if d["day"] == today)
    assert day["wins"] == {"count": 3, "items": ["Answer the email"], "linked": 1, "other": 1}
    words = journal.day_wins_text(day)
    assert "+1 more in your linked app" in words and words.rstrip().endswith("+1 more")
    assert paper.journal_day_sheets(day, "letter", lambda page_id: None)            # draws it
    alone = {"day": today, "wins": {"count": 1, "items": [], "linked": 0, "other": 1}}
    assert "linked" not in journal.day_wins_text(alone)
    week = coach.week_message(date.today() - timedelta(days=6), date.today(), {today: alone["wins"]}, [])
    assert "linked" not in week and f"{date.today():%a %b %-d}: 1" in week


# --- R-B11: Easy-to-read tag bands aren't capitals ---------------------------------------------------------
def test_easy_to_read_tag_bands_print_the_tag_as_written(monkeypatch):
    drawn: list[str] = []
    real = render._Draw.text

    def spy(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return real(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(render._Draw, "text", spy)
    sticker = Sticker(1, "Pay rent", tag="errand", color="yellow", when="today")
    render_sticker(sticker, "#261016-ABCD")
    assert "ERRAND" in drawn
    drawn.clear()
    with reading_font(True):
        render_sticker(sticker, "#261016-ABCD")
    assert "errand" in drawn and "ERRAND" not in drawn
