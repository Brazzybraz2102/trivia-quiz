"""Feature batch FA, My list faster: checkable tiny steps, one-tap moves (→ Tomorrow, with undo and
slides), Brain dump's live preview, slugs on My list rows and "Skip this time" on repeating tasks.
(Search, tag filter, + Add task and bulk actions are in the web app, on the endpoints tested here.)"""
import dataclasses
from datetime import date

from fastapi.testclient import TestClient

from conftest import FakeTodoist, rows
from nextbox.auth import Accounts
from nextbox.journal import Slides
from nextbox.mylist import LocalTasks, Merged, preview_dump
from nextbox.providers import ProviderError
from nextbox.server import create_app

FRI = date(2026, 10, 16)


class MovingTodoist(FakeTodoist):
    """A fake linked app whose set_due_date really moves the task (FakeTodoist only records it)."""

    def set_due_date(self, task_id, date_iso):
        super().set_due_date(task_id, date_iso)
        self.tasks[task_id] = {**self.tasks[task_id], "due": {**(self.tasks[task_id].get("due") or {}), "date": date_iso}}


def site(ctx, linked=None, today=date.today):
    """sam and kim, each with My list; sam also has `linked` (a fake to-do app) when given."""
    a = Accounts(ctx.settings.data_dir)
    factory = (lambda: Merged(LocalTasks(a.db, "sam", today=today), linked)) if linked else None
    ctx = dataclasses.replace(ctx, tasks_factory=factory, today=today)
    ctx.settings.todoist_token = ""
    a.set_password("sam", "sam password", create=True)
    a.create("kim", "kim password")
    app = create_app(ctx)

    def login(name="sam", pw="sam password"):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, login


def add(c, line):
    return c.post("/list/dump", json={"text": line}).json()["added"][0]


def get(c, task_id):
    return next(t for t in c.get("/list?include_done=true").json()["tasks"] if t["id"] == task_id)


def wins_today(c):
    return next(d for d in c.get("/journal/wins").json()["series"] if d["is_today"])["count"]


