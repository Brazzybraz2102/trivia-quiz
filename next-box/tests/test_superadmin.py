"""The super admin panel: every person in every household, managed in one place."""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from nextbox.auth import Accounts
from nextbox.mylist import LocalTasks
from nextbox.server import create_app


@pytest.fixture
def panel(ctx):
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)          # first account: superadmin
    other = a.create_household("Chen family")
    a.create("helper", "helper pass", role="admin")
    a.create("sam", "sam password", email="sam@example.com")
    a.create("lin", "lin password", household_id=other, beta=True)
    LocalTasks(ctx.settings.data_dir, "sam").create({"content": "Secret plans"})
    app = create_app(ctx)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return a, other, login


def test_only_superadmins_get_in(panel):
    a, other, login = panel
    for name, pw in (("helper", "helper pass"), ("sam", "sam password")):
        c = login(name, pw)
        assert c.get("/super/users").status_code == 403
        assert c.get("/super/overview").status_code == 403
        assert c.post("/super/users/bulk", json={"usernames": ["boss"], "action": "disable"}).status_code == 403
    signed_in = login("boss", "boss password")
    assert TestClient(signed_in.app, base_url="http://testserver").get("/super/users").status_code == 401


def test_sees_everyone_across_households_with_filters(panel):
    a, other, login = panel
    boss = login("boss", "boss password")
    rows = {r["username"]: r for r in boss.get("/super/users").json()}
    assert set(rows) == {"boss", "helper", "sam", "lin"}
    assert rows["lin"]["household"] == "Chen family" and rows["sam"]["open_tasks"] == 1
    assert "Secret plans" not in boss.get("/super/users").text           # counts, never task text
    assert "hash" not in rows["sam"] and "salt" not in rows["sam"]
    assert [r["username"] for r in boss.get("/super/users?status=beta").json()] == ["lin"]
    assert [r["username"] for r in boss.get(f"/super/users?household={other}").json()] == ["lin"]
    assert [r["username"] for r in boss.get("/super/users?q=example.com").json()] == ["sam"]
    assert {r["username"] for r in boss.get("/super/users?status=never").json()} == {"helper", "sam", "lin"}
    assert boss.get("/super/users?status=nope").status_code == 400
    o = boss.get("/super/overview").json()
    assert o["people"] == 4 and o["households"] == 2 and o["beta"] == 1 and o["open_tasks"] == 1
    d = boss.get("/super/users/sam").json()
    assert d["connection"] == {"connected": False} and d["tags"] and "secret" not in str(d["connection"])


def test_edit_move_and_protect(panel):
    a, other, login = panel
    boss = login("boss", "boss password")
    r = boss.patch("/super/users/sam", json={"email": "sam@new.example", "household_id": other, "beta": True})
    assert r.status_code == 200 and r.json()["household_id"] == other and r.json()["email"] == "sam@new.example"
    assert boss.patch("/super/users/sam", json={"email": "not-an-email"}).status_code == 400
    assert boss.patch("/super/users/helper", json={"email": "sam@new.example"}).status_code == 400  # taken
    assert boss.patch("/super/users/sam", json={"household_id": "nowhere"}).status_code == 400
    # Can't lock yourself out.
    assert boss.patch("/super/users/boss", json={"disabled": True}).status_code == 400
    assert boss.patch("/super/users/boss", json={"role": "user"}).status_code == 400
    # Forcing a new password signs them out.
    sam = login("sam", "sam password")
    boss.patch("/super/users/sam", json={"must_change": True})
    assert sam.get("/list").status_code == 401
    h = boss.post("/super/households", json={"name": "Park house"}).json()
    assert any(x["id"] == h["id"] for x in boss.get("/super/households").json())


def test_bulk_actions_skip_yourself(panel):
    a, other, login = panel
    boss = login("boss", "boss password")
    r = boss.post("/super/users/bulk", json={"usernames": ["sam", "lin", "boss", "ghost"], "action": "disable"}).json()
    assert r["results"] == {"sam": "done", "lin": "done", "boss": "skipped: that's you", "ghost": "no such user"}
    assert a.get("sam")["disabled"] and a.get("lin")["disabled"] and not a.get("boss")["disabled"]
    boss.post("/super/users/bulk", json={"usernames": ["sam", "lin"], "action": "enable"})
    assert not a.get("sam")["disabled"]
    assert boss.post("/super/users/bulk", json={"usernames": ["sam"], "action": "explode"}).status_code == 400
    events = boss.get("/super/events?kind=audit").json()
    assert any(e["action"] == "bulk_users" for e in events)


def test_create_reset_unlink_and_csv(panel):
    a, other, login = panel
    boss = login("boss", "boss password")
    r = boss.post("/super/users", json={"username": "kim", "household_id": other, "role": "admin",
                                         "email": "=cmd@example.com"}).json()
    assert r["temp_password"] and a.get("kim")["household_id"] == other and a.get("kim")["must_change"]
    a.set_prefs("sam", {"max_rows": 9})
    assert boss.post("/super/users/sam/reset-settings").status_code == 200
    assert a.get("sam")["prefs"]["max_rows"] == 5
    assert boss.post("/super/users/sam/unlink").status_code == 200
    a.update("sam", email=None)
    csv = boss.get("/super/users.csv")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert "username,email,role" in csv.text and "hash" not in csv.text
    assert "'=cmd@example.com" in csv.text  # a spreadsheet won't run it as a formula
