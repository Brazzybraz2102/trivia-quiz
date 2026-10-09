"""Invite-only sign-up, the protected admin account, and who Home Assistant prints for."""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from nextbox import cli, dbtools, jobs
from nextbox.auth import Accounts, AuthError
from nextbox.mylist import LocalTasks
from nextbox.printers import Printers
from nextbox.server import create_app


@pytest.fixture
def site(ctx):
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("usersuperadmin", "admin password", create=True)
    a.create("mike", "mike password")
    app = create_app(ctx)

    def client():
        return TestClient(app, base_url="http://testserver")

    def login(name, pw):
        c = client()
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, client, login


def test_protected_admin_always_stays_superadmin(site):
    ctx, a, client, login = site
    a.set_protected("usersuperadmin")
    a.create("other", "other password", role="superadmin")
    for change in ({"role": "user"}, {"disabled": True}):
        with pytest.raises(AuthError, match="protected"):
            a.update("usersuperadmin", **change)
    with pytest.raises(AuthError, match="protected"):
        a.remove_user("usersuperadmin")
    other = login("other", "other password")
    assert other.patch("/super/users/usersuperadmin", json={"role": "user"}).status_code == 400
    assert other.delete("/admin/users/usersuperadmin").status_code == 400
    r = other.post("/super/users/bulk", json={"usernames": ["usersuperadmin"], "action": "disable"}).json()
    assert "protected" in r["results"]["usersuperadmin"]
    assert {u["username"]: u["protected"] for u in other.get("/super/users").json()}["usersuperadmin"]
    a.update("usersuperadmin", beta=True)  # other changes are fine
    with pytest.raises(AuthError):
        a.set_protected("mike")  # must be a superadmin first
    a.set_protected(None)
    a.update("usersuperadmin", role="admin")  # unprotected: normal rules again


def test_home_assistant_prints_for_the_chosen_owner(site):
    ctx, a, client, login = site
    assert a.owner() == "usersuperadmin"  # oldest superadmin by default
    a.set_print_owner("mike")
    assert a.owner() == "mike"
    LocalTasks(ctx.settings.data_dir, "mike").create({"content": "Mike's thing", "due_date": ctx.today().isoformat()})
    r = client().post("/print/today", json={}, headers={"X-NextBox-Key": "k"}).json()
    assert r["by"] == "mike" and [m["content"] for m in r["manifest"]] == ["Mike's thing"]
    a.update("mike", disabled=True)
    assert a.owner() == "usersuperadmin"  # falls back when the chosen person is turned off


def test_invite_join_and_own_household(site):
    ctx, a, client, login = site
    boss = login("usersuperadmin", "admin password")
    join = boss.post("/admin/invites", json={"note": "Sam", "beta": True}).json()
    own = boss.post("/admin/invites", json={"own_household": True, "beta": False}).json()
    assert join["link"] == f"/app/#join={join['code']}"
    # No code, no account.
    assert client().post("/auth/signup", json={"username": "x", "password": "long enough", "invite": ""}).status_code == 400
    assert client().post("/auth/signup", json={"username": "x", "password": "long enough", "invite": "BAD-CODE-1"}).status_code == 400
    sam = client()
    r = sam.post("/auth/signup", json={"username": "Sam", "password": "sam password", "invite": join["code"].lower()})
    assert r.status_code == 200 and r.json() == {"username": "sam", "role": "user"}
    assert sam.get("/auth/me").json()["household_id"] == "home" and a.get("sam")["beta"]
    # Used once only.
    assert client().post("/auth/signup", json={"username": "sam2", "password": "long enough",
                                                "invite": join["code"]}).status_code == 400
    kim = client()
    assert kim.post("/auth/signup", json={"username": "kim", "password": "kim password", "invite": own["code"],
                                          "email": "kim@example.com"}).status_code == 200
    k = a.get("kim")
    assert k["household_id"] != "home" and k["role"] == "admin" and not k["beta"]
    statuses = {i["code"]: i["status"] for i in boss.get("/admin/invites").json()}
    assert statuses[join["code"]] == "used" and statuses[own["code"]] == "used"
    # Household admins invite into their own household only, and can't see others' invites.
    kc = login("kim", "kim password")
    assert kc.post("/admin/invites", json={"own_household": True}).status_code == 403
    assert kc.post("/admin/invites", json={"household_id": "home"}).status_code == 403
    mine = kc.post("/admin/invites", json={}).json()
    assert [i["code"] for i in kc.get("/admin/invites").json()] == [mine["code"]]
    assert kc.delete(f"/admin/invites/{join['code']}").status_code == 404
    assert kc.delete(f"/admin/invites/{mine['code']}").status_code == 200
    # Regular users can't make invites.
    assert sam.post("/admin/invites", json={}).status_code == 403


def test_guessing_codes_gets_locked_out(site):
    ctx, a, client, login = site
    c = client()
    codes = [c.post("/auth/signup", json={"username": f"g{i}", "password": "long enough", "invite": f"AAAA-BBBB-{i:04d}"}).status_code
             for i in range(6)]
    assert codes[:5] == [400] * 5 and codes[5] == 429


def test_the_env_printer_is_only_for_home(site, tmp_path):
    ctx, a, client, login = site
    ctx.settings.dry_run = False
    ctx.settings.printer_ip = "10.0.0.9"
    other = a.create_household("Elsewhere")
    mine = LocalTasks(ctx.settings.data_dir, "kim", other)
    mine.create({"content": "Kim's task", "due_date": ctx.today().isoformat()})
    c = dataclasses.replace(ctx, tasks_factory=lambda: mine, household=other, user="kim",
                            printers=Printers(ctx.settings.data_dir, ctx.settings))
    r = jobs.print_today(c)  # the no_real_printer fixture would fail this if it reached the printer
    assert r["status"] == "dry_run" and r["note"] == jobs.NO_PRINTER


def test_cli_protect_owner_and_invite(site, monkeypatch, capsys):
    ctx, a, client, login = site
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(ctx.settings.data_dir))
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(ctx.settings.data_dir / "none.env"))
    assert cli.main(["user", "protect", "usersuperadmin"]) == 0
    assert cli.main(["user", "owner", "mike"]) == 0
    assert cli.main(["user", "list"]) == 0
    out = capsys.readouterr().out
    assert "usersuperadmin" in out and "protected" in out and "prints-for-home-assistant" in out
    assert cli.main(["invite", "--days", "3", "--beta", "--note", "Lin"]) == 0
    assert "Invite code:" in capsys.readouterr().out
    assert cli.main(["invite", "--days", "500"]) == 1


def test_practice_url_follows_postgres(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nextbox:pw@127.0.0.1:5432/nextbox")
    assert dbtools.practice_url(tmp_path) == "postgresql://nextbox:pw@127.0.0.1:5432/nextbox_practice"
    assert "pw" not in dbtools.describe_location(tmp_path)
    monkeypatch.setenv("NEXTBOX_PRACTICE_URL", "sqlite:///x.db")
    assert dbtools.practice_url(tmp_path) == "sqlite:///x.db"


def test_copy_database_into_an_empty_one(tmp_path):
    dbtools.build_practice(tmp_path)
    target = f"sqlite:///{tmp_path / 'copy.db'}"
    counts = dbtools.copy_database(dbtools.practice_url(tmp_path), target)
    assert counts["users"] == 9 and counts["tasks"] > 0
    assert dbtools.run_sql(target, "SELECT COUNT(*) FROM tickets")[1] == [[counts["tickets"]]]
    with pytest.raises(RuntimeError, match="isn't empty"):
        dbtools.copy_database(dbtools.practice_url(tmp_path), target)
