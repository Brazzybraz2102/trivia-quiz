"""Printing is the default. "Preview only" used to start ticked on every page load (index.html `checked`, and
loadPrefs ticked it again), and the setup questions turned on Always preview only for "Regular printer" and "No
printer yet", so phones and computers made previews when Mike tapped Print. Now the box starts unticked and is ticked
(and locked, with the reason) only when a real print can't happen; with no printer, the app says "No printer added".

Nothing here prints: the ctx fixture is a dry run, and the no_real_printer fixture fails a test that reaches one.
The app's pieces run in node with small stand-ins for the page, as in test_round2_c_settings_bulk.py."""
import dataclasses
import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nextbox import db, jobs, printer, quiz, upkeep
from nextbox.auth import Accounts
from nextbox.server import create_app
from nextbox.store import Store

WEB = Path(__file__).resolve().parent.parent / "web"
PRINTER = {"name": "Kitchen", "driver": "escpos", "address": "192.0.2.10:9100"}  # TEST-NET: never answers
NO_PRINTER_WHY = "No printer added: print previews on paper."


@pytest.fixture(scope="module")
def js():
    return (WEB / "app.js").read_text()


@pytest.fixture(scope="module")
def html():
    return (WEB / "index.html").read_text()


def fn(js, name):
    """A top-level function of app.js, as written."""
    m = re.search(rf"^(?:async )?function {name}\(.*?\n}}\n", js, re.S | re.M)
    assert m, name
    return m[0]


def const(js, name):
    """A top-level `const name = ...` of app.js, with its indented continuation lines."""
    m = re.search(rf"^const {name} = .*?\n(?=\S)", js, re.S | re.M)
    assert m, name
    return m[0]


def run(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


# --- the setup questions ---------------------------------------------------------------------------------------------
def test_the_setup_questions_no_longer_turn_on_always_preview_only():
    assert quiz.changes_for({"print": "paper"}) == {"print_with": "paper"}
    assert quiz.changes_for({"print": "none"}) == {"print_with": "none"}
    assert quiz.changes_for({"print": "label"}) == {"print_with": "label", "always_dry_run": False}
    assert not any(v.get("always_dry_run") for v in quiz.PRINT.values())


@pytest.fixture
def site(ctx, monkeypatch):
    """boss (staff) and mike (a plain user) in Home; kim with her own household (its admin) and lee, whom she invited."""
    monkeypatch.setattr(printer, "is_reachable", lambda *a, **k: False)  # /status and /setup never knock on a printer
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
    return ctx, a, people


def test_answering_regular_printer_or_none_leaves_printing_on(site):
    _, a, p = site
    for answer in ("paper", "none"):
        r = p["mike"].post("/setup/quiz", json={"answers": {"print": answer}})
        assert r.status_code == 200, r.text
        assert r.json()["prefs"]["always_dry_run"] is False and "always_dry_run" not in r.json()["changed"]
        assert a.get("mike")["prefs"]["print_with"] == answer
    # Someone who turned Always preview only on themselves keeps it, whatever they answer.
    assert p["lee"].patch("/settings", json={"always_dry_run": True}).status_code == 200
    p["lee"].post("/setup/quiz", json={"answers": {"print": "paper"}})
    assert a.get("lee")["prefs"]["always_dry_run"] is True


def test_once_a_printer_is_added_a_regular_printer_answer_really_prints(site):
    """The bug Mike hit: the answer kept everything a preview after a printer was added. The job's own check
    (Context.is_dry) is what decides; nothing is sent here."""
    ctx, a, p = site
    p["mike"].post("/setup/quiz", json={"answers": {"print": "paper"}})
    ctx.settings.dry_run = False
    prefs = a.get("mike")["prefs"]
    assert dataclasses.replace(ctx, prefs=prefs).is_dry(None) is False
    assert dataclasses.replace(ctx, prefs=prefs).is_dry(True) is True  # Preview only ticked: a preview
    assert p["boss"].post("/admin/printers", json=PRINTER).status_code == 200
    assert p["mike"].get("/status").json()["dry_run_forced"] is False


# --- the one-time clean-up -------------------------------------------------------------------------------------------
def _prefs(engine):
    with engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(select(db.users.c.username, db.users.c.prefs))}


