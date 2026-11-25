"""Hosted mode (SPEC §35): NEXTBOX_HOSTED=1 means this Next Box runs at a hosting company. Every request is from the
internet, and there's no printer, home network or systemd where it runs.

Nothing here prints: hosted mode alone turns every print into a dry run (the fixture sets dry_run False on purpose),
and the no_real_printer fixture fails a test that reaches a printer. Nothing knocks on a network either: the fixture
fails the test if discovery or a printer check is tried."""
import dataclasses
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nextbox import coach as coaching
from nextbox import discover, doctor, jobs, printer, server, upkeep
from nextbox.auth import Accounts
from nextbox.config import Settings, app_version, load_settings
from nextbox.printers import Printers
from nextbox.server import create_app

from conftest import dump, rows

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
PRINTER = {"name": "Kitchen", "driver": "escpos", "address": "192.0.2.10:9100"}  # TEST-NET: never answers
FORWARDED = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}


def proxied(app):
    """What `nextbox serve` does behind the host's proxy: trust X-Forwarded-For and X-Forwarded-Proto."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    return ProxyHeadersMiddleware(app, trusted_hosts="*")


@pytest.fixture
def hosted(ctx, monkeypatch):
    """A hosted Next Box with a .env printer it must ignore (not a dry run by settings: hosted mode alone makes
    every print a preview). boss (staff) and mike in Home; kim with her own household, which has a printer row
    copied from the home PC. Nothing may look at a network."""
    for mod, name in ((discover, "find"), (discover, "read_media"), (printer, "is_reachable")):
        monkeypatch.setattr(mod, name, lambda *a, **k: pytest.fail(f"a hosted Next Box never calls {name}"))
    monkeypatch.delenv("NEXTBOX_HOSTING", raising=False)
    monkeypatch.delenv("NEXTBOX_FIRST_ADMIN", raising=False)
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    ctx.settings.dry_run = False
    ctx.settings.printer_ip = "192.0.2.20"
    ctx.settings.hosted = True
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("mike", "mike password")
    other = a.create_household("Kim's place")
    a.create("kim", "kim password", role="admin", household_id=other)
    # Kim's printer, as `nextbox db copy-to` would bring it over: in the table, never used here.
    Printers(ctx.settings.data_dir, Settings(data_dir=ctx.settings.data_dir)).add(PRINTER, other)
    ctx = dataclasses.replace(ctx, printers=Printers(ctx.settings.data_dir, ctx.settings))
    app = create_app(ctx)

    def client(scheme="https"):  # https, as the host's proxy presents it: the Secure cookie comes back on https only
        return TestClient(app, base_url=f"{scheme}://testserver")

    def login(name, pw):
        c = client()
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c

    people = {"boss": login("boss", "boss password"), "mike": login("mike", "mike password"),
              "kim": login("kim", "kim password")}
    return ctx, a, client, people, app


def add_task(c, text="Water the plants"):
    r = c.post("/list", json={"content": text, "due_date": date.today().isoformat()})
    assert r.status_code == 200
    return r.json()["id"]


# --- settings --------------------------------------------------------------------------------------------------------
def test_hosted_is_off_unless_the_environment_says_so(tmp_path, monkeypatch):
    assert Settings().hosted is False
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.delenv("NEXTBOX_HOSTED", raising=False)
    assert load_settings().hosted is False
    for value, want in (("1", True), ("true", True), ("yes", True), ("0", False), ("", False)):
        monkeypatch.setenv("NEXTBOX_HOSTED", value)
        assert load_settings().hosted is want, value


# --- every request is from the internet -------------------------------------------------------------------------------
def test_the_shared_key_never_works_and_https_is_assumed(hosted):
    ctx, a, client, p, app = hosted
    anon = client()
    for headers in ({"X-NextBox-Key": "k"}, {"X-NextBox-Key": "k", "CF-Ray": "abc"}):  # right key, no cf-* headers needed
        r = anon.get("/status", headers=headers)
        assert r.status_code == 403 and r.json()["detail"] == server.KEY_HOSTED, headers
        r = anon.post("/print/today", json={"source": "auto"}, headers=headers)
        assert r.status_code == 403 and r.json()["detail"] == server.KEY_HOSTED
    assert "sign in with an account" in server.KEY_HOSTED
    # HSTS on every answer, even though the host's proxy talks plain http to Next Box.
    assert "max-age" in anon.get("/health").headers.get("strict-transport-security", "")
    # /about names the host, or says "the hosting company" until NEXTBOX_HOSTING is set; never "Cloudflare" by guess.
    about = anon.get("/about").json()
    assert about["hosting"] == "the hosting company" and about["hosted"] is True
    os.environ["NEXTBOX_HOSTING"] = "Render"
    try:
        assert anon.get("/about").json()["hosting"] == "Render"
    finally:
        del os.environ["NEXTBOX_HOSTING"]
    # No key at all is fine for a hosted Next Box: build_default_app only insists on one at home.
    assert "not settings.hosted" in Path(server.__file__).read_text().split("def build_default_app")[1]


def test_cookies_are_secure_and_lockouts_count_the_forwarded_address(hosted):
    ctx, a, client, p, app = hosted
    # Plain http from the proxy: Secure anyway (hosted is only ever reached over the host's https).
    r = client("http").post("/auth/login", json={"username": "mike", "password": "mike password"})
    assert r.status_code == 200 and "Secure" in r.headers["set-cookie"] and "SameSite=lax" in r.headers["set-cookie"]
    # Through the proxy headers `nextbox serve` trusts: the forwarded address is the one that gets locked out.
    c = TestClient(proxied(app), base_url="http://testserver")
    for _ in range(5):
        assert c.post("/auth/login", json={"username": "mike", "password": "wrong"}, headers=FORWARDED).status_code == 401
    assert c.post("/auth/login", json={"username": "mike", "password": "mike password"}, headers=FORWARDED).status_code == 429
    other = {**FORWARDED, "X-Forwarded-For": "203.0.113.10"}
    r = c.post("/auth/login", json={"username": "mike", "password": "mike password"}, headers=other)
    assert r.status_code == 200 and "Secure" in r.headers["set-cookie"]
    # The events name the forwarded addresses (the fixture's own sign-ins went straight in, as "testclient").
    ips = [e["detail"].get("ip") for e in rows(ctx.settings.data_dir, "events") if e["action"] == "login"]
    assert ips.count("203.0.113.9") == 5 and ips.count("203.0.113.10") == 1


def test_at_home_the_proxy_headers_alone_make_the_cookie_secure(ctx):
    """Not hosted: behind the tunnel, X-Forwarded-Proto https (trusted from localhost) is what makes it Secure."""
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    Accounts(ctx.settings.data_dir).set_password("boss", "boss password", create=True)
    app = create_app(ctx)
    plain = TestClient(app, base_url="http://testserver").post("/auth/login", json={"username": "boss", "password": "boss password"})
    assert plain.status_code == 200 and "Secure" not in plain.headers["set-cookie"]
    forwarded = TestClient(proxied(app), base_url="http://testserver").post(
        "/auth/login", json={"username": "boss", "password": "boss password"}, headers=FORWARDED)
    assert forwarded.status_code == 200 and "Secure" in forwarded.headers["set-cookie"]


# --- nothing prints at the host ----------------------------------------------------------------------------------------
def test_nobody_prints_here_whatever_env_or_the_printers_table_say(hosted):
    ctx, a, client, p, app = hosted
    for name in ("boss", "mike", "kim"):
        me = p[name].get("/auth/me").json()
        assert me["prints_here"] is False and me["hosted"] is True and me["printers"] == 0, name
        s = p[name].get("/status").json()
        assert s["prints_here"] is False and s["hosted"] is True and s["printers"] == 0, name
        assert s["printer"] is None and s["printer_reachable"] is False and s["can_add_printer"] is False, name
    assert jobs.prints_here("home", [], hosted=True) is False and jobs.prints_here("home", []) is True
    assert jobs.prints_here("h2", [printer.legacy_config(ctx.settings)], hosted=True) is False
    assert ctx.printers.all("home") == [] and ctx.printers.all(a.get("kim")["household_id"]) == []
    assert rows(ctx.settings.data_dir, "printers")  # kim's row is still there for a later home print helper
    assert p["kim"].get("/printing-options").json()["printers"] == []
    assert p["boss"].get("/setup").json()["can_add_printer"] is False


def test_every_print_is_a_preview_with_the_hosted_words(hosted):
    ctx, a, client, p, app = hosted
    for name in ("boss", "mike", "kim"):  # staff with the .env printer, a Home user, a household with a printer row
        tid = add_task(p[name])
        r = p[name].post("/print/today", json={"dry_run": False})
        assert r.status_code == 200, r.text
        r = r.json()
        assert r["status"] == "dry_run" and r["dry_run"] is True and r["printer"] is None, name
        assert r["note"] == jobs.PRINT_AWAY_HOSTED, name
        r = p[name].post("/print/text", json={"text": "Buy milk", "dry_run": False}).json()
        assert r["status"] == "dry_run" and r["note"] == jobs.PRINT_AWAY_HOSTED, name
        r = p[name].post("/print/task", json={"task_id": tid, "dry_run": False}).json()
        assert r["status"] == "dry_run" and r["note"] == jobs.PRINT_AWAY_HOSTED, name
        r = p[name].post("/print/stickers", json={"task_ids": [tid]}).json()
        assert r["status"] == "dry_run" and r["note"] == jobs.PRINT_AWAY_HOSTED and r["kind"] == "stickers", name
        r = p[name].post("/print/focus", json={"task_id": tid}).json()
        assert r["status"] == "dry_run" and r["note"] == jobs.PRINT_AWAY_HOSTED, name
        # Print on paper and Save label image, the two paths people without a printer use, still work.
        assert p[name].get(f"/printed/{r['id']}/png?download=1").status_code == 200
    assert jobs.PRINT_AWAY_HOSTED == ("This Next Box runs online, so labels can't go straight to a printer. Tap Print on "
                                      "paper, or Save label image and open it in your label printer's app (like Brother "
                                      "iPrint&Label).")
    assert "test version" not in jobs.PRINT_AWAY_HOSTED and "Next Box computer" not in jobs.PRINT_AWAY_HOSTED


def test_home_assistants_auto_print_is_refused_and_never_claims_the_day(hosted):
    ctx, a, client, p, app = hosted
    add_task(p["boss"])
    r = p["boss"].post("/print/today", json={"source": "auto"})
    assert r.status_code == 403 and r.json()["detail"] == jobs.AUTO_PRINT_HOSTED
    assert p["boss"].post("/print/today", json={"source": "manual"}).json()["status"] == "dry_run"  # a person's tap works
    # The shared path too (the CLI's `today --auto` on such a box): skipped before the day's print is claimed.
    c = dataclasses.replace(ctx, user="boss", household="home")
    assert jobs.print_today(c, source="auto") == {"status": "skipped", "reason": jobs.AUTO_PRINT_HOSTED}
    assert ctx.store.claim_auto(date.today().isoformat(), True, scope="boss") is True  # nothing had claimed it
    assert ctx.store.claim_auto(date.today().isoformat(), False, scope="boss") is True


# --- no home network -----------------------------------------------------------------------------------------------------
def test_discovery_media_and_adding_printers_are_off_for_everyone(hosted):
    ctx, a, client, p, app = hosted
    for name in ("boss", "kim"):  # staff and a household admin alike
        r = p[name].post("/admin/printers/discover")
        assert r.status_code == 403 and r.json()["detail"] == jobs.NO_NETWORK_HOSTED, name
        r = p[name].get("/admin/printers/media", params={"address": "192.0.2.10"})
        assert r.status_code == 403 and r.json()["detail"] == jobs.NO_NETWORK_HOSTED, name
        r = p[name].post("/admin/printers", json=PRINTER)
        assert r.status_code == 403 and r.json()["detail"] == jobs.NO_PRINTER_HERE_HOSTED, name
        r = p[name].patch("/admin/printers/nope", json={"name": "x"})
        assert r.status_code == 403 and r.json()["detail"] == jobs.NO_PRINTER_HERE_HOSTED, name
        assert p[name].get("/admin/printers/status").json() == {}  # knocks on nothing (the fixture would fail it)
        assert p[name].get("/printing-options").json()["printers"] == []
    assert p["mike"].post("/admin/printers/discover").status_code == 403  # a plain user: as always
    assert len(rows(ctx.settings.data_dir, "printers")) == 1  # nothing was added
    for sentence in (jobs.NO_NETWORK_HOSTED, jobs.NO_PRINTER_HERE_HOSTED, jobs.AUTO_PRINT_HOSTED):
        assert sentence.count(". ") == 0 and sentence.endswith("."), sentence  # one sentence each
    with pytest.raises(Exception, match="hosted Next Box has no printers"):
        ctx.printers.add(PRINTER, "home")


# --- the first admin ---------------------------------------------------------------------------------------------------
def test_first_admin_is_made_once_on_an_empty_database_and_the_password_is_never_logged(ctx, monkeypatch, capsys):
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    monkeypatch.setenv("NEXTBOX_FIRST_ADMIN", "Mike:mike password")
    app = create_app(ctx)
    a = Accounts(ctx.settings.data_dir)
    mike = a.get("mike")
    assert mike and mike["role"] == "superadmin" and mike["must_change"] is True and a.list_users() == ["mike"]
    events = [e for e in rows(ctx.settings.data_dir, "events") if e["action"] == "first_admin"]
    assert len(events) == 1 and events[0]["user"] == "mike" and events[0]["kind"] == "audit"
    assert "mike password" not in dump(ctx.settings.data_dir) and "mike password" not in capsys.readouterr().err
    # Still set at the next start (and the one after): nothing more happens, nobody is made twice.
    create_app(ctx)
    monkeypatch.setenv("NEXTBOX_FIRST_ADMIN", "zed:zed password")
    create_app(ctx)
    assert a.list_users() == ["mike"] and a.get("zed") is None
    # Signing in works, and asks for a new password first (the host's settings page showed this one).
    c = TestClient(app, base_url="http://testserver")
    r = c.post("/auth/login", json={"username": "mike", "password": "mike password"})
    assert r.status_code == 200 and r.json()["must_change"] is True
    assert c.get("/auth/me").json()["must_change"] is True
    assert c.get("/list").status_code == 403  # until then, nothing else


def test_first_admin_does_nothing_with_accounts_or_a_bad_value(tmp_path, monkeypatch, capsys):
    a = Accounts(tmp_path)
    for bad in ("mike", "mike:short", "x:long enough password", ":mike password", "  "):
        monkeypatch.setenv("NEXTBOX_FIRST_ADMIN", bad)
        assert upkeep.first_admin(a, os.environ["NEXTBOX_FIRST_ADMIN"]) is None, bad
        assert not a.has_users(), bad
    err = capsys.readouterr().err
    assert err.count(upkeep.FIRST_ADMIN_BAD) == 4 and "short" not in err and "long enough" not in err  # "  ": silent
    assert upkeep.first_admin(a, None) is None and upkeep.first_admin(a, "") is None
    a.set_password("boss", "boss password", create=True)
    assert upkeep.first_admin(a, "mike:mike password") is None and a.get("mike") is None  # someone's there already


# --- the owner's hints -----------------------------------------------------------------------------------------------------
def test_the_update_banner_says_the_host_redeploys(hosted, monkeypatch):
    ctx, a, client, p, app = hosted
    import httpx

    class Answer:
        status_code, text = 200, '[project]\nversion = "99.0.0"\n'
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Answer())
    monkeypatch.setenv("NEXTBOX_HOSTING", "Render")
    r = p["boss"].get("/super/update-check").json()
    assert r["update_available"] is True and r["latest"] == "99.0.0" and r["current"] == app_version()
    assert r["how"] == "Render redeploys when the branch is pushed."
    assert "update.sh" not in r["how"]
    assert p["mike"].get("/super/update-check").status_code == 403


def test_the_no_ai_key_hint_points_at_the_hosts_settings(hosted):
    ctx, a, client, p, app = hosted
    t = p["boss"].post("/list", json={"content": "Call the vet"}).json()
    r = p["boss"].post(f"/list/{t['id']}/breakdown")
    assert r.status_code == 503 and r.json()["detail"] == server.NO_KEY_OWNER_HOSTED
    assert ".env" not in server.NO_KEY_OWNER_HOSTED and "systemctl" not in server.NO_KEY_OWNER_HOSTED
    assert "ANTHROPIC_API_KEY" in server.NO_KEY_OWNER_HOSTED and "redeploy" in server.NO_KEY_OWNER_HOSTED
    t = p["mike"].post("/list", json={"content": "Call the vet"}).json()
    assert p["mike"].post(f"/list/{t['id']}/breakdown").json()["detail"] == coaching.NO_KEY  # everyone else: as before


# --- doctor -----------------------------------------------------------------------------------------------------------------
def test_doctor_skips_what_a_host_doesnt_have_and_says_so(tmp_path, monkeypatch, capsys):
    for name in ("_service_state", "_firewall", "_printer_checks", "_page_answers", "_cloudflared_on", "_lan_ip"):
        monkeypatch.setattr(doctor, name, lambda *a, **k: pytest.fail(f"doctor must not run {name} when hosted"))
    monkeypatch.setattr(printer, "is_reachable", lambda *a, **k: pytest.fail("doctor must not knock on a printer"))
    monkeypatch.setenv("NEXTBOX_HOSTING", "Render")
    monkeypatch.delenv("NEXTBOX_PUBLIC_URL", raising=False)
    settings = Settings(server_key="", data_dir=tmp_path, hosted=True, printer_ip="192.0.2.20")
    accounts = Accounts(tmp_path)
    code = doctor.run_doctor(settings, accounts)
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == f"• Next Box {app_version()}"
    assert lines[1] == "• " + doctor.HOSTED_SKIPPED.format(host="Render")
    kind = "PostgreSQL" if os.environ.get("DATABASE_URL") else "SQLite"  # the suite runs on both
    assert f"✓ database: {kind}" in out and "✗ no sign-in accounts yet" in out and "NEXTBOX_FIRST_ADMIN" in out
    assert "the shared Next Box key is off here" in out and "NEXTBOX_KEY is missing" not in out
    assert "✗ ANTHROPIC_API_KEY is missing" in out and "host's environment settings" in out
    assert "no printer here: everyone prints from their own device" in out
    assert "NEXTBOX_PUBLIC_URL isn't set" in out
    for never in ("systemctl", "firewall", "phone", "background service is", "PRINTER_IP", "192.0.2.20", "install.sh"):
        assert never not in out, never
    assert code == 1  # two things to fix (accounts, the AI key)
    accounts.set_password("boss", "boss password", create=True)
    settings.anthropic_api_key = "sk-test-not-real"
    assert doctor.run_doctor(settings, accounts) == 0
    out = capsys.readouterr().out
    assert "✓ 1 sign-in account(s)" in out and "✓ ANTHROPIC_API_KEY is set" in out and "sk-test" not in out
    # --json: the same checks, one object.
    assert doctor.run_doctor(settings, accounts, as_json=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["checks"][1]["text"].startswith("hosted on Render") and report["problems"] == 0
    assert "sk-test" not in json.dumps(report)


def test_doctor_at_home_is_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_service_state", lambda: "no-systemd")
    monkeypatch.delenv("NEXTBOX_PUBLIC_URL", raising=False)
    monkeypatch.delenv("NEXTBOX_HOSTING", raising=False)
    doctor.run_doctor(Settings(server_key="k", data_dir=tmp_path), Accounts(tmp_path))
    out = capsys.readouterr().out
    assert "hosted on" not in out and "✓ NEXTBOX_KEY is set" in out and "no background service here" in out


# --- the web app -------------------------------------------------------------------------------------------------------------
def js_const(js: str, name: str) -> str:
    start = js.index(f"const {name} = ")
    expr = js[start:js.index(";\n", start)]
    return "".join(json.loads(s) for s in re.findall(r'"(?:[^"\\]|\\.)*"', expr))


def test_the_app_says_the_same_words_and_offers_no_printer_to_add():
    js = (WEB / "app.js").read_text()
    assert js.count("const PRINT_AWAY_HOSTED = ") == 1 and js_const(js, "PRINT_AWAY_HOSTED") == jobs.PRINT_AWAY_HOSTED
    assert "const hosted = () => me?.hosted === true;" in js
    assert "const canAddPrinter = () => !hosted() && !!me && RANK[me.role] >= RANK.admin;" in js
    # Every place that talks about printing picks the hosted words when the server says hosted.
    for where, marker in (("Preview only note", "const why = st.printing_paused"),
                          ("Settings printers", '$("#noPrinterNext").textContent'),
                          ("Get started", "  printer: { title:"),
                          ("setup answer", "function quizSummary"),
                          ("Admin printers", '$("#prAway").textContent')):
        chunk = js[js.index(marker):js.index(marker) + 900]
        assert "hosted() ? PRINT_AWAY_HOSTED : PRINT_AWAY" in chunk, where
    # The walkthrough never opens, Admin → Printers puts the add row and the Advanced form away.
    setup = js[js.index("async function openPrinterSetup("):]
    assert setup.index("if (hosted()) return toast(PRINT_AWAY_HOSTED") < setup.index("if (!canAddPrinter())")
    assert '$("#prAddRow").hidden = $("#npForm").hidden = hosted();' in js
    assert '$("#prSameNet").hidden = !printerAway() || hosted();' in js
    html = (WEB / "index.html").read_text()
    assert '<div class="row" id="prAddRow"><button type="button" class="go" id="prAdd"' in html
    # .row is display:flex, which beats the browser's own [hidden] rule: the row needs a rule of its own, or the
    # Add a printer button stays on screen at a hosted Next Box with hidden=true (seen in a real browser).
    assert "#prAddRow[hidden] { display:none; }" in html
    # The owner's no-key hint, in the server's words for a host.
    assert js_const(js, "NO_KEY_STEPS_HOSTED") == server.NO_KEY_OWNER_HOSTED.split("This needs an AI key. ")[1]
    assert "`This needs an AI key. ${NO_KEY_STEPS_HOSTED}`" in js and "? keySteps() :" in js
    # The Privacy page can say who runs the server; About keeps "you reach it through".
    privacy = (WEB / "privacy.html").read_text()
    assert 'data-needs="hosted"' in privacy and "runs the server this Next Box lives on" in privacy
    assert 'data-needs="hosting"' in privacy  # the carrier line stays: a host carries the traffic too
    # No "Move to another PC" or tunnel hints anywhere in the app.
    assert "another PC" not in js and "tunnel" not in js.lower() and "cloudflared" not in js.lower()


def test_the_spec_and_the_working_rules_cover_hosted_mode():
    spec = (ROOT / "SPEC.md").read_text()
    assert "## 35. Hosted mode" in spec
    section = spec.split("## 35. Hosted mode")[1]
    for word in ("NEXTBOX_HOSTED=1", "PRINT_AWAY_HOSTED", "NO_NETWORK_HOSTED", "NEXTBOX_FIRST_ADMIN", "KEY_HOSTED",
                 "Secure", "redeploys when the branch is pushed", "HOSTED_SKIPPED", "tests/test_hosted.py"):
        assert word in section, word
    guide = (ROOT / "CLAUDE.md").read_text()
    assert "LAN only for now" not in guide
    assert "NEXTBOX_HOSTED=1" in guide and "Settings.hosted" in guide and "PRINT_AWAY_HOSTED" in guide
    assert "nothing prints\n  at the host" in guide or "nothing prints at the host" in guide.replace("\n  ", " ")
    assert "NEXTBOX_FIRST_ADMIN" in guide and "never logs the password" in guide


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(hosted):
    """The hosted app on a real port, for a browser. boss has agreed to the notice and done the setup questions, so
    the app opens straight on My list."""
    import uvicorn
    ctx, a, client, p, app = hosted
    a.record_consent("boss", server.DATA_NOTICE_VERSION, terms=server.TERMS_VERSION)
    a.set_prefs("boss", {"setup_quiz_done": True})
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    yield port
    srv.should_exit = True
    thread.join(5)


def test_in_a_browser_the_hosted_words_show_and_nothing_offers_a_printer(served):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    if subprocess.run([node, "-e", "require('playwright')"], capture_output=True).returncode:
        pytest.skip("playwright isn't installed for node")
    script = """
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const out = { errors: [] };
  page.on('pageerror', (e) => out.errors.push(String(e)));
  await page.goto(`http://localhost:${process.env.PORT}/app/`);
  await page.fill('#user', 'boss'); await page.fill('#pass', 'boss password');
  await page.click('#loginForm button[type=submit]');
  await page.waitForSelector('#appShell:not([hidden])');
  await page.waitForFunction(() => document.querySelector('#dryWhy').textContent.length > 0);
  await page.waitForFunction(() => statusNow !== null);
  out.hosted = await page.evaluate(() => hosted());
  out.canAdd = await page.evaluate(() => canAddPrinter());
  out.why = await page.textContent('#dryWhy');
  out.locked = await page.evaluate(() => $('#dry').disabled && $('#dry').checked);
  out.status = await page.textContent('#status');
  await page.evaluate(() => goto('settings'));
  await page.waitForFunction(() => document.querySelector('#noPrinterNext').textContent.length > 0);
  out.settingsNext = await page.textContent('#noPrinterNext');
  out.settingsButtons = await page.evaluate(() => [$('#noPrinterGo').hidden, $('#labelAddRow').hidden]);
  await page.evaluate(() => goto('admin'));
  await page.waitForFunction(() => !document.querySelector('#prAway').hidden);
  out.adminAway = await page.textContent('#prAway');
  out.adminHidden = await page.evaluate(() => [$('#prAddRow').hidden, $('#npForm').hidden, $('#prSameNet').hidden]);
  out.toast = await page.evaluate(async () => { await openPrinterSetup({ from: 'admin' }); return [$('#pwDialog').open, document.body.textContent.includes('runs online')]; });
  await browser.close();
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=120,
                       env={**os.environ, "PORT": str(served)})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["errors"] == []
    assert out["hosted"] is True and out["canAdd"] is False
    assert out["why"] == jobs.PRINT_AWAY_HOSTED and out["locked"] is True
    assert out["status"] == "Previews · print from your device"
    assert out["settingsNext"] == jobs.PRINT_AWAY_HOSTED and out["settingsButtons"] == [True, True]
    assert out["adminAway"] == jobs.PRINT_AWAY_HOSTED and out["adminHidden"] == [True, True, True]
    assert out["toast"] == [False, True]  # the walkthrough stayed closed; the hosted words were shown instead


