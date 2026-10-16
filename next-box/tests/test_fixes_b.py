"""Batch B fixes: privacy, security and data lifecycle on the server."""
import contextlib
import dataclasses
import json
import os
import socket
import threading
import time
from dataclasses import asdict
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from conftest import dump, rows, tiny_jpeg
from nextbox import calendars, cli, db, dbtools, jobs, printer, scan
from nextbox.auth import Accounts, AuthError
from nextbox.events import Events
from nextbox.journal import Journal
from nextbox.mylist import LocalTasks
from nextbox.printer import PrinterConfig
from nextbox.providers import ProviderError
from nextbox.providers import http as provider_http
from nextbox.providers.caldav_tasks import CalDAVTasks
from nextbox.scan import ReadBack, RowMark
from nextbox.server import create_app

SQLITE_ONLY = pytest.mark.skipif(bool(os.environ.get("DATABASE_URL")), reason="SQLite authorizer")


def site(ctx, today=date.today):
    """boss (superadmin), anna (Home's household admin), mike and sam, with My list only.
    `seen` is what the fake camera reads from the next photo."""
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("anna", "anna password", role="admin")
    a.create("mike", "mike password")
    a.create("sam", "sam password")
    seen: dict = {}

    def vision(image, media_type, manifest):
        return ReadBack(label_code=seen.get("code"), rows=[RowMark(**r) for r in seen.get("rows", [])],
                        notes=seen.get("notes", ""))
    app = create_app(ctx, vision=vision)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, app, login, seen


