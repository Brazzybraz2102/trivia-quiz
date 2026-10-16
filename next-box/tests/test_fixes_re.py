"""Review batch RE: the routine card, the focus timer, reminders, Quick add and Share to Next Box (SPEC §30).

Most of it runs in the browser, so the logic that decides what shows when is run here in node with a fixed
clock (TZ=UTC), the same functions app.js uses; the server parts (GET /printed?since=, POST /app/share) are
tested through the app."""
import dataclasses
import json
import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import dump
from nextbox.auth import Accounts
from nextbox.server import MAX_BODY_BYTES, MAX_UPLOAD_BYTES, create_app

WEB = Path(__file__).resolve().parent.parent / "web"
JS = (WEB / "app.js").read_text()
HTML = (WEB / "index.html").read_text()
SW = (WEB / "sw.js").read_text()


@pytest.fixture
def site(ctx):
    """boss (superadmin) and sam (user), My list only."""
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
    return ctx, client


# --- node: app.js's own functions, with a fixed clock ------------------------------------------------
def js_fn(name: str) -> str:
    """One top-level function or const from app.js, as written there."""
    m = re.search(rf"^(?:async )?function {name}\(.*?\n}}\n", JS, re.S | re.M) or re.search(rf"^const {name} = .*\n", JS, re.M)
    assert m, name
    return m.group(0)


def run_js(code: str, at: str, prelude: str = ""):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    clock = f"""const NOW = new Date("{at}").getTime(), RealDate = Date;
globalThis.Date = class extends RealDate {{ constructor(...a) {{ if (a.length) super(...a); else super(NOW); }} static now() {{ return NOW; }} }};
"""
    script = clock + js_fn("today") + js_fn("schedMinutes") + prelude + f"\nconsole.log(JSON.stringify((() => {{ {code} }})()));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, env={**os.environ, "TZ": "UTC"})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def routine_day(at, plan="08:00", look="18:00"):
    prelude = f"let me = {{ prefs: {{ routine_morning: '{plan}', routine_evening: '{look}' }} }};\n" + js_fn("routineDay")
    return run_js("return routineDay();", at, prelude)


# FE-R05: a night owl's routine day is one day across midnight
@pytest.mark.parametrize("at, day, since, part", [
    ("2026-10-16T20:30:00", "2026-10-16", "2026-10-16T13:00:00", "morning"),   # plan time: print today's label
    ("2026-10-17T00:30:00", "2026-10-16", "2026-10-16T13:00:00", "morning"),   # after midnight: still that label's day
    ("2026-10-17T07:00:00", "2026-10-16", "2026-10-16T13:00:00", "evening"),   # look back (from 6:00)
    ("2026-10-17T14:00:00", "2026-10-17", "2026-10-17T13:00:00", "quiet"),     # a new day, quiet until its plan time
    ("2026-10-17T20:00:00", "2026-10-17", "2026-10-17T13:00:00", "morning"),
])
def test_a_night_owls_routine_day_runs_past_midnight(at, day, since, part):
    r = routine_day(at, plan="20:00", look="06:00")
    assert (r["day"], r["since"]) == (day, since)
    assert part == ("morning" if r["morning"] else "evening" if r["evening"] else "quiet")


@pytest.mark.parametrize("at, day, part", [
    ("2026-10-16T07:30:00", "2026-10-16", "quiet"),    # a label made now (before 8:00) is today's
    ("2026-10-16T08:00:00", "2026-10-16", "morning"),
    ("2026-10-16T18:00:00", "2026-10-16", "evening"),
    ("2026-10-17T00:30:00", "2026-10-16", "evening"),  # still looking back on the 16th until 1:00
    ("2026-10-17T01:00:00", "2026-10-17", "quiet"),
])
def test_the_usual_routine_day_is_unchanged_in_the_day(at, day, part):
    r = routine_day(at)
    assert r["day"] == day and r["since"] == f"{day}T01:00:00"
    assert part == ("morning" if r["morning"] else "evening" if r["evening"] else "quiet")


def test_plan_and_look_back_at_the_same_time_is_a_morning_all_day():
    assert routine_day("2026-10-16T15:00:00", plan="09:00", look="09:00")["morning"] is True
    assert routine_day("2026-10-16T03:00:00", plan="09:00", look="09:00")["evening"] is False


