"""Admin visibility (everyone agreed), the recorded data notice, and the revision mark."""
from fastapi.testclient import TestClient

from conftest import tiny_jpeg
from nextbox.auth import Accounts
from nextbox.scan import ReadBack, RowMark
from nextbox.server import create_app


def setup(ctx):
    a = Accounts(ctx.settings.data_dir)
    a.set_password("mike", "super secret", create=True)
    a.create("helper", "helper pass", role="admin")
    a.create("sam", "sam password")

    def vision(image, media_type, manifest):
        return ReadBack(label_code=None, rows=[RowMark(row=1, mark="done", confidence=0.9)])
    app = create_app(ctx, vision=vision)

    def login(n, p):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": n, "password": p}).status_code == 200
        return c
    return login


def test_data_notice_is_shown_and_recorded(ctx):
    login = setup(ctx)
    sam = login("sam", "sam password")
    me = sam.get("/auth/me").json()
    assert me["consented"] is False and "can see how you use Next Box" in me["data_notice"]
    assert "never visible" in me["data_notice"] and "anonymous" in me["data_notice"]
    assert sam.post("/auth/consent").json()["ok"]
    me = sam.get("/auth/me").json()
    assert me["consented"] is True and me["consented_at"]


def test_superadmin_sees_how_everyone_uses_it(ctx):
    login = setup(ctx)
    sam, helper, mike = login("sam", "sam password"), login("helper", "helper pass"), login("mike", "super secret")
    sam.post("/auth/consent")
    t = sam.post("/print/today", json={}).json()
    sam.post("/print/text", json={"text": "hi"})
    sam.post("/scan", data={"label_id": t["id"]}, files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")})
    sam.patch("/settings", json={"max_rows": 6})

    assert helper.get("/super/usage").status_code == 403
    u = mike.get("/super/usage", params={"days": 7}).json()
    assert u["totals"]["tickets"] == 2 and u["totals"]["scans"] == 1
    s = {p["username"]: p for p in u["people"]}["sam"]
    assert s["tickets"] == 2 and s["by_reason"] == {"today": 1, "note": 1} and s["marks_applied"] == 1
    assert s["consented_at"] and s["prefs"]["max_rows"] == 6
    assert dict(s["top_actions"])["print_today_manual"] == 1
    assert u["series"][-1]["tickets"] == 2 and u["series"][-1]["people"] >= 1

    detail = mike.get("/super/users/sam/tickets").json()
    assert {r["id"] for r in detail["tickets"]} >= {t["id"]} and len(detail["scans"]) == 1
    assert mike.get(f"/printed/{t['id']}/png").status_code == 200      # superadmin can view it
    assert helper.get(f"/printed/{t['id']}/png").status_code == 404    # nobody else can
    # Feedback stays anonymous even here.
    sam.post("/feedback", json={"mode": "open", "message": "nice"})
    assert "feedback" not in str(mike.get("/super/usage").json()["people"])


def test_version_and_credit(ctx):
    ctx.settings.credit = "Created by Mike"
    c = TestClient(create_app(ctx))
    v = c.get("/version").json()
    assert v["version"] == "0.6.0" and v["credit"] == "Created by Mike" and "build" in v
