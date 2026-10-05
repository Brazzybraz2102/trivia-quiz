"""Roles, settings, support (admin) and debug (superadmin) features."""
import json

from fastapi.testclient import TestClient

from ticket import jobs, scan
from ticket.auth import Accounts
from ticket.scan import ReadBack, RowMark
from ticket.server import create_app

H = {"X-Ticket-Key": "k"}


def app_client(ctx):
    def vision(image, media_type, manifest):
        return ReadBack(label_code=None, rows=[RowMark(row=1, mark="done", confidence=0.9)])
    return TestClient(create_app(ctx, vision=vision), base_url="http://testserver")


def login(ctx, name, pw):
    c = app_client(ctx)
    r = c.post("/auth/login", json={"username": name, "password": pw})
    assert r.status_code == 200, r.text
    return c


def setup(ctx):
    a = Accounts(ctx.settings.data_dir)
    a.set_password("mike", "super secret", create=True)   # first account -> superadmin
    a.create("helper", "helper pass", role="admin")
    a.create("tester", "tester pass", beta=True)
    return a


def test_first_account_is_superadmin(ctx):
    a = setup(ctx)
    assert a.get("mike")["role"] == "superadmin"
    assert a.get("tester")["role"] == "user"
    assert "hash" not in a.get("mike") and "salt" not in a.get("mike")


# --- settings ---------------------------------------------------------------
def test_settings_roundtrip_and_validation(ctx):
    setup(ctx)
    c = login(ctx, "tester", "tester pass")
    assert c.get("/settings").json()["max_rows"] == 10
    assert c.patch("/settings", json={"max_rows": 5, "time_24h": True}).json()["max_rows"] == 5
    assert c.patch("/settings", json={"max_rows": 99}).status_code == 400
    assert c.patch("/settings", json={"evil": 1}).status_code == 400
    assert c.patch("/settings", json={"confidence": 0.2}).status_code == 400


def test_prefs_change_the_ticket(ctx, todo):
    for i in range(8):
        todo.tasks[f"x{i}"] = {"id": f"x{i}", "content": f"Chore {i}", "priority": 1,
                               "due": {"date": "2026-09-28T13:30:00"}}
    setup(ctx)
    c = login(ctx, "tester", "tester pass")
    c.patch("/settings", json={"max_rows": 4, "show_waiting": False})
    r = c.post("/print/today", json={}).json()
    assert len(r["manifest"]) == 4
    assert r["by"] == "tester"


def test_always_dry_run_pref_blocks_real_prints(ctx, monkeypatch):
    ctx.settings.dry_run = False
    sent = []
    monkeypatch.setattr("ticket.printer._send_raster", lambda img, s: sent.append(1))
    ctx.settings.printer_ip = "10.0.0.9"
    setup(ctx)
    c = login(ctx, "tester", "tester pass")
    c.patch("/settings", json={"always_dry_run": True})
    assert c.post("/print/text", json={"text": "hi"}).json()["status"] == "dry_run"
    assert sent == []


def test_readback_prefs(ctx, todo):
    from ticket.jobs import Context
    import dataclasses
    label = jobs.print_today(ctx)
    vision = lambda *a: ReadBack(label_code=label["id"], rows=[RowMark(row=1, mark="done", confidence=0.8)])
    strict = dataclasses.replace(ctx, prefs={**ctx.prefs, "confidence": 0.9})
    rec = scan.scan_photo(strict, vision, b"x", "image/jpeg")
    assert rec["applied"] == [] and rec["needs_confirmation"]
    manual = dataclasses.replace(ctx, prefs={**ctx.prefs, "auto_apply": False})
    rec = scan.scan_photo(manual, vision, b"x", "image/jpeg")
    assert rec["applied"] == [] and rec["vision"]["rows"][0]["mark"] == "done"