def test_the_routine_card_reads_its_days_tickets_and_keys_not_today_by_that_day():
    routine = JS[JS.index("// ---------------------------------------------------------------- FE: the routine card"):
                 JS.index("// ---------------------------------------------------------------- FE: focus timer")]
    # FE-R06: every ticket of this routine day (stickers or one-task prints don't push today's label out)
    assert "api(`/printed?since=${encodeURIComponent(since)}&limit=100`)" in routine and "limit=5" not in routine
    # FE-R05: a label counts from the routine day's start, and "Not today" lasts that day
    assert '(t.created_at || "") >= since' in routine and 'slice(0, 10) === day' not in routine
    assert routine.count("routineDay().day") == 2 and "v.day === today()" not in routine


# --- FE-R06: GET /printed?since= ----------------------------------------------------------------------
def test_printed_since_gives_this_routine_days_tickets_only(site):
    ctx, client = site
    for i, (by, at, kind) in enumerate([("sam", "2026-10-16T12:59:59", "today"), ("sam", "2026-10-16T20:30:00", "today"),
                                        ("sam", "2026-10-17T00:10:00", "focus"), ("boss", "2026-10-16T21:00:00", "today")]):
        ctx.store.save_printed({"id": f"NB-T{i}", "by": by, "created_at": at, "kind": kind, "title": "x"})
    sam = client("sam", "sam password")
    got = sam.get("/printed", params={"since": "2026-10-16T13:00:00", "limit": 100}).json()
    assert [t["id"] for t in got] == ["NB-T2", "NB-T1"]  # newest first; not boss's, not before the day started
    assert [t["id"] for t in sam.get("/printed?since=2026-10-16T13:00").json()] == ["NB-T2", "NB-T1"]  # seconds optional
    assert len(sam.get("/printed").json()) == 3  # without it: as before
    for bad in ("2026-10-16", "yesterday", "2026-10-16T13:00:00Z", "2026-10-16 13:00", "2026-10-16T13:00:00' or 1=1"):
        assert sam.get("/printed", params={"since": bad}).status_code == 422, bad
    assert client().get("/printed?since=2026-10-16T13:00").status_code == 401


# --- FE-R10: POST /app/share holds a body to 256 KB, and answers a bigger photo with Snap -------------
def multipart(files, data=None):
    req = httpx.Request("POST", "http://testserver/app/share", files=files, data=data or {})
    return req.read(), req.headers["content-type"]


def asgi_post(app, path, body: bytes, headers: dict, chunk=64 * 1024):
    """POST with no Content-Length, the body sent in pieces as a phone may: what came back, and how much was read."""
    import anyio
    read, out = [0], {}
    pieces = [body[i:i + chunk] for i in range(0, len(body), chunk)] or [b""]

    async def receive():
        if not pieces:
            return {"type": "http.disconnect"}
        p = pieces.pop(0)
        read[0] += len(p)
        return {"type": "http.request", "body": p, "more_body": bool(pieces)}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"], out["headers"] = m["status"], {k.decode(): v.decode() for k, v in m["headers"]}

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "", "client": ("127.0.0.1", 1),
             "server": ("testserver", 80), "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    anyio.run(app, scope, receive, send)
    return out, read[0]


def test_a_streamed_photo_share_is_cut_off_at_256_kb_and_still_goes_to_snap(site):
    ctx, client = site
    before = dump(ctx.settings.data_dir, skip=("sessions",))
    body, ctype = multipart({"photo": ("page.jpg", b"\xff\xd8" + b"0" * (MAX_BODY_BYTES * 8), "image/jpeg")})
    out, read = asgi_post(client().app, "/app/share", body, {"content-type": ctype})  # no sign-in
    assert out["status"] == 303 and out["headers"]["location"] == "/app/#snap&shared=missed"
    assert read <= MAX_BODY_BYTES + 64 * 1024 < len(body)  # not read (or parsed into temp files) past 256 KB
    assert dump(ctx.settings.data_dir, skip=("sessions",)) == before  # nothing kept


