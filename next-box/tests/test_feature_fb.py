"""Feature batch FB, the journal loop: notes -> tasks, download my journal (zip and PDF), moving pages
between days, search, each day's ticket and read-back changes, and printing one day's page."""
import dataclasses
import io
import re
import zipfile
from datetime import date, timedelta

from fastapi.testclient import TestClient

from nextbox import paper
from nextbox.auth import Accounts
from nextbox.journal import note_lines
from nextbox.scan import ReadBack, RowMark
from nextbox.server import create_app

from conftest import rows, tiny_jpeg

TODAY = date.today()


def site(ctx, read=None):
    """Sam and the boss (a superadmin), with My list only. `read` is what the fake camera sees."""
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    seen = read if read is not None else {}

    def vision(image, media_type, manifest):
        return ReadBack(label_code=seen.get("code"), rows=[RowMark(**r) for r in seen.get("rows", [])],
                        notes=seen.get("notes", ""), win=seen.get("win", ""))
    app = create_app(ctx, vision=vision)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, login, seen


def snap(c, label_id=None, size=(40, 30)):
    return c.post("/scan", data={"label_id": label_id} if label_id else {},
                  files={"photo": ("p.jpg", tiny_jpeg(size), "image/jpeg")}).json()


def day(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def check_pdf(data: bytes) -> int:
    """A well-formed PDF: every cross-reference entry points at its object. Returns the page count."""
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    start = int(re.search(rb"startxref\n(\d+)\n%%EOF", data)[1])
    assert data[start:start + 4] == b"xref"
    head = re.match(rb"xref\n0 (\d+)\n", data[start:])
    size, table = int(head[1]), data[start + head.end():]
    for n in range(1, size):
        offset = int(table[n * 20:n * 20 + 10])
        assert data[offset:].startswith(f"{n} 0 obj".encode()), n
    return int(re.search(rb"/Type /Pages /Kids \[[^\]]*\] /Count (\d+)", data)[1])


# --- 1. notes -> tasks -------------------------------------------------------------------------------
def test_note_lines_suggests_the_lines_that_look_like_to_dos():
    text = ("Rough start, but the walk helped.\n- Finally called the dentist!\n[ ] call mom tomorrow 3pm #call\n"
            "☐ buy milk\n[x] pay rent\ntodo: book the vet 2026-12-01\n1. grateful for coffee\n\nTo do:\nwater plants\n"
            "renew passport !\n\nGrateful for Sam.\n- call [unclear] about the boiler\nfix the chair ✓\n---\n")
    lines = {x["text"]: x for x in note_lines(text, date(2026, 10, 16), on_list={"buy milk"})}
    suggested = [t for t, x in lines.items() if x["suggest"]]
    assert suggested == ["Finally called the dentist!", "call mom tomorrow 3pm #call", "book the vet 2026-12-01",
                         "water plants", "renew passport !"]
    assert lines["call mom tomorrow 3pm #call"]["kind"] == "box"
    assert lines["call mom tomorrow 3pm #call"]["task"] == {
        "content": "call mom", "tags": ["call"], "priority": 1, "due_date": "2026-10-17", "due_time": "15:00"}
    assert lines["renew passport !"]["task"]["priority"] == 4 and lines["water plants"]["kind"] == "list"
    assert lines["buy milk"]["on_list"] and not lines["buy milk"]["suggest"]       # already on My list
    assert lines["pay rent"]["done"] and lines["fix the chair"]["done"]            # ticked: not again
    assert lines["call [unclear] about the boiler"]["unclear"]                     # fix the word first
    assert not lines["grateful for coffee"]["suggest"] and not lines["Grateful for Sam."]["suggest"]
    assert "---" not in lines and "To do:" not in lines                           # rules and headings aren't tasks
    assert [x["line"] for x in note_lines("one\n\n- two", TODAY)] == [1, 3]
    # The camera is asked to write a hand-drawn box as "[ ]", so those lines come back as to-dos.
    from nextbox import scan
    assert '"[ ] "' in scan.PROMPT and scan.PROMPT.format(manifest_hint="")


def test_lines_endpoint_previews_and_the_dump_adds_only_the_chosen_ones(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "Buy milk"})
    r = sam.post("/journal/lines", json={"text": "- buy milk\n- call the plumber tomorrow\nslept badly"})
    assert r.status_code == 200
    got = r.json()["lines"]
    assert [(x["text"], x["suggest"], x["on_list"]) for x in got] == [
        ("buy milk", False, True), ("call the plumber tomorrow", True, False), ("slept badly", False, False)]
    assert got[1]["task"]["due_date"] == (TODAY + timedelta(days=1)).isoformat()
    added = sam.post("/list/dump", json={"text": got[1]["text"]}).json()["added"]
    assert [t["content"] for t in added] == ["call the plumber"]
    # The journal text is never written to the activity log (admins read it).
    events = str(rows(ctx.settings.data_dir, "events"))
    assert "plumber" not in events and "slept badly" not in events
    assert TestClient(sam.app).post("/journal/lines", json={"text": "- x"}).status_code == 401
    assert sam.post("/journal/lines", json={"text": "x" * 20_001}).status_code == 422


