"""Batch F fixes: Snap, read-back, the journal and wins."""
import dataclasses
import threading
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert

from conftest import FakeTodoist, rows, tiny_jpeg
from nextbox import db, dbtools, jobs, scan, upkeep
from nextbox.auth import Accounts
from nextbox.journal import Journal, Slides, Wins
from nextbox.mylist import LocalTasks, Merged
from nextbox.scan import ReadBack, RowMark
from nextbox.server import create_app
from nextbox.store import normalize_id

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)


def site(ctx, linked=None):
    """boss (superadmin) and sam. Sam has My list, plus `linked` (a FakeTodoist) as a linked app.
    `seen` is what the fake camera reads from the next photo; seen["fail"] makes reading fail."""
    a = Accounts(ctx.settings.data_dir)
    factory = (lambda: Merged(LocalTasks(a.db, "sam", today=date.today), linked)) if linked else None
    ctx = dataclasses.replace(ctx, tasks_factory=factory, today=date.today)
    ctx.settings.todoist_token = ""
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    seen: dict = {}

    def vision(image, media_type, manifest):
        if seen.get("fail"):
            raise RuntimeError("vision service down")
        return ReadBack(label_code=seen.get("code"), rows=[RowMark(**r) for r in seen.get("rows", [])],
                        notes=seen.get("notes", ""), win=seen.get("win", ""))
    app = create_app(ctx, vision=vision)

    def login(name="sam", pw="sam password"):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, login, seen


def snap(c, label_id=None, photo=None):
    return c.post("/scan", data={"label_id": label_id} if label_id else {},
                  files={"photo": ("p.jpg", photo or tiny_jpeg(), "image/jpeg")}).json()


def row_of(label, content):
    return next(m["row"] for m in label["manifest"] if m["content"] == content)


def task(c, content):
    return next(t for t in c.get("/list?include_done=true").json()["tasks"] if t["content"] == content)


def day_card(c, day):
    return next((d for d in c.get("/journal").json()["days"] if d["day"] == day.isoformat()), None)


def page_count(ctx):
    return len(rows(ctx.settings.data_dir, "journal"))