# --- account self-service ---------------------------------------------------
def test_change_password_keeps_this_device_only(ctx):
    setup(ctx)
    phone = login(ctx, "tester", "tester pass")
    laptop = login(ctx, "tester", "tester pass")
    assert len(laptop.get("/auth/sessions").json()) == 2
    assert laptop.post("/auth/password", json={"current": "nope", "new": "x" * 10}).status_code == 401
    assert laptop.post("/auth/password", json={"current": "tester pass", "new": "short"}).status_code == 400
    assert laptop.post("/auth/password", json={"current": "tester pass", "new": "brand new pass"}).status_code == 200
    assert laptop.get("/auth/me").status_code == 200
    assert phone.get("/auth/me").status_code == 401


def test_signout_others(ctx):
    setup(ctx)
    a, b = login(ctx, "tester", "tester pass"), login(ctx, "tester", "tester pass")
    assert a.post("/auth/signout-others").json()["signed_out"] == 1
    assert a.get("/auth/me").status_code == 200 and b.get("/auth/me").status_code == 401


def test_feedback_reaches_admins(ctx):
    setup(ctx)
    c = login(ctx, "tester", "tester pass")
    assert c.post("/feedback", json={"message": "scan missed my checkmark"}).json()["ok"]
    assert login(ctx, "tester", "tester pass").get("/admin/feedback").status_code == 403
    inbox = login(ctx, "helper", "helper pass").get("/admin/feedback").json()
    assert inbox[0]["detail"]["message"] == "scan missed my checkmark"


# --- admin (support) --------------------------------------------------------
def test_users_and_service_key_cant_use_admin(ctx):
    setup(ctx)
    assert login(ctx, "tester", "tester pass").get("/admin/users").status_code == 403
    assert app_client(ctx).get("/admin/users", headers=H).status_code == 403
    assert login(ctx, "helper", "helper pass").get("/super/diagnostics").status_code == 403


def test_admin_creates_user_with_temp_password(ctx):
    setup(ctx)
    helper = login(ctx, "helper", "helper pass")
    r = helper.post("/admin/users", json={"username": "Sam", "beta": True}).json()
    assert r["username"] == "sam" and len(r["temp_password"]) == 14
    assert helper.post("/admin/users", json={"username": "x2", "role": "admin"}).status_code == 403
    sam = login(ctx, "sam", r["temp_password"])
    me = sam.get("/auth/me").json()
    assert me["must_change"] and me["beta"]
    sam.post("/auth/password", json={"current": r["temp_password"], "new": "sams own pass"})
    assert sam.get("/auth/me").json()["must_change"] is False


def test_admin_limits(ctx):
    setup(ctx)
    helper = login(ctx, "helper", "helper pass")
    assert helper.post("/admin/users/mike/reset-password").status_code == 403
    assert helper.patch("/admin/users/helper", json={"disabled": True}).status_code == 403
    assert helper.patch("/admin/users/tester", json={"debug": True}).status_code == 403
    assert helper.patch("/admin/users/tester", json={"role": "admin"}).status_code == 403
    assert helper.delete("/admin/users/tester").status_code == 403


def test_admin_disable_and_reset(ctx):
    setup(ctx)
    tester = login(ctx, "tester", "tester pass")
    helper = login(ctx, "helper", "helper pass")
    assert helper.patch("/admin/users/tester", json={"disabled": True}).json()["disabled"]
    assert tester.get("/auth/me").status_code == 401
    r = app_client(ctx).post("/auth/login", json={"username": "tester", "password": "tester pass"})
    assert r.status_code == 403 and "turned off" in r.json()["detail"]
    helper.patch("/admin/users/tester", json={"disabled": False})
    temp = helper.post("/admin/users/tester/reset-password").json()["temp_password"]
    assert app_client(ctx).post("/auth/login", json={"username": "tester", "password": "tester pass"}).status_code == 401
    login(ctx, "tester", temp)
    users = {u["username"]: u for u in helper.get("/admin/users").json()}
    assert users["tester"]["must_change"] and "hash" not in users["tester"]


