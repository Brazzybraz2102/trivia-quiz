"""Audit batch E: printing, rendering, paper and the Printed history."""
import dataclasses
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from nextbox import jobs, paper, printer, render
from nextbox.auth import Accounts
from nextbox.mylist import LocalTasks, Merged
from nextbox.printers import Printers
from nextbox.providers import ProviderError
from nextbox.render import DayRow, Sticker, render_day, render_label
from nextbox.server import create_app

from conftest import TODAY


def black_rows(img: Image.Image) -> int:
    g = img.convert("L")
    px = g.load()
    return sum(1 for y in range(g.height) if all(px[x, y] < 10 for x in range(0, g.width, 8)))


def site(ctx):
    """Sam (a user) and the boss (superadmin), with My list only."""
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
    return ctx, login


class Capture:
    """Stands in for a render function: keeps what it was asked to draw."""
    def __init__(self, real):
        self.real, self.calls = real, []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.real(*args, **kwargs)


# --- E-01: long text never runs into black tape, and never pins the CPU -----------------------
def test_long_note_has_no_black_tape_and_keeps_its_code():
    start = time.time()
    img = render_label("NOTE", "Thu", body="lorem ipsum dolor sit amet " * 400, code="261015-ABCD")
    assert time.time() - start < 3
    assert img.height > 4000 and black_rows(img) == 0


def test_one_huge_word_wraps_quickly():
    start = time.time()
    img = render_label("NOTE", body="x" * 5000, code="261015-ABCD")
    assert time.time() - start < 2 and black_rows(img) == 0


def test_daily_ticket_with_long_rows_has_no_black_rows():
    rows = [DayRow("word " * 100, "9:00a") for _ in range(20)]
    img = render_day("Thursday", "October 15", rows, "261015-ABCD", waiting=["x" * 500] * 5)
    assert black_rows(img) == 0
    one = render_day("Thursday", "October 15", rows[:1], "261015-ABCD")
    assert one.height < 600  # a 500-character task is three lines at most


def test_notes_are_cut_to_fit_the_label(ctx):
    r = jobs.print_text(ctx, "word " * 3000)
    assert len(r["text"]) <= jobs.MAX_TEXT
    assert Image.open(ctx.store.png_path(r["id"])).height < 3500
    with pytest.raises(ValueError):
        jobs.print_text(ctx, "   ")


def test_print_endpoints_refuse_giant_inputs(ctx):
    ctx, login = site(ctx)
    sam = login("sam", "sam password")
    assert sam.post("/print/text", json={"text": "x" * 4001, "dry_run": True}).status_code == 422
    assert sam.post("/print/text", json={"text": "hi", "title": "t" * 81, "dry_run": True}).status_code == 422
    assert sam.post("/print/list", json={"query": "q" * 201, "dry_run": True}).status_code == 422
    assert sam.post("/print/text", json={"text": "x" * 4000, "dry_run": True}).status_code == 200


# --- E-03 and E-20: Printed history pages, and never shows file paths ---------------------------
def test_printed_history_pages_and_hides_file_paths(ctx):
    ctx, login = site(ctx)
    sam = login("sam", "sam password")
    made = [sam.post("/print/text", json={"text": f"note {i}", "dry_run": True}).json() for i in range(5)]
    assert all("png" not in r for r in made)
    # An older record that still has its file path stored.
    old = ctx.store.get_printed(made[0]["id"])
    ctx.store.save_printed({**old, "png": "/home/someone/.local/share/nextbox/png/x.png"})
    first = sam.get("/printed?limit=3").json()
    rest = sam.get("/printed?limit=3&offset=3").json()
    assert len(first) == 3 and len(rest) == 2
    assert {r["id"] for r in first + rest} == {r["id"] for r in made}
    assert "/home/" not in str(first + rest)
    assert sam.get("/printed?limit=101").status_code == 422


# --- E-04: a linked app that's down doesn't stop the paper routine --------------------------------
class DownApp:
    key = "todoist"

    def today(self):
        raise ProviderError("Todoist didn't answer (503)")

    def filter_tasks(self, query):
        raise ProviderError("Todoist didn't answer (503)")