# --- the second review: one small process, private addresses, the words a hosted owner sees ---------------------------
def test_health_answers_while_a_snap_is_being_read(hosted):
    """/scan decodes and reads back in a worker thread: a minute-long AI call must not stop /health (the host would
    stop sending traffic after 15 s of failed checks and restart the server after 60)."""
    from conftest import tiny_jpeg

    from nextbox.scan import ReadBack
    ctx, a, client, p, app = hosted
    started, release = threading.Event(), threading.Event()

    def slow_vision(image, media_type, manifest):
        started.set()
        release.wait(10)
        return ReadBack(label_code=None, rows=[], notes="", win="")
    slow = create_app(ctx, vision=slow_vision)
    with TestClient(slow, base_url="https://testserver") as c:  # one event loop for every request, as in uvicorn
        assert c.post("/auth/login", json={"username": "mike", "password": "mike password"}).status_code == 200
        answers = {}
        t = threading.Thread(target=lambda: answers.setdefault("scan", c.post(
            "/scan", files={"photo": ("p.jpg", tiny_jpeg((800, 600)), "image/jpeg")})))
        t.start()
        try:
            assert started.wait(10), "the read-back never started"
            t0 = time.monotonic()
            assert c.get("/health").json() == {"ok": True}
            assert time.monotonic() - t0 < 2, "/health waited for the read-back"
        finally:
            release.set()
            t.join(15)
        assert answers["scan"].status_code == 200, answers["scan"].text


