"""Your own account: profile, download your data, delete your account, update check."""
import dataclasses
import json

from fastapi.testclient import TestClient

from nextbox.auth import Accounts
from nextbox.server import create_app


def site(ctx):
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    app = create_app(ctx)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, login


def test_profile_and_me(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    me = sam.get("/auth/me").json()
    assert me["email"] is None and me["version"] and me["protected"] is False
    assert sam.patch("/auth/profile", json={"email": "Sam@Example.com"}).json()["email"] == "sam@example.com"
    assert sam.patch("/auth/profile", json={"email": "nope"}).status_code == 400
    # Sign in with the email too.
    r = TestClient(sam.app, base_url="http://testserver").post(
        "/auth/login", json={"username": "Sam@Example.com", "password": "sam password"})
    assert r.status_code == 200 and r.json()["username"] == "sam"


def test_download_my_data_has_no_secrets(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "Pay bill #errand today"})
    sam.post("/print/stickers", json={})
    r = sam.get("/auth/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["account"]["username"] == "sam" and data["tasks"][0]["content"] == "Pay bill"
    assert data["tickets"] and data["tags"]
    text = json.dumps(data)
    assert "hash" not in data["account"] and "salt" not in text and "secret" not in text


def test_delete_my_account(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "Pay bill today"})
    sam.post("/print/today", json={})
    assert sam.post("/auth/delete-account", json={"password": "sam password", "confirm": "wrong"}).status_code == 400
    assert sam.post("/auth/delete-account", json={"password": "nope", "confirm": "sam"}).status_code == 403
    assert sam.post("/auth/delete-account", json={"password": "sam password", "confirm": "sam"}).status_code == 200
    assert a.get("sam") is None and ctx.store.list_printed(10, by="sam") == []
    assert sam.get("/auth/me").status_code == 401
    # The protected admin can't delete itself.
    a.set_protected("boss")
    boss = login("boss", "boss password")
    assert boss.post("/auth/delete-account", json={"password": "boss password", "confirm": "boss"}).status_code == 400


def test_update_check_is_superadmin_only(ctx, monkeypatch):
    ctx, a, login = site(ctx)
    import httpx

    class R:
        status_code = 200
        text = '[project]\nname = "nextbox"\nversion = "99.0.0"\n'
    monkeypatch.setattr(httpx, "get", lambda *args, **kw: R())
    assert login("sam", "sam password").get("/super/update-check").status_code == 403
    r = login("boss", "boss password").get("/super/update-check").json()
    assert r["latest"] == "99.0.0" and r["update_available"] and "update.sh" in r["how"]


def test_profile_settings_and_fresh_copies(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    prefs = sam.patch("/settings", json={"display_name": "Sammy", "text_size": "large", "start_tab": "print"}).json()
    assert (prefs["display_name"], prefs["text_size"], prefs["start_tab"]) == ("Sammy", "large", "print")
    assert sam.patch("/settings", json={"text_size": "huge"}).status_code == 400
    me = sam.get("/auth/me").json()
    assert me["household"] and me["created"]
    page = sam.get("/app/")
    assert page.headers["cache-control"] == "no-cache" and 'src="app.js?v=' in page.text
    assert sam.get("/app/app.js").headers["cache-control"] == "no-cache"