def test_linked_app_down_still_prints_my_list(ctx):
    local = LocalTasks(ctx.settings.data_dir, "sam", today=lambda: TODAY)
    local.create({"content": "Water the plants", "due_date": TODAY.isoformat()})
    merged = Merged(local, DownApp())
    c = dataclasses.replace(ctx, tasks_factory=lambda: merged, _tasks=None)
    r = jobs.print_today(c)
    assert [m["content"] for m in r["manifest"]] == ["Water the plants"]
    assert "Todoist didn't answer, so this has My list only." in r["note"]
    for fn in (jobs.print_stickers, jobs.print_strips, jobs.print_focus):
        c = dataclasses.replace(ctx, tasks_factory=lambda: Merged(local, DownApp()), _tasks=None)
        assert "My list only" in fn(c)["note"]


def test_linked_app_down_and_nothing_on_my_list_says_so(ctx):
    local = LocalTasks(ctx.settings.data_dir, "sam", today=lambda: TODAY)
    c = dataclasses.replace(ctx, tasks_factory=lambda: Merged(local, DownApp()), _tasks=None)
    with pytest.raises(ProviderError):
        jobs.print_today(c)  # never a quiet "Nothing due"


# --- E-05: an offline printer gives a preview and keeps the manifest --------------------------------
@pytest.fixture
def real_printer(ctx, tmp_path):
    ctx.settings.dry_run = False
    reg = Printers(tmp_path / "reg", ctx.settings)
    cfg = reg.add({"name": "Kitchen QL", "driver": "escpos", "address": "192.168.1.60"})
    return dataclasses.replace(ctx, printers=reg), cfg


def test_offline_printer_returns_a_failed_preview(real_printer, monkeypatch):
    ctx, cfg = real_printer

    def offline(img, target):
        raise OSError("timed out")
    monkeypatch.setattr(printer, "_send_raster", offline)
    r = jobs.print_today(ctx, dry_run=False)
    assert r["status"] == "failed" and r["failed"] and r["sent_parts"] == 0
    assert r["note"] == "Kitchen QL didn't answer. Is it on? Tap Print on paper instead."
    assert "timed out" not in str(r) and "192.168" not in str(r)
    saved = ctx.store.get_printed(r["id"])
    assert saved["manifest"] == r["manifest"] and saved["failed"] and saved["dry_run"]
    assert ctx.store.png_path(r["id"]).exists()


def test_half_sent_stickers_keep_their_manifest(real_printer, monkeypatch):
    ctx, cfg = real_printer
    sent = []

    def flaky(img, target):
        if sent:
            raise OSError("timed out")
        sent.append(img)
    monkeypatch.setattr(printer, "_send_raster", flaky)
    r = jobs.print_stickers(ctx, task_ids=["1", "2", "3"], dry_run=False)
    assert r["status"] == "failed" and r["sent_parts"] == 1 and len(sent) == 1
    assert "after 1 of 3 stickers" in r["note"]
    saved = ctx.store.get_printed(r["id"])
    assert len(saved["manifest"]) == 3 and saved["dry_run"] is False  # sticker 1 is out there: readable


def test_offline_printer_is_a_200_through_the_api(ctx, monkeypatch):
    ctx.settings.printer_ip = "192.168.1.50"
    ctx.settings.dry_run = False
    ctx, login = site(ctx)
    monkeypatch.setattr(printer, "_send_raster", lambda img, target: (_ for _ in ()).throw(OSError("refused")))
    sam = login("sam", "sam password")
    sam.post("/list", json={"content": "Pay rent", "due_date": date.today().isoformat()})
    r = sam.post("/print/today", json={"dry_run": False})
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert sam.get(f"/printed/{r.json()['id']}/pdf").status_code == 200  # Print on paper still works


# --- E-06: no printer address in Home means previews, never an error ----------------------------
def test_home_without_printer_address_previews_with_a_note(ctx, monkeypatch):
    ctx = dataclasses.replace(ctx, printers=Printers(ctx.settings.data_dir, ctx.settings))
    ctx.settings.dry_run = False
    r = jobs.print_today(ctx, dry_run=False)
    assert r["status"] == "dry_run" and r["note"] == jobs.NO_PRINTER and r["printer"] is None
    admin = dataclasses.replace(ctx, can_add_printer=True)
    assert "Admin → Printers" in jobs.print_today(admin, dry_run=False)["note"]