# --- 3. move pages, search, the day's ticket and read-back changes ------------------------------------
def test_move_a_page_to_another_day(ctx):
    ctx, a, login, seen = site(ctx)
    sam, boss = login("sam", "sam password"), login("boss", "boss password")
    page = sam.post("/journal", json={"notes": "Late-night thoughts about yesterday"}).json()
    assert page["day"] == TODAY.isoformat()
    moved = sam.patch(f"/journal/{page['id']}", json={"day": day(1)})
    assert moved.status_code == 200 and moved.json()["day"] == day(1)
    assert moved.json()["notes"] == "Late-night thoughts about yesterday"  # left out: unchanged
    days = sam.get("/journal").json()["days"]
    assert [d["day"] for d in days] == [day(1)] and days[0]["is_yesterday"]
    r = sam.patch(f"/journal/{page['id']}", json={"day": (TODAY + timedelta(days=1)).isoformat()})
    assert r.status_code == 400 and "hasn't happened" in r.json()["detail"]
    assert sam.patch(f"/journal/{page['id']}", json={"day": "16/10/2026"}).status_code == 400
    assert sam.patch(f"/journal/{page['id']}", json={}).status_code == 400
    assert boss.patch(f"/journal/{page['id']}", json={"day": day(2)}).status_code == 404  # not theirs
    # Notes alone still save the way they always did, and both together work too.
    assert sam.patch(f"/journal/{page['id']}", json={"notes": "Edited"}).json()["day"] == day(1)
    both = sam.patch(f"/journal/{page['id']}", json={"notes": "Edited again", "day": day(3)}).json()
    assert (both["notes"], both["day"]) == ("Edited again", day(3))


def test_search_is_private_ignores_case_and_accents_and_marks_the_matches(ctx):
    ctx, a, login, seen = site(ctx)
    sam, boss = login("sam", "sam password"), login("boss", "boss password")
    sam.post("/journal", json={"day": day(30), "notes": "Called the DENTIST about my tooth. Café after."})
    sam.post("/journal", json={"day": day(2), "notes": "Dentist again: the filling is fine.\nThen groceries."})
    sam.post("/journal", json={"day": day(1), "notes": "Nothing about teeth today."})
    boss.post("/journal", json={"notes": "boss went to the dentist"})
    r = sam.get("/journal/search", params={"q": "dentist"}).json()
    assert r["total"] == 2 and [x["day"] for x in r["results"]] == [day(2), day(30)]  # newest first
    first = r["results"][0]
    assert "".join(p["t"] for p in first["parts"]).startswith("Dentist again")
    assert [p["t"] for p in first["parts"] if p["m"]] == ["Dentist"]
    assert sam.get("/journal/search", params={"q": "cafe tooth"}).json()["total"] == 1  # every word, accents aside
    assert sam.get("/journal/search", params={"q": "cafe groceries"}).json()["total"] == 0
    assert sam.get("/journal/search", params={"q": "x"}).status_code == 400
    assert boss.get("/journal/search", params={"q": "tooth"}).json()["total"] == 0  # only your own pages
    # A long page gives a short piece around the match, with … where it was cut.
    sam.post("/journal", json={"notes": ("word " * 200) + "needle " + ("word " * 200)})
    parts = sam.get("/journal/search", params={"q": "needle"}).json()["results"][0]["parts"]
    text = "".join(p["t"] for p in parts)
    assert text.startswith("…") and text.endswith("…") and len(text) < 260
    assert "needle" not in str(rows(ctx.settings.data_dir, "events"))