def test_the_clean_up_turns_off_only_what_the_setup_questions_turned_on_and_runs_once(tmp_path):
    a = Accounts(tmp_path)
    a.set_password("boss", "boss password", create=True)
    setups = {
        "pam": {"always_dry_run": True, "print_with": "paper", "max_rows": 3},  # the setup questions did this: off
        "nan": {"always_dry_run": True, "print_with": "none"},                  # and this
        "lab": {"always_dry_run": True, "print_with": "label"},                 # their own choice: stays
        "own": {"always_dry_run": True},                                        # never answered: their own choice
        "off": {"always_dry_run": False, "print_with": "paper"},                # nothing to do
    }
    for name, prefs in setups.items():
        a.create(name, f"{name} password 1")
        a.set_prefs(name, prefs)
    engine = db.database(tmp_path)
    upkeep.run_startup_fixes(engine, Store(tmp_path))
    now = _prefs(engine)
    assert {n: now[n]["always_dry_run"] for n in setups} == {"pam": False, "nan": False, "lab": True, "own": True,
                                                            "off": False}
    assert now["pam"]["max_rows"] == 3 and now["pam"]["print_with"] == "paper"  # the rest of their settings stay
    assert now["nan"]["print_with"] == "none"
    with engine.connect() as conn:
        flags = {r[0] for r in conn.execute(select(db.server_settings.c.key))}
    assert "_upkeep:preview-default-v1" in flags
    # Once: pam turns it on herself afterwards, and the next start leaves it alone.
    a.set_prefs("pam", {"always_dry_run": True})
    upkeep.run_startup_fixes(engine, Store(tmp_path))
    assert _prefs(engine)["pam"]["always_dry_run"] is True
    assert upkeep.preview_default(engine) == ["pam"]  # (what it would do, run by hand)


def test_the_clean_up_runs_when_next_box_starts(ctx):
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    a.set_prefs("sam", {"always_dry_run": True, "print_with": "none"})
    create_app(ctx)
    assert a.get("sam")["prefs"]["always_dry_run"] is False
    a.set_prefs("sam", {"always_dry_run": True})
    create_app(ctx)  # started again: already done, so her own switch stays
    assert a.get("sam")["prefs"]["always_dry_run"] is True


# --- what the server tells the app --------------------------------------------------------------------------------------
def test_status_says_how_many_printers_and_who_can_add_one(site):
    _, _, p = site
    want = {"mike": (True, False), "boss": (True, True), "kim": (False, True), "lee": (False, False)}
    for name, (here, can) in want.items():
        s = p[name].get("/status").json()
        assert (s["printer"], s["printers"], s["prints_here"], s["can_add_printer"]) == (None, 0, here, can), name
        assert p[name].get("/setup").json()["printers"] == 0, name
    assert p["boss"].post("/admin/printers", json=PRINTER).status_code == 200  # Home gets a printer
    for name in ("mike", "boss"):
        s = p[name].get("/status").json()
        assert (s["printer"], s["printers"]) == ("Kitchen", 1)
        assert p[name].get("/setup").json()["printers"] == 1
    for name in ("kim", "lee"):  # another household's printers are never counted
        assert p[name].get("/status").json()["printers"] == 0
    # A Home ticket made with no printer said "No label printer in your household yet": the same words now.
    assert jobs.NO_PRINTER == "No printer added. Tap Print on paper to print it on any printer."
    assert jobs.NO_PRINTER_ADMIN.startswith("No printer added. Add one in Admin → Printers, ")
    # Who may add printers is unchanged.
    assert p["mike"].post("/admin/printers", json=PRINTER).status_code == 403
    assert p["lee"].post("/admin/printers", json=PRINTER).status_code == 403


# --- the app: Preview only ---------------------------------------------------------------------------------------------
def test_preview_only_starts_unticked(js, html):
    box = re.search(r'<input type="checkbox" id="dry"[^>]*>', html)
    assert box and "checked" not in box[0], box
    assert '"#dry").checked = true' not in js  # nothing ticks it outside applyDry
    body = fn(js, "loadPrefs")
    assert "#dry" not in body.replace("applyDry", "") and "applyDry();" in body
    # A new person on this device starts from unticked: signing out, and the first boot after signing in.
    assert "resetDry();" in fn(js, "clearPerson")
    first = fn(js, "boot")
    first = first[first.index("if (!boot.started)"):first.index("loadPrefs(me.prefs)")]
    assert "resetDry();" in first


