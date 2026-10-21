"""Round 2, batch A2: Snap's ticket candidates and messages, what's printed (render.py, paper.py), the
journal export's dates and the routine card's "You finished N" by routine day.

Server-side fixes are tested through the API and the renderers (every string a label draws is recorded).
The routine card's wording was checked in a real browser; the last test pins the pieces that make it work."""
import dataclasses
import io
import random
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from nextbox import jobs, paper, render, scan, server
from nextbox.auth import Accounts
from nextbox.journal import Wins, day_notes_text
from nextbox.scan import ReadBack, RowMark
from nextbox.server import create_app

from conftest import FakeTodoist, tiny_jpeg

WEB = Path(server.WEB_DIR)


def site(ctx, marks=("done",)):
    """sam on My list, with a fake camera that reads rows (with `marks`) but no ticket code."""
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("sam", "sam password", create=True)

    def vision(image, media_type, manifest):
        return ReadBack(label_code=None, rows=[RowMark(row=n, mark=m, confidence=0.9) for n, m in enumerate(marks, 1)])
    c = TestClient(create_app(ctx, vision=vision), base_url="http://testserver")
    assert c.post("/auth/login", json={"username": "sam", "password": "sam password"}).status_code == 200
    return ctx, a, c


@pytest.fixture
def drawn(monkeypatch):
    """Every piece of text a label draws: (x, text, how far right its ink reaches)."""
    seen: list[tuple[float, str, float]] = []
    original = render._Draw.text

    def spy(self, xy, text, fill=None, font=None, *a, **k):
        if font is not None and not a and not k:
            seen.append((xy[0], text, xy[0] + self.textlength(text, font=font)))
        return original(self, xy, text, fill, font, *a, **k)
    monkeypatch.setattr(render._Draw, "text", spy)
    return seen


def texts(seen) -> list[str]:
    return [t for _, t, _ in seen]


# ------------------------------------------------------------------ R1-03: notes don't hide the ticket with tasks
def _ticket(ctx, label_id: str, at: datetime, kind: str, manifest: list) -> None:
    ctx.store.save_printed({"id": label_id, "kind": kind, "title": kind, "by": "sam", "household_id": "home",
                            "created_at": at.isoformat(timespec="seconds"), "manifest": manifest, "dry_run": True})


