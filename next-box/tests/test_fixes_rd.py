"""Docs, static pages and owner setup: fixes from the RD batch of the audit."""
import dataclasses
import socket
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import rows, tiny_jpeg
from nextbox import coach as coaching
from nextbox import doctor, printer, scan, server
from nextbox.auth import Accounts
from nextbox.config import PROJECT_DIR, Settings, app_version
from nextbox.printer import legacy_config
from nextbox.printers import EXAMPLE_PRINTER_IP, Printers
from nextbox.scan import ReadBack
from nextbox.server import create_app

WEB = Path(server.WEB_DIR)


def site(ctx, vision=None, key="", coach=None):
    """boss (superadmin) and sam (user), My list only. No vision and no key unless given."""
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    ctx.settings.anthropic_api_key = key
    a = Accounts(ctx.settings.data_dir)
    if not a.get("boss"):
        a.set_password("boss", "boss password", create=True)
        a.create("sam", "sam password")
    app = create_app(ctx, vision=vision, coach=coach)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, login


def photo():
    return {"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")}


# ------------------------------------------------------------------ R-D01: no AI key
def test_without_a_key_snap_says_so_plainly_and_never_calls_or_counts(ctx):
    ctx, a, login = site(ctx)
    sam, boss = login("sam", "sam password"), login("boss", "boss password")
    me = sam.get("/auth/me").json()
    assert me["scan_ready"] is False and me["ai_ready"] is False
    for _ in range(35):  # more than the hour's 30 photos: none of them count, so no 429
        r = sam.post("/scan", files=photo())
        assert r.status_code == 503 and r.json()["detail"] == coaching.NO_KEY
    r = boss.post("/scan", files=photo())
    assert r.status_code == 503 and r.json()["detail"] == coaching.NO_KEY_OWNER
    assert "ANTHROPIC_API_KEY" in coaching.NO_KEY_OWNER and "systemctl --user restart nextbox" in coaching.NO_KEY_OWNER
    assert ctx.store.list_scans(10, by="sam") == [] and rows(ctx.settings.data_dir, "journal") == []


def test_try_again_without_a_key_says_so_instead_of_failing_again(ctx):
    def broken(image, media_type, manifest):
        raise TypeError("Could not resolve authentication method")
    ctx, a, login = site(ctx, vision=broken)
    rec = login("sam", "sam password").post("/scan", files=photo()).json()
    assert rec["status"] == "failed" and rec["journal_id"]  # kept, to try again
    # The same Next Box, now with no key (the stand-in vision is gone).
    _, _, login = site(ctx)
    r = login("sam", "sam password").post(f"/scan/{rec['id']}/retry")
    assert r.status_code == 503 and r.json()["detail"] == coaching.NO_KEY
    r = login("boss", "boss password").post(f"/scan/{rec['id']}/retry")
    assert r.status_code == 404  # still only the owner's own read-back


def test_snap_and_the_ai_helpers_are_ready_with_a_key_and_the_key_never_shows(ctx):
    ctx, a, login = site(ctx, key="sk-test-not-real")
    me = login("sam", "sam password").get("/auth/me")
    assert me.json()["scan_ready"] is True and me.json()["ai_ready"] is True
    assert "sk-test" not in me.text


def test_scan_ready_is_separate_from_a_stand_in_coach(ctx):
    class Coach:
        def breakdown(self, message):
            return coaching.Breakdown(steps=["Open the bank app"])
    ctx, a, login = site(ctx, coach=Coach())
    me = login("sam", "sam password").get("/auth/me").json()
    assert me["ai_ready"] is True and me["scan_ready"] is False


def test_break_it_down_without_a_key_tells_the_owner_how_to_add_one(ctx):
    ctx, a, login = site(ctx)
    boss = login("boss", "boss password")
    t = boss.post("/list", json={"content": "Call the vet"}).json()
    r = boss.post(f"/list/{t['id']}/breakdown")
    assert r.status_code == 503 and r.json()["detail"] == coaching.NO_KEY_OWNER
    sam = login("sam", "sam password")
    t = sam.post("/list", json={"content": "Call the vet"}).json()
    assert sam.post(f"/list/{t['id']}/breakdown").json()["detail"] == coaching.NO_KEY


def test_cli_scan_without_a_key_says_how_to_add_one(tmp_path, monkeypatch, capsys):
    from nextbox import cli
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(tmp_path / "data"))
    called = []
    monkeypatch.setattr(scan, "claude_vision", lambda settings: called.append(1))
    (tmp_path / "p.jpg").write_bytes(tiny_jpeg())
    assert cli.main(["scan", str(tmp_path / "p.jpg")]) == 1
    assert coaching.NO_KEY_OWNER in capsys.readouterr().err and called == []


# ------------------------------------------------------------------ doctor (R-D01, R-D03, R-D09, R-D10, R-D12)
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_doctor(tmp_path, capsys, **kw) -> str:
    settings = Settings(server_key="k", data_dir=tmp_path, port=_free_port(), **kw)
    doctor.run_doctor(settings, Accounts(tmp_path))
    return capsys.readouterr().out


