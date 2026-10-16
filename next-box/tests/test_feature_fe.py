"""Feature batch FE: the routine card, the focus timer, reminders before timed tasks, Quick add, app shortcuts,
Share to Next Box and keyboard shortcuts.

Almost all of it runs in the browser while the app is open (no server timer, no push, nothing prints by
itself); the server keeps three settings and answers a share when the app's service worker isn't there."""
import dataclasses
import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from conftest import dump, tiny_jpeg
from nextbox.auth import PREF_HINTS, PREFS, Accounts, default_prefs
from nextbox.server import MAX_BODY_BYTES, create_app, share_text

WEB = Path(__file__).resolve().parent.parent / "web"


def site(ctx):
    """Sam, with My list only."""
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    app = create_app(ctx)

    def client(name=None, pw=None):
        c = TestClient(app, base_url="http://testserver")
        if name:
            assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, client


def files_in(folder: Path) -> dict[str, int]:
    return {str(p.relative_to(folder)): p.stat().st_size for p in folder.rglob("*") if p.is_file()}


# --- the settings ----------------------------------------------------------------------------------
def test_new_settings_have_gentle_defaults():
    d = default_prefs()
    assert d["routine_card"] is True       # the card shows unless it's turned off
    assert d["remind_before"] == 10        # 10 minutes before a timed task
    assert d["remind_notify"] is False     # browser notifications only when someone turns them on
    # FC's routine times are reused, not duplicated
    assert d["routine_morning"] == "08:00" and d["routine_evening"] == "18:00"
    assert not any(k in PREFS for k in ("routine_print", "routine_snap", "remind_minutes", "evening_time"))


def test_remind_before_takes_whole_minutes_from_off_to_two_hours():
    ok = PREFS["remind_before"][1]
    assert all(ok(v) for v in (0, 5, 10, 15, 30, 60, 120))
    assert not any(ok(v) for v in (-1, 121, 10.0, "10", True, None, []))
    assert PREFS["routine_card"][1](False) and not PREFS["routine_card"][1]("yes")
    assert PREFS["remind_notify"][1](True) and not PREFS["remind_notify"][1](1)
    assert "remind_before" in PREF_HINTS


def test_the_settings_save_and_say_what_is_wrong_in_plain_words(ctx):
    ctx, a, client = site(ctx)
    sam = client("sam", "sam password")
    r = sam.patch("/settings", json={"routine_card": False, "remind_before": 30, "remind_notify": True})
    assert r.status_code == 200
    assert (r.json()["routine_card"], r.json()["remind_before"], r.json()["remind_notify"]) == (False, 30, True)
    assert sam.get("/auth/me").json()["prefs"]["remind_before"] == 30
    r = sam.patch("/settings", json={"remind_before": "15"})  # a list sends text unless the app turns it into a number
    assert r.status_code == 400 and r.json()["detail"] == "Pick how long before from the list."
    assert sam.patch("/settings", json={"remind_before": 0}).json()["remind_before"] == 0  # Off


def test_existing_accounts_keep_their_settings_and_get_the_new_ones(ctx):
    ctx, a, client = site(ctx)
    a.set_prefs("sam", {"max_rows": 7, "routine_evening": "21:30", "routine_mode": True})
    prefs = a.get("sam")["prefs"]
    assert prefs["max_rows"] == 7 and prefs["routine_evening"] == "21:30" and prefs["routine_mode"] is True
    assert prefs["routine_card"] is True and prefs["remind_before"] == 10 and prefs["remind_notify"] is False


# --- Share to Next Box: what a share becomes -------------------------------------------------------
@pytest.mark.parametrize("title, text, url, want", [
    ("", "Call the dentist", "", "Call the dentist"),
    ("Dentist", "Call the dentist about the bill", "", "Call the dentist about the bill"),  # the title is in the text
    ("Bike shop", "https://shop.example/bikes", "https://shop.example/bikes", "Bike shop https://shop.example/bikes"),
    ("Recipe", "Try this https://food.example/soup", "https://food.example/soup", "Recipe Try this https://food.example/soup"),
    ("", "", "https://example.com/a#b", "https://example.com/a#b"),
    ("Groceries", "milk\neggs\nbread", "", "milk\neggs\nbread"),   # a shared list keeps its lines, without the note's name
    ("  ", "  ", None, ""),
])
def test_share_text_keeps_each_part_once(title, text, url, want):
    assert share_text(title, text, url) == want