def test_photos_decode_small_and_hosted_caps_whole_decodes():
    """prepare_photo shrinks before turning upright (no full-size copy), a JPEG decodes small at any size, and a
    hosted Next Box refuses a PNG or HEIC over HOSTED_MAX_PIXELS with words a person can act on."""
    import io

    from PIL import Image

    from nextbox import friends, scan
    exif = Image.Exif()
    exif[0x0112] = 6  # stored sideways: turned upright after the shrink
    jpeg = io.BytesIO()
    Image.new("RGB", (6000, 4500), "white").save(jpeg, "JPEG", exif=exif.tobytes())
    data, media = scan.prepare_photo(jpeg.getvalue(), scan.HOSTED_MAX_PIXELS)
    assert media == "image/jpeg" and Image.open(io.BytesIO(data)).size == (1536, 2048)
    png = io.BytesIO()
    Image.new("1", (6000, 4500)).save(png, "PNG")  # 27 MP: fine at home, too many for a hosted Next Box
    with pytest.raises(scan.PhotoError) as e:
        scan.prepare_photo(png.getvalue(), scan.HOSTED_MAX_PIXELS)
    assert str(e.value) == scan.TOO_MANY_PIXELS and "24 MP" in scan.TOO_MANY_PIXELS
    assert max(Image.open(io.BytesIO(scan.prepare_photo(png.getvalue())[0])).size) == scan.MAX_EDGE  # at home
    pal = io.BytesIO()
    Image.new("P", (3000, 1000), 3).save(pal, "PNG")  # a palette picture is resized by colour
    assert Image.open(io.BytesIO(scan.prepare_photo(pal.getvalue())[0])).size == (2048, 683)
    # Friends' photos: the same cap for pictures that decode whole, none for a JPEG.
    with pytest.raises(friends.FriendError) as e:
        friends.clean_photo(png.getvalue(), scan.HOSTED_MAX_PIXELS)
    assert e.value.status == 413 and "JPEG" in e.value.message
    assert friends.clean_photo(jpeg.getvalue(), scan.HOSTED_MAX_PIXELS)
    src = Path(server.__file__).read_text()
    do_scan = src[src.index("async def do_scan("):src.index("def _scan_reply(")]
    assert "run_in_threadpool(scan.prepare_photo" in do_scan and "run_in_threadpool(run," in do_scan
    assert "_picture_slot()" in do_scan and "picture_slots.release()" in do_scan