def test_doctor_says_its_version_and_whether_the_ai_key_is_there(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("NEXTBOX_PUBLIC_URL", raising=False)
    out = run_doctor(tmp_path, capsys)
    assert out.splitlines()[0] == f"• Next Box {app_version()}"
    assert "✗ ANTHROPIC_API_KEY is missing: Snap (marks and handwriting) and the AI helpers won't work" in out
    assert "systemctl --user restart nextbox" in out
    out = run_doctor(tmp_path, capsys, anthropic_api_key="sk-test-not-real")
    assert "✓ ANTHROPIC_API_KEY is set" in out and "sk-test" not in out


def test_doctor_calls_the_example_printer_address_what_it_is(tmp_path, capsys, monkeypatch):
    knocked = []
    monkeypatch.setattr(printer, "is_reachable", lambda p: knocked.append(p) or False)
    out = run_doctor(tmp_path, capsys, printer_ip=EXAMPLE_PRINTER_IP, dry_run=True)
    assert "✗ PRINTER_IP is still the example address" in out
    assert "Admin → Printers" in out and "NEXTBOX_DRY_RUN=1" in out
    assert "does not answer" not in out and knocked == []  # not a hardware fault: nothing to knock on


def test_doctor_notes_heic_and_an_unnamed_host(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(scan, "heic_supported", lambda: False)
    monkeypatch.setenv("NEXTBOX_PUBLIC_URL", "https://nextbox.example.com")
    monkeypatch.delenv("NEXTBOX_HOSTING", raising=False)
    out = run_doctor(tmp_path, capsys)
    assert "• iPhone HEIC photos can't be opened" in out and ".[heic]" in out
    assert "NEXTBOX_HOSTING=Cloudflare" in out
    monkeypatch.setenv("NEXTBOX_HOSTING", "Cloudflare")
    assert "NEXTBOX_HOSTING isn't" not in run_doctor(tmp_path, capsys)


# ------------------------------------------------------------------ R-D09: the example printer address
def test_the_example_printer_address_is_never_added_as_a_printer(tmp_path):
    settings = Settings(printer_ip=EXAMPLE_PRINTER_IP, data_dir=tmp_path)
    assert Printers(tmp_path, settings).all() == []
    settings.printer_ip = "192.168.1.77"  # the owner typed the real one later: it's added then
    assert [p.address for p in Printers(tmp_path, settings).all()] == ["192.168.1.77"]


def test_an_older_seeded_example_printer_gets_the_real_address(tmp_path):
    old = Settings(printer_ip=EXAMPLE_PRINTER_IP, data_dir=tmp_path)
    reg = Printers(tmp_path, old)
    reg._insert(legacy_config(old), "home")  # what versions before this one did
    assert [p.address for p in reg.all()] == [EXAMPLE_PRINTER_IP]  # still the example: left as is
    new = Settings(printer_ip="192.168.1.77", data_dir=tmp_path)
    fixed = Printers(tmp_path, new).all()
    assert [(p.id, p.address) for p in fixed] == [("default", "192.168.1.77")]
    assert [p.address for p in Printers(tmp_path, Settings(data_dir=tmp_path)).all()] == ["192.168.1.77"]  # saved


def test_a_printer_someone_added_is_never_rewritten(tmp_path):
    old = Settings(printer_ip=EXAMPLE_PRINTER_IP, data_dir=tmp_path)
    reg = Printers(tmp_path, old)
    added = reg.add({"name": "Office", "driver": "zpl", "address": EXAMPLE_PRINTER_IP})
    new = Settings(printer_ip="192.168.1.77", data_dir=tmp_path)
    assert [(p.id, p.address) for p in Printers(tmp_path, new).all()] == [(added.id, EXAMPLE_PRINTER_IP)]


# ------------------------------------------------------------------ R-D02: Journal mode off, and what's kept
def test_journal_mode_off_keeps_only_photos_with_handwriting(ctx):
    seen = {}

    def vision(image, media_type, manifest):
        return ReadBack(label_code=None, rows=[], notes=seen.get("notes", ""), win=seen.get("win", ""))
    ctx, a, login = site(ctx, vision=vision)
    sam = login("sam", "sam password")
    assert sam.patch("/settings", json={"journal_mode": False}).status_code == 200
    seen.update(notes="Walk helped.")
    rec = sam.post("/scan", files=photo()).json()
    assert rec["journal_id"]
    page = sam.get("/journal").json()["days"][0]["pages"][0]
    assert page["has_photo"] and page["notes"] == "Walk helped."
    seen.update(notes="", win="Called the bank")
    assert sam.post("/scan", files=photo()).json()["journal_id"]  # a win is handwriting too
    kept = len(rows(ctx.settings.data_dir, "journal"))
    seen.update(notes="", win="")
    rec = sam.post("/scan", files=photo()).json()
    assert "journal_id" not in rec and len(rows(ctx.settings.data_dir, "journal")) == kept


def test_privacy_policy_says_what_journal_mode_off_keeps_and_is_agreed_again(ctx):
    ctx, a, login = site(ctx)
    page = TestClient(create_app(ctx)).get("/app/privacy.html").text
    assert "With it off, the photo isn't kept" not in page and "to stop keeping photos" not in page
    assert "a photo is kept only" in page and "has handwriting (notes or a win)" in page
    assert "to keep only photos with handwriting" in page
    assert server.TERMS_VERSION >= 3  # more is kept than the old policy said: everyone reads it again
    a.record_consent("sam", server.DATA_NOTICE_VERSION, terms=2)
    me = login("sam", "sam password").get("/auth/me").json()
    assert me["terms_ok"] is False and me["notice_changed"] is True
    hint = (WEB / "index.html").read_text()
    assert "With it off, a photo is kept only when it has handwriting (notes or a win)." in hint


# ------------------------------------------------------------------ R-D03: who carries the traffic
def test_about_names_cloudflare_when_the_request_came_through_it(ctx, monkeypatch):
    monkeypatch.delenv("NEXTBOX_HOSTING", raising=False)
    anon = TestClient(create_app(ctx))
    assert anon.get("/about").json()["hosting"] == ""  # the home network
    assert anon.get("/about", headers={"cf-ray": "8a1b2c3d4e5f-LHR"}).json()["hosting"] == "Cloudflare"
    monkeypatch.setenv("NEXTBOX_HOSTING", "Cloudflare Tunnel")
    assert anon.get("/about").json()["hosting"] == "Cloudflare Tunnel"  # the owner's own words win


def test_going_online_names_the_host():
    script = (PROJECT_DIR / "scripts" / "go-online.sh").read_text()
    assert "NEXTBOX_HOSTING=Cloudflare" in script and "^NEXTBOX_HOSTING=" in script


# ------------------------------------------------------------------ R-D06: updating
def test_update_instructions_use_the_path_the_install_made(ctx, monkeypatch):
    import httpx

    class R:
        status_code = 200
        text = '[project]\nname = "nextbox"\nversion = "99.0.0"\n'
    monkeypatch.setattr(httpx, "get", lambda *args, **kw: R())
    ctx, a, login = site(ctx)
    how = login("boss", "boss password").get("/super/update-check").json()["how"]
    assert "~/.local/share/nextbox/app/scripts/update.sh" in how and "cd ~/next-box" not in how
    update = (PROJECT_DIR / "scripts" / "update.sh").read_text()
    assert "~/.local/share/nextbox/app/scripts/update.sh" in update
    # Run through the ~/.local/share/nextbox/app link, install.sh must not point that link at itself.
    install = (PROJECT_DIR / "scripts" / "install.sh").read_text()
    assert 'pwd -P)"' in install.split("ln -sfn")[0]


# ------------------------------------------------------------------ R-D10: iPhone photos
def test_heic_is_an_installable_extra_and_its_help_names_the_settings_app():
    assert "Settings app" in scan.HEIC_HELP and "In Settings →" not in scan.HEIC_HELP
    assert "heic = [" in (PROJECT_DIR / "pyproject.toml").read_text()
    assert any(line.startswith("pillow-heif==") for line in (PROJECT_DIR / "constraints.txt").read_text().splitlines())
    assert "heic]'" in (PROJECT_DIR / "scripts" / "install.sh").read_text()


# ------------------------------------------------------------------ R-D11: the help pages follow Text size and Easy-to-read
def test_help_pages_follow_text_size_and_easy_to_read():
    js, css = (WEB / "pages.js").read_text(), (WEB / "pages.css").read_text()
    assert "nb.reading" in js and "nb.textSize" in js
    assert "Atkinson Hyperlegible" in css and 'html[data-reading] body' in css and 'html[data-text="large"] body' in css
    app = (WEB / "app.js").read_text()
    assert 'localStorage.setItem("nb.reading"' in app and 'localStorage.setItem("nb.textSize"' in app


# ------------------------------------------------------------------ R-D04, R-D12: the guide, legal pages and version
def test_guide_covers_this_release():
    guide = (WEB / "guide.html").read_text()
    for words in ("5 quick questions", "Run setup again", "Quick add", "List | Schedule", "Start 5 min", "Break it down",
                  "Add lines to my list", "Search your journal", "Download my journal", "Print my journal page",
                  "Look back on my week", "Easy-to-read text", "Same words every time", "Routine card",
                  "Remind me before timed tasks", 'href="#easier"'):
        assert words in guide, words


def test_legal_pages_version_and_notice_are_up_to_date(capsys):
    from nextbox import cli
    assert "keeping your journal, and the AI helpers when you tap them" in (WEB / "terms.html").read_text()
    about = (WEB / "about.html").read_text()
    assert "Journal" in about and "Atkinson Hyperlegible by the Braille Institute" in about
    assert "Your journal stays private, even from the admin." in server.DATA_NOTICE
    assert "Todoist to Brother" not in (PROJECT_DIR / "pyproject.toml").read_text()
    with pytest.raises(SystemExit) as done:
        cli.main(["--version"])
    assert done.value.code == 0 and capsys.readouterr().out.strip() == f"Next Box {app_version()}"
    assert "Checking everything works" in (PROJECT_DIR / "scripts" / "install.sh").read_text()
