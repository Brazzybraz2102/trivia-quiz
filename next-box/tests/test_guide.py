"""The Get started checklist and the quick start page."""
import dataclasses

from fastapi.testclient import TestClient

from nextbox.auth import Accounts
from nextbox.server import create_app


def test_get_started_ticks_itself_off(ctx):
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    ctx.settings.todoist_token = ""
    Accounts(ctx.settings.data_dir).set_password("sam", "sam password", create=True)
    c = TestClient(create_app(ctx), base_url="http://testserver")
    c.post("/auth/login", json={"username": "sam", "password": "sam password"})

    def state():
        return {s["key"]: (s["done"], s["skipped"]) for s in c.get("/setup").json()["steps"]}
    assert all(not done for done, _ in state().values())
    c.post("/list/dump", json={"text": "Pay bill today"})
    c.post("/print/stickers", json={})
    s = state()
    assert s["tasks"] == (True, False) and s["preview"] == (True, False)
    assert c.post("/setup/calendar/done").status_code == 200
    assert state()["calendar"] == (True, True)  # skipped, not really done
    c.post("/setup/calendar/undo")
    assert state()["calendar"] == (False, False)
    assert c.post("/setup/nope/done").status_code == 404
    assert c.post("/setup/home/explode").status_code == 400
    c.post("/setup/guide/hide")
    assert c.get("/setup").json()["hidden"] is True
    c.post("/setup/guide/show")
    assert c.get("/setup").json()["hidden"] is False


def test_quick_start_page_and_home_screen_icon_need_no_sign_in(ctx):
    c = TestClient(create_app(ctx))
    assert "Next Box quick start" in c.get("/app/guide.html").text
    assert c.get("/app/manifest.json").json()["display"] == "standalone"
    assert c.get("/app/icons/icon-180.png").headers["content-type"] == "image/png"
