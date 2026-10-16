"""My list, Schedule, dialogs and accessibility: fixes from review batch C (R-C01 … R-C21).

The server-side parts are tested through the API. The rest lives in the browser (web/app.js and
web/index.html) and was checked by hand in a real browser; these tests pin the pieces that make each
fix work, so a later change doesn't quietly undo one."""
import dataclasses
import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nextbox import server
from nextbox.auth import Accounts
from nextbox.server import create_app

from conftest import TODAY

WEB = Path(server.WEB_DIR)


@pytest.fixture
def site(ctx):
    """sam (user), My list only, on a fixed day (Monday, TODAY)."""
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    app = create_app(ctx)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return a, TestClient(app, base_url="http://testserver"), login


@pytest.fixture(scope="module")
def js():
    return (WEB / "app.js").read_text()


@pytest.fixture(scope="module")
def html():
    return (WEB / "index.html").read_text()


def body_of(src: str, start: str, end: str = "\n}\n") -> str:
    """The source from `start` to the end of that function or handler."""
    i = src.index(start)
    return src[i:src.index(end, i) + len(end)]


def day(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def due_of(client, task_id: str) -> str:
    t = next(t for t in client.get("/list").json()["tasks"] if t["id"] == task_id)
    return t["due"]["date"][:10]


# ------------------------------------------------------------------ R-C04 and R-C17: → Tomorrow never moves a task sooner
def test_move_to_tomorrow_never_pulls_a_later_task_sooner(site):
    a, anon, login = site
    sam = login("sam", "sam password")
    later = sam.post("/list", json={"content": "Renew passport", "due_date": day(5)}).json()
    due = sam.post("/list", json={"content": "Call the dentist", "due_date": day(0)}).json()

    r = sam.post(f"/list/{later['id']}/move", json={"to": "tomorrow"}).json()
    assert r["moved"] is False and r["later"] is True and r["to"] == day(5)
    assert due_of(sam, later["id"]) == day(5)  # stays on its own day, not tomorrow

    r = sam.post(f"/list/{due['id']}/move", json={"to": "tomorrow"}).json()
    assert r["moved"] is True and r["to"] == day(1) and r["undo"] == {"to": day(0), "undo": True}
    # Undo puts it back (earlier), which is allowed: it's where it was.
    assert sam.post(f"/list/{due['id']}/move", json=r["undo"]).json()["to"] == day(0)
    # A chosen day (the Edit dialog, read-back) can still be any day.
    assert sam.post(f"/list/{later['id']}/move", json={"to": day(1)}).json()["moved"] is True


def test_bulk_tomorrow_skips_tasks_not_due_yet_and_says_where_they_went(js):
    h = body_of(js, '$("#bulkTomorrow").onclick', "\n};\n")
    assert 't.due.date.slice(0, 10) <= now' in h  # only tasks due today or waiting are sent
    assert "wasn't" in h and "due yet" in h  # the snack says how many stayed
    assert "dayWords" in h and "Moved ${moved.length} to ${where}" in h  # "to Mon, Oct 19", not always "tomorrow"


# ------------------------------------------------------------------ R-C14: finishing your last task isn't a fresh start
def test_list_says_whether_the_person_ever_had_a_task(site, js):
    a, anon, login = site
    sam = login("sam", "sam password")
    assert sam.get("/list").json()["has_tasks"] is False
    t = sam.post("/list", json={"content": "call mom", "due_date": day(0)}).json()
    assert sam.post(f"/list/{t['id']}/done").status_code == 200
    got = sam.get("/list").json()
    assert got["tasks"] == [] and got["has_tasks"] is True  # the card says "Nothing else is due today"
    other = login("boss", "boss password").get("/list").json()
    assert other["has_tasks"] is False  # someone else's tasks don't count
    now = body_of(js, "function renderNow()")
    assert "!listData.has_tasks && !listData.done_today" in now


# ------------------------------------------------------------------ R-C16: the AI disclosure comes first, and says it all
def test_terms_version_went_up_for_the_ai_text(site):
    a, anon, login = site
    assert server.TERMS_VERSION == 4
    a.record_consent("sam", server.DATA_NOTICE_VERSION, terms=3)  # agreed before the AI paragraph
    sam = login("sam", "sam password")
    me = sam.get("/auth/me").json()
    assert me["terms_ok"] is False and me["notice_changed"] is True
    assert sam.post("/auth/consent").status_code == 200
    assert sam.get("/auth/me").json()["terms_ok"] is True


def test_break_it_down_says_what_it_sends_before_sending(site, js):
    a, anon, login = site
    privacy = anon.get("/app/privacy.html").text
    assert "how long it takes, its day, its steps and anything you type in “What makes it hard?”" in privacy
    assert "What makes it hard?" in js[js.index("const BREAK_SENT"):js.index("\n", js.index("const BREAK_SENT"))]
    opener = body_of(js, "function openBreak(")
    assert opener.index('drawBreak("intro")') < opener.index("askBreak()")  # nothing is sent on the first tap
    assert 'data-b="ask">Suggest steps' in js and '(what === "ask") { breakSeenNow(); askBreak(); }' in js
    assert "AI helpers (Break it down and Look back on my week)" in js  # the "this changed" note names them


# ------------------------------------------------------------------ R-C01: Schedule follows the search and tag chips
def test_schedule_filters_like_list_and_prints_what_it_shows(js):
    sched = body_of(js, "function scheduleHtml()")
    assert "const shown = filtering() ? matches : () => true;" in sched
    assert "all.filter(shown)" in sched and 'noMatchHtml("today")' in sched
    assert "return { html, shown: drawn, total };" in sched
    assert "applyFilterUi(s.shown, s.total);" in body_of(js, "function renderSchedule(")
    ui = body_of(js, "function applyFilterUi(")
    assert "shownOpen = openShown" in ui and "renderTagFilter()" in ui and "#listFilterClear" in ui
    assert "applyFilterUi(" in body_of(js, "function renderList()")


# ------------------------------------------------------------------ R-C02 and R-C07: one save at a time; Break it down saves first
def test_task_dialog_saves_once_even_with_a_double_tap(js):
    assert 'const TD_SAVERS = ["#tdSave", "#tdFocus", "#tdBreak", "#tdSkipRepeat"];' in js
    for start in ('$("#tdFocus").onclick', '$("#tdBreak").onclick', '$("#tdSkipRepeat").onclick'):
        h = body_of(js, start, "\n};\n")
        assert "tdBusy()" in h and "tdHold(saveEditing)" in h, start
    submit = body_of(js, '$("#taskForm").onsubmit', "\n};\n")
    assert "if (tdBusy()) return;" in submit and "await tdHold(" in submit
    brk = body_of(js, '$("#tdBreak").onclick', "\n};\n")
    assert "!editing.id || tdDirty()" in brk  # a new task, or changed words, are saved before asking


# ------------------------------------------------------------------ R-C08: Esc doesn't throw away what was typed
def test_escape_asks_before_discarding_a_task_or_feedback(js):
    h = body_of(js, '$("#taskDialog").addEventListener("cancel"', "\n});\n")
    assert "tdDirty()" in h and "confirm(" in h and "e.preventDefault()" in h
    fb = body_of(js, '$("#fbDialog").addEventListener("cancel"', "\n});\n")
    assert "confirm(" in fb and "#fbDone" in fb


# ------------------------------------------------------------------ R-C06: Run setup again starts from today's settings
def test_setup_questions_opened_by_hand_show_current_settings(js):
    assert "function openQuiz(manual = false)" in js and "rerun: manual || !!me?.prefs?.setup_quiz_done" in js
    assert '$("#quizAgain").onclick = () => openQuiz(true);' in js
    assert '["Answer 5 quick questions", () => openQuiz(true)]' in js
    assert js.count("openQuiz()") == 1  # only the first-run, automatic one


# ------------------------------------------------------------------ R-C09 and R-C10: keyboard and screen readers
def test_undo_is_reachable_from_the_keyboard(js, html):
    snack = body_of(js, "function snack(")
    assert "snackUndo" in snack and "sr-only" in snack and "hide.waits = snackKeys" in snack
    assert 'e.key.toLowerCase() === "z" && snackUndo' in js
    assert "<kbd>Z</kbd></dt><dd>Undo" in html


def test_focus_never_falls_to_the_page(js, html):
    now = body_of(js, "function renderNow()")
    assert "const hadFocus = $(\"#nowCard\").contains(document.activeElement);" in now
    assert 'msg.querySelector("[data-go-tab]").focus()' in js  # Add lines: "Open My list"
    assert '$("#quizDialog").addEventListener("close", () => setTimeout(() => { if (document.activeElement === document.body' in js
    assert '<h2 id="noticeTitle" tabindex="-1" autofocus>' in html


# ------------------------------------------------------------------ R-C03, R-C05, R-C11, R-C19, R-C20: layout
def test_phone_layout_rules(html):
    phone = html[html.index("/* Phones: a row's words get the whole width"):]
    phone = phone[:phone.index("\n  }\n")]
    for rule in (".stepedit li { flex-wrap:wrap;", ".stepedit li textarea { flex:1 1 calc(100% - 42px); }",
                 '.stepedit li [data-smove="-1"] { margin-left:auto; }', ".srow { flex-wrap:wrap;", ".srow .stime { width:100%;"):
        assert rule in phone, rule
    # The smallest phones: a row's when and steps get their own line, the buttons go under them.
    assert '@media (max-width:359px) {\n    .task { grid-template-areas:"pick text text" ". meta meta" ". acts acts" ". steps steps"; }' in html
    # Messages above the tab bar and the stuck print bar, measured, not guessed.
    assert "bottom:124px" not in html and "196px" not in html
    assert "var(--barH, 0px)" in html and "var(--tabH" in html
    assert 'setProperty("--barH"' in js_text() and "function placeToast()" in js_text()
    assert re.search(r"#breakActs \{ position:sticky; bottom:-18px;", html)
    assert ".now .nowstep { font-size:16px; margin:4px 0 10px; overflow-wrap:anywhere; }" in html
    assert ".srow .sbody .meta {" in html and "overflow-wrap:anywhere; }" in html[html.index(".srow .sbody .meta {"):][:120]


def js_text() -> str:
    return (WEB / "app.js").read_text()


# ------------------------------------------------------------------ R-C12: a waiting linked task comes over for today
def test_add_to_my_list_from_a_waiting_task_starts_today(js):
    copy = body_of(js, "async function copyBreak(")
    assert "b.task.due_date < today() ? today()" in copy and "due_date: due" in copy


# ------------------------------------------------------------------ R-C13 and R-C15: the words fit the person and match
def test_schedule_rows_follow_the_setup_answers(js):
    row = body_of(js, "function schedRow(")
    assert 'p.print_with !== "none"' in row and "p.journal_mode !== false" in row
    assert "Preview today's label" in row and '"Snap your pages"' in row and "data-sched-look" in row
    assert '"Look back"' not in row


def test_one_name_for_each_thing(html, js):
    assert "daily ticket" not in html.lower() and "daily ticket" not in js.lower()
    assert "Look back at" not in html and "<b>Snap your pages at</b>" in html
    assert "Print it big" not in js and "Print on a card" not in js
    assert "today's ticket" not in js.lower()


# ------------------------------------------------------------------ R-C18: a yes/no question has only yes and no
def test_break_it_down_hides_while_finish_the_task_is_asked(js):
    # Also hidden with no AI key (walkthrough W2-02): no Break it down button to dead-end on.
    assert '$("#nowBreak").hidden = !aiOn() || !me?.ai_ready || !t || asking;' in body_of(js, "function renderNow()")


# ------------------------------------------------------------------ R-C21: nothing private stays on a shared phone
def test_sign_out_and_sign_in_clear_drafts_and_search_words(js):
    out = body_of(js, "async function signOut(")
    assert "forgetDraft();" in out and "forgetMyKeysHere();" in out
    mine = body_of(js, "function forgetMyKeysHere()")
    assert "filterKey()" in mine and "myListViewKey()" in mine
    assert r"const PERSON_KEYS = /^nb\.(dumpDraft|listFilter|listView):/;" in js
    assert "forgetOthersHere(me.username);" in body_of(js, "async function boot()")
    # A draft left by a session that ended on its own is dropped after a day.
    assert "DRAFT_KEEP" in body_of(js, "function restoreDraft()")
    assert "at: Date.now()" in body_of(js, "function saveDraft()")
