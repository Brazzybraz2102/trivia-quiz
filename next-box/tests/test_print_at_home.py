"""Printing away from home. Next Box runs on one computer, so in this test version only that computer's home (the
Home household, or a household with a printer of its own there) prints straight to a label printer. Everyone else
prints from their own device: Print on paper, or Save label image for their label printer's app.

Nothing here prints: the ctx fixture is a dry run, and the no_real_printer fixture fails a test that reaches one."""
import dataclasses
import io
import json
import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from nextbox import jobs, printer
from nextbox.auth import Accounts
from nextbox.printer import legacy_config
from nextbox.server import create_app

WEB = Path(__file__).resolve().parent.parent / "web"
PRINTER = {"name": "Kitchen", "driver": "escpos", "address": "192.0.2.10:9100"}  # TEST-NET: never answers


@pytest.fixture
def site(ctx, monkeypatch):
    """boss (staff) and mike in Home; kim with her own household (its admin) and lee, whom kim invited."""
    monkeypatch.setattr(printer, "is_reachable", lambda *a, **k: False)  # /status never knocks on a printer
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("mike", "mike password")
    app = create_app(ctx)

    def client():
        return TestClient(app, base_url="http://testserver")

    def login(name, pw):
        c = client()
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c

    boss = login("boss", "boss password")
    own = boss.post("/admin/invites", json={"own_household": True}).json()["code"]
    assert client().post("/auth/signup", json={"username": "kim", "password": "kim password", "invite": own,
                                               "agree": True}).status_code == 200
    kim = login("kim", "kim password")
    code = kim.post("/admin/invites", json={}).json()["code"]
    assert client().post("/auth/signup", json={"username": "lee", "password": "lee password", "invite": code,
                                               "agree": True}).status_code == 200
    people = {"boss": boss, "mike": login("mike", "mike password"), "kim": kim, "lee": login("lee", "lee password")}
    assert a.get("kim")["household_id"] == a.get("lee")["household_id"] != "home"
    assert a.get("kim")["role"] == "admin" and a.get("lee")["role"] == "user"
    return ctx, a, client, people


def add_task(c, text="Water the plants"):
    r = c.post("/list", json={"content": text, "due_date": date.today().isoformat()})
    assert r.status_code == 200
    return r.json()["id"]


# --- who prints here ---------------------------------------------------------------------------------------------
def test_prints_here_is_true_for_home_and_a_household_with_a_printer(site):
    ctx, a, client, p = site
    for name in ("boss", "mike"):  # Home, even with no printer added yet: its messages stay as they are
        assert p[name].get("/auth/me").json()["prints_here"] is True, name
        assert p[name].get("/status").json()["prints_here"] is True, name
    for name in ("kim", "lee"):  # a friend's own household, no printer: print from your own device
        assert p[name].get("/auth/me").json()["prints_here"] is False, name
        assert p[name].get("/status").json()["prints_here"] is False, name
    # A household living at the Next Box computer's home can add a printer there, and then it prints here.
    added = p["kim"].post("/admin/printers", json=PRINTER)
    assert added.status_code == 200
    for name in ("kim", "lee"):
        assert p[name].get("/auth/me").json()["prints_here"] is True, name
        assert p[name].get("/status").json()["prints_here"] is True, name
    assert p["kim"].delete(f"/admin/printers/{added.json()['id']}").status_code == 200
    assert p["lee"].get("/auth/me").json()["prints_here"] is False

    assert jobs.prints_here("home", []) is True
    assert jobs.prints_here("h2", []) is False
    assert jobs.prints_here("h2", [legacy_config(ctx.settings)]) is True


def test_who_may_add_printers_is_unchanged(site):
    ctx, a, client, p = site
    assert p["lee"].post("/admin/printers", json=PRINTER).status_code == 403  # not an admin
    for address in ("/dev/usb/lp0", "127.0.0.1:9100", "192.0.2.10:22"):  # outside Home: network printer ports only
        assert p["kim"].post("/admin/printers", json={**PRINTER, "address": address}).status_code == 400, address
    assert p["boss"].post("/admin/printers", json={**PRINTER, "address": "/dev/usb/lp0"}).status_code == 200


# --- what a preview says -----------------------------------------------------------------------------------------
def test_the_words_are_plain_and_home_keeps_its_own():
    assert jobs.PRINT_AWAY == (
        "In this test version, labels print straight to a printer only at the Next Box computer's home. "
        "To print yours, tap Print on paper, or Save label image and open it in your label printer's app "
        "(like Brother iPrint&Label).")
    assert "dev mode" not in jobs.PRINT_AWAY.lower()
    # Home's messages, exactly as they were
    assert jobs.NO_PRINTER == "No label printer in your household yet. Tap Print on paper to print it on any printer."
    assert jobs.NO_PRINTER_ADMIN == ("No label printer in your household yet. Add one under Admin → Printers, "
                                     "or tap Print on paper to print it on any printer.")


