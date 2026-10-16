"""Round 2, batch C: Settings saves, Download my data, bulk Undo and messages, the wins chart.

All of these are in the browser (web/app.js, web/index.html), and each was checked in a real browser. The
pieces of app.js that changed are run here in node with small stand-ins for the page, so the behavior itself
is tested, not just the words; the CSS is pinned like test_walkthrough.py does."""
import dataclasses
import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nextbox import server
from nextbox.auth import Accounts
from nextbox.server import create_app

WEB = Path(server.WEB_DIR)


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


OFFLINE = "Can't reach Next Box. Check your connection."
STUBS = f"""
const OFFLINE = {json.dumps(OFFLINE)}, TASK_GONE = "That task was already removed.";
const errText = (e) => (e.status ? e.message : OFFLINE);
const shortName = (t) => t.content;
const plural = (n, w) => `${{n}} ${{w}}${{n === 1 ? "" : "s"}}`;
const toasts = [];
const toast = (msg, opts = {{}}) => toasts.push([msg, !!(opts && opts.error)]);
const tick = () => new Promise((r) => setTimeout(r, 0));
const fail = (status, message) => Object.assign(new Error(message), status ? {{ status }} : {{}});
"""


# ------------------------------------------------------------------ R2-01: Download my data, tapped twice
def test_download_my_data_ignores_a_second_tap_and_never_navigates(js):
    body = fn(js, "exportMyData")
    assert "location.href" not in body  # a failed download can't take the app's place
    script = STUBS + """
let checks = 0, answer = Promise.resolve({ ok: true });
const api = (path) => { checks++; return answer; };
const clicks = [];
const document = { body: { append() {} }, createElement: () => ({ click() { clicks.push([this.href, this.download, this.hidden]); }, remove() {} }) };
""" + body + """
(async () => {
  const out = {};
  await Promise.all([exportMyData(), exportMyData(), exportMyData()]);   // a double (triple) tap
  out.tap = { checks, clicks: clicks.slice(), toasts: toasts.slice() };
  exportMyData.busy = false; checks = 0; clicks.length = 0; toasts.length = 0;
  answer = Promise.reject(fail(429, "That's a lot of downloads this hour. Try again in a bit."));
  answer.catch(() => {});
  await exportMyData();
  out.limit = { checks, clicks: clicks.length, toasts: toasts.slice(), busy: !!exportMyData.busy };
  console.log(JSON.stringify(out));
  process.exit(0);  // not waiting out the 8 s that keeps a second tap from counting
})();
"""
    out = run(script)
    assert out["tap"]["checks"] == 1
    assert out["tap"]["clicks"] == [["/auth/export?format=zip", "", True]]  # one download link, with `download`
    assert out["tap"]["toasts"] == [["Getting your .zip ready…", False]]
    # At the hourly limit: a message, no download, and the next tap asks again.
    assert out["limit"] == {"checks": 1, "clicks": 0, "busy": False,
                            "toasts": [["That's a lot of downloads this hour. Try again in a bit.", True]]}


def test_a_check_isnt_a_download_and_one_download_counts_once(ctx):
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    c = TestClient(create_app(ctx), base_url="http://testserver")
    assert c.post("/auth/login", json={"username": "sam", "password": "sam password"}).status_code == 200
    assert c.get("/auth/export/check").status_code == 200
    assert c.get("/auth/export", params={"format": "zip"}).status_code == 200
    left = 0
    while c.get("/auth/export/check").status_code == 200 and left < 10:
        assert c.get("/auth/export", params={"format": "zip"}).status_code == 200
        left += 1
    assert left == 4


