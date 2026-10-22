"""Brother die-cut and round labels (the 4x6 DK-1241, 102 x 152 mm; 62 x 29 address labels, ...).

brother_ql refuses a die-cut image that isn't exactly the label's printable size, and Next Box draws
tickets of any height. So each ticket is fitted to the label: padded when it's short, shrunk onto one
label when it's a little too long, split over several labels (each saying "n/N  #code") when it's
longer, shrunk into the circle on a round label. Everything here builds the raster bytes in memory;
nothing is sent (the conftest fails any test that reaches the printer, and the tests that follow a
send swap in a stand-in that only records it).
"""
from __future__ import annotations

import dataclasses
import io
import struct

import pytest
from brother_ql.labels import FormFactor
from brother_ql.reader import chunker
from conftest import FakeTodoist
from PIL import Image, ImageDraw

from nextbox import jobs, printer
from nextbox.printer import BROTHER_LABELS, PrinterConfig
from nextbox.printers import Printers

DIE_CUT = ("62x29", "62x100", "29x90", "29x42", "17x54", "17x87", "23x23", "39x90", "39x48", "52x29",
           "102x51", "102x152", "d24", "d58")
WIDTHS = {i: w for i, _, w in BROTHER_LABELS}


def cfg_for(label: str, **kw) -> PrinterConfig:
    model = kw.pop("model", "QL-1110NWB")
    return PrinterConfig(id="t", name="OFFICE", driver="brother_ql", address="192.168.68.110",
                         width_px=WIDTHS.get(label, 696), model=model, label=label, **kw)