def test_removed_env_printer_is_never_sent_to(ctx):
    ctx.settings.printer_ip = "10.0.0.9"
    ctx.settings.dry_run = False
    reg = Printers(ctx.settings.data_dir, ctx.settings)
    seeded = reg.all()
    assert [p.address for p in seeded] == ["10.0.0.9"]
    reg.remove(seeded[0].id)
    c = dataclasses.replace(ctx, printers=reg)
    r = jobs.print_today(c, dry_run=False)  # the no_real_printer fixture fails if this sends
    assert r["status"] == "dry_run" and r["note"] == jobs.NO_PRINTER


def test_server_uses_the_printer_registry(ctx):
    ctx.settings.dry_run = False
    ctx, login = site(ctx)
    sam, boss = login("sam", "sam password"), login("boss", "boss password")
    r = sam.post("/print/text", json={"text": "hi", "dry_run": False})
    assert r.status_code == 200 and r.json()["note"] == jobs.NO_PRINTER and r.json()["printer"] is None
    assert "Admin → Printers" in boss.post("/print/text", json={"text": "hi", "dry_run": False}).json()["note"]


# --- E-07: 10, 11 and 12 o'clock leave a gap before the task ---------------------------------------
def test_wide_times_leave_a_gap_before_the_task():
    img = render_day("Thursday", "Oct 15", [DayRow("Task", "12:48p")], "261015-ABCD").convert("L")
    time_w = render._font(28, True).getlength("12:48p")
    left = render.MARGIN + 4
    px = img.load()
    # The row band: find the time's first dark row, then look at the columns between time and task.
    top = next(y for y in range(200, img.height) if any(px[x, y] < 128 for x in range(left, left + 40)))
    gap = range(int(left + time_w) + 3, int(left + time_w) + 12)
    assert all(px[x, y] > 200 for x in gap for y in range(top, top + 30))


# --- E-08: emoji, Chinese and Hindi never print as empty boxes ----------------------------------------
def test_characters_no_font_has_are_left_out_not_boxed():
    assert render_label("Hi\U0010fffd there").tobytes() == render_label("Hi there").tobytes()


def test_cjk_uses_a_fallback_font_when_one_is_installed():
    font = render._font(31)
    other = render._font_for("买", font)
    if other is None:
        pytest.skip("no CJK font installed here")
    assert other is not font and render._runs("Buy 买菜", font)[-1][1] is other
    assert render._runs("Plain ASCII", font) is None


# --- E-09: a white or missing tag never looks like a second checkbox -----------------------------
def test_white_tags_draw_no_swatch():
    plain = render_day("Thu", "Oct 15", [DayRow("Milk")], "261015-ABCD")
    white = render_day("Thu", "Oct 15", [DayRow("Milk", color="white")], "261015-ABCD")
    green = render_day("Thu", "Oct 15", [DayRow("Milk", color="green")], "261015-ABCD")
    assert plain.tobytes() == white.tobytes() != green.tobytes()


# --- E-10 and E-16: list prints ------------------------------------------------------------------
def test_lists_get_a_box_for_every_item_and_typos_cost_nothing(ctx, todo):
    for i in range(12):
        todo.tasks[f"g{i}"] = {"id": f"g{i}", "content": f"Item {i}", "priority": 1, "due": None}
    r = jobs.print_filter(ctx, "#groceries", None)
    assert len(r["manifest"]) == 16 and r["title"] == "#groceries"
    todo.filter_tasks = lambda q: []
    with pytest.raises(ValueError, match="Nothing matches"):
        jobs.print_filter(ctx, "#nosuchtag")


def test_empty_filter_is_titled_my_list(ctx):
    assert jobs.print_filter(ctx, "", None)["title"] == "My list"