# --- 1. checkable tiny steps -----------------------------------------------------------------------
def test_one_step_can_be_ticked_and_unticked(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    t = add(sam, "Pay the bill today")
    sam.patch(f"/list/{t['id']}", json={"steps": ["Find the bill", "Open the website", "Pay"]})
    r = sam.patch(f"/list/{t['id']}/steps/0", json={"done": True, "text": "Find the bill"})
    assert r.status_code == 200, r.text
    assert [s["done"] for s in r.json()["steps"]] == [True, False, False]
    assert not r.json()["done"]  # ticking a step never finishes the task
    r = sam.patch(f"/list/{t['id']}/steps/0", json={"done": False})  # no text: just this index
    assert [s["done"] for s in r.json()["steps"]] == [False, False, False]
    assert wins_today(sam) == 0  # a step isn't a win; the whole task is


def test_a_step_changed_on_another_device_is_not_ticked(ctx):
    ctx, a, login = site(ctx)
    sam, kim = login(), login("kim", "kim password")
    t = add(sam, "Pay the bill today")
    sam.patch(f"/list/{t['id']}", json={"steps": ["Find the bill", "Pay"]})
    sam.patch(f"/list/{t['id']}", json={"steps": ["Pay", "Find the bill"]})  # reordered elsewhere
    r = sam.patch(f"/list/{t['id']}/steps/0", json={"done": True, "text": "Find the bill"})
    assert r.status_code == 409 and "another device" in r.json()["detail"]
    assert sam.patch(f"/list/{t['id']}/steps/5", json={"done": True}).status_code == 409
    assert [s["done"] for s in get(sam, t["id"])["steps"]] == [False, False]
    assert kim.patch(f"/list/{t['id']}/steps/0", json={"done": True}).status_code == 404  # not hers
    assert sam.patch("/list/t1/steps/0", json={"done": True}).status_code == 400          # linked: no steps
    sam.post(f"/list/{t['id']}/done")
    assert sam.patch(f"/list/{t['id']}/steps/0", json={"done": True}).status_code == 409  # finished


def test_only_a_new_step_clears_the_slides(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    t = add(sam, "Clean garage today")
    sam.patch(f"/list/{t['id']}", json={"steps": ["Clear the bench", "Sweep"]})
    slides = Slides(a.db, "sam")
    for _ in range(3):
        slides.bump(t["id"])
    sam.patch(f"/list/{t['id']}", json={"steps": [{"text": "Sweep", "done": True}, {"text": "Clear the bench"}]})
    assert slides.counts()[t["id"]] == 3                    # ticked and reordered: still a slug
    sam.patch(f"/list/{t['id']}", json={"steps": ["Sweep"]})
    assert slides.counts()[t["id"]] == 3                    # one taken off: still a slug
    sam.patch(f"/list/{t['id']}", json={"steps": ["Sweep", "Find the broom"]})
    assert t["id"] not in slides.counts()                   # made smaller: a fresh start


# --- 2. one-tap moves ------------------------------------------------------------------------------
def test_tomorrow_in_one_tap_counts_a_slide_and_undo_takes_it_back(ctx):
    ctx, a, login = site(ctx, today=lambda: FRI)
    sam = login()
    t = add(sam, "Call the dentist today 9am")
    r = sam.post(f"/list/{t['id']}/move", json={"to": "tomorrow"}).json()
    assert (r["moved"], r["from"], r["to"], r["slid"]) == (True, "2026-10-16", "2026-10-17", 1)
    assert r["task"]["due"]["date"] == "2026-10-17T09:00:00"  # the time stays
    assert r["undo"] == {"to": "2026-10-16", "undo": True}
    assert Slides(a.db, "sam").counts()[t["id"]] == 1
    again = sam.post(f"/list/{t['id']}/move", json={"to": "tomorrow"}).json()
    assert again["moved"] is False and Slides(a.db, "sam").counts()[t["id"]] == 1  # a double tap counts once
    back = sam.post(f"/list/{t['id']}/move", json=r["undo"]).json()
    assert back["to"] == "2026-10-16" and back["slid"] is None
    assert get(sam, t["id"])["due"]["date"] == "2026-10-16T09:00:00"
    assert t["id"] not in Slides(a.db, "sam").counts()


def test_moves_to_a_day_and_off_the_calendar(ctx):
    ctx, a, login = site(ctx, today=lambda: FRI)
    sam = login()
    someday = add(sam, "Read a book")
    r = sam.post(f"/list/{someday['id']}/move", json={"to": "2026-10-19"}).json()
    assert r["from"] is None and r["to"] == "2026-10-19" and r["slid"] is None  # scheduling isn't sliding
    assert sam.post(f"/list/{someday['id']}/move", json=r["undo"]).json()["to"] is None
    assert get(sam, someday["id"])["due"] is None
    earlier = add(sam, "Plan trip 2026-10-25")
    assert sam.post(f"/list/{earlier['id']}/move", json={"to": "today"}).json()["slid"] is None  # sooner: no slide
    assert earlier["id"] not in Slides(a.db, "sam").counts()
    # A weekday repeat never lands on a weekend: Friday's "tomorrow" is Monday.
    meds = add(sam, "Stretch every weekday")
    assert sam.post(f"/list/{meds['id']}/move", json={"to": "tomorrow"}).json()["to"] == "2026-10-19"
    assert sam.post(f"/list/{meds['id']}/move", json={"to": "2026-02-30"}).status_code == 400
    assert sam.post(f"/list/{meds['id']}/move", json={"to": "soon"}).status_code == 400
    done = add(sam, "Done thing today")
    sam.post(f"/list/{done['id']}/done")
    assert sam.post(f"/list/{done['id']}/move", json={"to": "tomorrow"}).status_code == 400
    kim = login("kim", "kim password")
    assert kim.post(f"/list/{meds['id']}/move", json={"to": "tomorrow"}).status_code == 404


def test_a_linked_task_moves_in_its_own_app(ctx):
    app_tasks = MovingTodoist([{"id": "t1", "content": "Linked thing", "priority": 1, "due": {"date": "2026-10-16"}}])
    ctx, a, login = site(ctx, linked=app_tasks, today=lambda: FRI)
    sam = login()
    r = sam.post("/list/t1/move", json={"to": "tomorrow"}).json()
    assert ("due_date", "t1", "2026-10-17") in app_tasks.calls and r["slid"] == 1 and r["task"] is None
    sam.post("/list/t1/move", json=r["undo"])
    assert ("due_date", "t1", "2026-10-16") in app_tasks.calls and "t1" not in Slides(a.db, "sam").counts()
    assert sam.post("/list/t1/move", json={"to": None}).status_code == 400  # a date comes off in the app
    assert not any(r for r in rows(ctx.settings.data_dir, "tasks"))           # nothing copied here


def test_a_linked_app_that_refuses_a_move_says_why(ctx):
    class Stubborn(MovingTodoist):
        def set_due_date(self, task_id, date_iso):
            raise ProviderError("Can't move one day of a repeating task in this app. Skip it, "
                                "or move it in your to-do app.")
    ctx, a, login = site(ctx, linked=Stubborn([{"id": "r1", "content": "Bins", "priority": 1,
                                                 "due": {"date": "2026-10-16", "is_recurring": True}}]),
                         today=lambda: FRI)
    sam = login()
    r = sam.post("/list/r1/move", json={"to": "tomorrow"})
    assert r.status_code == 502 and "repeating task" in r.json()["detail"]
    assert "r1" not in Slides(a.db, "sam").counts()  # it didn't move, so it didn't slide


# --- 4. Brain dump preview -------------------------------------------------------------------------
def test_preview_says_what_brain_dump_understood_and_writes_nothing(ctx):
    ctx, a, login = site(ctx, today=lambda: FRI)
    sam = login()
    before = {t: rows(ctx.settings.data_dir, t) for t in ("tasks", "tags", "server_settings", "events")}
    text = ("Call mom tomorrow 3pm #call ~15m\nfri\n[x] Gym\nPay rent 2026-02-30\n#errand\n"
            "Water plants every day\n\nBuy milk #newtag")
    r = sam.post("/list/dump?preview=1", json={"text": text})
    assert r.status_code == 200, r.text
    p = r.json()
    lines = {x["line"]: x for x in p["lines"]}
    assert (lines[1]["content"], lines[1]["due_date"], lines[1]["due_time"], lines[1]["minutes"], lines[1]["tags"]) == \
        ("Call mom", "2026-10-17", "15:00", 15, ["call"])
    assert lines[3]["ticked"] is True and lines[4]["problem"] == "2026-02-30 isn't a real date"
    assert lines[6]["repeat"] == "daily" and lines[8]["text"] == "Buy milk #newtag"
    assert p["dropped"] == [{"line": 2, "text": "fri"}, {"line": 5, "text": "#errand"}]
    assert p["to"] == "My list" and p["new_tags"] == ["call", "newtag"] and p["more"] == 0
    after = {t: rows(ctx.settings.data_dir, t) for t in ("tasks", "tags", "server_settings", "events")}
    assert after == before  # not even the starter tags
    assert sam.post("/list/dump?preview=1", json={"text": "\n".join(f"T{i}" for i in range(203))}).json()["more"] == 3


def test_preview_keeps_line_numbers_as_typed():
    p = preview_dump("a\n   \nb #x\n[x]", FRI)
    assert [x["line"] for x in p["lines"]] == [1, 3] and p["dropped"] == [{"line": 4, "text": "[x]"}]


def test_preview_names_where_tasks_go_and_the_tag_limit(ctx):
    class Todo(FakeTodoist):
        key = "todoist"
    app_tasks = Todo([])
    ctx, _, login = site(ctx, linked=app_tasks, today=lambda: FRI)
    sam = login()
    sam.get("/tags")  # the 5 starter tags
    for i in range(25):
        assert sam.post("/tags", json={"name": f"t{i}", "color": "white"}).status_code == 200
    p = sam.post("/list/dump?preview=1", json={"text": "Buy milk #t3 #fresh"}).json()
    assert p["new_tags"] == [] and p["over_tags"] == ["fresh"] and p["to"] == "My list" and p["notes"] == []
    assert sam.patch("/settings", json={"new_tasks_to": "linked"}).status_code == 200
    p = sam.post("/list/dump?preview=1", json={"text": "Buy milk #t3"}).json()
    assert p["to"] == "Todoist" and "tags" in p["notes"][0]
    assert not [c for c in app_tasks.calls if c[0] == "add"]  # a preview adds nothing anywhere


# --- 6. slugs on My list ---------------------------------------------------------------------------
def test_the_list_says_which_tasks_keep_sliding(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    garage, desk, gone = add(sam, "Clean garage today"), add(sam, "Tidy desk today"), add(sam, "Old thing today")
    slides = Slides(a.db, "sam")
    for _ in range(3):
        slides.bump(garage["id"])
        slides.bump(gone["id"])
    slides.bump(desk["id"])
    sam.post(f"/list/{gone['id']}/done")  # finished: no longer a slug (and the count is cleared)
    r = sam.get("/list").json()
    assert r["slides"] == {garage["id"]: 3} and r["slug_after"] == 3
    assert sam.patch("/settings", json={"slug_after": 2}).status_code == 200
    slides.bump(desk["id"])
    assert sam.get("/list").json()["slides"] == {garage["id"]: 3, desk["id"]: 2}
    sam.delete(f"/journal/slugs/{garage['id']}")  # "Not a slug"
    assert sam.get("/list").json()["slides"] == {desk["id"]: 2}


# --- 7. skip this time -----------------------------------------------------------------------------
def test_skip_this_time_moves_a_repeat_on_without_a_win(ctx):
    ctx, a, login = site(ctx, today=lambda: FRI)
    sam = login()
    meds = add(sam, "Take meds every day 8am")
    sam.patch(f"/list/{meds['id']}", json={"steps": [{"text": "water", "done": True}]})
    r = sam.post(f"/list/{meds['id']}/skip", json={"due": "2026-10-16"}).json()
    assert r["undo"] == {"prev_due": "2026-10-16", "prev_steps": [True], "next_due": "2026-10-17"}
    assert r["task"]["due"]["date"] == "2026-10-17T08:00:00" and r["task"]["steps"][0]["done"] is False
    assert wins_today(sam) == 0 and meds["id"] not in Slides(a.db, "sam").counts()  # not a win, not a slide
    twice = sam.post(f"/list/{meds['id']}/skip", json={"due": "2026-10-16"}).json()  # a double tap
    assert twice["undo"] is None and twice["task"]["due"]["date"].startswith("2026-10-17")
    back = sam.post(f"/list/{meds['id']}/unskip", json=r["undo"]).json()
    assert back["due"]["date"] == "2026-10-16T08:00:00" and back["steps"][0]["done"] is True
    assert sam.post(f"/list/{meds['id']}/unskip", json={}).status_code == 400
    once = add(sam, "Not a repeat today")
    assert sam.post(f"/list/{once['id']}/skip").status_code == 400
    assert sam.post("/list/t1/skip").status_code == 400  # a linked app's repeat: skip it there


def test_skipping_a_weekly_and_a_monthly_repeat(ctx):
    ctx, a, login = site(ctx, today=lambda: FRI)
    sam = login()
    bins = add(sam, "Bins every week")
    assert sam.post(f"/list/{bins['id']}/skip").json()["task"]["due"]["date"] == "2026-10-23"
    rent = add(sam, "Rent 2026-10-31 every month")
    assert sam.post(f"/list/{rent['id']}/skip").json()["task"]["due"]["date"] == "2026-11-30"
    assert sam.post(f"/list/{rent['id']}/skip").json()["task"]["due"]["date"] == "2026-12-31"  # stays on its day