# --- F-01 reading the same ticket again -----------------------------------------------------------
def test_reading_the_same_ticket_again_changes_nothing_twice(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Buy milk today\nWater plants today\nEmail landlord today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    seen.update(rows=[{"row": row_of(label, "Buy milk"), "mark": "done", "confidence": 0.95},
                      {"row": row_of(label, "Water plants"), "mark": "done", "confidence": 0.95},
                      {"row": row_of(label, "Email landlord"), "mark": "tomorrow", "confidence": 0.6}],
                notes="Lunch notes")
    first = snap(sam, label["id"])
    assert len(first["applied"]) == 2 and len(first["needs_confirmation"]) == 1
    seen["notes"] = "Lunch notes\nEvening: good day."
    again = snap(sam, label["id"])
    assert again["applied"] == [] and again["needs_confirmation"] == []
    assert {i["reason"] for i in again["ignored"]} == {scan.READ_BEFORE}
    assert again["previous_scan"]["id"] == first["id"]
    # Wins counted once, one page for the ticket (with the newer notes), one mark still waiting.
    card = day_card(sam, TODAY)
    assert card["wins"]["count"] == 2 and sorted(card["wins"]["items"]) == ["Buy milk", "Water plants"]
    assert len(card["pages"]) == 1 and card["pages"][0]["notes"] == "Lunch notes\nEvening: good day."
    assert sam.get("/list").json()["marks_waiting"] == 1
    assert sam.post(f"/scan/{first['id']}/confirm", json={"decisions": {"3": "confirm"}}).status_code == 200
    assert Slides(a.db, "sam").counts().get(task(sam, "Email landlord")["id"]) == 1


def test_a_new_mark_on_a_ticket_read_before_is_still_applied(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Buy milk today\nWater plants today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    seen.update(rows=[{"row": row_of(label, "Buy milk"), "mark": "done", "confidence": 0.95}])
    snap(sam, label["id"])
    seen["rows"] = seen["rows"] + [{"row": row_of(label, "Water plants"), "mark": "done", "confidence": 0.95}]
    later = snap(sam, label["id"])
    assert [x["content"] for x in later["applied"]] == ["Water plants"]
    assert day_card(sam, TODAY)["wins"]["count"] == 2


# --- F-02 ticked in the app and on paper -------------------------------------------------------------
def test_a_task_ticked_in_the_app_and_on_paper_counts_once(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Call the dentist today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    t = task(sam, "Call the dentist")
    sam.post(f"/list/{t['id']}/done")
    done_at = task(sam, "Call the dentist")
    seen.update(rows=[{"row": 1, "mark": "done", "confidence": 0.95}])
    rec = snap(sam, label["id"])
    assert rec["applied"] == [] and rec["ignored"][0]["reason"] == scan.ALREADY_DONE
    assert day_card(sam, TODAY)["wins"]["count"] == 1
    assert len([e for e in rows(ctx.settings.data_dir, "events") if e["action"] == "task_done"]) == 1
    assert task(sam, "Call the dentist") == done_at  # done time not overwritten


def test_closing_a_finished_task_does_nothing(tmp_path):
    mine = LocalTasks(tmp_path, "sam")
    t = mine.create({"content": "Pay rent", "due_date": TODAY.isoformat()})
    assert mine.close_task(t["id"]) is not None
    assert mine.close_task(t["id"]) is None  # a double tap
    assert len(Wins(mine.db, "sam").between("2000-01-01", "2999-01-01")) == 1


def test_linked_tasks_already_done_or_moved_on_are_left_alone(ctx, todo):
    label = jobs.print_today(ctx)
    todo.tasks["1"]["checked"] = True                     # finished in Todoist already
    todo.tasks["2"]["due"] = {"date": "2026-09-29", "is_recurring": True}  # the repeat moved on
    todo.tasks["3"]["due"] = {"date": "2026-09-30"}       # already moved past tomorrow
    marks = [{"row": row_of(label, c), "mark": m, "confidence": 0.95}
             for c, m in (("Call the dentist", "done"), ("Water plants", "done"), ("Renew passport", "tomorrow"))]
    rec = scan.scan_photo(ctx, lambda *a: ReadBack(label_code=label["id"], rows=[RowMark(**r) for r in marks]),
                          b"img", "image/jpeg")
    assert [c for c in todo.calls if c[0] != "filter"] == []
    assert sorted(i["reason"] for i in rec["ignored"]) == [scan.ALREADY_DONE, scan.ALREADY_DONE, scan.ALREADY_MOVED]


# --- F-03 snapped after midnight ---------------------------------------------------------------------
def test_yesterdays_ticket_snapped_after_midnight_counts_for_yesterday(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    y = YESTERDAY.isoformat()
    sam.post("/list/dump", json={"text": f"Call the dentist {y}\nWater the plants {y} every day\nPick up prescription {y}"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    rec = ctx.store.get_printed(label["id"])
    ctx.store.save_printed({**rec, "created_at": f"{y}T23:12:00"})  # printed last night
    seen.update(rows=[{"row": row_of(label, "Call the dentist"), "mark": "done", "confidence": 0.95},
                      {"row": row_of(label, "Water the plants"), "mark": "done", "confidence": 0.95},
                      {"row": row_of(label, "Pick up prescription"), "mark": "tomorrow", "confidence": 0.95}])
    rec = snap(sam, label["id"])
    assert rec["day"] == y and len(rec["applied"]) == 3
    card = day_card(sam, YESTERDAY)
    assert card["wins"]["count"] == 2 and len(card["pages"]) == 1
    assert day_card(sam, TODAY) is None
    # "→" means the day after the ticket's day (today), and today's watering isn't skipped.
    assert task(sam, "Pick up prescription")["due"]["date"] == TODAY.isoformat()
    assert task(sam, "Water the plants")["due"]["date"] == TODAY.isoformat()


# --- F-04 names, linked counts and reopening ---------------------------------------------------------
def test_repeats_are_named_reopening_takes_the_win_back_and_linked_wins_say_so(ctx):
    todo = FakeTodoist([{"id": "t1", "content": "Linked thing", "priority": 1, "due": {"date": TODAY.isoformat()}}])
    ctx, a, login, seen = site(ctx, linked=todo)
    sam = login()
    sam.post("/list/dump", json={"text": "Water plants today every day\nWalk dog today"})
    sam.post(f"/list/{task(sam, 'Water plants')['id']}/done")
    walk = task(sam, "Walk dog")["id"]
    sam.post(f"/list/{walk}/done")
    sam.post(f"/list/{walk}/reopen")
    sam.post(f"/list/{walk}/done")
    sam.post("/list/t1/done")
    wins = day_card(sam, TODAY)["wins"]
    assert wins == {"count": 3, "items": ["Water plants", "Walk dog"], "linked": 1}
    sam.post(f"/list/{walk}/reopen")
    assert day_card(sam, TODAY)["wins"]["count"] == 2
    ev = [e for e in rows(ctx.settings.data_dir, "events") if e["action"] == "task_done"]
    assert {e["detail"]["task_id"] for e in ev} == {task(sam, "Water plants")["id"], walk, "t1"}
    assert all("content" not in e["detail"] for e in ev)  # admins read events: ids only


def test_a_linked_task_ticked_in_the_app_and_read_back_counts_once(ctx):
    todo = FakeTodoist([{"id": "t1", "content": "Linked thing", "priority": 1, "due": {"date": TODAY.isoformat()}}])
    ctx, a, login, seen = site(ctx, linked=todo)
    sam = login()
    label = sam.post("/print/today", json={"dry_run": True}).json()
    sam.post("/list/t1/done")  # FakeTodoist doesn't remember it's done, like an app that won't say
    seen.update(rows=[{"row": row_of(label, "Linked thing"), "mark": "done", "confidence": 0.95}])
    snap(sam, label["id"])
    assert day_card(sam, TODAY)["wins"]["count"] == 1


# --- F-05 marks waiting -------------------------------------------------------------------------------
def test_marks_waiting_for_you_can_be_found_again(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Email landlord today\nBuy milk today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    seen.update(rows=[{"row": row_of(label, "Buy milk"), "mark": "done", "confidence": 0.95}])
    snap(sam, label["id"])
    seen.update(rows=[{"row": row_of(label, "Email landlord"), "mark": "tomorrow", "confidence": 0.5}])
    held = snap(sam, label["id"])
    waiting = sam.get("/scans?pending=true").json()
    assert [r["id"] for r in waiting] == [held["id"]] and sam.get("/list").json()["marks_waiting"] == 1
    assert len(sam.get("/scans").json()) == 2
    sam.post(f"/scan/{held['id']}/confirm", json={"decisions": {str(row_of(label, 'Email landlord')): "skip"}})
    assert sam.get("/scans?pending=true").json() == [] and sam.get("/list").json()["marks_waiting"] == 0


# --- F-06 typed code vs the code on paper ---------------------------------------------------------------
def test_a_typed_code_that_disagrees_with_the_photo_holds_every_mark(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Alpha today\nBravo today"})
    old = sam.post("/print/today", json={"dry_run": True}).json()
    sam.post("/list/dump", json={"text": "Charlie today"})
    new = sam.post("/print/today", json={"dry_run": True}).json()
    seen.update(code=new["id"], rows=[{"row": row_of(new, "Charlie"), "mark": "done", "confidence": 0.95}])
    rec = snap(sam, old["id"])
    assert rec["label_conflict"] == {"typed": old["id"], "read": new["id"]} and rec["label_id"] == new["id"]
    assert rec["applied"] == [] and [p["status"] for p in rec["needs_confirmation"]] == ["pending"]
    assert not task(sam, "Charlie")["done"]
    picked = sam.post(f"/scan/{rec['id']}/label", json={"label_id": new["id"]}).json()
    assert [x["content"] for x in picked["applied"]] == ["Charlie"] and "label_conflict" not in picked
    assert task(sam, "Charlie")["done"]
    # Once marks are used, the photo can't be moved to another ticket.
    assert sam.post(f"/scan/{rec['id']}/label", json={"label_id": old["id"]}).status_code == 400


# --- F-07 undo ------------------------------------------------------------------------------------------
def test_marks_applied_by_the_ai_can_be_undone(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Buy milk today\nWater plants today every day\nEmail landlord today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    rows_ = {c: row_of(label, c) for c in ("Buy milk", "Water plants", "Email landlord")}
    seen.update(rows=[{"row": rows_["Buy milk"], "mark": "done", "confidence": 0.95},
                      {"row": rows_["Water plants"], "mark": "done", "confidence": 0.95},
                      {"row": rows_["Email landlord"], "mark": "tomorrow", "confidence": 0.95}])
    rec = snap(sam, label["id"])
    assert day_card(sam, TODAY)["wins"]["count"] == 2
    for name in rows_:
        r = sam.post(f"/scan/{rec['id']}/undo", json={"row": rows_[name]})
        assert r.status_code == 200, r.text
    again = sam.post(f"/scan/{rec['id']}/undo", json={"row": rows_["Buy milk"]})  # a double tap
    assert again.status_code == 200
    assert {e["status"] for e in again.json()["applied"]} == {"undone"}
    assert not task(sam, "Buy milk")["done"]
    assert task(sam, "Water plants")["due"]["date"] == TODAY.isoformat()
    assert task(sam, "Email landlord")["due"]["date"] == TODAY.isoformat()
    assert task(sam, "Email landlord")["id"] not in Slides(a.db, "sam").counts()
    card = day_card(sam, TODAY)
    assert card is None or card["wins"]["count"] == 0


def test_undo_on_a_linked_app(ctx, todo):
    label = jobs.print_today(ctx)
    marks = [{"row": row_of(label, "Call the dentist"), "mark": "done", "confidence": 0.95},
             {"row": row_of(label, "Water plants"), "mark": "done", "confidence": 0.95}]
    rec = scan.scan_photo(ctx, lambda *a: ReadBack(label_code=label["id"], rows=[RowMark(**r) for r in marks]),
                          b"img", "image/jpeg")
    with pytest.raises(scan.CantUndo, match="Reopen it in"):  # FakeTodoist can't reopen
        scan.undo(ctx, rec["id"], row_of(label, "Call the dentist"))
    assert ctx.store.get_scan(rec["id"])["applied"][0].get("status") is None  # still applied
    scan.undo(ctx, rec["id"], row_of(label, "Water plants"))  # a repeat: its day goes back
    assert ("due_date", "2", "2026-09-28") in todo.calls
    todo.reopen_task = lambda tid: todo.calls.append(("reopen", tid))
    scan.undo(ctx, rec["id"], row_of(label, "Call the dentist"))
    assert ("reopen", "1") in todo.calls


# --- F-08 the code can't be read ------------------------------------------------------------------------
@pytest.mark.parametrize("typed", ["261015MESR", "261015 mesr", "#261015-mesr", "261015–MESR"])
def test_ticket_codes_however_they_are_written(typed):
    assert normalize_id(typed) == "261015-MESR"


def test_an_unreadable_code_offers_the_tickets_and_keeps_one_page(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Buy milk today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    seen.update(rows=[{"row": 1, "mark": "done", "confidence": 0.95}], notes="Tired but ok")
    photo = tiny_jpeg((60, 40))
    for _ in range(3):  # trying again with the same photo
        rec = snap(sam, photo=photo)
    assert rec["errors"] == [scan.NO_CODE] and rec["candidates"][0]["id"] == label["id"]
    assert "label_id" not in str(rec["errors"]) and page_count(ctx) == 1
    picked = sam.post(f"/scan/{rec['id']}/label", json={"label_id": label["id"][-4:].lower()}).json()
    assert picked["applied"][0]["content"] == "Buy milk" and picked["errors"] == []
    pages = rows(ctx.settings.data_dir, "journal")
    assert len(pages) == 1 and pages[0]["label_id"] == label["id"] and pages[0]["notes"] == "Tired but ok"
    assert sam.post(f"/scan/{rec['id']}/label", json={"label_id": "ZZZZ"}).status_code == 400


def test_a_typed_suffix_or_code_without_a_dash_finds_the_ticket(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Buy milk today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    seen.update(rows=[{"row": 1, "mark": "done", "confidence": 0.95}])
    assert snap(sam, label["id"].replace("-", ""))["label_id"] == label["id"]
    seen.update(code=label["id"][-4:])  # the camera only caught the end of the code
    assert snap(sam)["label_id"] == label["id"]


# --- F-09 private photos aren't cached ------------------------------------------------------------------
def test_journal_photos_and_tickets_are_never_cached(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Buy milk today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    rec = snap(sam, label["id"])
    for path in (f"/journal/{rec['journal_id']}/photo", f"/printed/{label['id']}/png", f"/printed/{label['id']}/pdf"):
        r = sam.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store", path
    assert "Cookie" in sam.get(f"/journal/{rec['journal_id']}/photo").headers["vary"]
    assert sam.post("/auth/logout").headers["clear-site-data"] == '"cache"'


# --- F-10 iPhone photos -----------------------------------------------------------------------------------
def test_an_iphone_heic_photo_gets_a_helpful_answer(ctx, monkeypatch):
    ctx, a, login, seen = site(ctx)
    monkeypatch.setattr(scan, "heic_supported", lambda: False)
    r = login().post("/scan", files={"photo": ("IMG_1.HEIC", b"\0\0\0\x18ftypheic" + b"\0" * 64, "image/heic")})
    assert r.status_code == 415 and "Most Compatible" in r.json()["detail"]
    with pytest.raises(scan.PhotoError, match="HEIC"):
        scan.prepare_photo(b"\0\0\0\x18ftypheic" + b"\0" * 64)


# --- F-12 older days ------------------------------------------------------------------------------------
def test_older_days_skip_empty_weeks_and_say_when_there_is_no_more(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    long_ago = (TODAY - timedelta(days=40)).isoformat()
    sam.post("/journal", json={"notes": "Today"})
    sam.post("/journal", json={"notes": "Long ago", "day": long_ago})
    first = sam.get("/journal").json()
    assert first["has_more"] and first["next_until"] == long_ago and first["today"] == TODAY.isoformat()
    assert first["days"][0]["is_today"] and not first["days"][0]["is_yesterday"]
    older = sam.get(f"/journal?until={first['next_until']}").json()
    assert [d["day"] for d in older["days"]] == [long_ago] and not older["has_more"]


# --- F-13 empty pages ----------------------------------------------------------------------------------
def test_a_typed_page_cant_be_emptied_but_a_photo_page_can(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    page = sam.post("/journal", json={"notes": "Good day."}).json()
    r = sam.patch(f"/journal/{page['id']}", json={"notes": "   "})
    assert r.status_code == 400 and "Delete it instead" in r.json()["detail"]
    rec = snap(sam)
    assert sam.patch(f"/journal/{rec['journal_id']}", json={"notes": ""}).status_code == 200


# --- F-15 wins last ---------------------------------------------------------------------------------------
def test_wins_survive_the_activity_log_and_read_backs_being_cleared(ctx):
    todo = FakeTodoist([{"id": "t1", "content": "Linked thing", "priority": 1, "due": {"date": TODAY.isoformat()}}])
    ctx, a, login, seen = site(ctx, linked=todo)
    sam = login()
    sam.post("/list/dump", json={"text": "Buy milk today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    seen.update(rows=[{"row": row_of(label, "Buy milk"), "mark": "done", "confidence": 0.95},
                      {"row": row_of(label, "Linked thing"), "mark": "done", "confidence": 0.95}])
    snap(sam, label["id"])
    assert day_card(sam, TODAY)["wins"] == {"count": 2, "items": ["Buy milk", "Linked thing"], "linked": 0}
    with a.db.begin() as conn:
        conn.execute(delete(db.events))
        for _ in range(3):  # lots of newer activity doesn't push wins out either
            conn.execute(insert(db.events), [{"ts": 1e10, "user": "sam", "kind": "activity", "action": "x", "ok": True}] * 2000)
    ctx.store.prune(days=-1)  # tickets and read-backs gone
    # Still 2: the linked one has no name to show any more (its text was never stored).
    assert day_card(sam, TODAY)["wins"] == {"count": 2, "items": ["Buy milk"], "linked": 1}
    assert "Linked thing" not in str(rows(ctx.settings.data_dir, "wins"))


def test_wins_from_older_versions_are_brought_over_once(ctx):
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    mine = LocalTasks(a.db, "sam")
    t = mine.create({"content": "Old win", "due_date": TODAY.isoformat()})
    ts = datetime.now().timestamp()
    with a.db.begin() as conn:  # what 0.10.1 left: a done task with its event, and a linked one
        conn.execute(db.tasks.update().where(db.tasks.c.id == int(t["id"][3:])).values(done_at=int(ts)))
        conn.execute(insert(db.events), [
            {"ts": ts, "user": "sam", "kind": "activity", "action": "task_done", "ok": True, "detail": {"source": "nextbox"}},
            {"ts": ts, "user": "sam", "kind": "activity", "action": "task_done", "ok": True, "detail": {"source": "linked"}}])
    create_app(dataclasses.replace(ctx, tasks_factory=None))
    got = Wins(a.db, "sam").between("2000-01-01", "2999-01-01")
    assert sorted((w["source"], w["content"]) for w in got) == [("linked", None), ("nextbox", "Old win")]
    create_app(dataclasses.replace(ctx, tasks_factory=None))  # once only
    assert len(Wins(a.db, "sam").between("2000-01-01", "2999-01-01")) == 2


def test_deleting_the_account_removes_its_wins(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Secret errand today"})
    sam.post(f"/list/{task(sam, 'Secret errand')['id']}/done")
    assert rows(ctx.settings.data_dir, "wins")
    assert sam.post("/auth/delete-account", json={"password": "sam password", "confirm": "sam"}).status_code == 200
    assert not rows(ctx.settings.data_dir, "wins")


# --- F-16 slugs -------------------------------------------------------------------------------------------
def test_not_a_slug_and_making_it_smaller_clear_the_slug(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Clean garage today\nSort photos today"})
    garage, photos = task(sam, "Clean garage")["id"], task(sam, "Sort photos")["id"]
    slides = Slides(a.db, "sam")
    for tid in (garage, photos):
        for _ in range(3):
            slides.bump(tid)
    assert {s["id"] for s in sam.get("/journal/slugs").json()["slugs"]} == {garage, photos}
    assert sam.delete(f"/journal/slugs/{garage}").json() == {"ok": True}
    sam.patch(f"/list/{photos}", json={"steps": ["Find the box"]})
    assert sam.get("/journal/slugs").json()["slugs"] == []
    sam.patch(f"/list/{photos}", json={"steps": ["Find the box"]})  # saving the same steps again: no change
    assert slides.bump(garage) == 1  # moved again: counting starts over


# --- F-17 today's win ----------------------------------------------------------------------------------
def test_the_handwritten_win_is_kept_privately_on_the_page(ctx, tmp_path):
    ctx, a, login, seen = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Buy milk today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    seen.update(win="Called mom")
    rec = snap(sam, label["id"])
    assert rec["win"] == "Called mom"
    assert day_card(sam, TODAY)["pages"][0]["win"] == "Called mom"
    assert "Called mom" not in str(rows(ctx.settings.data_dir, "scans"))
    if dbtools.is_postgres(db.url_for(ctx.settings.data_dir)):
        return
    assert dbtools.run_sql(ctx.settings.data_dir, "SELECT win AS w FROM journal")[1] != [["Called mom"]]


# --- F-18 the prompt --------------------------------------------------------------------------------------
def test_the_prompt_and_the_schema_agree_on_where_the_code_is():
    field = ReadBack.model_fields["label_code"].description
    assert "bottom edge" in field and "bottom edge" in scan.PROMPT
    assert "bottom left" not in scan.PROMPT and "bottom-right" not in field
    assert "Today's win:" in scan.PROMPT and "win" in ReadBack.model_fields


# --- F-19 reading fails -------------------------------------------------------------------------------------
def test_a_failed_read_keeps_the_photo_and_can_be_tried_again(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login()
    sam.post("/list/dump", json={"text": "Buy milk today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    seen.update(fail=True)
    r = sam.post("/scan", data={"label_id": label["id"]}, files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")})
    rec = r.json()
    assert r.status_code == 200 and rec["status"] == "failed" and rec["errors"] == [scan.READ_FAILED]
    assert rec["journal_id"] and ctx.store.get_scan(rec["id"])
    assert any(e["kind"] == "error" and e["action"] == "scan" for e in rows(ctx.settings.data_dir, "events"))
    still = sam.post(f"/scan/{rec['id']}/retry").json()
    assert still["status"] == "failed"
    seen.update(fail=False, rows=[{"row": 1, "mark": "done", "confidence": 0.95}], notes="Felt good")
    read = sam.post(f"/scan/{rec['id']}/retry").json()
    assert "status" not in read and read["applied"][0]["content"] == "Buy milk" and read["notes"] == "Felt good"
    assert page_count(ctx) == 1
    assert sam.post(f"/scan/{rec['id']}/retry").json()["applied"]  # done already: nothing again
    assert day_card(sam, TODAY)["wins"]["count"] == 1


def test_a_journal_that_wont_save_still_keeps_the_read_back(ctx, todo):
    label = jobs.print_today(ctx)

    def broken(**kw):
        raise OSError("disk full")
    ctx = dataclasses.replace(ctx, save_page=broken)
    vision = lambda *a: ReadBack(label_code=label["id"], rows=[RowMark(row=1, mark="done", confidence=0.95)])  # noqa: E731
    rec = scan.scan_photo(ctx, vision, b"img", "image/jpeg")
    assert rec["applied"] and ctx.store.get_scan(rec["id"])["applied"]
    assert "journal" in rec["errors"][0]


# --- F-20 two taps at once -------------------------------------------------------------------------------
def test_confirming_the_same_row_at_once_applies_it_once(ctx, todo):
    label = jobs.print_today(ctx)
    row = row_of(label, "Renew passport")
    vision = lambda *a: ReadBack(label_code=label["id"], rows=[RowMark(row=row, mark="tomorrow", confidence=0.5)])  # noqa: E731
    rec = scan.scan_photo(ctx, vision, b"img", "image/jpeg")
    moves = []
    ctx = dataclasses.replace(ctx, on_move=moves.append)
    errors = []

    def tap():
        try:
            scan.confirm(ctx, rec["id"], {row: "confirm"})
        except Exception as exc:  # pragma: no cover - shown below
            errors.append(exc)
    threads = [threading.Thread(target=tap) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and moves == ["3"]
    assert [c for c in todo.calls if c[0] == "due_date"] == [("due_date", "3", "2026-09-29")]
    assert ctx.store.get_scan(rec["id"])["needs_confirmation"][0]["status"] == "applied"


def test_journal_pages_merge_notes_without_repeating_them(tmp_path):
    j = Journal(db.database(tmp_path), "sam")
    photo = tiny_jpeg()
    first = j.save_snap("2026-10-15", "Morning", photo, "s1", "L1")
    again = j.save_snap("2026-10-15", "Morning", tiny_jpeg((50, 50)), "s2", "L1")
    assert again["id"] == first["id"] and again["notes"] == "Morning"
    more = j.save_snap("2026-10-15", "Evening walk", None, "s3", "L1", page_id=first["id"])
    assert more["notes"] == "Morning\n\nEvening walk" and j.count() == 1