def test_filter_typo_is_a_400_with_help(ctx):
    ctx, login = site(ctx)
    sam = login("sam", "sam password")
    r = sam.post("/print/list", json={"query": "#nosuchtag", "dry_run": True})
    assert r.status_code == 400 and "Try today" in r.json()["detail"]
    assert sam.get("/printed").json() == []


# --- E-11 and E-12: one task -------------------------------------------------------------------
def test_one_task_ticket_has_a_numbered_box_and_plain_notes(ctx, todo, monkeypatch):
    cap = Capture(render_label)
    monkeypatch.setattr(jobs, "render_label", cap)
    todo.tasks["9"] = {"id": "9", "content": "Call **mom**", "priority": 1, "labels": ["call"],
                       "description": "See [the notes](http://a.b) " + "x " * 2000,
                       "due": {"date": "2026-09-28T13:00:00"}}
    r = jobs.print_task(ctx, "9")
    args, kwargs = cap.calls[-1]
    assert args[0] == "TASK" and args[1] == "today 1:00p"
    assert [(row.number, row.text, row.meta) for row in kwargs["rows"]] == [(1, "Call mom", "call")]
    assert kwargs["body"].startswith("See the notes") and "](" not in kwargs["body"]
    assert len(kwargs["body"]) < jobs.MAX_BODY + 40
    assert r["manifest"][0]["row"] == 1
    with pytest.raises(ValueError, match="Pick a task"):
        jobs.print_task(ctx, "  ")


# --- E-13 and E-14: paper ------------------------------------------------------------------------
def test_paper_pages_break_between_lines():
    img = Image.new("L", (1050, 6000), 255)
    for y in range(0, 6000, 50):  # a 30px "line" every 50px
        img.paste(0, (0, y, 1050, y + 30))
    usable = paper.PAGES["a5"][1] - 2 * paper.MARGIN
    end = paper._break_at(img, 0, usable)
    assert end <= usable and img.getpixel((10, end)) == 255
    pages = paper.single_pages(img, "a5", code="261015-ABCD")
    assert len(pages) >= 3


def test_a_ticket_a_little_too_long_shrinks_onto_one_page():
    usable = paper.PAGES["letter"][1] - 2 * paper.MARGIN
    img = Image.new("L", (paper.SINGLE_WIDTH, int(usable * 1.2)), 255)
    assert len(paper.single_pages(img, "letter")) == 1


def test_sticker_sheets_two_per_row_on_journal_pages_and_cut_lines_at_page_breaks():
    s = Image.new("L", (696, 300), 0)
    page = paper.sticker_sheets([s, s], "a5")
    assert len(page) == 1 and page[0].getpixel((paper.STICKER_MARGIN["a5"] + 696 + 40 + 10, 110)) == 0
    tall, short, taller = Image.new("L", (696, 2690), 0), Image.new("L", (696, 100), 0), Image.new("L", (696, 400), 0)
    pages = paper.sticker_sheets([tall, tall, tall, short, taller], "letter")
    assert len(pages) == 2
    # The short sticker's row was cut by the page break: it still has its dashed cut box.
    x0, y0 = paper.MARGIN - paper.GAP // 2, 150 + 2690 + 60 - paper.GAP // 2
    assert any(pages[0].getpixel((x0, y)) == 150 for y in range(y0, y0 + 24))


def test_a_single_sticker_keeps_its_part_for_real_size_paper(ctx):
    r = jobs.print_stickers(ctx, task_ids=["1"])
    assert len(ctx.store.get_parts(r["id"])) == 1


