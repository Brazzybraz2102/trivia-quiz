"""Feature batch FC: the setup questions, Easy-to-read text (reading_mode, also on labels) and Same words
every time (routine_mode) with the routine times. The Schedule view itself is computed in the browser."""
import dataclasses
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import ImageFont

from nextbox import jobs, quiz, render
from nextbox.auth import PREFS, Accounts, default_prefs
from nextbox.render import DayRow, reading_font, render_day, render_focus, render_label
from nextbox.server import create_app

from conftest import dump, rows

WEB = Path(__file__).resolve().parent.parent / "web"


def site(ctx):
    """Sam and the boss (a superadmin), with My list only."""
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    app = create_app(ctx)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, login


# --- the new settings ------------------------------------------------------------------------------
def test_new_settings_have_safe_defaults_and_check_their_values(ctx):
    d = default_prefs()
    assert d["setup_quiz_done"] is False and d["reading_mode"] is False and d["routine_mode"] is False
    assert d["print_with"] == "" and d["routine_morning"] == "08:00" and d["routine_evening"] == "18:00"
    ok = PREFS["routine_morning"][1]
    assert ok("07:30") and ok("23:59") and ok("00:00")
    assert not any(ok(v) for v in ("7:30", "24:00", "12:60", "", None, 730, "07:30:00", "noon"))
    assert not PREFS["print_with"][1]("laser")
    ctx, a, login = site(ctx)
    c = login("sam", "sam password")
    r = c.patch("/settings", json={"routine_evening": "25:00"})
    assert r.status_code == 400 and r.json()["detail"] == "Pick a time, like 18:00."
    r = c.patch("/settings", json={"reading_mode": True, "routine_mode": True, "routine_morning": "06:45"})
    assert r.status_code == 200 and r.json()["routine_morning"] == "06:45" and r.json()["reading_mode"] is True


def test_existing_accounts_keep_their_settings_and_get_the_new_defaults(ctx):
    ctx, a, login = site(ctx)
    a.set_prefs("sam", {"max_rows": 7, "gentle_words": False})
    prefs = a.get("sam")["prefs"]
    assert prefs["max_rows"] == 7 and prefs["gentle_words"] is False  # nothing they chose is lost
    assert prefs["routine_mode"] is False and prefs["routine_evening"] == "18:00"


# --- the setup questions: who's asked --------------------------------------------------------------
def test_only_new_accounts_are_asked_and_only_until_they_answer_or_skip(ctx):
    ctx, a, login = site(ctx)
    sam, boss = login("sam", "sam password"), login("boss", "boss password")
    assert sam.get("/auth/me").json()["setup_quiz"] is True
    assert boss.get("/auth/me").json()["setup_quiz"] is True
    # Someone who already uses Next Box isn't interrupted: Settings → Help has it.
    assert boss.post("/list", json={"content": "Water the plants"}).status_code == 200
    assert boss.get("/auth/me").json()["setup_quiz"] is False
    # Skipping counts as done, and changes nothing else.
    before = sam.get("/settings").json()
    r = sam.post("/setup/quiz", json={"skip": True})
    assert r.status_code == 200 and r.json()["changed"] == {}
    after = sam.get("/settings").json()
    assert after["setup_quiz_done"] is True
    assert {k: v for k, v in after.items() if k != "setup_quiz_done"} == \
           {k: v for k, v in before.items() if k != "setup_quiz_done"}
    assert sam.get("/auth/me").json()["setup_quiz"] is False


def test_a_ticket_or_a_linked_app_also_means_not_new(ctx, todo):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    assert sam.post("/print/text", json={"text": "hello", "dry_run": True}).status_code == 200
    assert sam.get("/auth/me").json()["setup_quiz"] is False