def test_private_addresses_stay_refused_when_hosted_whatever_the_env_says(monkeypatch, tmp_path, capsys):
    from nextbox.providers import http
    monkeypatch.setenv("NEXTBOX_ALLOW_PRIVATE_URLS", "1")
    monkeypatch.delenv("NEXTBOX_HOSTED", raising=False)
    assert http.allow_private() is True  # at home, for a CalDAV server on the LAN
    monkeypatch.setenv("NEXTBOX_HOSTED", "1")
    assert http.allow_private() is False
    for url in ("http://10.0.0.5/dav", "https://10.0.0.5/dav", "https://169.254.169.254/latest", "https://localhost/x"):
        with pytest.raises(Exception):
            http.check_public_url(url)
    # doctor says the setting is ignored here, and flags a "*" trust list.
    monkeypatch.setenv("NEXTBOX_HOSTING", "Render")
    settings = Settings(data_dir=tmp_path, hosted=True, forwarded_allow_ips="*")
    doctor.run_doctor(settings, Accounts(tmp_path))
    out = capsys.readouterr().out
    assert "NEXTBOX_ALLOW_PRIVATE_URLS is ignored here" in out and 'NEXTBOX_FORWARDED_ALLOW_IPS is "*"' in out


def test_spoofed_forwarded_addresses_dont_get_past_the_lockout(hosted):
    """Through uvicorn's proxy middleware with the hosted trust list, a made-up X-Forwarded-For entry is ignored:
    the address the host's proxy added is the one that's locked out."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from nextbox.config import HOSTED_FORWARDED_ALLOW_IPS
    ctx, a, client, p, app = hosted
    inner = ProxyHeadersMiddleware(app, trusted_hosts=HOSTED_FORWARDED_ALLOW_IPS)

    async def from_render(scope, receive, send):  # the test client connects as "testclient": stand in for 10.0.0.2
        if scope["type"] == "http":
            scope = {**scope, "client": ("10.0.0.2", 40000)}
        await inner(scope, receive, send)
    c = TestClient(from_render, base_url="https://testserver")
    codes = []
    for i in range(7):
        h = {"X-Forwarded-For": f"203.0.113.{i}, 198.51.100.7", "X-Forwarded-Proto": "https"}
        codes.append(c.post("/auth/login", json={"username": "nobody-check", "password": "wrong"}, headers=h).status_code)
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429], codes


def test_hosted_words_name_the_host_not_a_next_box_computer(hosted, monkeypatch):
    ctx, a, client, p, app = hosted
    src = Path(server.__file__).read_text()
    assert "Shell" in server.PROTECTED_HOSTED and "Next Box computer" not in server.PROTECTED_HOSTED
    assert "NEXTBOX_FIRST_ADMIN" in server.NO_ACCOUNTS_HOSTED and "desktop" not in server.NO_ACCOUNTS_HOSTED
    assert "UPDATE_HINT_HOSTED if ctx.settings.hosted" in src
    js = (WEB / "app.js").read_text()
    assert "Only a shell on the server can change it" in js
    # /auth/me names the host for the "what's new" line; the notice and the Terms changed for the move.
    monkeypatch.setenv("NEXTBOX_HOSTING", "Render")
    me = p["mike"].get("/auth/me").json()
    assert me["hosting"] == "Render" and me["hosted"] is True
    assert server.TERMS_VERSION == 7 and "#   7: hosted at a hosting company" in src
    assert server.DATA_NOTICE_VERSION == 4 and "kept by this Next Box" in server.DATA_NOTICE
    assert "computer" not in server.DATA_NOTICE
    assert "Next Box now runs at ${me.hosting" in js
    privacy = (WEB / "privacy.html").read_text()
    assert "server run by <span data-operator>" in privacy
    assert '<span data-needs="hosted" hidden> at <span data-hosting>' in privacy
    # Someone who agreed to the Terms before the move is asked again.
    a.record_consent("mike", server.DATA_NOTICE_VERSION, terms=6)
    me = p["mike"].get("/auth/me").json()
    assert me["terms_ok"] is False and me["notice_changed"] is True