def test_share_text_ignores_files_and_is_capped():
    assert share_text(object(), "Buy stamps", 42) == "Buy stamps"
    assert len(share_text("", "x" * 5000, "")) == 2000


def test_the_service_worker_shares_text_the_same_way():
    """sw.js answers shares on the phone; server.share_text answers without it. Same rule, same result."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    js = (WEB / "sw.js").read_text()
    fn = re.search(r"function shareText\(.*?\n}\n", js, re.S)[0]
    cases = [("Dentist", "Call the dentist about the bill", ""), ("Bike shop", "https://shop.example/bikes", "https://shop.example/bikes"),
             ("Groceries", "milk\neggs\nbread", ""), ("", "", "https://example.com/a#b"), ("Note", "x" * 3000, "")]
    script = "const SHARE_TEXT_MAX = 2000;\n" + fn + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map((c) => shareText(...c))));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    assert json.loads(out) == [share_text(*c) for c in cases]


# --- Share to Next Box without the service worker (the server's answer) ------------------------------
def test_shared_text_opens_quick_add_filled_in(ctx):
    ctx, a, client = site(ctx)
    before = dump(ctx.settings.data_dir, skip=("sessions",))
    r = client().post("/app/share", data={"title": "Dentist", "text": "Call the dentist 3pm #call", "url": ""},
                      files={"photo": ("", b"", "application/octet-stream")}, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/app/#add&text=")
    assert unquote(loc.split("&text=", 1)[1]) == "Call the dentist 3pm #call"
    # Only an address back to the app: nothing is added, kept or logged, and no sign-in is needed for that.
    assert dump(ctx.settings.data_dir, skip=("sessions",)) == before


def test_a_shared_photo_goes_to_snap_and_is_never_kept(ctx):
    ctx, a, client = site(ctx)
    folder = Path(ctx.settings.data_dir)
    c = client("sam", "sam password")  # signed in or not, it's the same answer
    before, files_before = dump(folder, skip=("sessions",)), files_in(folder)
    small = c.post("/app/share", files={"photo": ("page.jpg", tiny_jpeg(), "image/jpeg")}, data={"text": ""}, follow_redirects=False)
    assert small.status_code == 303 and small.headers["location"] == "/app/#snap&shared=missed"
    # A real phone photo is bigger than everything else Next Box takes: it's answered without being read.
    big = c.post("/app/share", files={"photo": ("page.jpg", b"\xff\xd8" + b"0" * (MAX_BODY_BYTES * 3), "image/jpeg")},
                 follow_redirects=False)
    assert big.status_code == 303 and big.headers["location"] == "/app/#snap&shared=missed"
    assert dump(folder, skip=("sessions",)) == before
    assert {k: v for k, v in files_in(folder).items() if not k.startswith("nextbox.db")} == \
        {k: v for k, v in files_before.items() if not k.startswith("nextbox.db")}


def test_an_empty_or_odd_share_still_lands_in_quick_add(ctx):
    ctx, a, client = site(ctx)
    c = client()
    assert c.post("/app/share", data={"title": "", "text": " "}, follow_redirects=False).headers["location"] == "/app/#add"
    r = c.post("/app/share", content=b"not a form", headers={"Content-Type": "text/plain"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/#add"
    long = c.post("/app/share", data={"text": "Read this " * 600}, follow_redirects=False).headers["location"]
    assert len(unquote(long.split("&text=", 1)[1])) == 2000


def test_share_is_only_a_post(ctx):
    ctx, a, client = site(ctx)
    assert client().get("/app/share").status_code in (404, 405)


# --- manifest.json and the service worker ----------------------------------------------------------
def test_manifest_has_the_three_shortcuts_and_a_share_target():
    m = json.loads((WEB / "manifest.json").read_text())
    shortcuts = {s["short_name"]: s for s in m["shortcuts"]}
    assert [s["url"] for s in m["shortcuts"]] == ["/app/#add", "/app/#snap", "/app/#print"]
    assert all(s["url"].startswith(m["scope"]) and s["name"] for s in m["shortcuts"])
    assert "Nothing prints" in shortcuts["Print"]["description"]  # it opens the Print tab, never prints
    for s in m["shortcuts"]:
        for icon in s["icons"]:
            assert (WEB / icon["src"]).exists()
    t = m["share_target"]
    assert (t["action"], t["method"], t["enctype"]) == ("/app/share", "POST", "multipart/form-data")
    assert t["params"]["text"] == "text" and t["params"]["title"] == "title" and t["params"]["url"] == "url"
    assert t["params"]["files"][0]["name"] == "photo" and "image/*" in t["params"]["files"][0]["accept"]


def test_the_service_worker_is_served_and_only_answers_shares(ctx):
    ctx, a, client = site(ctx)
    r = client().get("/app/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache"  # an update to it is picked up right away
    js = (WEB / "sw.js").read_text()
    assert 'e.request.method !== "POST"' in js and "respondWith(shareIn" in js
    assert "addEventListener(\"push\"" not in js and "periodicsync" not in js and "sync\"" not in js  # nothing from a server
    assert "fetch(" not in js and "addAll(" not in js  # it doesn't fetch or cache the app
    assert "notificationclick" in js


# --- the web app ------------------------------------------------------------------------------------
def test_the_web_app_has_the_new_controls():
    html = (WEB / "index.html").read_text()
    for el in ("routineCard", "timerGo", "timerGoEst", "timerBox", "timerPause", "timerStop", "timerMore", "timerDone",
               "timerNotNow", "timerSound", "remindBar", "remindNotify", "quickBtn", "quickDialog", "quickText", "quickGo",
               "keysDialog", "keysShow"):
        assert f'id="{el}"' in html, el
    for pref in ("routine_card", "remind_before"):
        assert f'data-pref="{pref}"' in html
    assert 'role="timer"' in html and 'aria-keyshortcuts="n"' in html


def test_the_routine_card_only_reads_and_never_prints_by_itself():
    js = (WEB / "app.js").read_text()
    routine = js[js.index("// ---------------------------------------------------------------- FE: the routine card"):
                 js.index("// ---------------------------------------------------------------- FE: focus timer")]
    assert 'api("/printed?limit=5"), api("/scans?limit=1")' in routine
    # The only print is the Today's label button's own call, from a tap on the card's button.
    assert routine.count("printFrom(") == 1
    assert re.search(r'addEventListener\("click".*?printFrom\("/print/today"', routine, re.S)
    assert "setInterval" not in routine and "setTimeout(() => printFrom" not in routine


def test_reminders_and_the_timer_are_worked_out_in_the_browser():
    js = (WEB / "app.js").read_text()
    fe = js[js.index("// ---------------------------------------------------------------- FE: focus timer"):]
    assert '"/print/' not in fe  # no print from the timer, reminders, Quick add, shortcuts or keys
    assert "setInterval" in fe and "/remind" not in fe and "/timer" not in fe  # no server timer to call
    # Notification permission is asked for in one place: when "Show a notification too" is turned on.
    handler = fe[fe.index('$("#remindNotify").onchange'):]
    handler = handler[:handler.index("\n};\n")]
    assert fe.count("Notification.requestPermission") == 1 and "Notification.requestPermission" in handler


def test_new_script_names_are_prefixed():
    """Another batch works in app.js at the same time: FE's top-level names start with its own words."""
    js = (WEB / "app.js").read_text()
    fe = js[js.index("// ---------------------------------------------------------------- FE: the routine card"):]
    names = re.findall(r"^(?:async )?function (\w+)|^(?:const|let) (\w+)", fe, re.M)
    names = [a or b for a, b in names]
    assert names and all(re.match(r"(loadRoutine|routine|ROUTINE|timer|TIMER|remind|REMIND|quick|keys|KEYS|share)", n) for n in names), names