def test_a_friend_household_preview_says_print_from_your_device(site):
    ctx, a, client, p = site
    for name in ("kim", "lee"):  # the household's admin and its other people alike
        add_task(p[name])
        r = p[name].post("/print/today", json={})
        assert r.status_code == 200, r.text
        r = r.json()
        assert r["status"] == "dry_run" and r["printer"] is None and r["note"] == jobs.PRINT_AWAY, name
        r = p[name].post("/print/text", json={"text": "Buy milk", "dry_run": False}).json()
        assert r["status"] == "dry_run" and r["note"] == jobs.PRINT_AWAY, name
        sid = add_task(p[name], "Stickers too")
        r = p[name].post("/print/stickers", json={"task_ids": [sid]}).json()
        assert r["note"] == jobs.PRINT_AWAY and r["kind"] == "stickers", name


def test_home_keeps_its_messages(site):
    ctx, a, client, p = site
    add_task(p["mike"])
    r = p["mike"].post("/print/today", json={}).json()
    assert r["status"] == "dry_run" and r["note"] == jobs.NO_PRINTER
    assert p["boss"].post("/print/text", json={"text": "hi"}).json()["note"] == jobs.NO_PRINTER_ADMIN


def test_a_friend_household_with_its_own_printer_keeps_the_usual_messages(site):
    ctx, a, client, p = site
    assert p["kim"].post("/admin/printers", json=PRINTER).status_code == 200
    add_task(p["lee"])
    r = p["lee"].post("/print/today", json={}).json()  # a dry run (ctx), so nothing is sent
    assert r["status"] == "dry_run" and r["printer"]["name"] == "Kitchen"
    assert jobs.PRINT_AWAY not in (r.get("note") or "")


# --- Save label image ---------------------------------------------------------------------------------------------
def test_save_label_image_downloads_with_a_filename_and_the_same_access_rules(site):
    ctx, a, client, p = site
    tid = p["kim"].post("/print/text", json={"text": "Call Mum"}).json()["id"]
    before = len(ctx.store.list_printed(100))
    r = p["kim"].get(f"/printed/{tid}/png?download=1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"] == f'attachment; filename="next-box-{tid}.png"'
    assert r.headers["cache-control"] == "no-store"
    assert r.content == ctx.store.get_png(tid)
    Image.open(io.BytesIO(r.content)).verify()
    shown = p["kim"].get(f"/printed/{tid}/png")  # the picture in the app: shown, not downloaded
    assert shown.headers["content-disposition"] == f'inline; filename="next-box-{tid}.png"'
    assert shown.headers["cache-control"] == "no-store" and shown.content == r.content
    assert p["kim"].get(f"/printed/{tid.lower()}/png?download=1").status_code == 200  # typed in small letters
    # Only the owner, and the superadmin (everyone agreed to the data notice): nobody else, not even their household.
    for name in ("lee", "mike"):
        for url in (f"/printed/{tid}/png?download=1", f"/printed/{tid}/png"):
            assert p[name].get(url).status_code == 404, (name, url)
    assert p["boss"].get(f"/printed/{tid}/png?download=1").status_code == 200
    assert client().get(f"/printed/{tid}/png?download=1").status_code == 401
    assert p["kim"].get("/printed/NOPE-0000/png?download=1").status_code == 404
    # Saving the image makes no ticket and prints nothing (no_real_printer would fail this test).
    assert len(ctx.store.list_printed(100)) == before


def test_sticker_images_hold_every_sticker_and_strips_are_one_image(site):
    ctx, a, client, p = site
    ids = [add_task(p["kim"], f"Task {n}") for n in range(3)]
    t = p["kim"].post("/print/stickers", json={"task_ids": ids}).json()
    assert t["kind"] == "stickers" and len(t["manifest"]) == 3 and not t.get("also")
    img = Image.open(io.BytesIO(p["kim"].get(f"/printed/{t['id']}/png?download=1").content))
    parts = ctx.store.get_parts(t["id"])
    assert len(parts) == 3
    # All three in one tall picture, a 26-dot gap with a dashed cut line between each.
    assert img.size == (parts[0].width, sum(x.height for x in parts) + 26 * 2)

    one = p["kim"].post("/print/strips", json={"task_ids": ids[:1]}).json()
    three = p["kim"].post("/print/strips", json={"task_ids": ids}).json()
    assert ctx.store.get_parts(three["id"]) is None  # tear-off strips are drawn as one label already
    h1 = Image.open(io.BytesIO(p["kim"].get(f"/printed/{one['id']}/png?download=1").content)).height
    h3 = Image.open(io.BytesIO(p["kim"].get(f"/printed/{three['id']}/png?download=1").content)).height
    assert h3 > h1 + 2 * 80  # every strip is in the picture