# ------------------------------------------------------------------ R2-02, R2-03: Settings saves
def settings_script(js, body):
    start = js.index("let prefQueue = Promise.resolve()")
    block = js[start:js.index("\n}\n", js.index("async function savePref(")) + 3]
    return STUBS + """
const els = {};
function el(pref, type) {
  return els[pref] = { dataset: { pref }, type, checked: true, value: "", on: {},
    addEventListener(ev, f) { this.on[ev] = f; } };
}
el("show_wins", "checkbox"); el("gentle_words", "checkbox"); el("slug_after", "number");
const $$ = () => Object.values(els);
const sent = [];
const api = (path, opts) => new Promise((res, rej) => sent.push({ json: opts.json, res, rej }));
let me = { prefs: { show_wins: true, gentle_words: true, slug_after: 3 } };
const loads = [];
const loadPrefs = (p) => loads.push({ ...p });
const errs = {};
function prefErr(el, msg) {
  if (msg) { el.dataset.bad = "1"; errs[el.dataset.pref] = msg; }
  else { delete el.dataset.bad; delete el.dataset.retry; delete errs[el.dataset.pref]; }
}
const noop = () => {};
const applyLook = noop, refreshStatus = noop, renderList = noop, loadToday = noop, forgetListView = noop,
  routineRender = noop, remindRender = noop;
""" + block + "\n(async () => {\n" + body + "\n})();"


def test_only_the_newest_settings_answer_redraws_the_settings(js):
    out = run(settings_script(js, """
  els.show_wins.checked = false; els.show_wins.onchange();
  els.gentle_words.checked = false; els.gentle_words.onchange();   // tapped before the first save is back
  await tick();
  sent[0].res({ show_wins: false, gentle_words: true, slug_after: 3 });   // the first answer: gentle_words still on
  await tick(); await tick();
  const afterFirst = loads.length;
  sent[1].res({ show_wins: false, gentle_words: false, slug_after: 3 });
  await tick(); await tick();
  console.log(JSON.stringify({ afterFirst, loads, sent: sent.map((s) => s.json), pending: prefPending }));
"""))
    assert out["sent"] == [{"show_wins": False}, {"gentle_words": False}]
    assert out["afterFirst"] == 0  # the older answer doesn't flip Gentle words back on for a moment
    assert out["loads"] == [{"show_wins": False, "gentle_words": False, "slug_after": 3}]
    assert out["pending"] == 0


def test_a_failed_switch_goes_back_once_the_saves_after_it_are_done(js):
    out = run(settings_script(js, """
  els.show_wins.checked = false; els.show_wins.onchange();
  els.gentle_words.checked = false; els.gentle_words.onchange();
  await tick();
  sent[0].rej(fail(400, "Nope."));
  await tick(); await tick();
  const afterFirst = loads.length;
  sent[1].res({ show_wins: true, gentle_words: false, slug_after: 3 });
  await tick(); await tick();
  console.log(JSON.stringify({ afterFirst, loads, toasts }));
"""))
    assert out["afterFirst"] == 0
    assert out["loads"] == [{"show_wins": True, "gentle_words": False, "slug_after": 3}]  # Show yesterday's wins: on again
    assert ["Nope.", False] in out["toasts"]


def test_a_typed_setting_that_couldnt_reach_next_box_saves_when_they_leave_the_box_again(js):
    out = run(settings_script(js, """
  const s = els.slug_after;
  s.value = "6"; s.onchange();
  await tick();
  sent[0].rej(fail(0, OFFLINE));                      // offline: no status
  await tick(); await tick();
  const offline = { retry: s.dataset.retry, err: errs.slug_after };
  s.on.blur();                                        // back online: click the box, Tab away (same value)
  await tick();
  const retried = sent.length === 2 ? sent[1].json : null;
  sent[1].res({ show_wins: true, gentle_words: true, slug_after: 6 });
  await tick(); await tick();
  const saved = { retry: s.dataset.retry || null, bad: s.dataset.bad || null, err: errs.slug_after || null };
  s.on.blur();                                        // saved: leaving the box again sends nothing
  await tick();
  // A value the server refuses (it has a status) isn't tried again on leaving the box: it waits for a fix.
  s.value = "25"; s.onchange(); await tick();
  sent[2].rej(fail(400, "Pick a number from 2 to 10.")); await tick(); await tick();
  s.on.blur(); await tick();
  console.log(JSON.stringify({ offline, retried, saved, sent: sent.length, bad: errs.slug_after }));
"""))
    assert out["offline"] == {"retry": "1", "err": f"{OFFLINE} It saves when you leave this box again."}
    assert out["retried"] == {"slug_after": 6}
    assert out["saved"] == {"retry": None, "bad": None, "err": None}
    assert out["sent"] == 3 and out["bad"] == "Pick a number from 2 to 10."


