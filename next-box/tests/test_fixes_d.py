"""Batch D fixes: My list, Brain dump and task editing."""
import dataclasses
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import TODAY, FakeTodoist, rows
from nextbox import jobs
from nextbox.auth import Accounts
from nextbox.mylist import LocalTasks, Merged, Tags, next_due, parse_dump, parse_line
from nextbox.server import create_app

THU = date(2026, 10, 15)  # a Thursday
SAT = date(2026, 10, 17)
NOW = date.today()


def site(ctx, linked=None):
    """sam and kim with My list (sam also has `linked`, a fake to-do app, when given). Today is NOW,
    the real day, as wins and the journal use it."""
    a = Accounts(ctx.settings.data_dir)
    factory = (lambda: Merged(LocalTasks(a.db, "sam"), linked)) if linked else None
    ctx = dataclasses.replace(ctx, tasks_factory=factory, today=date.today)
    ctx.settings.todoist_token = ""
    a.set_password("sam", "sam password", create=True)
    a.create("kim", "kim password")
    app = create_app(ctx)

    def login(name="sam", pw="sam password"):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, login


def mine(c, content):
    return next(t for t in c.get("/list?include_done=true").json()["tasks"] if t["content"] == content)


def wins_today(c):
    return next(d for d in c.get("/journal/wins").json()["series"] if d["is_today"])["count"]


