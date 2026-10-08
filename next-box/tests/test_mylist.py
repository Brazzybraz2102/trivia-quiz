"""The built-in list, color tags, brain dump, stickers, tear-off strips and Just one thing."""
import dataclasses
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import TODAY, FakeTodoist
from nextbox import jobs
from nextbox.auth import Accounts
from nextbox.mylist import LocalTasks, Merged, Tags, next_due, parse_dump, parse_line
from nextbox.printer import PrinterConfig
from nextbox.printers import PrinterError, Printers, validate
from nextbox.providers import ProviderError
from nextbox.server import create_app


@pytest.fixture
def app(ctx):
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    ctx.settings.todoist_token = ""  # no legacy .env token: everyone uses their own list
    a = Accounts(ctx.settings.data_dir)
    a.set_password("sam", "sam password", create=True)
    a.create("kim", "kim password")
    application = create_app(ctx)

    def login(name, pw):
        c = TestClient(application, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, login


# --- brain dump ---------------------------------------------------------------------------------
def test_brain_dump_understands_the_shortcuts():
    t = parse_line("- Pay water bill #bills !1 tomorrow 3pm ~15m", TODAY)
    assert t == {"content": "Pay water bill", "tags": ["bills"], "priority": 4,
                 "due_date": "2026-09-29", "due_time": "15:00", "minutes": 15}
    assert parse_line("Call mom fri", TODAY)["due_date"] == "2026-10-02"     # TODAY is Mon Sep 28
    assert parse_line("Take meds every day 8:30", TODAY) == {
        "content": "Take meds", "tags": [], "priority": 1, "repeat": "daily",
        "due_date": "2026-09-28", "due_time": "08:30"}
    assert parse_line("Email about the 3pm meeting", TODAY)["content"] == "Email about the meeting"
    assert parse_line("   ", TODAY) is None and parse_line("#only-a-tag", TODAY) is None
    assert [t["content"] for t in parse_dump("a\n\n[ ] b\n2. c", TODAY)] == ["a", "b", "c"]


def test_repeating_tasks_never_pile_up():
    assert next_due("daily", date(2026, 9, 20), TODAY) == date(2026, 9, 29)
    assert next_due("weekdays", date(2026, 10, 2), date(2026, 10, 2)) == date(2026, 10, 5)
    assert next_due("weekly", date(2026, 9, 16), TODAY) == date(2026, 9, 30)
    assert next_due("monthly", date(2026, 1, 31), TODAY) == date(2026, 9, 30)
    assert next_due("monthly", date(2026, 1, 31), date(2026, 9, 30)) == date(2026, 10, 31)


# --- the list -----------------------------------------------------------------------------------
def test_local_list_round_trip(tmp_path):
    mine = LocalTasks(tmp_path, "sam", today=lambda: TODAY)
    t = mine.create({"content": "Pay bill", "due_date": "2026-09-26", "tags": ["Bills"], "minutes": 10,
                     "steps": ["find it", {"text": "pay", "done": False}]})
    assert t["id"].startswith("nb:") and t["labels"] == ["bills"] and len(t["steps"]) == 2
    assert [x["id"] for x in mine.today()] == [t["id"]]
    assert mine.filter_tasks("#bills") and mine.filter_tasks("overdue") and not mine.filter_tasks("tomorrow")
    mine.close_task(t["id"])
    assert mine.today() == [] and mine.reopen(t["id"])["done"] is False
    daily = mine.create({"content": "Meds", "repeat": "daily", "steps": ["water"]})
    mine.update(daily["id"], {"steps": [{"text": "water", "done": True}]})
    mine.close_task(daily["id"])
    again = mine.get_task(daily["id"])
    assert again["due"]["date"] == "2026-09-29" and not again["done"] and not again["steps"][0]["done"]
    with pytest.raises(ValueError):
        mine.create({"content": "  "})
    with pytest.raises(ValueError):
        mine.update(t["id"], {"due_time": "25:00"})
    # Other people's tasks are invisible and untouchable.
    kim = LocalTasks(tmp_path, "kim", today=lambda: TODAY)
    with pytest.raises(ProviderError):
        kim.get_task(t["id"])
    with pytest.raises(ProviderError):
        kim.delete_task(t["id"])


def test_merged_routes_each_task_to_its_own_list(tmp_path):
    linked = FakeTodoist([{"id": "77", "content": "From Todoist", "priority": 1,
                           "due": {"date": TODAY.isoformat(), "is_recurring": False}}])
    mine = LocalTasks(tmp_path, "sam", today=lambda: TODAY)
    local = mine.create({"content": "Mine", "due_date": TODAY.isoformat()})
    both = Merged(mine, linked)
    assert {t["content"] for t in both.today()} == {"Mine", "From Todoist"}
    both.close_task("77")
    assert ("close", "77") in linked.calls
    both.close_task(local["id"])
    assert mine.today() == []
    assert both.add_task("New")["id"].startswith("nb:")
    assert Merged(mine, None).today() == []


# --- tags ---------------------------------------------------------------------------------------
def test_tags_start_with_defaults_and_stay_deleted(tmp_path):
    tags = Tags(tmp_path, "sam")
    assert tags.colors()["urgent"] == "red"
    for t in tags.all():
        tags.remove(t["name"])
    assert tags.all() == []  # not re-seeded
    tags.set("#Bills", "orange")
    assert tags.all() == [{"name": "bills", "color": "orange"}]
    with pytest.raises(ValueError):
        tags.set("x", "gold")


# --- endpoints ----------------------------------------------------------------------------------
def test_list_endpoints(app):
    ctx, login = app
    sam, kim = login("sam", "sam password"), login("kim", "kim password")
    added = sam.post("/list/dump", json={"text": "Pay bill #bills !1 today\nCall dentist #call"}).json()["added"]
    assert len(added) == 2
    body = sam.get("/list").json()
    assert {t["content"] for t in body["tasks"]} == {"Pay bill", "Call dentist"}
    assert any(t["name"] == "bills" for t in body["tags"])  # a new tag is made on first use
    assert body["one_thing"]["content"] == "Pay bill"
    assert kim.get("/list").json()["tasks"] == []
    tid = added[0]["id"]
    assert sam.patch(f"/list/{tid}", json={"steps": ["open the bill"]}).json()["steps"][0]["text"] == "open the bill"
    assert kim.patch(f"/list/{tid}", json={"content": "x"}).status_code == 502
    # Deleting needs an explicit yes.
    assert sam.delete(f"/list/{tid}").status_code == 400
    assert sam.delete(f"/list/{tid}?confirm=true").status_code == 200
    assert sam.post(f"/list/{added[1]['id']}/done").status_code == 200
    assert sam.get("/list").json()["tasks"] == []
    assert sam.post("/list/dump", json={"text": "\n \n"}).status_code == 400
    assert sam.put("/tags/bills", json={"color": "gold"}).status_code == 400


def test_stickers_strips_and_focus_print(app):
    ctx, login = app
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "Pay bill #urgent today ~15m\nReturn books #errand today\nNo tag today"})
    sam.patch(f"/list/{sam.get('/list').json()['one_thing']['id']}", json={"steps": ["find it"]})
    r = sam.post("/print/stickers", json={}).json()
    assert r["status"] == "dry_run" and r["kind"] == "stickers" and len(r["manifest"]) == 3
    assert [m["row"] for m in r["manifest"]] == [1, 2, 3]
    s = sam.post("/print/strips", json={}).json()
    assert s["kind"] == "strips" and len(s["manifest"]) == 3
    f = sam.post("/print/focus", json={}).json()
    assert f["kind"] == "focus" and f["manifest"][0]["content"] == "Pay bill"
    assert sam.get(f"/printed/{r['id']}/png").status_code == 200
    assert sam.post("/print/stickers", json={"task_ids": ["nb:999"]}).status_code == 502
    sam2 = login("kim", "kim password")
    assert sam2.post("/print/stickers", json={}).status_code == 400  # nothing to print yet