def test_settings_never_redraw_the_box_being_typed_in(js):
    body = fn(js, "loadPrefs")
    script = STUBS + """
function el(pref, type, value) { return { dataset: { pref }, type, value, checked: false }; }
const num = el("slug_after", "number", "7"), text = el("default_filter", "text", "#Groc"), sw = el("show_wins", "checkbox");
const other = el("max_rows", "number", "9");
const document = { activeElement: num };
const $$ = () => [num, text, sw, other];
const box = { checked: false, value: "" };
const $ = () => box;
const noop = () => {};
const confText = noop, remindShowPrefs = noop, applyAi = noop, applySnapReady = noop, applyDry = noop, setAppNames = noop;
""" + body + """
const prefs = { slug_after: 3, default_filter: "", show_wins: true, max_rows: 5, confidence: 0.8 };
loadPrefs(prefs);
const first = [num.value, text.value, sw.checked, other.value];
text.value = "#Groc"; document.activeElement = text; loadPrefs(prefs);
const second = [num.value, text.value];
document.activeElement = sw; sw.checked = false; loadPrefs(prefs);    // a switch with focus still shows what's saved
console.log(JSON.stringify({ first, second, sw: sw.checked }));
"""
    out = run(script)
    assert out["first"] == ["7", "", True, 5]   # the number box being typed in keeps its 7; the rest show what's saved
    assert out["second"] == [3, "#Groc"]             # once they've left it, it shows what's saved
    assert out["sw"] is True


def test_the_setup_questions_clear_a_stale_error_on_the_settings_they_change(js):
    body = fn(js, "saveQuiz")
    clear = body.index('Object.keys(r.changed).forEach((k) => { const el = $(`[data-pref=${k}]`); if (el) prefErr(el, ""); });')
    assert clear < body.index("loadPrefs(me.prefs)")


# ------------------------------------------------------------------ W4-04, W4-05: bulk Undo and partial failures
def bulk_script(js, body):
    return STUBS + """
const lists = [];
const loadList = () => lists.push(1);
""" + const(js, "didntChange") + fn(js, "undoPicked") + "\n(async () => {\n" + body + "\n})();"


def test_bulk_undo_says_how_many_went_back_and_names_one_that_didnt(js):
    out = run(bulk_script(js, """
  const a = { id: "nb:2", content: "Pick up prescription" }, b = { id: "nb:5", content: "Text Sam back" };
  const res = {};
  await undoPicked([[a, {}], [b, {}]], (t) => t === a ? Promise.reject(fail(409, "That task is finished already.")) : Promise.resolve({}),
    "Back where they were");
  res.tomorrow = toasts.pop();
  await undoPicked([[a, {}], [b, {}]], (t) => t === a ? Promise.reject(fail(404, TASK_GONE)) : Promise.resolve({}), "Back on your list", 3);
  res.done = toasts.pop();
  await undoPicked([[a, {}], [b, {}]], () => Promise.resolve({}), "Back where they were");
  res.all = toasts.pop();
  let tries = 0;
  await undoPicked([[a, {}], [b, {}]], () => { tries++; return Promise.reject(fail(0, OFFLINE)); }, "Back where they were");
  res.offline = [toasts.pop(), tries];
  res.lists = lists.length;
  console.log(JSON.stringify(res));
"""))
    assert out["tomorrow"] == ["Back where they were (1 of 2) · “Pick up prescription” didn't change: That task is finished already.", True]
    assert out["done"] == ["Back on your list (1 of 3) · “Pick up prescription” didn't change: That task was already removed.", True]
    assert out["all"] == ["Back where they were", False]
    # Offline: it stops at the first, and says neither went back.
    assert out["offline"] == [[f"“Pick up prescription” and 1 more didn't change: {OFFLINE}", True], 1]
    assert out["lists"] == 4  # the list is drawn again after each Undo