def test_each_day_shows_its_ticket_and_what_read_back_changed(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "Call the dentist today\nWater the plants today\nOld idea today"})
    label = sam.post("/print/today", json={"dry_run": True}).json()
    names = {m["row"]: m["content"] for m in label["manifest"]}
    seen.update(rows=[{"row": 1, "mark": "done", "confidence": 0.95}, {"row": 2, "mark": "tomorrow", "confidence": 0.95},
                      {"row": 3, "mark": "drop", "confidence": 0.95}], notes="Good day")
    rec = snap(sam, label["id"])
    assert sam.post(f"/scan/{rec['id']}/confirm", json={"decisions": {"3": "confirm"}}).status_code == 200
    today = sam.get("/journal").json()["days"][0]
    assert today["ticket"] == {"id": label["id"], "kind": "today", "title": label.get("title", ""), "dry_run": True}
    assert today["changes"] == {"moved": [names[2]], "dropped": [names[3]]}
    # Undoing the move takes it off the day's line.
    sam.post(f"/scan/{rec['id']}/undo", json={"row": 2})
    assert sam.get("/journal").json()["days"][0]["changes"] == {"moved": [], "dropped": [names[3]]}


def test_a_day_without_a_read_back_shows_its_daily_ticket_real_print_first(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "Something today"})
    preview = sam.post("/print/today", json={"dry_run": True}).json()
    sam.post("/journal", json={"notes": "typed note"})
    assert sam.get("/journal").json()["days"][0]["ticket"]["id"] == preview["id"]
    sam.post("/print/text", json={"text": "a note, not a daily ticket", "dry_run": True})
    assert sam.get("/journal").json()["days"][0]["ticket"]["id"] == preview["id"]
    # A ticket that really went to the printer this morning beats a later preview.
    real = {**{k: preview[k] for k in ("kind", "title", "manifest", "household_id", "by")},
            "id": ctx.store.new_id(), "dry_run": False, "created_at": f"{TODAY.isoformat()}T00:00:01"}
    ctx.store.save_printed(real, png=b"\x89PNG")
    got = sam.get("/journal").json()["days"][0]["ticket"]
    assert got == {"id": real["id"], "kind": "today", "title": preview["title"], "dry_run": False}
    # Another person's tickets are never shown.
    boss = login("boss", "boss password")
    boss.post("/journal", json={"notes": "boss note"})
    assert boss.get("/journal").json()["days"][0]["ticket"] is None


# --- 2 and 4. download my journal, print one day's page ------------------------------------------------
def journal_with_photos(ctx):
    ctx, a, login, seen = site(ctx)
    sam = login("sam", "sam password")
    seen.update(notes="Walked to the park.\n- call mom", win="Walked")
    rec = snap(sam)
    sam.patch(f"/journal/{rec['journal_id']}", json={"day": day(2)})
    sam.post("/journal", json={"day": day(2), "notes": "Typed later."})
    sam.post("/journal", json={"day": day(40), "notes": "An old page"})
    sam.post("/list/dump", json={"text": "Paid the water bill today"})
    sam.post(f"/list/{sam.get('/list').json()['tasks'][0]['id']}/done")
    return ctx, a, login, sam