def test_stickers_go_to_the_printer_with_their_color(ctx, tmp_path):
    reg = Printers(tmp_path, ctx.settings)
    white = reg.add({"name": "Desk", "driver": "escpos", "address": "10.0.0.5"})
    red = reg.add({"name": "Red roll", "driver": "escpos", "address": "10.0.0.6", "stock_color": "red"})
    mine = LocalTasks(tmp_path, "cli", today=lambda: TODAY)
    for name, tags in (("Pay bill", ["urgent"]), ("Books", ["errand"]), ("Plain", [])):
        mine.create({"content": name, "due_date": TODAY.isoformat(), "tags": tags})
    c = dataclasses.replace(ctx, tasks_factory=lambda: mine, printers=reg,
                            tag_colors={"urgent": "red", "errand": "yellow"})
    r = jobs.print_stickers(c)
    jobs_by_printer = {x["printer"]["name"]: [m["content"] for m in x["manifest"]] for x in [r, *r.get("also", [])]}
    assert jobs_by_printer == {"Red roll": ["Pay bill"], "Desk": ["Books", "Plain"]}
    assert white.id and red.id


def test_daily_ticket_is_short_and_kind(ctx):
    mine = LocalTasks(ctx.settings.data_dir, "cli", today=lambda: TODAY)
    for i in range(8):
        mine.create({"content": f"Task {i}", "due_date": "2026-09-26" if i == 0 else TODAY.isoformat()})
    c = dataclasses.replace(ctx, tasks_factory=lambda: mine, wins=lambda: 3)
    r = jobs.print_today(c)
    assert len(r["manifest"]) == jobs.MAX_ROWS == 5
    assert jobs._tag({"due": {"date": "2026-09-26"}}, TODAY, gentle=True) == "waiting 2d"


def test_only_ql800_series_prints_red():
    with pytest.raises(PrinterError, match="QL-800"):
        validate({"name": "QL", "driver": "brother_ql", "address": "10.0.0.9", "ink": "black_red"})
    assert validate({"name": "QL", "driver": "brother_ql", "address": "10.0.0.9", "ink": "black_red",
                     "model": "QL-820NWB"}).ink == "black_red"
    assert isinstance(validate({"name": "QL", "driver": "brother_ql", "address": "10.0.0.9"}), PrinterConfig)