def test_a_partial_bulk_failure_names_the_task_and_says_why(js):
    script = STUBS + """
const BULK = ["#bulkDone"];
const node = { setAttribute() {}, removeAttribute() {}, textContent: "" };
const $ = () => node;
const picked = new Set(["nb:2", "nb:5", "nb:7"]);
const updatePicks = () => {};
const lists = [];
const loadList = () => lists.push(1);
""" + fn(js, "eachPicked") + const(js, "didntChange") + """
(async () => {
  const a = { id: "nb:2", content: "Pick up prescription" }, b = { id: "nb:5", content: "Text Sam back" }, c = { id: "nb:7", content: "Water plants" };
  const out = {};
  let ok = await eachPicked("Moving", [a, b], (t) => t === a ? Promise.reject(fail(404, TASK_GONE)) : Promise.resolve({ moved: true }));
  out.partial = [ok.length, ok.missed, toasts.length, [...picked]];
  ok = await eachPicked("Moving", [a, c], () => Promise.reject(fail(404, TASK_GONE)));
  out.none = [ok.length, toasts.pop(), lists.length];
  ok = await eachPicked("Finishing", [b, a, c], (t) => t === b ? Promise.resolve({}) : Promise.reject(fail(0, OFFLINE)));
  out.offline = ok.missed;
  console.log(JSON.stringify(out));
})();
"""
    out = run(script)
    assert out["partial"] == [1, " · “Pick up prescription” didn't change: That task was already removed.", 0, ["nb:2", "nb:7"]]
    assert out["none"] == [0, ["“Pick up prescription” and 1 more didn't change: That task was already removed.", True], 1]
    assert out["offline"] == f" · “Pick up prescription” and 1 more didn't change: {OFFLINE}"  # stopped: the last wasn't tried


def test_bulk_done_and_tomorrow_undo_through_undo_picked(js):
    assert 'undoPicked(undo,\n    (t, r) => api(`/list/${encodeURIComponent(t.id)}/reopen`' in js
    assert 'undoPicked(moved,\n    (t, r) => api(`/list/${encodeURIComponent(t.id)}/move`' in js
    assert "catch {} }" not in js[js.index("async function eachPicked("):js.index("function bulkDialog(")]


# ------------------------------------------------------------------ W4-10: Tag… with only linked tasks picked
def test_tag_sheet_title_fits_when_only_linked_tasks_are_picked(js):
    tag = js[js.index('$("#bulkTag").onclick'):js.index('$("#bulkDelete").onclick')]
    assert 'bulkDialog(mine.length ? `Tag ${plural(mine.length, "task")}` : "Tags are for My list tasks",' in tag
    assert "Tag 0" not in tag and "Tags are for My list tasks. Change" not in tag


# ------------------------------------------------------------------ W5-05: today's bar on the same line
def test_every_wins_bar_stands_on_one_line(html):
    css = {m[1].strip(): m[2] for m in re.finditer(r"^\s*(\.wins[^{]*)\{([^}]*)\}", html, re.M)}
    every, today = css[".wins div span::after"], css[".wins div.today span::after"]
    assert "display:block" in every and "height:6px" in every and "margin:2px auto 0" in every and "visibility:hidden" in every
    # Today only shows the dot: it adds no height of its own, so its bar isn't lifted.
    assert "visibility:visible" in today and not re.search(r"height|margin|display|padding", today)
    assert "height:96px" in css[".wins"]  # room for the dot row, so the tallest bar keeps its full 52px