def ticket(width: int, height: int, mode: str = "1", rule: int = 1) -> Image.Image:
    """A stand-in ticket: blocks of "text" 30 rows tall with 20 white rows between them, and a full
    width rule (`rule` rows thick) under every third block, like the dotted line under a list's rows."""
    img = Image.new("L", (width, height), 255)
    d = ImageDraw.Draw(img)
    for n, y in enumerate(range(10, height - 40, 50)):
        d.rectangle((width // 10 + 4, y, width // 2, y + 29), fill=0)
        if n % 3 == 2:
            d.rectangle((0, y + 36, width - 1, y + 35 + rule), fill=0)
    return img.convert(mode) if mode != "L" else img


def decode(data: bytes) -> list[dict]:
    """Each page of a raster job: the ESC i z media fields, and how many raster rows it sends."""
    pages, cur, ends = [], None, []
    for ins in chunker(data, raise_exception=True):
        if ins.startswith(b"\x1b\x69\x7a"):  # ESC i z: print media and quality
            assert cur is None
            cur = {"media": ins[4], "width_mm": ins[5], "length_mm": ins[6],
                   "lines": struct.unpack("<L", ins[7:11])[0], "rows": 0}
        elif ins[:1] in (b"\x67", b"\x77", b"\x47"):  # one raster row
            cur["rows"] += 1
        elif ins in (b"\x0c", b"\x1a"):  # FF: next page, SUB: last page
            pages.append(cur)
            ends.append(ins)
            cur = None
    assert cur is None and ends and ends[-1] == b"\x1a" and b"\x1a" not in ends[:-1]
    return pages


def ink_runs(page: Image.Image) -> list[tuple[int, int]]:
    """(first row, rows) of each run of rows with any ink in them."""
    g = page.convert("L")
    inky = [min(g.crop((0, y, g.width, y + 1)).tobytes()) < 128 for y in range(g.height)]
    runs, start = [], None
    for y, on in enumerate(inky + [False]):
        if on and start is None:
            start = y
        elif not on and start is not None:
            runs.append((start, y - start))
            start = None
    return runs


# --- which labels are die-cut ---------------------------------------------------------------------
def test_every_die_cut_and_round_label_is_handled():
    found = {i for i, _, _ in BROTHER_LABELS if printer.die_cut_spec(cfg_for(i))}
    assert found == set(DIE_CUT)
    for label in DIE_CUT:  # the label's printable width is the one Admin → Printers suggests
        assert printer.brother_label(label).dots_printable[0] == WIDTHS[label]
    assert printer.die_cut_spec(cfg_for("62")) is None and printer.die_cut_spec(cfg_for("102")) is None
    assert printer.die_cut_spec(dataclasses.replace(cfg_for("62x29"), driver="escpos")) is None


def test_the_reported_4x6_failure_now_prints():
    """Mike's QL-1110NWB with a 4x6 roll: brother_ql used to say
    "Bad image dimensions: (1164, 500). Expecting: (1164, 1660).\""""
    img = Image.new("1", (1164, 500), 1)
    with pytest.raises(ValueError, match="Bad image dimensions"):
        old_brother_bytes(img, cfg_for("102x152"))
    [page] = decode(printer.brother_bytes(img, cfg_for("102x152")))
    assert page == {"media": 0x0B, "width_mm": 102, "length_mm": 153, "lines": 1660, "rows": 1660}


# --- every die-cut label: short, exact and tall tickets ---------------------------------------------
@pytest.mark.parametrize("label", DIE_CUT)
@pytest.mark.parametrize("size", ["short", "exact", "tall"])
def test_die_cut_tickets_convert_to_whole_labels(label, size):
    spec = printer.brother_label(label)
    w, h = spec.dots_printable
    height = {"short": max(20, h // 3), "exact": h, "tall": h * 2 + h // 2}[size]
    cfg = cfg_for(label)
    img = ticket(w, height)
    data = printer.brother_bytes(img, cfg)
    pages = decode(data)
    expected = printer.label_pages(img, cfg)
    assert len(pages) == len(expected) >= 1
    if spec.form_factor == FormFactor.ROUND_DIE_CUT or size != "tall":
        assert len(pages) == 1
    else:
        assert len(pages) >= 2
    for page in pages:
        assert page["media"] == 0x0B  # die-cut, not continuous (0x0A)
        assert (page["width_mm"], page["length_mm"]) == spec.tape_size
        assert page["lines"] == page["rows"] == h
    for p in expected:
        assert p.size == (w, h) and p.mode == "1"


@pytest.mark.parametrize("label", [lab for lab in DIE_CUT if not lab.startswith("d")])
def test_a_short_ticket_sits_at_the_top_of_its_label(label):
    w, h = printer.brother_label(label).dots_printable
    img = Image.new("1", (w, max(10, h // 4)), 1)
    ImageDraw.Draw(img).rectangle((0, 0, w - 1, 4), fill=0)
    [page] = printer.label_pages(img, cfg_for(label))
    assert page.size == (w, h)
    assert ink_runs(page) == [(0, 5)]  # the ink at the very top, white below it


@pytest.mark.parametrize("label", ["102x152", "62x29", "62x100", "29x42", "17x54"])
@pytest.mark.parametrize("mode", ["1", "L", "RGB"])
def test_a_long_ticket_splits_between_lines_never_through_one(label, mode):
    w, h = printer.brother_label(label).dots_printable
    img = ticket(w, h * 3 + 77, mode)
    whole = ink_runs(img)
    pages = printer.label_pages(img, cfg_for(label), code="260928-DL28")
    assert len(pages) >= 3 and all(p.size == (w, h) for p in pages)
    body = h - printer.stamp_height(w)  # below it, each label's "n/N  #code"
    pieces = [run for p in pages for run in ink_runs(p.crop((0, 0, w, body)))]
    # Every block and rule of the ticket comes out whole, in order, on exactly one label.
    assert [rows for _, rows in pieces] == [rows for _, rows in whole]


def test_white_below_the_last_line_does_not_cost_a_label():
    w, h = printer.brother_label("102x152").dots_printable
    img = Image.new("1", (w, h + 300), 1)
    ImageDraw.Draw(img).rectangle((100, 100, 600, h - 50), fill=0)
    assert len(printer.label_pages(img, cfg_for("102x152"))) == 1


def test_a_blank_ticket_is_one_blank_label():
    for label in ("102x152", "d58"):
        w, h = printer.brother_label(label).dots_printable
        [page] = printer.label_pages(Image.new("1", (w, 5000), 1), cfg_for(label))
        assert page.size == (w, h) and ink_runs(page) == []


@pytest.mark.parametrize("width", [696, 2000])
def test_a_ticket_of_another_width_is_scaled_to_the_label(width):
    w, h = printer.brother_label("102x152").dots_printable
    img = ticket(width, 600)
    [page] = printer.label_pages(img, cfg_for("102x152"))
    assert page.size == (w, h)
    _, _, right, bottom = printer._ink_box(page)
    assert right == pytest.approx(w, abs=3)  # the full-width rule still reaches across the label
    assert bottom == pytest.approx(printer._ink_box(img)[3] * w / width, abs=4)  # aspect kept
    pages = decode(printer.brother_bytes(img, cfg_for("102x152")))
    assert [p["rows"] for p in pages] == [h]


@pytest.mark.parametrize("label", ["d24", "d58"])
def test_a_round_label_gets_the_ticket_centred_inside_its_circle(label):
    w, h = printer.brother_label(label).dots_printable
    img = Image.new("1", (WIDTHS[label], 900), 1)
    ImageDraw.Draw(img).rectangle((30, 40, WIDTHS[label] - 20, 700), fill=0)  # off-centre on the ticket
    [page] = printer.label_pages(img, cfg_for(label))
    assert page.size == (w, h)
    left, top, right, bottom = printer._ink_box(page)
    r = w / 2
    for x, y in ((left, top), (right, top), (left, bottom), (right, bottom)):  # all four corners inside
        assert (x - r) ** 2 + (y - r) ** 2 <= r * r
    assert abs((left + right) / 2 - r) <= 2 and abs((top + bottom) / 2 - r) <= 2
    assert bottom - top >= w * 0.65  # as big as fits


def test_the_job_has_one_page_per_label():
    cfg = cfg_for("62x29")
    img = ticket(WIDTHS["62x29"], 1000)
    pages = printer.label_pages(img, cfg)
    assert len(decode(printer.brother_bytes(img, cfg))) == len(pages) >= 2


# --- a split ticket: every label can be snapped and read back on its own ------------------------------
def footer(page: Image.Image, side: str) -> Image.Image:
    """The bottom strip a split ticket's label keeps for "n/N" (left half) and "#code" (right half)."""
    w, h = page.size
    top = h - printer.stamp_height(w)
    return page.crop((0, top, w // 2, h) if side == "left" else (w // 2, top, w, h))


def blank(img: Image.Image) -> bool:
    return printer._ink_box(img) is None


@pytest.mark.parametrize("label", [lab for lab in DIE_CUT if not lab.startswith("d")])
def test_every_label_of_a_split_ticket_says_which_label_and_the_code(label):
    w, h = printer.brother_label(label).dots_printable
    img = printer.with_code(ticket(w, h * 3 + 77), "260928-DL28")
    pages = printer.label_pages(img, cfg_for(label))  # the code comes with the image, as jobs sends it
    assert len(pages) >= 3
    plain = printer.label_pages(ticket(w, h * 3 + 77), cfg_for(label), code="")
    other = printer.label_pages(img, cfg_for(label), code="261003-ZZ9Q")
    for n, page in enumerate(pages):
        assert not blank(footer(page, "left")) and not blank(footer(page, "right"))
        # "#code" is the same on every label, and it's this ticket's code.
        assert footer(page, "right").tobytes() == footer(pages[0], "right").tobytes()
        assert footer(other[n], "right").tobytes() != footer(page, "right").tobytes()
        # Without a code: only "n/N". The ticket itself is the same either way, above the footer.
        assert blank(footer(plain[n], "right")) and not blank(footer(plain[n], "left"))
        body = (0, 0, w, h - printer.stamp_height(w))
        assert page.crop(body).tobytes() == plain[n].crop(body).tobytes()
        # Nothing runs off the label: the stamp keeps clear of both edges.
        left, _, right, bottom = printer._ink_box(page.crop((0, h - printer.stamp_height(w), w, h)))
        assert left >= 2 and right <= w - 2 and bottom <= printer.stamp_height(w) - 2
    # "1/N", "2/N", ...: each label's number is different.
    assert len({footer(p, "left").tobytes() for p in pages}) == len(pages)


def test_a_ticket_on_one_label_has_no_stamp():
    w, h = printer.brother_label("102x152").dots_printable
    for height in (h // 2, h, round(h * 1.2)):  # its own #code is on it already
        img = ticket(w, height)
        [page] = printer.label_pages(img, cfg_for("102x152"), code="260928-DL28")
        assert page.tobytes() == printer.label_pages(img, cfg_for("102x152"), code="")[0].tobytes()


@pytest.mark.parametrize("label", ["102x152", "62x29", "62x100", "17x54"])
def test_a_ticket_a_little_too_long_is_shrunk_onto_one_label(label):
    """As Print on paper does (paper.single_pages): up to 30% too long is shrunk, not split, so the
    ticket stays whole, with its #code, on one label."""
    w, h = printer.brother_label(label).dots_printable
    img = ticket(w, round(h * 1.25), rule=2)  # tickets draw their lines 2 px or more
    _, top, _, bottom = printer._ink_box(img)  # white below the last line isn't kept
    assert h < bottom <= h * printer.SHRINK_ONTO_ONE
    [page] = printer.label_pages(img, cfg_for(label), code="260928-DL28")
    assert page.size == (w, h) and page.mode == "1"
    left, ptop, right, pbottom = printer._ink_box(page)
    assert h - 2 * 16 * w / 696 <= pbottom < h  # down to the bottom, less the ticket's own white edge
    scale = pbottom / bottom
    assert (ptop, pbottom - ptop) == pytest.approx((top * scale, (bottom - top) * scale), abs=4)  # aspect kept
    assert abs((left + right) / 2 - w / 2) <= 3  # centred across the label
    assert len(ink_runs(page)) == len(ink_runs(img))  # every line and rule, none cut or lost
    longer = ticket(w, round(h * 1.5), rule=2)
    assert printer._ink_box(longer)[3] > h * printer.SHRINK_ONTO_ONE
    assert len(printer.label_pages(longer, cfg_for(label))) >= 2  # more than 30% too long: split
    assert len(decode(printer.brother_bytes(img, cfg_for(label)))) == 1


# --- continuous rolls: exactly the bytes they were before ------------------------------------------
def old_brother_bytes(img: Image.Image, cfg: PrinterConfig) -> bytes:
    """printer.brother_bytes before die-cut labels were fitted (0.14.6), for the golden comparison."""
    from brother_ql.conversion import convert
    from brother_ql.raster import BrotherQLRaster

    qlr = BrotherQLRaster(cfg.model)
    qlr.exception_on_warning = True
    red = cfg.ink == "black_red"
    return convert(qlr=qlr, images=[img.convert("RGB")], label=cfg.label + ("red" if red and not cfg.label.endswith("red") else ""),
                   rotate="0", threshold=70.0, dither=False, compress=True, red=red, dpi_600=False,
                   hq=True, cut=cfg.cut)


@pytest.mark.parametrize("label,model,ink", [("62", "QL-1110NWB", "black"), ("102", "QL-1110NWB", "black"),
                                             ("29", "QL-700", "black"), ("62", "QL-820NWB", "black_red"),
                                             ("pt24", "PT-P750W", "black")])
@pytest.mark.parametrize("height", [120, 2600])
def test_continuous_rolls_are_unchanged_byte_for_byte(label, model, ink, height):
    cfg = cfg_for(label, model=model, ink=ink)
    img = ticket(cfg.width_px, height, "RGB" if ink == "black_red" else "1")
    if ink == "black_red":
        ImageDraw.Draw(img).rectangle((50, 20, 200, 60), fill=(255, 0, 0))
    assert printer.label_pages(img, cfg) == [img]
    assert printer.brother_bytes(img, cfg) == old_brother_bytes(img, cfg)
    for page in decode(printer.brother_bytes(img, cfg)):
        assert page["media"] in (0x0A, 0x00) and page["length_mm"] == 0


def test_continuous_roll_with_a_narrower_ticket_is_unchanged():
    cfg = cfg_for("102")
    img = ticket(696, 400)  # brother_ql scales a continuous ticket itself, as before
    assert printer.brother_bytes(img, cfg) == old_brother_bytes(img, cfg)


# --- through the print path: preview, saved image, and the error message -----------------------------
@pytest.fixture
def brother(ctx, tmp_path):
    def make(label: str, model: str = "QL-1110NWB"):
        reg = Printers(tmp_path / "reg", ctx.settings)
        cfg = reg.add({"name": "OFFICE", "driver": "brother_ql", "address": "192.168.68.110",
                       "model": model, "label": label, "width_px": WIDTHS[label]})  # as Admin → Printers sends it
        return dataclasses.replace(ctx, printers=reg), cfg
    return make


def saved_image(c, r) -> Image.Image:
    return Image.open(io.BytesIO(c.store.get_png(r["id"])))


def test_preview_of_a_4x6_label_is_the_whole_label(brother):
    c, cfg = brother("102x152")
    assert cfg.width_px == 1164
    r = jobs.print_text(c, "Pick up the parcel", dry_run=True)
    assert r["status"] == "dry_run"
    img = saved_image(c, r)
    assert img.size == (1164, 1660)
    assert ink_runs(img)[0][0] < 200 and ink_runs(img)[-1][0] < 1000  # the note at the top, white below


def test_preview_of_a_long_ticket_on_small_labels_shows_each_label(brother):
    c, _ = brother("62x29")
    r = jobs.print_today(c, dry_run=True)
    img = saved_image(c, r)
    w, h = printer.brother_label("62x29").dots_printable
    n = (img.height + 26) // (h + 26)
    assert n >= 2 and img.height == n * h + (n - 1) * 26 and img.width == w


def test_preview_on_a_continuous_roll_is_unchanged(brother):
    c, _ = brother("62")
    r = jobs.print_text(c, "Pick up the parcel", dry_run=True)
    img = saved_image(c, r)
    assert img.width == 696 and img.height < 1660


def test_wide_stickers_preview_is_not_cropped(brother):
    c, _ = brother("102")
    r = jobs.print_stickers(c, task_ids=["1", "2"], dry_run=True)
    assert saved_image(c, r).width == 1164


def busy_day(c, rows: int):
    """`rows` tasks due today, all on the Today ticket."""
    tasks = [{"id": str(n), "content": f"Task number {n} with some words", "priority": 1,
              "due": {"date": "2026-09-28"}} for n in range(rows)]
    todo = FakeTodoist(tasks)
    return dataclasses.replace(c, tasks_factory=lambda: todo, prefs={**c.prefs, "max_rows": rows})


def test_an_8_row_today_ticket_is_one_4x6_label_with_its_code(brother, monkeypatch):
    """The checker's case: 8 rows drew 1771 rows, 4% too tall for a 4x6 label (1660), and came out on
    two labels with the #code only on the second, so the label with the tick boxes couldn't be read back."""
    c, _ = brother("102x152")
    c = busy_day(c, 8)
    drawn = []
    real = printer.label_pages
    monkeypatch.setattr(printer, "label_pages", lambda img, cfg, code=None: drawn.append(img.height) or real(img, cfg, code))
    r = jobs.print_today(c, dry_run=True)
    assert len(r["manifest"]) == 8
    assert drawn and 1660 < drawn[0] <= 1660 * printer.SHRINK_ONTO_ONE
    img = saved_image(c, r)
    assert img.size == (1164, 1660)  # one label: the tick boxes and the #code together
    assert printer._ink_box(img)[3] > 1600  # the ticket's last line (its code) is on it


def test_every_label_of_a_split_ticket_has_its_code_in_the_preview_and_the_job(brother, monkeypatch):
    c, cfg = brother("62x29")
    c = busy_day(c, 8)
    c.settings.dry_run = False
    sent = []

    def record(img, target):  # stands in for the printer: keeps the labels the job would print
        sent.append(printer.label_pages(img, target))
    monkeypatch.setattr(printer, "_send_raster", record)
    r = jobs.print_today(c, dry_run=False)
    assert r["status"] == "printed"
    [labels] = sent
    w, h = printer.brother_label("62x29").dots_printable
    code = footer(printer.label_pages(ticket(w, h * 3), cfg, code=r["id"])[0], "right").tobytes()
    assert len(labels) >= 4
    assert all(footer(lab, "right").tobytes() == code for lab in labels)  # "#<this ticket's code>" on each
    preview = saved_image(c, r).convert("1")
    shown = [preview.crop((0, n * (h + 26), w, n * (h + 26) + h)) for n in range((preview.height + 26) // (h + 26))]
    assert [p.tobytes() for p in shown] == [lab.tobytes() for lab in labels]  # the preview is what prints


def test_a_brother_printer_that_did_not_answer_mentions_the_roll(brother, monkeypatch):
    c, _ = brother("102x152")
    c.settings.dry_run = False

    def refused(img, target):
        raise ConnectionRefusedError("[Errno 111] Connection refused")
    monkeypatch.setattr(printer, "_send_raster", refused)
    r = jobs.print_text(c, "Pick up the parcel", dry_run=False)
    assert r["status"] == "failed" and r["error"] == "ConnectionRefusedError"
    assert r["note"] == ("OFFICE didn't answer. Is it on? Tap Print on paper instead. If its light is flashing "
                         "red, the label roll may not match the Label size in Admin → Printers (this printer: "
                         "102 × 152 mm labels (QL-1000 series)). Switch it off and on to clear it.")
    assert "192.168" not in str(r) and "Errno" not in str(r)


def test_the_roll_hint_is_for_brother_printers_only():
    assert printer.trouble_hint(dataclasses.replace(cfg_for("62"), driver="escpos")) == ""
    assert "62 mm continuous (DK-22205)" in printer.trouble_hint(cfg_for("62"))
    # A label id that isn't in the list (an old .env value) is never echoed.
    hint = printer.trouble_hint(cfg_for("nonsense-secret"))
    assert "nonsense" not in hint and "flashing red" in hint
