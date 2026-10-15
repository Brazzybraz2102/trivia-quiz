"""The journal: snapped pages with notes and photos, wins, and slugs."""
import dataclasses
from datetime import date, timedelta

from fastapi.testclient import TestClient

from nextbox.auth import Accounts
from nextbox.journal import Journal, Slides, slugs
from nextbox.scan import ReadBack, RowMark
from nextbox.server import create_app

from conftest import rows, tiny_jpeg


def site(ctx, read=None):
    """Sam and the boss (a superadmin), with My list only. `read` is what the fake camera sees."""
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    seen = read or {}

    def vision(image, media_type, manifest):
        return ReadBack(label_code=seen.get("code"), rows=[RowMark(**r) for r in seen.get("rows", [])],
                        notes=seen.get("notes", ""))
    app = create_app(ctx, vision=vision)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, login, seen


def snap(c, label_id=None):
    return c.post("/scan", data={"label_id": label_id} if label_id else {},
                  files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")}).json()


def test_snapping_a_journal_spread_keeps_notes_and_photo(ctx):
    ctx, login, seen = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "Call the dentist today\nPay rent today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    seen.update(rows=[{"row": 1, "mark": "done", "confidence": 0.95}], notes="Felt scattered.\nWalk helped.")
    rec = snap(sam, label["id"])
    assert rec["applied"] and rec["notes"] == "Felt scattered.\nWalk helped." and rec["journal_id"]
    # The notes live only in the private journal, not in the read-back record admins can see.
    assert "scattered" not in str(rows(ctx.settings.data_dir, "scans"))

    days = sam.get("/journal").json()["days"]
    today = days[0]
    assert today["day"] == date.today().isoformat()
    assert today["pages"][0]["notes"] == "Felt scattered.\nWalk helped." and today["pages"][0]["has_photo"]
    assert today["wins"]["count"] == 1 and today["wins"]["items"] == [label["manifest"][0]["content"]]
    photo = sam.get(f"/journal/{rec['journal_id']}/photo")
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"

    # Private: nobody else can read it, not even a superadmin.
    boss = login("boss", "boss password")
    assert boss.get(f"/journal/{rec['journal_id']}/photo").status_code == 404
    assert boss.get("/journal").json()["days"] == []


def test_notes_without_a_label_still_go_in_the_journal(ctx):
    ctx, login, seen = site(ctx)
    sam = login("sam", "sam password")
    seen.update(notes="Ideas for the garden")
    rec = snap(sam)
    assert rec["journal_id"] and not rec["errors"] and "journal" in rec["message"]
    seen.update(notes="")  # a photo of nothing useful, journal mode off: not kept
    sam.patch("/settings", json={"journal_mode": False})
    rec = snap(sam)
    assert "journal_id" not in rec and rec["errors"]


def test_write_edit_and_delete_a_page(ctx):
    ctx, login, _ = site(ctx)
    sam = login("sam", "sam password")
    page = sam.post("/journal", json={"notes": "  Good day.  "}).json()
    assert page["notes"] == "Good day." and not page["has_photo"]
    assert sam.post("/journal", json={"notes": "   "}).status_code == 400
    assert sam.patch(f"/journal/{page['id']}", json={"notes": "Great day."}).json()["notes"] == "Great day."
    assert sam.delete(f"/journal/{page['id']}").status_code == 400  # needs confirm
    assert sam.delete(f"/journal/{page['id']}?confirm=true").json() == {"ok": True}
    assert sam.get("/journal").json()["days"] == []
    assert sam.get("/journal/zzz/photo").status_code == 404


def test_moving_a_task_again_and_again_makes_it_a_slug(ctx):
    ctx, login, seen = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "Clean garage today"})
    task = sam.get("/list").json()["tasks"][0]
    seen.update(rows=[{"row": 1, "mark": "tomorrow", "confidence": 0.95}])
    for _ in range(2):
        label = sam.post("/print/today", json={"dry_run": True}).json()
        snap(sam, label["id"])
        sam.patch(f"/list/{task['id']}", json={"due_date": date.today().isoformat()})  # back to today to test again
    later = (date.today() + timedelta(days=3)).isoformat()
    sam.patch(f"/list/{task['id']}", json={"due_date": later})  # moved on My list too
    assert Slides(Accounts(ctx.settings.data_dir).db, "sam").counts()[task["id"]] == 3
    sam.patch(f"/list/{task['id']}", json={"due_date": date.today().isoformat()})
    found = sam.get("/journal/slugs").json()["slugs"]
    assert found[0]["id"] == task["id"] and found[0]["moves"] == 3
    # Finishing it clears the count.
    sam.post(f"/list/{task['id']}/done")
    assert task["id"] not in Slides(Accounts(ctx.settings.data_dir).db, "sam").counts()


def test_slug_rules():
    today = date(2026, 10, 15)
    tasks = [{"id": "a", "content": "Long wait", "due": {"date": "2026-10-01"}},
             {"id": "b", "content": "Moved a lot", "due": {"date": "2026-10-15"}},
             {"id": "c", "content": "Fine", "due": {"date": "2026-10-14"}}]
    found = slugs(tasks, {"b": 4, "c": 1}, today)
    assert [s["id"] for s in found] == ["b", "a"]
    assert found[1]["waiting_days"] == 14


def test_wins_this_week_and_deleting_the_account_removes_the_journal(ctx):
    ctx, login, _ = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "One today\nTwo today"})
    for t in sam.get("/list").json()["tasks"]:
        sam.post(f"/list/{t['id']}/done")
    r = sam.get("/journal/wins").json()
    assert r["total"] == 2 and len(r["series"]) == 7 and r["series"][-1]["count"] == 2
    sam.post("/journal", json={"notes": "secret thoughts"})
    assert "journal" in sam.get("/auth/export").json()
    assert sam.post("/auth/delete-account", json={"password": "sam password", "confirm": "sam"}).status_code == 200
    assert not rows(ctx.settings.data_dir, "journal")


def test_journal_label_and_journal_paper_sizes(ctx):
    ctx, login, _ = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "Call mom today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    for size in ("a5", "half_letter"):
        r = sam.get(f"/printed/{label['id']}/pdf?paper={size}")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert sam.patch("/settings", json={"paper_size": "a5", "start_tab": "journal"}).status_code == 200


def test_journal_store_is_per_person(tmp_path):
    from nextbox import db
    engine = db.database(tmp_path)
    mine, theirs = Journal(engine, "sam"), Journal(engine, "kim")
    page = mine.add("2026-10-15", "hello")
    assert theirs.get(page["id"]) is None and theirs.photo(page["id"]) is None
    try:
        theirs.delete(page["id"])
        raise AssertionError("deleted someone else's page")
    except KeyError:
        pass
    assert mine.pages("2026-10-01", "2026-10-31")[0]["notes"] == "hello"