# --- the web app ---------------------------------------------------------------------------------------------------
def js_print_away(js: str) -> str:
    start = js.index("const PRINT_AWAY = ")
    expr = js[start:js.index(";\n", start)]
    return "".join(json.loads(s) for s in re.findall(r'"(?:[^"\\]|\\.)*"', expr))


def test_the_app_says_the_same_words_from_one_constant():
    js = (WEB / "app.js").read_text()
    assert js.count("const PRINT_AWAY = ") == 1 and js_print_away(js) == jobs.PRINT_AWAY
    assert "const printsHere = () => me?.prints_here !== false;" in js  # unknown yet: Home's words
    assert js.count("In this test version") == 1  # the words live in the constant only
    # Every place that talks about printing picks the constant when this household doesn't print here.
    for where, marker in (("Preview only note", 'const why = statusNow?.printing_paused'),
                          ("Settings printers", '$("#printersLoaded").innerHTML'),
                          ("Get started", "  printer: { title:"),
                          ("setup answer", "function quizSummary"),
                          ("Admin printers", '$("#prAway").textContent = PRINT_AWAY')):
        chunk = js[js.index(marker):js.index(marker) + 900]
        assert "PRINT_AWAY" in chunk and ("printsHere()" in chunk or "printerAway()" in chunk), where
    assert '"Previews · print from your device"' in js and '"Previews · print on paper"' in js  # the header
    assert "me.prints_here = s.prints_here" in js  # /status keeps it fresh after a printer is added or removed
    # Admin → Printers: staff and Home see the card as always; the form stays (collapsed), with one line.
    assert 'const printerAway = () => me?.role !== "superadmin" && me?.household_id !== "home";' in js
    html = (WEB / "index.html").read_text()
    card = html[html.index('<div class="card"><h2>Printers</h2>'):html.index('<div class="card"><h2>Invite people</h2>')]
    assert card.index('id="prAway"') < card.index('id="prSameNet"') < card.index('<details id="npForm"')
    assert ("<p class=\"muted\" id=\"prSameNet\" hidden>Only add a printer here if it's on the same network as the "
            "Next Box computer.</p>") in card
    assert '<details id="npForm" style="margin-top:10px">' in card  # not open


def test_save_label_image_sits_next_to_every_print_on_paper():
    js = (WEB / "app.js").read_text()
    spots = [m.start() for m in re.finditer(r">Print on paper</a>", js)]
    assert len(spots) >= 2  # paperButton (results, Printed) and the journal's ticket
    for at in spots:
        assert "saveImageLink(" in js[at:at + 400], js[at - 200:at + 400]
    helper = js[js.index("function saveImageLink("):]
    helper = helper[:helper.index("\n}\n")]
    assert '${png(id)}?download=1' in helper and 'download="${esc(name)}"' in helper and "next-box-${id}.png" in helper
    assert ">Save label image</a>" in helper
    # Its spoken name starts with the visible words, so "click Save label image" works by voice (WCAG 2.5.3).
    label = re.search(r'aria-label="([^"]*)"', helper)
    assert label and label.group(1).startswith("Save label image"), helper
    assert "/print/" not in helper and "api(" not in helper and "onclick" not in helper  # a download, never a print
    # Shown for everyone, Home included: no household check around it.
    paper = js[js.index("function paperButton("):js.index("function saveImageLink(")]
    assert "saveImageLink(id" in paper and "printsHere" not in paper


def test_the_guide_and_docs_explain_it():
    guide = (WEB / "guide.html").read_text()
    assert "Save label image" in guide and "In this test version, labels print straight to a printer only" in guide
    assert "iPrint&amp;Label" in guide and "same network as the Next Box computer" in guide
    root = WEB.parent
    assert "prints_here" in (root / "SPEC.md").read_text() and "PRINT_AWAY" in (root / "SPEC.md").read_text()
    readme = (root / "README.md").read_text()
    lets = readme[readme.index("### Let other people in to test"):readme.index("### Move to PostgreSQL")]
    assert "friends print from their own device" in lets