# --- E-15, E-17, E-19, E-26: what goes on the daily label ---------------------------------------------
def test_daily_rows_dates_slugs_and_split_extras(ctx, todo, monkeypatch):
    cap = Capture(render_day)
    monkeypatch.setattr(jobs, "render_day", cap)
    todo.tasks["f"] = {"id": "f", "content": "Plan dinner", "priority": 1, "due": {"date": "2026-09-29T15:00:00"}}
    todo.tasks["s"] = {"id": "s", "content": "Clean the garage and the attic and the shed", "priority": 1,
                       "due": {"date": "2026-09-27"}}
    for i in range(5):
        todo.tasks[f"x{i}"] = {"id": f"x{i}", "content": f"Chore {i}", "priority": 1, "due": {"date": "2026-09-28"}}
    c = dataclasses.replace(ctx, slides=lambda: {"s": 4}, wins=lambda: 3)
    jobs.print_filter(c, "all")
    rows = cap.calls[-1][0][2]
    plan = next(r for r in rows if r.text == "Plan dinner")
    assert plan.time == "" and plan.tag == "tomorrow 3:00p"  # never "3:00p" as if it were today
    jobs.print_today(c)
    waiting = cap.calls[-1][1]["waiting"]
    assert any(w.endswith("(slid 4×)") and len(w) <= 40 for w in waiting)
    c = dataclasses.replace(c, prefs={**c.prefs, "split_overdue": True})
    r = jobs.print_today(c)
    extra_kwargs, main_kwargs = cap.calls[-2][1], cap.calls[-1][1]
    assert r["also"][0]["title"] == "Waiting"
    assert extra_kwargs["win"] == "" and not extra_kwargs["journal"]
    assert main_kwargs["win"] == "Yesterday you finished 3. ✓" and main_kwargs["journal"]


# --- E-18: say why it's a preview -----------------------------------------------------------------
def test_preview_notes_say_why(real_printer):
    ctx, cfg = real_printer
    paused = dataclasses.replace(ctx, printing_paused=True)
    assert jobs.print_text(paused, "hi", dry_run=False)["note"] == "Printing is paused by an admin, so this is a preview."
    always = dataclasses.replace(ctx, prefs={**ctx.prefs, "always_dry_run": True})
    assert "Always preview only" in jobs.print_text(always, "hi", dry_run=False)["note"]
    assert "note" not in jobs.print_text(paused, "hi", dry_run=True)  # they asked for a preview anyway


# --- E-22 and E-23: stickers and strips ----------------------------------------------------------
def test_stickers_follow_next_step_and_size_prefs(ctx, todo):
    todo.tasks["1"]["steps"] = [{"text": "Find the number", "done": False}]
    stickers, _ = jobs._sticker_rows(ctx, [todo.tasks["1"]])
    assert stickers[0].next_step == "Find the number" and not stickers[0].big
    c = dataclasses.replace(ctx, prefs={**ctx.prefs, "show_next_step": False, "sticker_size": "big"})
    stickers, _ = jobs._sticker_rows(c, [todo.tasks["1"]])
    assert stickers[0].next_step == "" and stickers[0].big
    assert render.render_sticker(stickers[0], "261015-ABCD").height > 0


def test_strips_with_nothing_to_print_make_no_label(ctx, todo):
    todo.filter_tasks = lambda q: []
    with pytest.raises(ValueError, match="No tasks to print"):
        jobs.print_strips(ctx)
    assert ctx.store.list_printed() == []


# --- E-24: Add to my list + print understands Brain dump shortcuts -----------------------------------
def test_add_and_print_reads_brain_dump_shortcuts(ctx):
    ctx, login = site(ctx)
    sam = login("sam", "sam password")
    r = sam.post("/print/text", json={"text": "Call mom tomorrow 3pm #call !\nAsk about Sunday", "todo": True,
                                      "dry_run": True})
    assert r.status_code == 200
    task = next(t for t in sam.get("/list").json()["tasks"] if t["content"] == "Call mom")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert task["due"]["date"] == f"{tomorrow}T15:00:00" and task["labels"] == ["call"] and task["priority"] == 4
    assert task["description"] == "Ask about Sunday"
    assert any(t["name"] == "call" for t in sam.get("/tags").json()["tags"])


# --- E-27: small render fixes ----------------------------------------------------------------------
def test_minutes_read_naturally_and_ellipses_fit():
    assert [render._minutes(m) for m in (45, 60, 90, 125)] == ["45m", "1h", "1h 30m", "2h 5m"]
    d = render._Draw(Image.new("L", (10, 10)))
    font, lines = render._fit_text(d, "word " * 80, 400, (40, 34), 2)
    assert len(lines) == 2 and lines[-1].endswith("…") and d.textlength(lines[-1], font=font) <= 400