def snap(c, label_id=None):
    return c.post("/scan", data={"label_id": label_id} if label_id else {},
                  files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")}).json()


def audits(ctx, action):
    return [e for e in Events(ctx.settings.data_dir).query(kind="audit", limit=1000) if e["action"] == action]


# --- B-01 debug mode -------------------------------------------------------------------------
def test_debug_mode_never_copies_task_text_or_journal_notes(ctx):
    ctx, a, app, login, seen = site(ctx)
    boss, anna, mike = login("boss", "boss password"), login("anna", "anna password"), login("mike", "mike password")
    assert boss.patch("/admin/users/mike", json={"debug": True}).json()["debug"]
    assert mike.get("/auth/me").json()["debug"] is True  # the web app shows "Debug mode is on"
    mike.post("/list", json={"content": "Book the Zanzibar clinic", "description": "quokka details",
                             "steps": ["call the wombat"], "due_date": date.today().isoformat()})
    label = mike.post("/print/today", json={"dry_run": True}).json()
    seen.update(rows=[{"row": 1, "mark": "done", "confidence": 0.95,
                       "note": "circled, with handwriting beside it that says Finally called the dentist"}],
                notes="Rough start, but the walk at lunch helped.")
    rec = snap(mike, label["id"])
    assert rec["notes"] == "Rough start, but the walk at lunch helped." and rec["journal_id"]  # shown to mike

    events_seen = [boss.get("/super/events").text, json.dumps(boss.get("/super/users/mike/bundle").json()["events"]),
                   json.dumps(boss.get("/super/users/mike").json()["recent"]),
                   anna.get("/admin/users/mike/activity").text]
    for text in events_seen:
        for leak in ("Zanzibar", "quokka", "wombat", "walk at lunch", "dentist"):
            assert leak not in text
    everything_admins_see = [boss.get("/super/users/mike/bundle").text, boss.get("/super/users/mike/tickets").text]
    for text in everything_admins_see:  # tickets show task names (the data notice says so), never notes
        assert "walk at lunch" not in text and "dentist" not in text
    scan_ev = next(e for e in boss.get("/super/events", params={"user": "mike"}).json() if e["action"] == "scan")
    assert scan_ev["detail"]["result"]["journal_id"] == rec["journal_id"]
    assert scan_ev["detail"]["result"]["applied_count"] == 1
    stored = ctx.store.get_scan(rec["id"])
    assert all(len(r["note"]) <= scan.NOTE_CHARS for r in stored["vision"]["rows"] + stored["applied"])
    assert "describe what you saw" not in scan.PROMPT and "never copy handwritten words" in scan.PROMPT


def test_old_debug_details_are_removed_once_on_start(ctx):
    ev = Events(ctx.settings.data_dir)
    ev.log("mike", "scan", detail={"ms": 5, "args": {"arg0": "Zanzibar"}, "result": {"notes": "walk at lunch"}})
    ev.log("mike", "list_add", ok=False, kind="error", detail={"args": {"arg0": {"content": "Zanzibar"}}})
    Accounts(ctx.settings.data_dir).set_password("boss", "boss password", create=True)
    create_app(ctx)
    log = json.dumps(rows(ctx.settings.data_dir, "events"))
    assert "Zanzibar" not in log and "walk at lunch" not in log and '"ms": 5' in log


# --- B-02 deleting an account --------------------------------------------------------------
def test_a_new_account_with_an_old_name_starts_empty(ctx):
    ctx, a, app, login, seen = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "Bank PIN hint today"})
    sam.post("/journal", json={"notes": "Sam's private journal entry"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    seen.update(rows=[{"row": 1, "mark": "done", "confidence": 0.95}])
    snap(sam, label["id"])
    boss = login("boss", "boss password")
    assert boss.delete("/admin/users/sam").status_code == 200
    deleted = audits(ctx, "delete_user")
    assert deleted and deleted[0]["user"] == "boss" and deleted[0]["target"] == "sam"  # the audit survives

    temp = boss.post("/super/users", json={"username": "sam"}).json()["temp_password"]
    new = login("sam", temp)
    assert new.post("/auth/password", json={"current": temp, "new": "new sam password"}).status_code == 200
    assert new.get("/list").json()["tasks"] == []
    fresh = new.get("/journal").json()
    assert fresh["days"] == [] and fresh["total_pages"] == 0 and not fresh["has_more"]
    assert new.get("/printed").json() == [] and new.get("/scans").json() == []
    assert new.get(f"/printed/{label['id']}/png").status_code == 404
    assert new.get("/journal/wins").json()["total"] == 0
    old = {"brain_dump", "print_today_manual", "scan", "journal_add", "task_done"}
    assert not [e for e in boss.get("/admin/users/sam/activity").json() if e["action"] in old]
    assert "Bank PIN" not in dump(ctx.settings.data_dir) and "Sam's private" not in dump(ctx.settings.data_dir)


def test_cli_user_remove_takes_everything(ctx):
    ctx, a, app, login, seen = site(ctx)
    a.create("kim", "kim password")
    kim = login("kim", "kim password")
    kim.post("/list", json={"content": "Kim's private task"})
    kim.post("/journal", json={"notes": "Kim's private journal entry"})
    assert cli._user(a, "remove", "kim", data_dir=ctx.settings.data_dir) == 0
    a.create("kim", "kim password 2")
    kim2 = login("kim", "kim password 2")
    assert kim2.get("/list").json()["tasks"] == [] and kim2.get("/journal").json()["total_pages"] == 0
    assert "Kim's private" not in dump(ctx.settings.data_dir)


def test_leftovers_of_old_deleted_accounts_are_swept_once(ctx):
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    LocalTasks(ctx.settings.data_dir, "ghost").create({"content": "Ghost's task"})
    Journal(a.db, "ghost").add("2026-09-01", "ghost diary")
    jobs.print_text(dataclasses.replace(ctx, user="ghost"), "ghost ticket")
    jobs.print_text(ctx, "cli ticket")  # printed by "cli" before any account: not a person, left alone
    create_app(ctx)
    data = dump(ctx.settings.data_dir)
    assert "Ghost's task" not in data and "ghost diary" not in data and not ctx.store.list_printed(10, by="ghost")
    assert ctx.store.list_printed(10, by="cli")


# --- B-03 one-time passwords -----------------------------------------------------------------
def test_a_one_time_password_only_lets_you_choose_your_own(ctx):
    ctx, a, app, login, seen = site(ctx)
    login("mike", "mike password").post("/journal", json={"notes": "mike diary"})
    anna = login("anna", "anna password")
    temp = anna.post("/admin/users/mike/reset-password").json()["temp_password"]
    as_mike = login("mike", temp)
    for method, path, body in (("get", "/auth/export", None),
                               ("post", "/auth/delete-account", {"password": temp, "confirm": "mike"}),
                               ("patch", "/auth/profile", {"email": "x@example.com"}),
                               ("get", "/auth/sessions", None), ("post", "/auth/signout-others", None)):
        r = getattr(as_mike, method)(path, **({"json": body} if body else {}))
        assert r.status_code == 403 and "password" in r.json()["detail"], path
    assert as_mike.get("/auth/me").json()["password_reset"] is None
    assert as_mike.post("/auth/password", json={"current": temp, "new": "anna knows this"}).status_code == 200
    assert as_mike.get("/auth/me").json()["password_reset"]["by"] == "anna"  # mike is told who, and when
    # For 3 days the download and the delete wait, so whoever set the password can't take either.
    r = as_mike.get("/auth/export")
    assert r.status_code == 403 and "paused" in r.json()["detail"]
    assert as_mike.post("/auth/delete-account", json={"password": "anna knows this", "confirm": "mike"}).status_code == 403
    assert a.get("mike")
    with a.db.begin() as conn:
        conn.execute(update(db.users).where(db.users.c.username == "mike").values(reset_at=int(time.time()) - 73 * 3600))
    assert as_mike.get("/auth/export").status_code == 200
    assert audits(ctx, "reset_password")[0]["target"] == "mike"


# --- B-04 the protected admin ----------------------------------------------------------------
def test_only_the_computer_changes_the_protected_admin(ctx):
    ctx, a, app, login, seen = site(ctx)
    a.create("keeper", "keeper password", role="superadmin")
    a.set_protected("keeper")
    other = a.create_household("Elsewhere")
    keeper, boss = login("keeper", "keeper password"), login("boss", "boss password")
    assert boss.post("/admin/users/keeper/reset-password").status_code == 400
    assert boss.post("/admin/users/keeper/signout").status_code == 400
    for change in ({"must_change": True}, {"household_id": other}, {"email": "k@example.com"}):
        r = boss.patch("/super/users/keeper", json=change)
        assert r.status_code == 400 and "protected" in r.json()["detail"], change
    assert boss.post("/super/users/keeper/reset-settings").status_code == 400
    assert boss.post("/super/users/keeper/unlink").status_code == 400
    r = boss.post("/super/users/bulk", json={"usernames": ["keeper", "sam"], "action": "signout"}).json()
    assert r["results"]["keeper"] == "skipped: protected admin" and r["results"]["sam"].startswith("signed out")
    assert keeper.get("/auth/me").status_code == 200  # still signed in, still their own password
    k = a.get("keeper")
    assert not k["must_change"] and k["household_id"] == "home" and k["email"] is None
    assert keeper.patch("/super/users/keeper", json={"email": "keeper@example.com"}).status_code == 200  # their own
    assert boss.patch("/super/users/keeper", json={"beta": True}).status_code == 200  # harmless changes are fine


# --- B-05 / B-19 feedback moderation ----------------------------------------------------------
def test_feedback_moderation_is_staff_only_and_own_posts_arent_audited(ctx):
    ctx, a, app, login, seen = site(ctx)
    other = a.create_household("Outsiders")
    a.create("outsider", "outsider password", role="admin", household_id=other)
    mike, boss, outsider = login("mike", "mike password"), login("boss", "boss password"), login("outsider", "outsider password")
    fid = mike.post("/feedback", json={"mode": "open", "message": "Jane Doe at 12 Oak Street"}).json()["id"]
    boss.patch(f"/admin/feedback/{fid}", json={"status": "hidden"})
    assert fid not in [i["id"] for i in outsider.get("/feedback").json()]
    assert outsider.get("/admin/feedback").status_code == 403
    assert outsider.patch(f"/admin/feedback/{fid}", json={"status": "fixed", "reply": "lol"}).status_code == 403
    assert outsider.delete(f"/feedback/{fid}").status_code == 403
    assert fid not in [i["id"] for i in login("anna", "anna password").get("/feedback").json()]

    own = boss.post("/feedback", json={"mode": "open", "message": "A note from staff"}).json()["id"]
    assert boss.delete(f"/feedback/{own}").json()["ok"]
    assert audits(ctx, "remove_feedback") == []  # withdrawing your own post isn't moderation
    assert boss.delete(f"/feedback/{fid}").json()["ok"]
    assert [e["detail"] for e in audits(ctx, "remove_feedback")] == [{"feedback": fid}]


# --- B-06 / B-07 printer addresses -------------------------------------------------------------
def test_printer_addresses_are_checked(ctx):
    ctx, a, app, login, seen = site(ctx)
    other = a.create_household("Lee's")
    a.create("lee", "lee password", role="admin", household_id=other)
    lee, anna = login("lee", "lee password"), login("anna", "anna password")
    base = {"name": "Label", "driver": "escpos"}
    for address in ("127.0.0.1:22", "127.0.0.1:9100", "0.0.0.0", "169.254.1.1", "/dev/usb/lp0", "10.0.0.5:5432"):
        assert lee.post("/admin/printers", json={**base, "address": address}).status_code == 400, address
    assert lee.post("/admin/printers", json={"name": "Dymo", "driver": "cups", "address": "Dymo"}).status_code == 400
    assert lee.post("/admin/printers", json={**base, "address": "192.0.2.10:9100"}).status_code == 200
    assert anna.post("/admin/printers", json={**base, "address": "/dev/usb/lp0"}).status_code == 200  # Home: fine
    for address in ("...", "192.168..50", "-", ":99999", "1.2.3.4:99999", "printer..lan"):
        assert anna.post("/admin/printers", json={**base, "address": address}).status_code == 400, address
    assert anna.post("/admin/printers", json={"name": "Q", "driver": "cups", "address": "-hevil.example"}).status_code == 400


def test_a_bad_saved_printer_address_doesnt_break_pages(ctx, monkeypatch):
    ctx, a, app, login, seen = site(ctx)
    bad = PrinterConfig(id="bad1", name="Typo", driver="escpos", address="...", width_px=576)
    with a.db.begin() as conn:
        conn.execute(insert(db.printers).values(id="bad1", household_id="home", position=0, config=asdict(bad)))
    boss, mike = login("boss", "boss password"), login("mike", "mike password")
    assert boss.get("/admin/printers/status").json() == {"bad1": False}
    assert mike.get("/setup").status_code == 200
    assert boss.get("/super/diagnostics").status_code == 200
    assert printer.is_reachable("...") is False and printer.is_reachable("1.2.3.4:99999") is False


def test_printer_checks_are_cached_and_audited(ctx, monkeypatch):
    ctx, a, app, login, seen = site(ctx)
    knocks = []
    monkeypatch.setattr(printer, "is_reachable", lambda p, timeout=2.0: knocks.append(p.id) or True)
    anna = login("anna", "anna password")
    pid = anna.post("/admin/printers", json={"name": "Label", "driver": "escpos", "address": "192.0.2.10"}).json()["id"]
    assert anna.get("/admin/printers/status").json() == {pid: True}
    assert anna.get("/admin/printers/status").json() == {pid: True}
    assert knocks == [pid] and len(audits(ctx, "printer_check")) == 1


# --- B-08 CalDAV and .ics addresses ------------------------------------------------------------
@contextlib.contextmanager
def http_server(answer):
    hits: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def _any(self):
            hits.append((self.command, self.path))
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            status, headers, body = answer(self)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        do_GET = do_PROPFIND = do_OPTIONS = do_REPORT = do_POST = do_PUT = _any

        def log_message(self, *args):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", hits
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("guarded", [True, False])
def test_caldav_redirects_to_private_addresses_are_never_followed(monkeypatch, guarded):
    with http_server(lambda h: (200, {}, b"router admin page")) as (private, private_hits), \
            http_server(lambda h: (301, {"Location": f"{private}/admin/internal"}, b"")) as (front, front_hits):
        monkeypatch.delenv("NEXTBOX_ALLOW_PRIVATE_URLS")
        real = provider_http.check_public_url
        # Pretend the first server is out on the internet; everything else gets the real check.
        monkeypatch.setattr(provider_http, "check_public_url",
                            lambda url, what="That address": url if url.startswith(front) else real(url, what))
        if not guarded:  # proves the test can see a followed redirect at all
            monkeypatch.setattr(provider_http, "guard_session", lambda session, what="": None)
        with pytest.raises(ProviderError):
            CalDAVTasks(f"{front}/dav/", "sam", "app-password").check()
        assert front_hits
        assert (private_hits == []) is guarded  # with the guard: refused before anything was sent


def test_caldav_errors_dont_echo_the_server(monkeypatch):
    with http_server(lambda h: (500, {"Content-Type": "text/html"}, b"SECRET-ROUTER-PAGE")) as (url, hits):
        with pytest.raises(ProviderError) as err:
            CalDAVTasks(f"{url}/dav/", "sam", "app-password").check()
    assert hits and "SECRET-ROUTER-PAGE" not in str(err.value) and "Couldn't reach" in str(err.value)


ICS = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-WR-CALNAME:Work\r\nEND:VCALENDAR\r\n"


def test_ics_feeds_connect_to_the_address_that_was_checked(monkeypatch):
    monkeypatch.delenv("NEXTBOX_ALLOW_PRIVATE_URLS")
    answers = {"pinned.example.com": "93.184.216.34", "evil.example.com": "127.0.0.1"}
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], port))])
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers["host"], request.extensions.get("sni_hostname")))
        if request.url.path == "/moved.ics":
            return httpx.Response(302, headers={"location": "https://evil.example.com/x.ics"})
        return httpx.Response(200, content=ICS)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert calendars.IcsFeed("https://pinned.example.com/a.ics", client=client).check()["account"] == "Work"
    # Connected to the IP that was checked, asking for (and verifying TLS as) the name.
    assert seen == [("93.184.216.34", "pinned.example.com", "pinned.example.com")]
    with pytest.raises(ProviderError, match="private"):
        calendars.IcsFeed("https://pinned.example.com/moved.ics", client=client).check()
    assert len(seen) == 2  # the private hop was never requested