def test_a_photo_share_over_16_mb_goes_to_snap_not_a_bare_413(site):
    ctx, client = site
    body, ctype = multipart({"photo": ("page.jpg", b"\xff\xd8" + b"0" * (MAX_UPLOAD_BYTES + 1024 * 1024), "image/jpeg")})
    r = client().post("/app/share", content=body, headers={"Content-Type": ctype}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/#snap&shared=missed"


def test_a_small_streamed_text_share_still_opens_quick_add(site):
    ctx, client = site
    body, ctype = multipart({"photo": ("", b"", "application/octet-stream")}, {"title": "Dentist", "text": "Call the dentist 3pm"})
    r = client().post("/app/share", content=iter([body]), headers={"Content-Type": ctype}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/#add&text=" + quote("Call the dentist 3pm", safe="")


def test_other_routes_still_refuse_a_big_streamed_body(site):
    ctx, client = site
    sam = client("sam", "sam password")
    big = json.dumps({"text": "x" * (MAX_BODY_BYTES * 2)}).encode()
    r = sam.post("/list/dump", content=iter([big]), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    r = sam.post("/list/dump", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


# --- FE-R01: a timer that ran out long ago, or paused on an earlier day, is let go -------------------
def timer_stale(state, at):
    return run_js(f"return timerStale({json.dumps(state)});", at, js_fn("timerStale"))


def test_a_timer_that_ran_out_over_30_minutes_ago_is_let_go_open_page_or_not():
    end = "2026-10-16T10:05:00"
    start = run_js("return new Date('2026-10-16T10:00:00').getTime();", end)
    ended = {"id": "nb:1", "total": 300000, "start": start, "left": 0, "paused": False, "ended": True}
    assert timer_stale(ended, "2026-10-16T10:34:00") is False      # 29 minutes after the end: Time's up stays
    assert timer_stale(ended, "2026-10-16T10:36:00") is True       # 31: let go (it was marked ended with the page open)
    assert timer_stale({**ended, "ended": False}, "2026-10-16T10:36:00") is True  # never marked (a computer that slept)
    assert timer_stale({**ended, "ended": False}, "2026-10-16T10:04:00") is False  # still running


def test_a_paused_timer_is_kept_for_its_day_only():
    start = run_js("return new Date('2026-10-16T23:00:00').getTime();", "2026-10-16T23:00:00")
    paused = {"id": "nb:1", "total": 300000, "start": start, "left": 120000, "paused": True, "ended": False, "day": "2026-10-16"}
    assert timer_stale(paused, "2026-10-16T23:59:00") is False
    assert timer_stale(paused, "2026-10-17T00:01:00") is True
    no_day = {k: v for k, v in paused.items() if k != "day"}  # saved before 'day' was kept: the day it started
    assert timer_stale(no_day, "2026-10-16T23:30:00") is False and timer_stale(no_day, "2026-10-17T08:00:00") is True


def test_the_timer_lets_go_in_all_three_places_and_pausing_keeps_the_day():
    timer = JS[JS.index("// ---------------------------------------------------------------- FE: focus timer"):
               JS.index("// ---------------------------------------------------------------- FE: reminders")]
    assert "if (timerStale(timerState)) { timerState = null; timerSave(); return; }" in timer  # loading it
    assert "if (timerStale(timerState)) return timerDrop();" in timer                      # its own wake-up
    assert "if (timerStale(timerState)) timerDrop();" in JS                                # the 20 s check (page left open)
    assert "paused: true, left: Math.max(0, timerLeft()), day: today()" in timer
    drop = js_fn("timerDrop")
    assert "snack(" not in drop and "remindNote(" not in drop and "timerSay(" not in drop   # quietly


# --- FE-R07: the timer's live region is outside the screens -------------------------------------------
def test_the_timer_is_heard_on_every_screen():
    live = HTML.index('id="timerLive"')
    assert HTML.index('id="remindSay"') < live < HTML.index("<main>")  # beside the reminders' one, not in a hidden section
    assert HTML.count('id="timerLive"') == 1
    assert '$("#timerLive").textContent = "";' in js_fn("timerForget")  # signing out leaves nothing to read


# --- FE-R08: reminders by real time, across midnight ----------------------------------------------------
def remind_prelude(lead=30, tasks=()):
    return f"""
const REMIND_LATE = 30; let remindMem = null; const store = {{}};
const localStorage = {{ getItem: (k) => store[k] ?? null, setItem: (k, v) => {{ store[k] = String(v); }} }};
let me = {{ username: "sam", prefs: {{ remind_before: {lead} }} }}, lastLoad = {{ at: 1, day: "" }}, listData = {{ tasks: {json.dumps(list(tasks))}, linked: [] }};
""" + "".join(js_fn(n) for n in ("remindKey", "remindMemo", "remindKeep", "remindAt", "remindTimed", "remindDue"))


def remind_due(at, tasks, lead=30, memo=None):
    pre = remind_prelude(lead, tasks)
    if memo is not None:
        pre += f"\nstore['nb.remind:sam'] = {json.dumps(json.dumps(memo))};"
    return run_js("return remindDue().map((x) => [x.t.content, x.min]);", at, pre)


MEDS = {"id": "nb:7", "content": "Take night meds", "done": False, "due": {"date": "2026-10-17T00:15:00"}}
LATE = {"id": "nb:8", "content": "Lock the back door", "done": False, "due": {"date": "2026-10-16T23:50:00"}}


def test_a_reminder_just_after_midnight_starts_its_full_lead_before():
    assert remind_due("2026-10-16T23:44:00", [MEDS]) == []
    assert remind_due("2026-10-16T23:45:00", [MEDS]) == [["Take night meds", 30]]
    assert remind_due("2026-10-16T23:50:00", [MEDS]) == [["Take night meds", 25]]
    assert remind_due("2026-10-17T00:15:30", [MEDS]) == [["Take night meds", 0]]  # Now
    assert remind_due("2026-10-17T00:45:00", [MEDS]) == []                       # 30 minutes after: gone


def test_a_late_evening_reminder_keeps_its_30_minutes_after_midnight():
    assert remind_due("2026-10-17T00:10:00", [LATE]) == [["Lock the back door", -20]]
    assert remind_due("2026-10-17T00:20:00", [LATE]) == []


def test_done_and_untimed_tasks_never_remind():
    tasks = [{**MEDS, "done": True}, {"id": "nb:9", "content": "Someday", "done": False, "due": {"date": "2026-10-17"}},
             {"id": "nb:10", "content": "No date", "done": False, "due": None}]
    assert remind_due("2026-10-16T23:59:00", tasks) == []


def test_a_reminder_put_away_before_midnight_stays_away_after_it():
    key = "nb:7@2026-10-17T00:15"
    assert remind_due("2026-10-17T00:05:00", [MEDS], memo={"day": "2026-10-16", "off": {key: True}, "snooze": {}}) == []
    snoozed = run_js("return new Date('2026-10-17T00:20:00').getTime();", "2026-10-17T00:00:00")
    assert remind_due("2026-10-17T00:05:00", [MEDS], memo={"off": {}, "snooze": {key: snoozed}}) == []
    # Older ones are tidied away (they're ids and times only, and only kept till the day after)
    pre = remind_prelude() +"\nstore['nb.remind:sam'] = " + json.dumps(json.dumps({"off": {key: True, "nb:1@2026-10-14T09:00": True}, "snooze": {}}))
    assert run_js("return remindMemo();", "2026-10-17T09:00:00", pre) == {"off": {key: True}, "snooze": {}}


# --- FE-R02, R04, R13, R14, R16: the reminder bar -------------------------------------------------------
def remind_section():
    return JS[JS.index("// ---------------------------------------------------------------- FE: reminders"):
              JS.index("// ---------------------------------------------------------------- FE: Quick add")]


def test_a_snoozed_reminder_is_said_and_notified_again_when_it_comes_back():
    r = remind_section()
    snooze = r[r.index("memo.snooze[key] = Date.now() + 10 * 60000;"):]
    snooze = snooze[:snooze.index("if (inBar) remindFocusAfter();")]
    assert snooze.index("remindSeen.delete(key)") < snooze.index("remindNote(")
    undo = snooze[snooze.index("remindNote("):]
    assert undo.index("remindSeen.add(key)") < undo.index("remindRender()")  # Undo: back where you are, not said again
    assert 'remindUnnotify(`nb-${key}`)' in r  # its notification in the tray goes, so the next one alerts again


def test_background_notifications_wait_for_a_fresh_list():
    tick = js_fn("remindTick")
    assert "visible && near" not in tick  # fetched again in the background too, where notifications show
    assert "loadList().then(remindRender)" in tick and "REMIND_STALE" in tick
    # near covers the whole reminder (a snooze can bring it back at any point), not just its start
    assert "return d <= lead + 15 && d > -REMIND_LATE;" in tick
    render = js_fn("remindRender")
    assert 'document.visibilityState !== "visible" && Date.now() - lastLoad.at > REMIND_STALE' in render
    assert "const fresh = stale ? [] :" in render


def test_undo_in_the_note_waits_for_the_list_and_goes_back_to_the_reminder():
    for fn in ("undoDone", "undoMove"):
        body = js_fn(fn)
        assert "await loadList();" in body and not re.search(r"[^t] loadList\(\);", body), fn
    r = remind_section()
    assert 'const again = back && $(`#remindList [data-id="${CSS.escape(back)}"] [data-r=done]`);' in r
    assert "remindNote(`Done ✓ ${name}`, canUndo ? () => undoDone(id, r.undo) : null, id);" in r


def test_signing_out_closes_the_notifications_and_clears_what_was_said():
    forget = js_fn("remindForget")
    assert "remindUnnotify();" in forget and '$("#remindSay").textContent = "";' in forget
    un = js_fn("remindUnnotify")
    assert "getNotifications(" in un and 'startsWith("nb-")' in un and "remindShown" in un
    assert "remindShown.add(n)" in js_fn("remindNotify")


def test_names_start_with_the_words_on_the_button():
    assert 'id="timerGo" aria-label="Start 5 min focus timer"' in HTML
    r = remind_section()
    assert 'aria-label="Snooze 10m: ${name}"' in r and 'aria-label="Tomorrow: ${name}"' in r and 'aria-label="Done: ${name}"' in r
    assert '`or ${est} min: start a focus timer for your estimate`' in JS


# --- FE-R03, R09, R12, R15: the page around the note -----------------------------------------------------
def media_block(css: str, query: str, needle: str) -> str:
    """The @media block (query) that contains needle."""
    for m in re.finditer(re.escape(query) + r"\s*\{", css):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        if needle in css[m.end():i]:
            return css[m.end():i]
    raise AssertionError(f"{needle} not in any {query}")


def test_on_phones_the_note_scrolls_away_and_the_routine_card_is_small():
    phone = media_block(HTML, "@media (max-width:520px)", ".remindbar { position:static")
    assert ".routinecard .routinekick, .routinecard .routinesub { display:none; }" in phone
    assert "#routineActs [data-routine=hide] {" in phone and 'content:"✕" / ""' in phone  # name stays "Not today"
    # Just one thing's own reminder isn't said twice on My list (it's still announced and notified)
    r = remind_section()
    assert "const shown = items.filter((x) => x.t.id !== nowId);" in r
    assert "items.filter((x) => !remindSeen.has(x.key))" in r


def test_settings_jump_row_and_bulk_bars_stick_under_the_note():
    assert "#setJump, .bulkbar { top:var(--remindH, 0px); }" in HTML
    assert "#settings > .card { scroll-margin-top:calc(60px + var(--remindH, 0px)); }" in HTML
    measure = js_fn("remindMeasure")
    assert 'root.setProperty("--remindH", `${h}px`)' in measure
    assert "ResizeObserver" in remind_section() and 'removeProperty("--remindH")' in js_fn("remindForget")


def test_the_reminder_close_button_is_a_full_tap_target():
    rule = re.search(r"\.remind \.rclose \{[^}]*\}", HTML).group(0)
    assert "min-width:44px" in rule and "min-height:44px" in rule


def test_small_phones_keep_quick_add_and_the_round_button_together():
    small = media_block(HTML, "@media (max-width:400px)", "header .quickbtn")
    assert "header #roleBadge, header #betaBadge { order:6; }" in small


# --- FE-R11: a shared photo that's never picked up doesn't stay ------------------------------------------
def test_a_shared_photo_left_behind_is_let_go():
    quick = js_fn("quickRun")
    assert "if (!quickRoute?.shared && !quickRun.tidied) { quickRun.tidied = true; shareForget(); }" in quick
    assert quick.index("shareForget()") < quick.index("$$(\"dialog[open]\").length")  # before it waits for a sheet
    assert "e.waitUntil(Promise.all([self.clients.claim(), shareTidy()]))" in SW
    tidy = SW[SW.index("async function shareTidy"):]
    tidy = tidy[:tidy.index("\n}\n")]
    assert "3600000" in tidy and "caches.delete(SHARE_CACHE)" in tidy and "caches.has(SHARE_CACHE)" in tidy
    assert "fetch(" not in SW  # still fetches and caches nothing else