def test_snap_with_no_code_still_offers_the_ticket_behind_many_notes(ctx):
    ctx, _, sam = site(ctx)
    now = datetime.now().replace(microsecond=0)
    t0 = max(now - timedelta(hours=1), now.replace(hour=0, minute=0, second=0))  # still today just after midnight
    if (now - t0).total_seconds() < 60:
        t0 = now - timedelta(seconds=50)
    today_id = f"{t0:%y%m%d}-TDAY"
    _ticket(ctx, today_id, t0, "today", [{"row": 1, "task_id": "nb:1", "content": "Call the dentist"}])
    for n in range(41):  # notes, clip-hotkey and test labels: no tasks on them
        _ticket(ctx, f"{t0:%y%m%d}-N{n:03d}", t0 + timedelta(seconds=n + 1), "text", [])
    rec = sam.post("/scan", files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")}).json()
    assert [x["id"] for x in rec["candidates"]] == [today_id]
    assert rec["errors"] == [scan.NO_CODE]


def test_snap_candidates_page_past_notes_and_keep_todays_label_first(ctx):
    ctx, _, sam = site(ctx)
    now = datetime.now().replace(microsecond=0)
    t0 = max(now - timedelta(hours=2), now.replace(hour=0, minute=0, second=0))  # still today just after midnight
    if (now - t0).total_seconds() < 120:  # the first two minutes of a day: put the tickets just before now
        t0 = now - timedelta(seconds=110)
    day = f"{t0:%y%m%d}"
    _ticket(ctx, f"{day}-TDAY", t0, "today", [{"row": 1, "task_id": "nb:1", "content": "A"}])
    for n in range(6):  # six newer one-task tickets, then a run of notes on top
        _ticket(ctx, f"{day}-K{n:03d}", t0 + timedelta(seconds=n + 1), "task", [{"row": 1, "task_id": "nb:2", "content": "B"}])
    for n in range(90):
        _ticket(ctx, f"{day}-N{n:03d}", t0 + timedelta(seconds=n + 10), "text", [])
    got = scan._candidates(dataclasses.replace(ctx, user="sam"))
    assert [c["id"] for c in got] == [f"{day}-TDAY"] + [f"{day}-K{n:03d}" for n in (5, 4, 3, 2)]
    assert sam.post("/scan", files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")}).json()["errors"] == [scan.NO_CODE]


# ------------------------------------------------------------------ R1-04: "ticked boxes" only when one was ticked
@pytest.mark.parametrize("marks, message", [
    (("none", "drop"), scan.NO_TICKETS_UNMARKED),
    (("none", "none"), scan.NO_TICKETS_UNMARKED),
    (("tomorrow",), scan.NO_TICKETS_UNMARKED),
    (("none", "done"), scan.NO_TICKETS),
])
def test_no_ticket_yet_message_only_mentions_ticks_that_were_seen(ctx, marks, message):
    ctx, _, sam = site(ctx, marks)
    rec = sam.post("/scan", files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")}).json()
    assert rec["candidates"] == [] and rec["errors"] == [message]
    assert "ticked" not in scan.NO_TICKETS_UNMARKED


# ------------------------------------------------------------------ W4-03: a page break never splits a list row
def _long_list(monkeypatch, rows: list[str]) -> tuple[Image.Image, list[int]]:
    """A list label, and the y of each row's dotted separator (the row's bottom)."""
    seps: list[int] = []
    dotted = render._dotted

    def spy(d, y, x0, x1):
        seps.append(y)
        return dotted(d, y, x0, x1)
    monkeypatch.setattr(render, "_dotted", spy)
    img = render.render_day("Shopping", "Oct 16", [render.DayRow(t) for t in rows], "261016-ABCD")
    return img, sorted(seps)


@pytest.mark.parametrize("size", ["a5", "half_letter", "letter", "a4"])
def test_print_on_paper_breaks_pages_between_rows(monkeypatch, size):
    rows = [f"Grocery item {n} with a fairly long description so it wraps onto a second line" for n in range(1, 26)]
    img, seps = _long_list(monkeypatch, rows)
    scale = paper.SINGLE_WIDTH / img.width
    big = img.convert("L").resize((paper.SINGLE_WIDTH, round(img.height * scale)), Image.LANCZOS)
    px, usable = big.load(), paper.PAGES[size][1] - 2 * paper.MARGIN
    top, cuts = 0, []
    while top < big.height:
        top = paper._break_at(big, top, usable)
        cuts.append(top)
    assert len(cuts) >= 2
    for y in cuts[:-1]:
        above = max(s * scale for s in seps if s * scale <= y)  # the last row line above the cut...
        assert y - above < 30, (size, y, above)                   # ...is just above it
        assert not any(px[x, yy] < 160 for yy in range(int(above) + 5, y) for x in range(big.width))  # none of the next row
    pages = paper.single_pages(img, size)
    assert len(pages) == len(cuts)


def test_a_rule_is_told_from_a_line_of_text(monkeypatch):
    img, seps = _long_list(monkeypatch, ["Milk and eggs and bread and butter and cheese and jam", "Tea"])
    gray = img.convert("L")

    def row(y):
        return gray.crop((0, y, gray.width, y + 1)).tobytes()
    assert all(paper._is_rule(row(y)) for y in seps)
    text_rows = [y for y in range(seps[0] - 70, seps[0] - 6) if min(row(y)) < 160]  # the first row's two lines and box
    assert text_rows and not any(paper._is_rule(row(y)) for y in text_rows)
    assert paper._is_rule(bytes(gray.width)) and not paper._is_rule(bytes([255]) * gray.width)  # solid rule; blank


# ------------------------------------------------------------------ W4-06, W4-08, W4-09: single-task prints
def _ctx(ctx, task: dict):
    todo = FakeTodoist([task])
    return dataclasses.replace(ctx, tasks_factory=lambda: todo)


CAKE = {"id": "9", "content": "Bake the birthday cake", "priority": 1, "minutes": 90,
        "due": {"date": "2026-09-28T17:00:00"}, "steps": [{"text": f"Step {n} of the cake plan", "done": False} for n in range(1, 13)]}


def test_every_step_of_twelve_is_printed(ctx, drawn):
    c = _ctx(ctx, CAKE)
    jobs.print_focus(c, "9")
    steps = [t for t in texts(drawn) if t.startswith("Step ")]
    assert steps == [f"Step {n} of the cake plan" for n in range(1, 13)]
    drawn.clear()
    jobs.print_task(c, "9")
    assert [t for t in texts(drawn) if t.startswith("• Step")] == [f"• Step {n} of the cake plan" for n in range(1, 13)]


def test_a_card_with_more_steps_than_fit_says_so(drawn):
    render.render_focus("Move house", [f"Box {n}" for n in range(1, 15)], "261016-ABCD")
    assert "Box 12" in texts(drawn) and "Box 13" not in texts(drawn) and "+2 more in Next Box" in texts(drawn)


def test_start_by_stays_with_its_time(ctx, drawn):
    for task, start in ((CAKE, "start by 3:30p"),
                        ({"id": "1", "content": "Call the dentist", "priority": 4, "minutes": 5,
                          "due": {"date": "2026-09-28T09:00:00"}}, "start by 8:55a")):
        drawn.clear()
        jobs.print_focus(_ctx(ctx, task), task["id"])
        lines = [t for t in texts(drawn) if "start" in t or "about" in t or "today" in t]
        assert any(t.endswith(start) for t in lines), lines
        assert not any(t.endswith(("start", "start by")) for t in lines), lines


def test_single_task_prints_mark_urgent_and_repeating_tasks(ctx, drawn):
    dentist = {"id": "1", "content": "Call the dentist", "priority": 4, "due": {"date": "2026-09-28"}}
    jobs.print_focus(_ctx(ctx, dentist), "1")
    assert any(t.startswith("! Call") for t in texts(drawn))
    plants = {"id": "2", "content": "Water plants", "priority": 1,
              "due": {"date": "2026-09-28", "is_recurring": True, "string": "every day"}}
    c = _ctx(ctx, plants)
    for print_it in (lambda: jobs.print_focus(c, "2"), lambda: jobs.print_task(c, "2"),
                     lambda: jobs.print_stickers(c, ["2"]), lambda: jobs.print_strips(c, ["2"])):
        drawn.clear()
        print_it()
        assert any("today ↻" in t for t in texts(drawn)), texts(drawn)
    assert jobs._when({"due": {"date": "2026-09-26", "is_recurring": True}}, date(2026, 9, 28), False, True) == "waiting 2d ↻"
    assert jobs._when({"due": {"date": "2026-09-28T09:00:00"}}, date(2026, 9, 28), False, True) == "today 9:00a"


# ---------------------------------------- W4-09 follow-up: a sticker's "when · how long" never runs into its code
@pytest.fixture
def boxes(monkeypatch):
    """Every piece of text a label draws, with the box it takes: (text, left, top, right, bottom)."""
    seen: list[tuple[str, float, float, float, float]] = []
    original = render._Draw.text

    def spy(self, xy, text, fill=None, font=None, *a, **k):
        if font is not None and not a and not k:
            _, top, _, bottom = self.textbbox(xy, text, font=font)
            seen.append((text, xy[0], top, xy[0] + self.textlength(text, font=font), bottom))
        return original(self, xy, text, fill, font, *a, **k)
    monkeypatch.setattr(render._Draw, "text", spy)
    return seen


def _code_is_clear(seen, code: str) -> tuple:
    """The ticket code (what Snap reads) is drawn once, and nothing else is drawn on top of it or too close."""
    [box] = [b for b in seen if b[0] == f"#{code}"]
    for other in seen:
        if other is box or other[4] <= box[2] or other[2] >= box[4]:
            continue  # the code itself, or not on the code's row
        assert other[3] + render.STICKER_CODE_GAP <= box[1] + 0.5, (other, box)
    return box


STICKER_WHENS = ("today", "today ↻", "tomorrow 12:45p ↻", "Mon Oct 19 6:00p ↻", "Wed Oct 21 10:30a ↻",
                 "Mon Oct 19 12:45p ↻", "Wed Sep 30 12:45p", "waiting 123d ↻", "Wed Sep 30 23:45 ↻", "")


@pytest.mark.parametrize("big", [True, False])
@pytest.mark.parametrize("reading", [False, True])
def test_sticker_meta_never_runs_into_the_code(boxes, big, reading):
    for when in STICKER_WHENS:
        for minutes in (None, 5, 15, 60, 90, 24 * 60):
            for code in ("261016-JCZ5", "261016-WWWW", "261016-MMMM"):
                boxes.clear()
                s = render.Sticker(1, "Team sync", when=when, minutes=minutes, big=big)
                with render.reading_font(reading):
                    img = render.render_sticker(s, code)
                box = _code_is_clear(boxes, code)
                assert box[4] <= img.height - 4, (box, img.height)  # the code isn't cut off at the bottom
                meta = render._meta_line(s)
                if meta:  # printed whole, inside the label
                    [m] = [b for b in boxes if b[0] == meta]
                    assert m[3] <= render.WIDTH - render.MARGIN, m
                    beside = not (m[4] <= box[2] or m[2] >= box[4])
                    # a short one stays beside the code, so a sticker only grows when it has to
                    assert beside or m[3] + render.STICKER_CODE_GAP > box[1], (m, box)


def test_big_repeating_sticker_from_the_app_keeps_its_code_clear(ctx, boxes):
    """The checker's case: Settings → Sticker size big, "Team sync every mon 12:45pm ~1h", Stickers."""
    ctx, _, c = site(ctx)
    assert c.patch("/settings", json={"sticker_size": "big"}).status_code == 200
    day = date.today() + timedelta(days=3)  # "Mon Oct 19": a later day prints its date, the longest "when"
    task = c.post("/list", json={"content": "Team sync", "due_date": day.isoformat(), "due_time": "12:45",
                                 "repeat": "weekly", "minutes": 60}).json()
    r = c.post("/print/stickers", json={"task_ids": [task["id"]], "dry_run": True})
    assert r.status_code == 200 and r.json()["status"] == "dry_run", r.text
    code = r.json()["id"]
    when = f"{day:%a %b %-d} 12:45p ↻  ·  ~1h"
    assert when in [b[0] for b in boxes]
    _code_is_clear(boxes, code)
    assert c.get(f"/printed/{code}/png").status_code == 200


# ------------------------------------------------------------------ W4-07: the "Not today" line stays inside the label
def test_not_today_line_stays_inside_the_margin_with_one_ellipsis(drawn):
    words = ("call the insurance company about claim dentist plants water sam back weekend plans dry cleaning "
             "shop main street library books renew car policy online").split()
    rng = random.Random(7)
    right = render.WIDTH - render.MARGIN - 4
    for reading in (True, False):
        for _ in range(40):
            names = [jobs._short(" ".join(rng.choice(words) for _ in range(rng.randint(2, 7))).capitalize(), 28)
                     for _ in range(rng.randint(3, 12))]
            drawn.clear()
            with render.reading_font(reading):
                render.render_day("Friday", "October 16", [render.DayRow("A task")], "261016-ABCD", waiting=names,
                                  waiting_head="Not today")
            i = next(k for k, (_, t, _) in enumerate(drawn) if t.startswith("Not today"))
            block = drawn[i:i + 3]
            assert max(r for _, _, r in block) <= right + 0.5, block
            assert not any("……" in t for _, t, _ in block), block


def test_ellipsize_never_doubles_up():
    d = render._Draw(Image.new("L", (10, 10)))
    font = render._font(25)
    assert render._ellipsize(d, "Sam back · Remember to call the insuran… ·", font, 2000).endswith("insuran…")
    assert "……" not in render._ellipsize(d, "Sam back · Remember to call the insuran… · Buy", font, 380)


# ------------------------------------------------------------------ W5-04: the export says when a page was made
def test_made_when_adds_the_date_for_a_page_made_on_another_day():
    noon = datetime(2026, 10, 22, 7, 30).timestamp()
    assert paper.made_when(noon, "2026-10-22") == "at 7:30 AM"
    assert paper.made_when(noon, "2026-10-21") == "Oct 22, 7:30 AM"
    assert paper.made_when(datetime(2026, 1, 1, 12, 0).timestamp(), "2025-12-31") == "Jan 1, 2026, 12:00 PM"
    day = {"day": "2026-10-21", "pages": [{"id": "a", "created": noon, "notes": "late note"},
                                          {"id": "b", "created": noon, "notes": "", "has_photo": True}]}
    text = day_notes_text(day, {"b": "photo-1.jpg"})
    assert "Written Oct 22, 7:30 AM\nlate note" in text and "Snapped Oct 22, 7:30 AM (photo-1.jpg)" in text


def test_journal_export_dates_a_note_written_for_yesterday(ctx, drawn):
    ctx, _, sam = site(ctx)
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    assert sam.post("/journal", json={"day": yesterday, "notes": "Written today, for yesterday."}).status_code == 200
    assert sam.post("/journal", json={"notes": "Written today."}).status_code == 200
    z = zipfile.ZipFile(io.BytesIO(sam.get("/journal/export", params={"format": "zip"}).content))
    files = {n.split("/", 1)[1]: z.read(n).decode() for n in z.namelist() if n.endswith("notes.txt")}
    assert f"Written {today:%b} {today.day}, " in files[f"{yesterday}/notes.txt"]
    assert "Written at " in files[f"{today.isoformat()}/notes.txt"]
    drawn.clear()
    r = sam.get("/journal/export", params={"format": "pdf", "day": yesterday})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert any(t.startswith(f"Written {today:%b} {today.day}, ") for t in texts(drawn))


# ------------------------------------------------------------------ W5-02: "You finished N" counts the routine day
def test_list_counts_whats_done_since_the_routine_day_started(ctx):
    ctx, a, sam = site(ctx)
    now = int(time.time())
    from nextbox import db
    with a.db.begin() as conn:  # a tick before the routine day, two in it (one a read-back filed under an older day)
        for ts, day, source in ((now - 3 * 3600, date.today().isoformat(), "nextbox"),
                                (now - 1800, date.today().isoformat(), "nextbox"),
                                (now - 60, (date.today() - timedelta(days=1)).isoformat(), "readback")):
            conn.execute(db.wins.insert().values(owner="sam", household_id="home", day=day, ts=ts, source=source))
        conn.execute(db.wins.insert().values(owner="kim", household_id="home", day=date.today().isoformat(),
                                             ts=now - 10, source="nextbox"))  # someone else's
    since = datetime.fromtimestamp(now - 2 * 3600).strftime("%Y-%m-%dT%H:%M:%S")
    got = sam.get("/list", params={"since": since}).json()
    assert got["done_since"] == 2
    assert "done_since" not in sam.get("/list").json()
    assert Wins(a.db, "sam").count_since(now - 4 * 3600) == 3
    assert sam.get("/list", params={"since": "2026-10-16"}).status_code == 422
    assert sam.get("/list", params={"since": "2026-13-45T01:00"}).status_code == 400


def test_routine_card_uses_the_routine_days_count(ctx):
    js = (WEB / "app.js").read_text()
    assert "api(`/list?since=${encodeURIComponent(routineDay().since)}`" in js
    assert "const n = listData.done_since ?? listData.done_today ?? 0;" in js
    assert '"Your page is in your journal."' in js and "snap.day !== today()" in js