# --- D-01 all or nothing ----------------------------------------------------------------------------
def test_a_bad_line_stops_the_whole_dump_and_says_which(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    text = "Buy milk\nCall bob #newtag\nFix tap\nPay rent 2026-02-30\nWalk dog"
    r = sam.post("/list/dump", json={"text": text})
    assert r.status_code == 400
    body = r.json()
    assert body["problems"] == [{"line": 4, "text": "Pay rent 2026-02-30", "problem": "2026-02-30 isn't a real date"}]
    assert "Line 4" in body["detail"] and "Nothing was added" in body["detail"]
    assert sam.get("/list").json()["tasks"] == []
    assert "newtag" not in {t["name"] for t in sam.get("/tags").json()["tags"]}  # no half-made tags either
    added = sam.post("/list/dump", json={"text": text.replace("02-30", "02-27")}).json()["added"]
    assert [t["content"] for t in added] == ["Buy milk", "Call bob", "Fix tap", "Pay rent", "Walk dog"]
    assert len(sam.get("/list").json()["tasks"]) == 5  # the retry made no duplicates


def test_a_full_list_refuses_the_whole_dump(tmp_path):
    tasks = LocalTasks(tmp_path, "sam", today=lambda: TODAY)
    tasks.create_many([{"content": f"t{i}"} for i in range(1998)])
    with pytest.raises(ValueError, match="2,000"):
        tasks.create_many([{"content": "a"}, {"content": "b"}, {"content": "c"}])
    assert len(tasks.all_open()) == 1998


def test_unreal_times_are_problems_too():
    assert parse_line("Call at 13pm", THU)["problem"] == "at 13pm isn't a real time"
    assert parse_line("Call at 13pm", THU)["content"] == "Call at 13pm"  # left as typed
    assert "problem" not in parse_line("Buy 2-3 apples", THU)


# --- D-02 / D-15 / D-16 repeats ---------------------------------------------------------------------
def test_ticking_a_repeat_early_moves_it_on():
    assert next_due("weekly", THU + timedelta(days=1), THU) == THU + timedelta(days=8)   # +7 from its day
    assert next_due("daily", THU + timedelta(days=1), THU) == THU + timedelta(days=2)
    assert next_due("monthly", date(2026, 10, 20), THU) == date(2026, 11, 20)
    assert next_due("weekdays", date(2026, 10, 16), THU) == date(2026, 10, 19)            # fri → mon
    # Overdue ones still jump past today, as before.
    assert next_due("weekly", date(2026, 9, 16), TODAY) == date(2026, 9, 30)
    assert next_due("daily", date(2026, 9, 20), TODAY) == date(2026, 9, 29)


def test_done_on_a_repeat_due_tomorrow_moves_it_and_counts_once(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Vitamins every day tomorrow\nTrash out every week tomorrow"})
    vit, trash = mine(sam, "Vitamins"), mine(sam, "Trash out")
    tomorrow = (NOW + timedelta(days=1)).isoformat()
    assert vit["due"]["date"] == trash["due"]["date"] == tomorrow
    sam.post(f"/list/{vit['id']}/done")
    sam.post(f"/list/{trash['id']}/done")
    assert mine(sam, "Vitamins")["due"]["date"] == (NOW + timedelta(days=2)).isoformat()
    assert mine(sam, "Trash out")["due"]["date"] == (NOW + timedelta(days=8)).isoformat()
    assert wins_today(sam) == 2


def test_monthly_on_the_31st_stays_on_the_31st(tmp_path):
    day = {"d": date(2027, 1, 31)}
    tasks = LocalTasks(tmp_path, "sam", today=lambda: day["d"])
    t = tasks.create({"content": "Pay rent", "due_date": "2027-01-31", "repeat": "monthly"})
    seen = []
    for _ in range(3):
        day["d"] = date.fromisoformat(tasks.get_task(t["id"])["due"]["date"])
        tasks.close_task(t["id"])
        seen.append(tasks.get_task(t["id"])["due"]["date"])
    assert seen == ["2027-02-28", "2027-03-31", "2027-04-30"]
    # Moving it by hand to the 15th makes the 15th its day.
    tasks.update(t["id"], {"due_date": "2027-05-15"})
    day["d"] = date(2027, 5, 15)
    tasks.close_task(t["id"])
    assert tasks.get_task(t["id"])["due"]["date"] == "2027-06-15"


def test_a_weekday_repeat_never_starts_on_a_weekend(tmp_path):
    p = parse_line("Standup every weekday 9am", SAT)
    assert (p["repeat"], p["due_date"], p["due_time"]) == ("weekdays", "2026-10-19", "09:00")
    tasks = LocalTasks(tmp_path, "sam", today=lambda: SAT)
    t = tasks.create({"content": "Standup", "repeat": "weekdays"})
    assert t["due"]["date"] == "2026-10-19"
    t = tasks.update(t["id"], {"due_date": "2026-10-24"})  # a saturday
    assert t["due"]["date"] == "2026-10-26"


# --- D-03 / D-04 / D-12 / D-13 the parser ------------------------------------------------------------
@pytest.mark.parametrize("line", ["Buy sun hat", "Fix the chair I sat on", "Pick up wed dress", "Read Sun Also Rises",
                                  "Tell Sam today was great", "Buy tickets for sat and sun",
                                  "Move the meeting from monday to friday", "Write the weekly report"])
def test_words_that_only_look_like_dates_stay_words(line):
    p = parse_line(line, THU)
    assert p["content"] == line and "due_date" not in p and "repeat" not in p


@pytest.mark.parametrize("line, content, due, time", [
    ("Call mom tomorrow, 3pm", "Call mom", "2026-10-16", "15:00"),
    ("Call mom tomorrow.", "Call mom", "2026-10-16", None),
    ("Call mom fri.", "Call mom", "2026-10-16", None),
    ("Call mom fri,", "Call mom", "2026-10-16", None),
    ("Call mom 3pm.", "Call mom", "2026-10-15", "15:00"),
    ("Dentist (3pm)", "Dentist", "2026-10-15", "15:00"),
    ("Call mom on sat", "Call mom", "2026-10-17", None),
    ("Gym sat 9am with Bob", "Gym with Bob", "2026-10-17", "09:00"),
    ("Call mom next fri", "Call mom", "2026-10-23", None),
    ("Pick up kids at 3:30p", "Pick up kids", "2026-10-15", "15:30"),
    ("Gym 3p", "Gym", "2026-10-15", "15:00"),
    ("Pay rent by 2026-11-01", "Pay rent", "2026-11-01", None),
    ("Do this today", "Do this", "2026-10-15", None),
])
def test_dates_and_times_with_punctuation(line, content, due, time):
    p = parse_line(line, THU)
    assert (p["content"], p.get("due_date"), p.get("due_time")) == (content, due, time)


def test_more_ways_to_write_durations_repeats_and_lists():
    assert [parse_line(f"Read {d}", THU)["minutes"] for d in ("~2hrs", "~30mins", "~1.5h", "~2 hours", "~1h30m")] == \
        [120, 30, 90, 120, 90]
    meeting = parse_line("Meeting 9am-10am", THU)
    assert (meeting["content"], meeting["due_time"], meeting["minutes"]) == ("Meeting", "09:00", 60)
    assert parse_line("Meeting 11-1pm", THU)["due_time"] == "11:00"
    assert parse_line("Water plants daily", THU)["repeat"] == "daily"
    gym = parse_line("Gym every monday", THU)
    assert (gym["content"], gym["repeat"], gym["due_date"]) == ("Gym", "weekly", "2026-10-19")
    flu = parse_line("- [ ] Book flu shot every month", THU)
    assert (flu["content"], flu["repeat"]) == ("Book flu shot", "monthly")
    assert parse_line("[x] Gym", THU)["ticked"] is True
    assert parse_line("Pay !4 low", THU)["priority"] == 1
    assert parse_line("2.5 kg flour", THU)["content"] == "2.5 kg flour"  # not a numbered list


def test_tags_keep_their_letters_and_numbers_stay_words():
    assert parse_line("Café ☕ #café", THU)["tags"] == ["café"]
    assert parse_line("Email #c++ team", THU) == {"content": "Email #c++ team", "tags": [], "priority": 1}
    assert parse_line("Fix bug #123", THU)["content"] == "Fix bug #123"
    assert parse_line("Home #home,", THU)["tags"] == ["home"]
    assert "too long for a tag" in parse_line("Tag #" + "a" * 30, THU)["problem"]


def test_past_30_tags_the_word_stays_in_the_task(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    tags = Tags(a.db, "sam")
    for i in range(30 - len(tags.all())):
        tags.set(f"t{i}", "white")
    r = sam.post("/list/dump", json={"text": "Buy paint #diy\nWalk #home"}).json()
    assert [(t["content"], t["labels"]) for t in r["added"]] == [("Buy paint #diy", []), ("Walk", ["home"])]
    assert "#diy" in r["notes"][0]


# --- D-06 New tasks go to: my linked app ----------------------------------------------------------
class DatedTodo(FakeTodoist):
    key = "todoist"

    def add_task(self, content, due_string=None, description=""):
        task = super().add_task(content, due_string=due_string, description=description)
        self.calls.append(("due", content, due_string))
        return task


class PlainApp(FakeTodoist):
    key = "caldav"

    def add_task(self, content, description=""):
        return super().add_task(content, description=description)


def test_brain_dump_goes_to_the_linked_app_when_asked(ctx):
    todo = DatedTodo([])
    ctx, a, login = site(ctx, linked=todo)
    sam = login()
    assert sam.patch("/settings", json={"new_tasks_to": "linked"}).status_code == 200
    r = sam.post("/list/dump", json={"text": "Call mom tomorrow 3pm #call\nGym every week\nBuy milk"}).json()
    assert r["to"] == "Todoist" and len(r["added"]) == 3
    tomorrow = (NOW + timedelta(days=1)).isoformat()
    assert [c for c in todo.calls if c[0] == "due"] == [
        ("due", "Call mom", f"{tomorrow} 15:00"), ("due", "Gym", f"every week starting {NOW.isoformat()}"),
        ("due", "Buy milk", None)]
    assert "tags" in r["notes"][0]
    assert rows(ctx.settings.data_dir, "tasks") == []  # never kept here


def test_apps_without_dates_get_each_line_as_typed(ctx):
    app = PlainApp([])
    ctx, a, login = site(ctx, linked=app)
    sam = login()
    sam.patch("/settings", json={"new_tasks_to": "linked"})
    sam.post("/list/dump", json={"text": "- Call mom tomorrow 3pm"})
    assert ("add", "Call mom tomorrow 3pm") in app.calls
    assert rows(ctx.settings.data_dir, "tasks") == []


# --- D-07 order -----------------------------------------------------------------------------------
def test_the_list_comes_in_ticket_order(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    d = lambda n: (NOW + timedelta(days=n)).isoformat()  # noqa: E731
    sam.post("/list/dump", json={"text": "\n".join([
        f"Far away {d(9)}", "No time today", "Meeting today 1pm", "Early call today 9am", f"Soon {d(2)} 8am",
        f"Soon earlier {d(2)} 7am", f"Next day {d(1)}", "Someday thing", "Urgent someday !"])})
    names = [t["content"] for t in sam.get("/list").json()["tasks"]]
    assert names == ["Early call", "Meeting", "No time", "Next day", "Soon earlier", "Soon", "Far away",
                     "Urgent someday", "Someday thing"]


# --- D-08 undo and double taps --------------------------------------------------------------------
def test_done_can_be_undone_and_a_double_tap_counts_once(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Water plants today every day\nPay bill today"})
    water, bill = mine(sam, "Water plants"), mine(sam, "Pay bill")
    sam.patch(f"/list/{water['id']}", json={"steps": [{"text": "fill can", "done": True}]})
    r = sam.post(f"/list/{water['id']}/done").json()
    assert r["undo"] == {"prev_due": NOW.isoformat(), "prev_steps": [True]}
    assert mine(sam, "Water plants")["due"]["date"] == (NOW + timedelta(days=1)).isoformat()
    sam.post(f"/list/{water['id']}/reopen", json=r["undo"])
    back = mine(sam, "Water plants")
    assert back["due"]["date"] == NOW.isoformat() and back["steps"][0]["done"] is True
    first = sam.post(f"/list/{bill['id']}/done").json()
    again = sam.post(f"/list/{bill['id']}/done").json()
    assert first["undo"] is not None and again["undo"] is None
    assert wins_today(sam) == 1
    assert sam.post(f"/list/{bill['id']}/reopen").status_code == 200
    assert wins_today(sam) == 0 and mine(sam, "Pay bill")["done"] is False


def test_a_linked_task_can_be_undone_where_the_app_allows_it(ctx):
    todo = FakeTodoist([{"id": "t1", "content": "Linked thing", "priority": 1, "due": {"date": NOW.isoformat()}}])
    ctx, a, login = site(ctx, linked=todo)
    sam = login()
    assert sam.post("/list/t1/done").json()["undo"] is None       # FakeTodoist can't reopen
    assert sam.post("/list/t1/reopen").status_code == 400
    todo.reopen_task = lambda tid: todo.calls.append(("reopen", tid))
    sam.post("/list/t1/reopen")  # (the win from above is taken back too)
    assert sam.post("/list/t1/done").json()["undo"] == {}
    assert wins_today(sam) == 1
    assert sam.post("/list/t1/reopen", json={}).status_code == 200
    assert ("reopen", "t1") in todo.calls and wins_today(sam) == 0


# --- D-09 / D-10 / D-11 / D-19 / D-24 editing ------------------------------------------------------
def test_notes_partial_edits_and_done_times(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    t = sam.post("/list/dump", json={"text": "Call vet !3 #call"}).json()["added"][0]
    assert t["priority"] == 2
    t = sam.patch(f"/list/{t['id']}", json={"description": "555-0100", "minutes": 5}).json()
    assert (t["description"], t["priority"], t["labels"], t["minutes"]) == ("555-0100", 2, ["call"], 5)
    t = sam.patch(f"/list/{t['id']}", json={"due_time": "15:30", "repeat": "daily"}).json()
    assert (t["due"]["date"], t["repeat"]) == (f"{NOW.isoformat()}T15:30:00", "daily")  # a time needs a day
    sam.post(f"/list/{t['id']}/reopen")
    one = sam.post("/list/dump", json={"text": "Read a book"}).json()["added"][0]
    assert one["done_at"] is None
    sam.post(f"/list/{one['id']}/done")
    assert isinstance(mine(sam, "Read a book")["done_at"], int)


def test_the_urgent_tag_means_urgent(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    assert parse_line("Pay #urgent", THU)["priority"] == 3
    t = sam.post("/list/dump", json={"text": "Pay bill"}).json()["added"][0]
    assert sam.patch(f"/list/{t['id']}", json={"tags": ["urgent"]}).json()["priority"] == 3
    assert sam.patch(f"/list/{t['id']}", json={"priority": 4}).json()["priority"] == 4


def test_removing_a_tag_can_take_it_off_tasks(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Call mom #call #home\nCall dad #call"})
    assert {t["name"]: t["uses"] for t in sam.get("/tags").json()["tags"]}["call"] == 2
    assert sam.delete("/tags/call?strip=true").json()["tasks_changed"] == 2
    assert mine(sam, "Call mom")["labels"] == ["home"] and mine(sam, "Call dad")["labels"] == []
    sam.post("/list/dump", json={"text": "Walk #home"})
    sam.delete("/tags/home")  # without strip, tasks keep the word (as before)
    assert mine(sam, "Walk")["labels"] == ["home"]


# --- D-14 nothing typed is lost -------------------------------------------------------------------
def test_long_dumps_and_long_lines_lose_nothing(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    text = "\n".join(f"Task {i}" for i in range(1, 206))
    r = sam.post("/list/dump", json={"text": text}).json()
    assert len(r["added"]) == 200 and r["skipped_text"].split("\n") == [f"Task {i}" for i in range(201, 206)]
    long = "Read " + "word " * 150
    t = sam.post("/list/dump", json={"text": long}).json()["added"][0]
    assert 490 < len(t["content"]) <= 500 and t["description"] == long.strip()


def test_ticked_lines_are_left_out(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    r = sam.post("/list/dump", json={"text": "[x] Gym\n- [ ] Read"}).json()
    assert [t["content"] for t in r["added"]] == ["Read"] and r["left_out"] == [{"line": 1, "text": "Gym"}]
    assert sam.post("/list/dump", json={"text": "[x] Gym"}).status_code == 400
    assert len(parse_dump("a\n\nb", THU)) == 2


# --- D-18 / D-26 Just one thing needs to know what was done today ---------------------------------
def test_the_list_says_how_many_were_done_today(ctx):
    ctx, a, login = site(ctx)
    sam = login()
    assert sam.get("/list").json()["done_today"] == 0
    t = sam.post("/list/dump", json={"text": "Read today"}).json()["added"][0]
    sam.post(f"/list/{t['id']}/done")
    assert sam.get("/list").json()["done_today"] == 1


# --- D-20 the 24-hour setting on the ticket footer ------------------------------------------------
def test_the_ticket_footer_follows_24_hour_times(ctx, monkeypatch):
    seen = {}
    real = jobs.render_day
    monkeypatch.setattr(jobs, "render_day", lambda *a, **k: seen.update(footer=k["footer"]) or real(*a, **k))
    jobs.print_today(dataclasses.replace(ctx, prefs={**ctx.prefs, "time_24h": True}))
    assert seen["footer"][-1:].isdigit()        # "made Thu 23:09"
    jobs.print_today(ctx)
    assert seen["footer"][-1:] in ("a", "p")     # "made Thu 11:09p"
