from fastapi.testclient import TestClient

from ticket.auth import Accounts
from ticket.scan import ReadBack, RowMark
from ticket.server import create_app

H = {"X-Ticket-Key": "k"}


def client(ctx, rows=None):
    def vision(image, media_type, manifest):
        return ReadBack(label_code=None, rows=[RowMark(**r) for r in rows or []])
    return TestClient(create_app(ctx, vision=vision), base_url="http://testserver")


def test_auth_required(ctx):
    c = client(ctx)
    assert c.get("/health").status_code == 200
    assert c.post("/print/today", json={}).status_code == 401
    assert c.post("/print/today", json={}, headers={"X-Ticket-Key": "nope"}).status_code == 401


def test_status_never_leaks_secrets(ctx):
    body = client(ctx).get("/status", headers=H).json()
    assert "printer_reachable" in body
    assert "config" not in body  # config details are superadmin-only (diagnostics)


def test_auto_twice_second_skipped(ctx):
    c = client(ctx)
    assert c.post("/print/today", json={"source": "auto"}, headers=H).json()["status"] == "dry_run"
    assert c.post("/print/today", json={"source": "auto"}, headers=H).json()["status"] == "skipped"


def test_print_types_and_history(ctx):
    c = client(ctx)
    c.post("/print/list", json={"query": "p1"}, headers=H)
    c.post("/print/task", json={"task_id": "1"}, headers=H)
    r = c.post("/print/text", json={"text": "hello"}, headers=H).json()
    assert c.get(f"/printed/{r['id']}/png", headers=H).headers["content-type"] == "image/png"
    assert c.get(f"/printed/{r['id']}/png", params={"k": "k"}).status_code == 401  # no keys in URLs
    assert len(c.get("/printed", headers=H).json()) == 3


def test_scan_multipart_then_confirm(ctx, todo):
    c = client(ctx, rows=[{"row": 3, "mark": "drop", "confidence": 0.99}])  # row 3 = task #4
    label = c.post("/print/today", json={}, headers=H).json()
    rec = c.post("/scan", headers=H, data={"label_id": label["id"]},
                 files={"photo": ("p.jpg", b"fake", "image/jpeg")}).json()
    assert rec["needs_confirmation"][0]["task_id"] == "4"
    assert not any(call[0] == "delete" for call in todo.calls)
    out = c.post(f"/scan/{rec['id']}/confirm", json={"decisions": {"3": "confirm"}}, headers=H).json()
    assert out["needs_confirmation"][0]["status"] == "applied"
    assert ("delete", "4") in todo.calls


def test_scan_rejects_non_images(ctx):
    r = client(ctx).post("/scan", headers=H, files={"photo": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 415


def signed_in(ctx):
    Accounts(ctx.settings.data_dir).set_password("mike", "correct horse", create=True)
    c = client(ctx)
    r = c.post("/auth/login", json={"username": "Mike", "password": "correct horse"})
    assert r.status_code == 200 and r.json()["username"] == "mike"
    return c


def test_me_explains_when_no_accounts(ctx):
    r = client(ctx).get("/auth/me")
    assert r.status_code == 401 and "ticket user add" in r.json()["detail"]


def test_sign_in_with_cookie(ctx):
    c = signed_in(ctx)
    cookie = c.cookies.get("ticket_session")
    assert cookie
    assert c.get("/auth/me").json()["username"] == "mike"
    assert c.post("/print/today", json={}).json()["status"] == "dry_run"
    # the session file never holds the raw token
    assert cookie not in (ctx.settings.data_dir / "sessions.json").read_text()


def test_wrong_password_and_rate_limit(ctx):
    Accounts(ctx.settings.data_dir).set_password("mike", "correct horse", create=True)
    c = client(ctx)
    for _ in range(5):
        assert c.post("/auth/login", json={"username": "mike", "password": "nope"}).status_code == 401
    assert c.post("/auth/login", json={"username": "mike", "password": "correct horse"}).status_code == 429


def test_logout_ends_session(ctx):
    c = signed_in(ctx)
    c.post("/auth/logout")
    assert c.get("/auth/me").status_code == 401
    assert c.post("/print/today", json={}).status_code == 401


def test_cross_site_write_refused(ctx):
    c = signed_in(ctx)
    r = c.post("/print/today", json={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert c.post("/print/today", json={}, headers={"Origin": "http://testserver"}).status_code == 200


def test_bearer_token_for_native_apps(ctx):
    Accounts(ctx.settings.data_dir).set_password("mike", "correct horse", create=True)
    c = client(ctx)
    tok = c.post("/auth/login", json={"username": "mike", "password": "correct horse",
                                      "want_token": True}).json()["token"]
    assert "ticket_session" not in c.cookies
    assert c.get("/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()["username"] == "mike"


def test_password_change_and_removal_sign_out(ctx):
    accts = Accounts(ctx.settings.data_dir)
    accts.create("boss", "boss password", role="superadmin")
    c = signed_in(ctx)
    accts.set_password("mike", "new password!")
    assert c.get("/auth/me").status_code == 401
    c = client(ctx)
    c.post("/auth/login", json={"username": "mike", "password": "new password!"})
    assert c.get("/auth/me").status_code == 200
    accts.remove_user("mike")
    assert c.get("/auth/me").status_code == 401