def test_every_real_print_still_asks_first(js):
    reads = [m.start() for m in re.finditer(r'const dry_run = \$\("#dry"\)\.checked;', js)]
    assert len(reads) >= 5  # Print tab, Printed, My list, Just one thing from a slug, Break it down
    for at in reads:
        assert 'if (!dry_run && !confirm("Send this to the printer?")) return;' in js[at:at + 600], js[at:at + 600]


def dry_script(js, body):
    return f"""
const RANK = {{ user: 0, admin: 1, superadmin: 2 }};
{const(js, "PRINT_AWAY")}
let me = null, statusNow = null;
const els = {{}};
const $ = (id) => (els[id] ||= {{ checked: false, disabled: false, dataset: {{}}, textContent: "", changes: 0,
                                  dispatchEvent(e) {{ if (e.type === "change") this.changes++; }} }});
const printsHere = () => me?.prints_here !== false;
{const(js, "canAddPrinter")}{const(js, "NO_PRINTER")}{const(js, "noPrinterNext")}{fn(js, "applyDry")}{fn(js, "resetDry")}
const state = () => ({{ ticked: $("#dry").checked, locked: $("#dry").disabled, why: $("#dryWhy").textContent }});
const home = {{ printer: null, printers: 0, prints_here: true, dry_run_forced: false, printing_paused: false }};
const out = {{}};
{body}
console.log(JSON.stringify(out));
"""


def test_preview_only_is_ticked_only_when_a_real_print_cant_happen(js):
    out = run(dry_script(js, """
me = { role: "user", prints_here: true, prefs: {} };
applyDry(); out.before_status = state();                      // signed in, /status not back yet: printing
statusNow = home; applyDry(); out.user_none = state();
me.role = "admin"; applyDry(); out.admin_none = state();
me.role = "superadmin"; applyDry(); out.super_none = state();
me.role = "user";
const before = $("#dry").changes;
statusNow = { ...home, printer: "Kitchen", printers: 1 }; applyDry(); out.printer_added = state();
out.changed = $("#dry").changes - before;                   // "Print today's label" follows it
$("#dry").checked = true; applyDry(); out.own_tick = state(); // ticked by the person: stays, and can be unticked
$("#dry").checked = false;
me.prefs = { always_dry_run: true, print_with: "paper" }; applyDry(); out.always = state();
me.prefs = {}; statusNow = { ...statusNow, dry_run_forced: false }; applyDry(); out.always_off = state();
statusNow = { ...statusNow, printing_paused: true, dry_run_forced: true }; applyDry(); out.paused = state();
statusNow = { ...statusNow, printing_paused: false, dry_run_forced: true }; applyDry(); out.env_dry = state();
statusNow = { ...statusNow, dry_run_forced: false };
me.prints_here = false; statusNow = { ...home, prints_here: false }; applyDry(); out.away = state();
resetDry(); out.reset = { ...state(), status: statusNow };
"""))
    printing = {"ticked": False, "locked": False, "why": ""}
    assert out["before_status"] == printing
    assert out["user_none"] == {"ticked": True, "locked": True, "why": NO_PRINTER_WHY}
    assert out["admin_none"]["why"] == out["super_none"]["why"] == NO_PRINTER_WHY + " Add one in Admin → Printers."
    assert out["printer_added"] == printing and out["changed"] == 1  # the reason went away: printing again
    assert out["own_tick"] == {"ticked": True, "locked": False, "why": ""}
    assert out["always"] == {"ticked": True, "locked": True, "why": "Always preview only is on in Settings."}
    assert out["always_off"] == printing
    assert out["paused"]["why"] == "Printing is paused by an admin." and out["paused"]["locked"]
    assert out["env_dry"]["why"] == "This Next Box makes previews only for now." and out["env_dry"]["ticked"]
    assert out["away"]["why"].startswith("In this test version") and out["away"]["locked"]
    assert out["reset"] == {**printing, "status": None}  # the next person starts from printing


def test_the_header_says_no_printer_added(js):
    body = fn(js, "refreshStatus")
    assert '!s.printer ? (printsHere() ? "No printer added · print on paper" : "Previews · print from your device")' in body