def test_download_my_journal_as_a_zip_with_a_folder_for_each_day(ctx):
    ctx, a, login, sam = journal_with_photos(ctx)
    before = len(rows(ctx.settings.data_dir, "events"))
    r = sam.get("/journal/export", params={"format": "zip"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "no-store"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = f"next-box-journal-{TODAY.isoformat()}"
    assert sorted(z.namelist()) == sorted([
        f"{root}/README.txt", f"{root}/{day(40)}/notes.txt", f"{root}/{day(2)}/notes.txt",
        f"{root}/{day(2)}/photo-1.jpg", f"{root}/{TODAY.isoformat()}/wins.txt"])
    notes = z.read(f"{root}/{day(2)}/notes.txt").decode()
    assert notes.splitlines()[0] == paper.day_title(day(2))
    assert "Your win: Walked" in notes and "(photo-1.jpg)" in notes and "- call mom" in notes and "Typed later." in notes
    assert z.read(f"{root}/{day(2)}/photo-1.jpg")[:2] == b"\xff\xd8"
    assert "✓ Paid the water bill" in z.read(f"{root}/{TODAY.isoformat()}/wins.txt").decode()
    # A stretch of days, and nothing about the download goes in the activity log.
    part = zipfile.ZipFile(io.BytesIO(sam.get("/journal/export", params={"since": day(7)}).content))
    assert not any(day(40) in n for n in part.namelist())
    assert len(rows(ctx.settings.data_dir, "events")) == before


def test_journal_pdf_and_print_one_day(ctx):
    ctx, a, login, sam = journal_with_photos(ctx)
    r = sam.get("/journal/export", params={"format": "pdf", "paper": "a5"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert check_pdf(r.content) == 3 and b"/MediaBox [0 0 419.53 595.28]" in r.content  # a page a day, A5
    one = sam.get("/journal/export", params={"format": "pdf", "day": day(2)})
    assert one.status_code == 200 and one.headers["content-disposition"].startswith("inline")
    assert check_pdf(one.content) == 1 and b"/MediaBox [0 0 612.00 792.00]" in one.content  # their paper_size: letter
    sam.patch("/settings", json={"paper_size": "a4"})
    assert b"/MediaBox [0 0 595.28 841.89]" in sam.get("/journal/export", params={"format": "pdf", "day": day(2)}).content
    r = sam.get("/journal/export", params={"format": "pdf", "day": day(5)})
    assert r.status_code == 404 and "Nothing" in r.json()["detail"]
    assert sam.get("/journal/export", params={"format": "pdf", "day": (TODAY + timedelta(days=1)).isoformat()}).status_code == 400
    assert sam.get("/journal/export", params={"format": "pdf", "paper": "legal"}).status_code == 400
    assert sam.get("/journal/export", params={"format": "docx"}).status_code == 400


def test_journal_download_is_owner_only_rate_limited_and_waits_out_a_password_reset(ctx):
    ctx, a, login, sam = journal_with_photos(ctx)
    boss = login("boss", "boss password")
    assert boss.get("/journal/export").status_code == 404  # the boss has no journal, and can't ask for sam's
    assert TestClient(sam.app).get("/journal/export").status_code == 401
    s = sam.get("/journal/export/summary").json()
    assert {k: s[k] for k in ("days", "pages", "photos", "wins", "first", "last", "can_download")} == {
        "days": 3, "pages": 3, "photos": 1, "wins": 1, "first": day(40), "last": TODAY.isoformat(), "can_download": True}
    assert s["bytes"] > 0 and s["paper"] == "letter"
    assert sam.get("/journal/export/summary", params={"since": day(3), "until": day(1)}).json()["days"] == 1
    assert sam.get("/journal/export/summary", params={"since": "soon"}).status_code == 400
    for _ in range(10):
        assert sam.get("/journal/export").status_code == 200
    r = sam.get("/journal/export")
    assert r.status_code == 429 and "Try again" in r.json()["detail"]
    assert sam.get("/journal/export/summary").json()["can_download"] is False
    assert sam.get("/journal/export", params={"format": "pdf", "day": day(2)}).status_code == 200  # printing a day still works
    a.mark_reset("sam", "boss")
    r = sam.get("/journal/export/summary")
    assert r.status_code == 403 and "paused" in r.json()["detail"]
    assert sam.get("/journal/export", params={"format": "pdf", "day": day(2)}).status_code == 200


def test_jpeg_pdf_writer_streams_pages():
    from PIL import Image
    out = io.BytesIO()
    pdf = paper.JpegPdf(out, title="Ünïcode ✓")
    for shade in (255, 200):
        pdf.add(Image.new("RGB", (100, 140), (shade, shade, shade)), paper.POINTS["a5"])
    assert pdf.finish() == 2 and check_pdf(out.getvalue()) == 2
    assert b"/Title <FEFF00DC006E00EF0063006F0064006500202713>" in out.getvalue()
    try:
        paper.JpegPdf(io.BytesIO()).finish()
        raise AssertionError("an empty PDF should be refused")
    except ValueError:
        pass


def test_a_long_day_carries_on_over_more_sheets():
    long = {"day": "2026-10-15", "wins": {"count": 0, "items": [], "linked": 0},
            "pages": [{"id": "a", "notes": "word " * 4000, "has_photo": False, "created": 0, "win": ""}]}
    sheets = paper.journal_day_sheets(long, "a5", lambda _: None)
    assert len(sheets) > 2 and all(s.size == (1165, 1654) for s in sheets)  # A5 at 200 dpi