def test_audit_log_records_admin_actions(ctx):
    setup(ctx)
    login(ctx, "helper", "helper pass").post("/admin/users/tester/signout")
    audit = login(ctx, "mike", "super secret").get("/super/events", params={"kind": "audit"}).json()
    assert audit[0]["user"] == "helper" and audit[0]["action"] == "signout_user"


def test_last_superadmin_is_protected(ctx):
    setup(ctx)
    mike = login(ctx, "mike", "super secret")
    assert mike.patch("/admin/users/mike", json={"role": "user"}).status_code == 403  # can't edit self
    a = Accounts(ctx.settings.data_dir)
    try:
        a.update("mike", role="user")
        raise AssertionError("should refuse")
    except ValueError:
        pass
    assert mike.delete("/admin/users/mike").status_code == 400


# --- superadmin (debug) -----------------------------------------------------
def test_errors_are_logged_with_trace_for_superadmin_only(ctx, todo):
    setup(ctx)

    def boom(task_id):
        raise RuntimeError("todoist said 404")
    todo.get_task = boom
    tester = login(ctx, "tester", "tester pass")
    assert tester.post("/print/task", json={"task_id": "zzz"}).status_code == 502
    errs = login(ctx, "mike", "super secret").get("/super/events", params={"errors_only": True}).json()
    assert errs[0]["user"] == "tester" and "todoist said 404" in errs[0]["error"]
    assert "Traceback" in errs[0]["trace"]
    as_admin = login(ctx, "helper", "helper pass").get("/admin/users/tester/activity").json()
    assert "trace" not in as_admin[0]


def test_debug_mode_records_details(ctx):
    setup(ctx)
    mike = login(ctx, "mike", "super secret")
    assert mike.patch("/admin/users/tester", json={"debug": True}).json()["debug"]
    login(ctx, "tester", "tester pass").post("/print/text", json={"text": "hello there"})
    ev = mike.get("/super/events", params={"user": "tester", "kind": "activity"}).json()[0]
    assert ev["detail"]["args"]["arg1"] == "hello there" and "result" in ev["detail"]


def test_pause_printing_and_auto_switch(ctx, monkeypatch):
    ctx.settings.dry_run = False
    ctx.settings.printer_ip = "10.0.0.9"
    sent = []
    monkeypatch.setattr("ticket.printer._send_raster", lambda img, s: sent.append(1))
    setup(ctx)
    mike = login(ctx, "mike", "super secret")
    mike.patch("/super/settings", json={"printing_paused": True, "auto_print_enabled": False,
                                        "announcement": "Beta: printer moved to the office"})
    assert mike.post("/print/text", json={"text": "x"}).json()["status"] == "dry_run"
    r = app_client(ctx).post("/print/today", json={"source": "auto"}, headers=H).json()
    assert r["status"] == "skipped" and "turned off" in r["reason"]
    assert sent == []
    me = login(ctx, "tester", "tester pass").get("/auth/me").json()
    assert me["announcement"].startswith("Beta") and me["printing_paused"]
    assert mike.patch("/super/settings", json={"printing_paused": "yes"}).status_code == 400
    assert mike.patch("/super/settings", json={"bogus": 1}).status_code == 400


def test_diagnostics_and_bundle_have_no_secrets(ctx):
    ctx.settings.todoist_token = "tdk_SECRET"
    ctx.settings.anthropic_api_key = "sk-ant-SECRET"
    ctx.settings.ticket_key = "KEY-SECRET"
    setup(ctx)
    mike = login(ctx, "mike", "super secret")
    d = mike.get("/super/diagnostics").json()
    assert d["todoist"]["ok"] and d["counts"]["users"] == 3
    r = mike.get("/super/users/tester/bundle")
    assert "attachment" in r.headers["content-disposition"]
    text = json.dumps(d) + r.text
    for secret in ("SECRET", "tester pass", '"hash"', '"salt"'):
        assert secret not in text, secret
    assert json.loads(r.text)["user"]["username"] == "tester"