# --- the app: Settings → Printers & label colors ----------------------------------------------------------------------
def test_the_settings_card_says_no_printer_added(html, js):
    card = html[html.index('<div class="card" id="labelCard">'):html.index('<div class="card" id="printCard">')]
    assert ('<div id="noPrinter" hidden><p><b>No printer added.</b> <span id="noPrinterNext"></span></p>'
            in card)
    assert '<button type="button" class="go small" id="noPrinterGo" hidden>Add a printer</button>' in card
    assert card.index('id="noPrinter"') < card.index('id="printersLoaded"') < card.index('id="defPrinterRow"')
    assert '$("#noPrinterGo").onclick = () => { $("#npForm").open = true; goto("admin", "#npForm"); };' in js
    assert 'const NO_PRINTER = "No printer added.";' in js  # the same words in the Preview only line and Get started


def labels_script(js, body):
    return f"""
const RANK = {{ user: 0, admin: 1, superadmin: 2 }};
{const(js, "PRINT_AWAY")}
let me = null, printOpts = null, opts = null;
const els = {{}};
const $ = (id) => (els[id] ||= {{ hidden: false, textContent: "", innerHTML: "", value: "", classList: {{ toggle() {{}} }} }});
const $$ = () => [];
const api = async () => opts;
const esc = (s) => String(s), sw = () => "", errText = (e) => e.message, refreshStatus = () => {{}};
const printsHere = () => me?.prints_here !== false;
{const(js, "canAddPrinter")}{const(js, "noPrinterNext")}{fn(js, "loadLabels")}
const card = () => ({{ none: !$("#noPrinter").hidden, next: $("#noPrinterNext").textContent, button: !$("#noPrinterGo").hidden,
  loaded: $("#printersLoaded").hidden ? null : $("#printersLoaded").innerHTML,
  rows: ["#defPrinterRow", "#ruleHead", "#colorRules"].filter((id) => !$(id).hidden).length }});
const out = {{}};
(async () => {{
{body}
console.log(JSON.stringify(out));
}})();
"""


def test_the_settings_card_says_the_next_step_for_each_person(js):
    out = run(labels_script(js, """
opts = { printers: [], colors: ["white"], reasons: {}, drivers: {} };
me = { role: "admin", prints_here: true, prefs: {} }; await loadLabels(); out.admin = card();
me = { role: "superadmin", prints_here: true, prefs: {} }; await loadLabels(); out.boss = card();
me = { role: "user", prints_here: true, prefs: {} }; await loadLabels(); out.user = card();
me = { role: "admin", prints_here: false, prefs: {} }; await loadLabels(); out.away = card();
opts = { printers: [{ id: "p1", name: "Kitchen", stock_color: "white" }], colors: ["white"], reasons: {}, drivers: {} };
me = { role: "user", prints_here: true, prefs: {} }; await loadLabels(); out.printer = card();
"""))
    until = "Until then, every ticket can be previewed and printed on paper."
    assert out["admin"] == {"none": True, "next": "Add one in Admin → Printers.", "button": True, "loaded": until, "rows": 0}
    assert out["boss"] == out["admin"]
    assert out["user"] == {"none": True, "next": "Ask your Next Box admin to add one.", "button": False, "loaded": until,
                           "rows": 0}
    # Outside Home: PRINT_AWAY once, as the next step, and nothing else under it.
    assert out["away"]["none"] and out["away"]["next"].startswith("In this test version")
    assert not out["away"]["button"] and out["away"]["loaded"] is None and out["away"]["rows"] == 0
    assert out["printer"]["none"] is False and out["printer"]["loaded"].startswith("Loaded right now: Kitchen")
    assert out["printer"]["rows"] == 3


# --- the app: Get started and the setup answers ------------------------------------------------------------------------
def test_get_started_and_the_setup_answers_use_the_same_words(js):
    step = js[js.index("  printer: { title:"):js.index("  preview: { title:")]
    assert ("guide?.printers === 0 ? `${NO_PRINTER} ${noPrinterNext()} Until then, every ticket has Print on paper, "
            "which works with any printer.`") in step
    summary = fn(js, "quizSummary")
    assert "starts ticked" not in summary and "untick it" not in summary
    assert "`Label printer: ${NO_PRINTER.toLowerCase()} ${noPrinterNext()}`" in summary
    assert "Send this to the printer?" in summary


def test_the_docs_say_printing_is_the_default():
    root = WEB.parent
    spec = (root / "SPEC.md").read_text()
    assert "printing is the default" in spec.lower() and "No printer added" in spec and "preview-default-v1" in spec
    assert "Printing is the default" in (root / "README.md").read_text()
    guide = (WEB / "guide.html").read_text()
    assert "Keep <b>Preview only</b> ticked" not in guide and "No printer added" in guide
    assert "Next Box prints by default" in guide