def test_the_feed_cache_is_bounded_and_forgets_removed_calendars():
    for i in range(calendars.MAX_CACHED_FEEDS + 10):
        calendars._remember(f"https://feed{i}.example.com/a.ics", b"x")
    assert len(calendars._cache) == calendars.MAX_CACHED_FEEDS
    assert "https://feed0.example.com/a.ics" not in calendars._cache
    calendars.forget_feed("webcal://feed59.example.com/a.ics")
    assert "https://feed59.example.com/a.ics" not in calendars._cache


# --- B-09 sign in with Google: the same browser must finish it ---------------------------------
def test_signin_callback_needs_the_browser_and_account_that_started_it(ctx, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    from nextbox.providers import oauth
    ctx, a, app, login, seen = site(ctx)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("NEXTBOX_PUBLIC_URL", "https://nextbox.example.com")
    monkeypatch.setattr(oauth, "_token_request", lambda app, data, client=None: {
        "access_token": "A", "refresh_token": "R", "expires_in": 3600})
    c = TestClient(app, base_url="http://testserver", follow_redirects=False)
    c.post("/auth/login", json={"username": "mike", "password": "mike password"})
    r = c.get("/connect/google/tasks/start")
    assert "nb_oauth" in r.headers["set-cookie"] and "httponly" in r.headers["set-cookie"].lower()
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    c.post("/auth/login", json={"username": "sam", "password": "sam password"})  # someone else on this browser
    back = c.get(f"/connect/google/callback?state={state}&code=abc")
    assert "somewhere%20else" in back.headers["location"]
    assert not a.get("mike")["connection"] and not a.get("sam")["connection"]


# --- B-10 feedback names -----------------------------------------------------------------------
def test_feedback_hides_display_names_but_keeps_common_words(ctx):
    ctx, a, app, login, seen = site(ctx)
    a.create("will", "will password")
    a.create("print", "print password")
    a.set_prefs("sam", {"display_name": "Zorblax Quimby"})
    sam, mike, boss = login("sam", "sam password"), login("mike", "mike password"), login("boss", "boss password")
    item = sam.post("/feedback", json={"mode": "open", "message": "Signed, Zorblax Quimby"}).json()
    assert item["message"] == "Signed, [someone]"
    item = mike.post("/feedback", json={"mode": "open", "message":
                                        "My housemate Zorblax says the print button will not work. Thanks Will!"}).json()
    assert item["message"] == "My housemate [someone] says the print button will not work. Thanks [someone]!"
    reply = boss.patch(f"/admin/feedback/{item['id']}", json={"reply": "Thanks Quimby, the print button will be fixed"})
    assert reply.json()["reply"] == "Thanks [someone], the print button will be fixed"


# --- B-11 turned-off superadmins -----------------------------------------------------------------
def test_a_turned_off_superadmin_can_be_demoted_or_removed(ctx):
    ctx, a, app, login, seen = site(ctx)
    a.create("sup2", "sup2 password", role="superadmin")
    a.create("sup3", "sup3 password", role="superadmin")
    boss = login("boss", "boss password")
    assert boss.patch("/admin/users/sup2", json={"disabled": True}).status_code == 200
    assert boss.patch("/admin/users/sup3", json={"disabled": True}).status_code == 200
    assert boss.patch("/admin/users/sup2", json={"role": "user"}).status_code == 200
    assert boss.delete("/admin/users/sup3").status_code == 200
    with pytest.raises(AuthError, match="last superadmin"):  # boss is still the last active one
        a.update("boss", role="user")


# --- B-12 last active ----------------------------------------------------------------------------
def test_using_the_app_counts_as_active(ctx):
    ctx, a, app, login, seen = site(ctx)
    mike, boss = login("mike", "mike password"), login("boss", "boss password")
    with a.db.begin() as conn:
        conn.execute(update(db.users).where(db.users.c.username == "mike").values(
            last_login=int(time.time()) - 10 * 86400, last_seen=None))
    mike.get("/list")
    first = a.get("mike")["last_seen"]
    assert first and first >= time.time() - 60
    row = {r["username"]: r for r in boss.get("/super/users").json()}["mike"]
    assert row["active_7d"] and row["last_seen"] == first
    assert "mike" in [r["username"] for r in boss.get("/super/users?status=active").json()]
    assert {p["username"]: p for p in boss.get("/super/usage").json()["people"]}["mike"]["last_active"] >= first
    mike.get("/list")
    assert a.get("mike")["last_seen"] == first  # written at most hourly
    assert "last_seen" in login("anna", "anna password").get("/admin/users").json()[0]


# --- B-13 who did what to whom ---------------------------------------------------------------------
def test_audit_says_who_did_what_to_whom(ctx):
    ctx, a, app, login, seen = site(ctx)
    boss, anna = login("boss", "boss password"), login("anna", "anna password")
    boss.post("/admin/users/mike/reset-password")
    boss.post("/super/users/bulk", json={"usernames": ["mike", "sam"], "action": "beta_off"})
    bulk = audits(ctx, "bulk_users")
    assert {(e["target"], e["detail"]["op"], e["detail"]["result"]) for e in bulk} == {
        ("mike", "beta_off", "done"), ("sam", "beta_off", "done")}
    mine = boss.get("/super/events", params={"user": "mike", "kind": "audit"}).json()
    assert {e["action"] for e in mine} >= {"reset_password", "bulk_users"} and all(e["user"] == "boss" for e in mine)
    assert boss.get("/super/events", params={"target": "sam"}).json()[0]["action"] == "bulk_users"

    code = boss.post("/admin/invites", json={}).json()["code"]
    boss.delete(f"/admin/invites/{code}")
    assert audits(ctx, "revoke_invite")[0]["detail"] == {"code": code, "household_id": "home"}
    pid = boss.post("/admin/printers", json={"name": "Label", "driver": "escpos", "address": "192.0.2.10"}).json()["id"]
    boss.patch(f"/admin/printers/{pid}", json={"name": "Kitchen"})
    assert audits(ctx, "edit_printer")[0]["detail"] == {"printer": pid, "changed": ["name"]}

    boss.get("/super/users/mike")
    boss.get("/super/users/mike/tickets")
    anna.get("/admin/users/mike/activity")
    views = {(e["user"], e["detail"]["what"]) for e in audits(ctx, "view_user") if e["target"] == "mike"}
    assert views == {("boss", "details"), ("boss", "tickets"), ("anna", "activity")}


# --- B-14 masking in `nextbox db sql` ----------------------------------------------------------------
@SQLITE_ONLY
def test_db_sql_masking_cant_be_dodged_with_an_alias(tmp_path):
    a = Accounts(tmp_path)
    a.create("sam", "sam password")
    a.create_session("sam")
    Journal(a.db, "sam").add("2026-10-01", "Rough start, but the walk helped")
    for sql in ("SELECT notes AS n FROM journal", "SELECT notes || '' AS n FROM journal",
                "SELECT upper(notes) FROM journal", "SELECT length(notes) AS n FROM journal",
                "SELECT hash || '' AS h, salt || '' AS s FROM users", "SELECT token_hash AS t FROM sessions"):
        cols, found, _ = dbtools.run_sql(tmp_path, sql)
        assert "walk" not in str(found) and all(v in (None, "•••• (hidden)") for v in found[0]), sql
    assert dbtools.run_sql(tmp_path, "SELECT count(*) FROM journal WHERE notes LIKE '%walk%'")[1] == [[0]]
    assert dbtools.run_sql(tmp_path, "SELECT owner, day FROM journal")[1] == [["sam", "2026-10-01"]]
    # The app's own queries on the same connection pool still read everything.
    assert Journal(a.db, "sam").pages("2026-01-01", "2026-12-31")[0]["notes"].startswith("Rough")
    assert a.verify("sam", "sam password")


# --- B-15 plain answers, not server errors -------------------------------------------------------------
def test_gone_tasks_and_odd_inputs_get_plain_answers(ctx):
    ctx, a, app, login, seen = site(ctx)
    sam, mike, boss = login("sam", "sam password"), login("mike", "mike password"), login("boss", "boss password")
    theirs = mike.post("/list", json={"content": "Mike's"}).json()["id"]
    r = sam.post(f"/list/{theirs}/done")
    assert r.status_code == 404 and r.json()["detail"] == "That task was already removed."
    assert sam.post("/list/nb:99999/done").status_code == 404
    r = sam.patch("/list/nb:99999999999999999999999", json={"content": "x"})
    assert r.status_code == 404 and "int" not in r.text
    assert sam.post("/list/12345/done").status_code == 409  # a linked app that isn't connected
    assert sam.post("/print/task", json={"task_id": ""}).status_code == 404
    assert sam.post("/list", json={"content": "x", "steps": [1]}).status_code == 422
    steps = sam.post("/list", json={"content": "y", "steps": ["a", {"text": "b", "done": True}]}).json()["steps"]
    assert steps == [{"text": "a", "done": False}, {"text": "b", "done": True}]
    for path in ("/printed?limit=-1", "/printed?limit=99999999999999999999", "/scans?limit=0"):
        assert sam.get(path).status_code == 422, path
    assert boss.get("/super/events?limit=5000").status_code == 422
    assert boss.get("/admin/users/sam/activity?limit=-1").status_code == 422
    assert sam.get("/journal?until=9999-12-31").status_code == 400
    assert sam.get("/journal?until=1999-12-31").status_code == 400
    keep = sam.post("/list", json={"content": "Keep me"}).json()["id"]
    r = sam.post("/print/stickers", json={"task_ids": [keep, "nb:99999"], "dry_run": True}).json()
    assert len(r["manifest"]) == 1 and r["missing"] == ["nb:99999"]  # one gone doesn't sink the batch
    assert boss.get("/super/events", params={"user": "sam", "kind": "error"}).json() == []


def test_an_app_saying_no_is_shown_but_isnt_a_server_error(ctx, todo):
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    app = create_app(ctx)

    def refuse(task_id):
        raise ProviderError("Todoist said no (signed out or no permission). Reconnect it in Settings.")
    todo.get_task = refuse
    c = TestClient(app, base_url="http://testserver")
    c.post("/auth/login", json={"username": "boss", "password": "boss password"})
    r = c.post("/print/task", json={"task_id": "1"})
    assert r.status_code == 502 and "Reconnect" in r.json()["detail"]
    ev = Events(ctx.settings.data_dir).query(user="boss", ok=False)[0]
    assert ev["kind"] == "activity" and "trace" not in ev


# --- B-16 dates and settings --------------------------------------------------------------------------
def test_dates_must_look_like_dates(ctx):
    ctx, a, app, login, seen = site(ctx)
    sam = login("sam", "sam password")
    for bad in ("20261016", "2026-W42-5", "2026-13-01"):
        assert sam.post("/list", json={"content": "x", "due_date": bad}).status_code == 400, bad
        assert sam.post("/journal", json={"notes": "x", "day": bad}).status_code == 400, bad
    assert sam.post("/journal", json={"notes": "x", "day": "9999-12-31"}).status_code == 400
    ok = (date.today() - timedelta(days=3)).isoformat()
    assert sam.post("/journal", json={"notes": "x", "day": ok}).json()["day"] == ok
    assert sam.post("/list", json={"content": "z", "due_date": "2026-10-16"}).json()["due"]["date"] == "2026-10-16"
    for change in ({"max_rows": True}, {"slug_after": False}):
        assert sam.patch("/settings", json=change).status_code == 400
    assert sam.patch("/settings", json={"max_rows": 6}).json()["max_rows"] == 6


def test_odd_dates_already_saved_are_fixed_once(ctx):
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    now = int(time.time())
    with a.db.begin() as conn:
        conn.execute(insert(db.tasks).values(owner="sam", household_id="home", content="Odd", description="",
                                             priority=1, due_date="20261016", tags=[], steps=[], created=now))
        for pid, day in (("aaaaaaaaaaaa", "2026-W42-5"), ("bbbbbbbbbbbb", "9999-12-31")):
            conn.execute(insert(db.journal).values(id=pid, owner="sam", household_id="home", day=day,
                                                   created=now, updated=now, notes="n"))
    create_app(ctx)
    assert [t["due_date"] for t in rows(ctx.settings.data_dir, "tasks")] == ["2026-10-16"]
    days = {p["id"]: p["day"] for p in rows(ctx.settings.data_dir, "journal")}
    assert days == {"aaaaaaaaaaaa": "2026-10-16", "bbbbbbbbbbbb": date.fromtimestamp(now).isoformat()}


# --- B-17 size and rate limits -------------------------------------------------------------------------
def test_big_requests_are_refused_before_they_are_read(ctx):
    ctx, a, app, login, seen = site(ctx)
    sam = login("sam", "sam password")
    assert sam.post("/list/dump", json={"text": "x" * (300 * 1024)}).status_code == 413
    chunked = sam.post("/list/dump", content=(b"x" * 1024 for _ in range(300)),
                       headers={"content-type": "application/json"})
    assert chunked.status_code == 413  # no Content-Length: counted as it arrives
    r = sam.post("/scan", files={"photo": ("p.jpg", b"\0" * (17 * 1024 * 1024), "image/jpeg")})
    assert r.status_code == 413
    assert sam.post("/list/dump", json={"text": "Fine today"}).status_code == 200


def test_paid_and_outgoing_calls_are_rate_limited(ctx, monkeypatch):
    from nextbox import server

    class Tight(server._Window):
        def __init__(self, limit, seconds):
            super().__init__(2, seconds)
    monkeypatch.setattr(server, "_Window", Tight)
    ctx, a, app, login, seen = site(ctx)
    sam = login("sam", "sam password")
    seen.update(notes="hello")
    assert [sam.post("/scan", files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")}).status_code
            for _ in range(3)] == [200, 200, 429]
    assert login("mike", "mike password").post("/scan", files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")}).status_code == 200
    codes = [sam.post("/calendars", json={"kind": "nope", "fields": {}}).status_code for _ in range(3)]
    assert codes == [400, 400, 429]
    codes = [sam.put("/connection", json={"provider": "nope", "fields": {}}).status_code for _ in range(3)]
    assert codes == [400, 400, 429]


# --- B-18 staff invites -----------------------------------------------------------------------------------
def test_household_admins_dont_see_or_cancel_staff_invites(ctx):
    ctx, a, app, login, seen = site(ctx)
    boss, anna = login("boss", "boss password"), login("anna", "anna password")
    staff = boss.post("/admin/invites", json={"own_household": True, "note": "New customer Patel"}).json()["code"]
    join = anna.post("/admin/invites", json={"note": "Sam"}).json()["code"]
    listed = anna.get("/admin/invites").json()
    assert [i["code"] for i in listed] == [join] and "Patel" not in str(listed)
    assert anna.delete(f"/admin/invites/{staff}").status_code == 404
    assert {staff, join} <= {i["code"] for i in boss.get("/admin/invites").json()}