# --- the setup questions: what each answer sets ----------------------------------------------------
def test_every_answer_sets_its_settings(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    r = sam.post("/setup/quiz", json={"answers": {"print": "paper", "journal": "a5", "rows": 3,
                                                  "helps": ["reading", "routine"], "plan_at": "07:30",
                                                  "review_at": "20:00"}})
    assert r.status_code == 200, r.text
    changed, prefs = r.json()["changed"], r.json()["prefs"]
    assert changed == {"print_with": "paper", "always_dry_run": True, "paper_size": "a5", "journal_mode": True,
                       "max_rows": 3, "reading_mode": True, "routine_mode": True,
                       "routine_morning": "07:30", "routine_evening": "20:00"}
    assert all(prefs[k] == v for k, v in changed.items())
    assert prefs["setup_quiz_done"] is True
    assert prefs["gentle_words"] is True  # not picked, but it's on for everyone: left alone
    assert a.get("sam")["prefs"]["routine_evening"] == "20:00"


def test_the_print_and_journal_answers():
    assert quiz.changes_for({"print": "label"}) == {"print_with": "label", "always_dry_run": False}
    assert quiz.changes_for({"print": "none"}) == {"print_with": "none", "always_dry_run": True}
    assert quiz.changes_for({"journal": "none"}) == {"journal_mode": False}  # paper size left as it was
    assert quiz.changes_for({"journal": "half_letter"}) == {"paper_size": "half_letter", "journal_mode": True}
    assert quiz.changes_for({}) == {} and quiz.changes_for({"print": None, "helps": []}) == {}


def test_what_helps_turns_the_helpers_on_and_just_the_basics_turns_them_off():
    assert quiz.changes_for({"helps": ["gentle"]}) == {"gentle_words": True, "reading_mode": False, "routine_mode": False}
    assert quiz.changes_for({"helps": ["reading"]}) == {"reading_mode": True, "routine_mode": False}
    assert quiz.changes_for({"helps": ["basics"]}) == {"gentle_words": False, "reading_mode": False,
                                                       "routine_mode": False}


@pytest.mark.parametrize("answers, words", [
    ({"print": "laser"}, "Pick how you'll print"),
    ({"journal": "a3"}, "journal size"),
    ({"rows": 0}, "3, 5 or 8"), ({"rows": "5"}, "3, 5 or 8"), ({"rows": True}, "3, 5 or 8"),
    ({"helps": ["adhd"]}, "what helps"), ({"helps": "gentle"}, "what helps"),
    ({"helps": ["basics", "reading"]}, "on its own"),
    ({"plan_at": "8am"}, "like 08:00"), ({"review_at": "25:00"}, "like 08:00"),
])
def test_answers_the_questions_dont_offer_are_refused_in_plain_words(ctx, answers, words):
    with pytest.raises(quiz.QuizError, match=words):
        quiz.changes_for(answers)


def test_a_refused_answer_saves_nothing(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    r = sam.post("/setup/quiz", json={"answers": {"print": "paper", "rows": 99}})
    assert r.status_code == 400 and "3, 5 or 8" in r.json()["detail"]
    prefs = sam.get("/settings").json()
    assert prefs["setup_quiz_done"] is False and prefs["print_with"] == "" and prefs["always_dry_run"] is False
    assert sam.post("/setup/quiz", json={"answers": ["paper"]}).status_code == 400


def test_answers_tick_the_get_started_guide(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/setup/quiz", json={"answers": {"print": "none"}})
    steps = {s["key"]: s for s in sam.get("/setup").json()["steps"]}
    assert steps["printer"]["done"] and steps["printer"]["skipped"]  # no printer yet: put the printer step away
    assert not steps["focus"]["done"]  # printing alone isn't "make it fit your brain"
    sam.post("/setup/quiz", json={"answers": {"rows": 5}})
    assert {s["key"]: s for s in sam.get("/setup").json()["steps"]}["focus"]["done"]


def test_what_helps_is_never_stored_or_logged(ctx):
    """It can hint at ADHD, dyslexia or autism: only the ordinary settings it changes are kept."""
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/setup/quiz", json={"answers": {"helps": ["gentle", "reading", "routine"]}})
    everything = dump(ctx.settings.data_dir)
    for word in ('"gentle"', '"reading"', '"routine"', "helps", "adhd", "dyslexia", "autis"):
        assert word not in everything.lower(), word
    logged = [e for e in rows(ctx.settings.data_dir, "events") if e["action"] == "setup_quiz"]
    assert len(logged) == 1 and logged[0]["detail"]["changed"] == ["gentle_words", "reading_mode", "routine_mode"]


def test_the_setup_questions_need_a_signed_in_person(ctx):
    ctx, a, login = site(ctx)
    anon = TestClient(create_app(ctx), base_url="http://testserver")
    assert anon.post("/setup/quiz", json={"skip": True}).status_code == 401
    key = anon.post("/setup/quiz", json={"skip": True}, headers={"X-NextBox-Key": "k"})
    assert key.status_code == 403


# --- previews say what to do next ------------------------------------------------------------------
def test_preview_note_follows_how_they_print(ctx):
    ctx = dataclasses.replace(ctx, prefs={**default_prefs(), "always_dry_run": True, "print_with": "paper"})
    ctx.settings.dry_run = False
    ctx.settings.printer_ip = "192.0.2.10"  # a printer exists, but this person always previews
    r = jobs.print_text(ctx, "hello")
    assert r["status"] == "dry_run" and r["note"] == jobs.PREVIEW_NOTES["paper"]
    ctx = dataclasses.replace(ctx, prefs={**ctx.prefs, "print_with": "none"})
    assert jobs.print_text(ctx, "hello")["note"] == jobs.PREVIEW_NOTES["none"]
    ctx = dataclasses.replace(ctx, prefs={**ctx.prefs, "print_with": ""})
    assert jobs.print_text(ctx, "hello")["note"] == "Always preview only is on in Settings, so this is a preview."


# --- Easy-to-read text on labels -------------------------------------------------------------------
def test_the_reading_font_ships_with_its_licence_and_is_served():
    for name in render.READING_FONTS:
        assert (render.READING_DIR / name).stat().st_size > 20000
        assert ImageFont.truetype(str(render.READING_DIR / name), 20).getname()[0] == "Atkinson Hyperlegible"
    assert "SIL Open Font License" in (render.READING_DIR / "OFL.txt").read_text()
    assert render.READING_DIR == WEB / "fonts"


def test_fonts_are_served_to_the_web_app(ctx):
    ctx, a, login = site(ctx)
    c = TestClient(create_app(ctx), base_url="http://testserver")
    r = c.get("/app/fonts/AtkinsonHyperlegible-Regular.ttf")
    assert r.status_code == 200 and len(r.content) > 20000
    css = (WEB / "index.html").read_text()
    assert 'url("fonts/AtkinsonHyperlegible-Regular.ttf")' in css and 'url("fonts/AtkinsonHyperlegible-Bold.ttf")' in css


def test_labels_use_the_reading_font_only_inside_reading_font():
    name = lambda f: Path(getattr(f, "path", "") or "").name  # noqa: E731
    assert "Atkinson" not in name(render._font(30))
    with reading_font(True):
        assert Path(render._font(30).path).name == "AtkinsonHyperlegible-Regular.ttf"
        assert Path(render._font(30, True).path).name == "AtkinsonHyperlegible-Bold.ttf"
        with reading_font(False):
            assert "Atkinson" not in name(render._font(30))
        assert "Atkinson" in name(render._font(30))
    assert "Atkinson" not in name(render._font(30))


def test_the_reading_font_borrows_the_legend_symbols_from_dejavu():
    if render._dejavu() is None:
        pytest.skip("DejaVu isn't installed here")
    with reading_font(True):
        f = render._font(28)
        runs = render._runs("✓ done → move ✗ drop ↻", f)
    assert runs is not None
    by_text = {t: Path(font.path).name for t, font in runs}
    assert by_text["✓"].startswith("DejaVuSans") and by_text[" done "].startswith("Atkinson")


def test_a_missing_reading_font_falls_back_to_dejavu(monkeypatch, tmp_path):
    monkeypatch.setattr(render, "READING_DIR", tmp_path)
    with reading_font(True):
        assert "Atkinson" not in Path(getattr(render._font(30), "path", "") or "").name


def test_capital_headings_go_sentence_case_with_easy_to_read_text():
    assert render._heading("JUST ONE THING") == "JUST ONE THING"
    with reading_font(True):
        assert render._heading("JUST ONE THING") == "Just one thing"
        assert render._heading("SCHEDULE") == "Schedule" and render._heading("NEW TASK") == "New task"
        assert render._heading("Groceries") == "Groceries"  # their own titles are left as they are
        assert render._heading("MILK") == "MILK"


def test_every_label_draws_in_the_reading_font():
    rows = [DayRow("Call the dentist", "9:00a", True, "waiting 2d ↻", late=True, note="Find the number")]
    for on in (False, True):
        with reading_font(on):
            assert render_day("Friday", "October 16", rows, code="261016-ABCD", schedule=[("9:00a", "Standup")],
                              journal=True).width == render.WIDTH
            assert render_focus("Call the dentist", ["Find the number"], "261016-ABCD").width == render.WIDTH
            assert render_label("NOTE", "Fri", body="hello ✓", code="261016-ABCD").width == render.WIDTH


def test_print_jobs_use_the_reading_font_when_it_is_on(ctx, monkeypatch):
    seen = []
    real = render._font

    def spy(size, bold=False):
        f = real(size, bold)
        seen.append(Path(getattr(f, "path", "default")).name)
        return f
    monkeypatch.setattr(render, "_font", spy)
    jobs.print_today(dataclasses.replace(ctx, prefs={**default_prefs(), "reading_mode": True}), dry_run=True)
    assert seen and all(n.startswith("Atkinson") for n in seen)
    seen.clear()
    jobs.print_today(ctx, dry_run=True)
    assert seen and not any(n.startswith("Atkinson") for n in seen)


# --- the web app has the pieces ----------------------------------------------------------------------
def test_the_web_app_has_the_toggles_and_the_setup_dialog():
    html = (WEB / "index.html").read_text()
    for needle in ('data-pref="reading_mode"', 'data-pref="routine_mode"', 'data-pref="routine_morning"',
                   'data-pref="routine_evening"', 'id="quizDialog"', 'id="quizAgain"', 'id="listView"',
                   'data-view="schedule"', "html[data-reading]"):
        assert needle in html, needle
    js = (WEB / "app.js").read_text()
    assert 'me?.prefs?.routine_mode ? "Done ✓"' in js  # same words every time


def test_app_js_never_declares_the_same_top_level_name_twice():
    """A second `function x` silently replaces the first (the Schedule's event rows once replaced the
    activity log's), so every top-level function, const and let name is declared once."""
    import collections
    import re
    js = (WEB / "app.js").read_text()
    names = re.findall(r"^(?:async )?function (\w+)|^(?:const|let) (\w+)", js, re.M)
    counts = collections.Counter(a or b for a, b in names)
    assert [n for n, k in counts.items() if k > 1] == []
