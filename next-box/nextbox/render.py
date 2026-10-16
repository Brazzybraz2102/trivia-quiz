"""Draw labels for 62 mm continuous tape (696 px printable width at 300 dpi)."""
from __future__ import annotations

import contextlib
import contextvars
import functools
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 696   # tickets are drawn at this width, then fitted to each printer (see finalize)
MARGIN = 16
ACCENT = (220, 0, 0)  # red ink on two-color printers; prints black everywhere else
FONT_DIRS = [
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/dejavu-sans-fonts"),
    Path("/usr/share/fonts/TTF"),
]


# Easy-to-read text (the reading_mode pref): Atkinson Hyperlegible (SIL OFL, see web/fonts/OFL.txt),
# the same copy the web app uses. jobs._finish draws a ticket inside reading_font(True); a font file
# that's missing falls back to DejaVu, and symbols it lacks (✓ → ✗ ↻) come from DejaVu too.
READING_DIR = Path(__file__).resolve().parent.parent / "web" / "fonts"
READING_FONTS = ("AtkinsonHyperlegible-Regular.ttf", "AtkinsonHyperlegible-Bold.ttf")
_reading = contextvars.ContextVar("nextbox_reading_font", default=False)


@contextlib.contextmanager
def reading_font(on: bool = True):
    token = _reading.set(bool(on))
    try:
        yield
    finally:
        _reading.reset(token)


def _dejavu(bold: bool = False) -> str | None:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return next((str(d / name) for d in FONT_DIRS if (d / name).exists()), None)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    if _reading.get():
        path = READING_DIR / READING_FONTS[bool(bold)]
        if path.exists():
            return ImageFont.truetype(str(path), size)
    path = _dejavu(bold)
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


# Headings printed in capitals are harder to read with dyslexia: with Easy-to-read text they're in sentence case.
_HEADINGS = {"SCHEDULE": "Schedule", "JUST ONE THING": "Just one thing", "TASK": "Task", "NEW TASK": "New task",
             "NOTE": "Note"}


def _heading(text: str) -> str:
    return _HEADINGS.get(text, text) if _reading.get() else text


# --- characters DejaVu doesn't have ------------------------------------------------------------
# Emoji, Chinese/Japanese and Hindi come from the first of these fonts that has them (regular,
# bold). Anything no installed font has is left out, so it never prints as an empty box.
FALLBACK_FONTS = [
    ("NotoSansCJK-Regular.ttc", "NotoSansCJK-Bold.ttc"), ("NotoSansCJKjp-Regular.otf", "NotoSansCJKjp-Bold.otf"),
    ("wqy-zenhei.ttc", ""), ("wqy-microhei.ttc", ""), ("ipag.ttf", ""), ("fonts-japanese-gothic.ttf", ""),
    ("DroidSansFallbackFull.ttf", ""),
    ("NotoSansDevanagari-Regular.ttf", "NotoSansDevanagari-Bold.ttf"), ("Lohit-Devanagari.ttf", ""),
    ("NotoEmoji-Regular.ttf", "NotoEmoji-Bold.ttf"), ("Symbola.ttf", ""), ("Symbola_hint.ttf", ""),
    ("NotoSansSymbols2-Regular.ttf", ""), ("NotoSansSymbols-Regular.ttf", "NotoSansSymbols-Bold.ttf"),
    ("FreeSans.ttf", "FreeSansBold.ttf"), ("unifont.otf", ""), ("unifont_upper.otf", ""),
]
FONT_ROOTS = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".local/share/fonts",
              Path.home() / ".fonts"]
_DROP = {"‍", "‌", "︎", "️"}  # joiners and emoji/text selectors: nothing to print


@functools.lru_cache(maxsize=1)
def _installed() -> dict[str, str]:
    found: dict[str, str] = {}
    for root in FONT_ROOTS:
        for folder, _, files in os.walk(root):
            for name in files:
                found.setdefault(name, os.path.join(folder, name))
    return found


@functools.lru_cache(maxsize=None)
def _probe(path: str):
    """A plain-layout copy of a font and its "no such character" glyph, to see what it can draw."""
    try:
        f = ImageFont.truetype(path, 24, layout_engine=ImageFont.Layout.BASIC)
        m = f.getmask("\U0010ffff")
        return f, (m.size, bytes(m))
    except OSError:
        return None


@functools.lru_cache(maxsize=20000)
def _covers(path: str, ch: str) -> bool:
    probe = _probe(path)
    if probe is None:
        return False
    try:
        m = probe[0].getmask(ch)
    except Exception:  # noqa: BLE001 - a font that can't draw it doesn't have it
        return False
    return (m.size, bytes(m)) != probe[1]


@functools.lru_cache(maxsize=256)
def _sized(path: str, size: int):
    return ImageFont.truetype(path, size)


def _font_for(ch: str, font):
    if ch.isspace() or _covers(font.path, ch):
        return font
    bold = "Bold" in os.path.basename(font.path)
    plain = _dejavu(bold)  # the reading font lacks the legend's symbols: DejaVu has them
    if plain and plain != font.path and _covers(plain, ch):
        return _sized(plain, int(font.size))
    have = _installed()
    for regular, heavy in FALLBACK_FONTS:
        path = have.get(heavy) if bold and heavy else None
        path = path or have.get(regular)
        if path and _covers(path, ch):
            return _sized(path, int(font.size))
    return None


def _runs(text: str, font) -> list[tuple[str, object]] | None:
    """Split text into (piece, font) runs. None means the font has every character (the usual case)."""
    if text.isascii() or not isinstance(getattr(font, "path", None), str):
        return None
    runs: list[list] = []
    for ch in text:
        if ch in _DROP or 0x1F3FB <= ord(ch) <= 0x1F3FF:  # skin tones: no color on a label
            continue
        if runs and unicodedata.category(ch) in ("Mn", "Mc", "Me"):  # accents and vowel signs stay
            runs[-1][0] += ch                                        # with their letter's font
            continue
        f = _font_for(ch, font)
        if f is None:
            continue
        if runs and runs[-1][1] is f:
            runs[-1][0] += ch
        else:
            runs.append([ch, f])
    if len(runs) == 1 and runs[0][1] is font and runs[0][0] == text:
        return None
    return [(t, f) for t, f in runs]


class _Draw(ImageDraw.ImageDraw):
    """ImageDraw that borrows other fonts for characters the main font lacks."""

    def textlength(self, text, font=None, *args, **kwargs):
        runs = _runs(text, font) if not args and not kwargs else None
        if runs is None:
            return super().textlength(text, font, *args, **kwargs)
        return sum(super(_Draw, self).textlength(t, f) for t, f in runs)

    def text(self, xy, text, fill=None, font=None, *args, **kwargs):
        runs = _runs(text, font) if not args and not kwargs else None
        if runs is None:
            return super().text(xy, text, fill, font, *args, **kwargs)
        x, y = xy
        base = y + font.getmetrics()[0]  # every piece sits on the main font's baseline
        for t, f in runs:
            super().text((x, base), t, fill, f, anchor="ls")
            x += super().textlength(t, f)


def _ellipsize(d: ImageDraw.ImageDraw, line: str, font, width: float) -> str:
    """End a cut-short line with "…", making sure the "…" still fits."""
    line = line.rstrip(" ·") + "…"
    while len(line) > 1 and d.textlength(line, font=font) > width:
        line = line[:-2].rstrip(" ·") + "…"
    return line


@dataclass
class Row:
    number: int  # printed next to the checkbox; what read-back matches on
    text: str
    meta: str = ""  # e.g. "p1", "overdue", "↻"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        words = para.split()
        line = ""
        for word in words:
            trial = f"{line} {word}".strip()
            if draw.textlength(trial, font=font) <= max_width:
                line = trial
                continue
            if line:
                lines.append(line)
            # hard-break words longer than a full line: binary search, so even a huge one is quick
            while len(word) > 1 and (len(word) > 400 or draw.textlength(word, font=font) > max_width):
                lo, hi = 1, min(len(word) - 1, 400)  # no line holds 400 characters
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if draw.textlength(word[:mid], font=font) <= max_width:
                        lo = mid
                    else:
                        hi = mid - 1
                lines.append(word[:lo])
                word = word[lo:]
            line = word
        lines.append(line)
    return lines


def finalize(img: Image.Image, width: int = WIDTH, red: bool = False) -> Image.Image:
    """Fit a drawn ticket to a printer's dot width. 1-bit, or black/red/white for two-color rolls."""
    if img.width != width:
        img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    rgb = img.convert("RGB")
    if not red:
        return rgb.convert("L").point(lambda p: 0 if p < 128 else 255, mode="1")
    out = Image.new("RGB", rgb.size, "white")
    src, dst = rgb.load(), out.load()
    for y in range(rgb.height):
        for x in range(rgb.width):
            r, g, b = src[x, y]
            if r > 150 and g < 110 and b < 110:
                dst[x, y] = (255, 0, 0)
            elif (r + g + b) / 3 < 128:
                dst[x, y] = (0, 0, 0)
    return out


def render_label(title: str, subtitle: str = "", rows: list[Row] | None = None,
                 body: str = "", code: str = "", legend: bool = True,
                 accent_subtitle: bool = False, _height: int = 4000) -> Image.Image:
    """Draw a ticket at WIDTH; pass it to finalize() for a printer. Rows get numbered checkboxes."""
    title_f, sub_f, row_f, meta_f, small_f = _font(46, True), _font(26), _font(34), _font(24, True), _font(20)
    canvas = Image.new("RGB", (WIDTH, _height), "white")
    d = _Draw(canvas)
    y = MARGIN
    for line in _wrap(d, _heading(title), title_f, WIDTH - 2 * MARGIN):
        d.text((MARGIN, y), line, font=title_f, fill=0)
        y += 54
    if subtitle:
        d.text((MARGIN, y), subtitle, font=sub_f, fill=ACCENT if accent_subtitle else 0)
        y += 34
    y += 6
    d.line((MARGIN, y, WIDTH - MARGIN, y), fill=0, width=4)
    y += 16

    box = 34
    num_w = 50
    text_x = MARGIN + box + 14 + num_w
    for row in rows or []:
        top = y
        d.rectangle((MARGIN, top + 2, MARGIN + box, top + 2 + box), outline=0, width=4)
        d.text((MARGIN + box + 12, top), f"{row.number:>2}", font=row_f, fill=0)
        reserve = d.textlength(row.meta, font=meta_f) + 12 if row.meta else 0
        lines = _wrap(d, row.text, row_f, WIDTH - MARGIN - text_x - int(reserve))
        for i, line in enumerate(lines):
            d.text((text_x, y), line, font=row_f, fill=0)
            if i == 0 and row.meta:
                w = d.textlength(row.meta, font=meta_f)
                d.text((WIDTH - MARGIN - w, y + 6), row.meta, font=meta_f, fill=0)
            y += 42
        y += 12
    if rows == []:
        d.text((MARGIN, y), "Nothing due. Nice.", font=row_f, fill=0)
        y += 50

    if body:
        for line in _wrap(d, body, row_f, WIDTH - 2 * MARGIN):
            d.text((MARGIN, y), line, font=row_f, fill=0)
            y += 42
        y += 8

    if rows and legend:
        y += 4
        d.line((MARGIN, y, WIDTH - MARGIN, y), fill=0, width=2)
        y += 10
        d.text((MARGIN, y), "✓ done    → move    ✗ drop", font=small_f, fill=0)
        y += 28
    if code:
        w = d.textlength(f"#{code}", font=meta_f)
        d.text((WIDTH - MARGIN - w, y), f"#{code}", font=meta_f, fill=0)
        y += 32
    y += MARGIN
    if y > _height:  # longer than the canvas: draw it again on one that fits (never crop into black)
        return render_label(title, subtitle, rows, body, code, legend, accent_subtitle, _height=y)
    return canvas.crop((0, 0, WIDTH, y))


@dataclass
class DayRow:
    text: str
    time: str = ""        # "9:00a"; blank rows get a write-in line
    urgent: bool = False  # p1/p2: bold with a leading "!"
    tag: str = ""         # "overdue 2d", "↻"
    late: bool = False    # overdue: its tag is drawn in the accent (red) color
    color: str = ""       # its tag's color: a small patterned square by the checkbox
    note: str = ""        # the next tiny step, printed small under the task


def _dotted(d: ImageDraw.ImageDraw, y: int, x0: int, x1: int) -> None:
    for x in range(x0, x1, 8):
        d.line((x, y, x + 3, y), fill=0, width=2)


def render_day(day_name: str, date_text: str, rows: list[DayRow], code: str, footer: str = "",
               subtitle: str = "", waiting: list[str] | None = None, accent: bool = False,
               waiting_head: str = "Also waiting", win: str = "",
               schedule: list[tuple[str, str]] | None = None, journal: bool = False,
               _height: int = 5000) -> Image.Image:
    """The daily sheet: big day name, legend on top, time column, checkboxes on the right.
    Read-back matches rows top to bottom against the manifest, so row order is the contract."""
    day_f, date_f, sub_f = _font(84, True), _font(40, True), _font(28)
    legend_f, time_f = _font(28), _font(28, True)
    text_f, text_bold_f, tag_f = _font(31), _font(31, True), _font(22)
    wait_bold_f, wait_f, code_f, foot_f = _font(25, True), _font(25), _font(26, True), _font(20)

    canvas = Image.new("RGB", (WIDTH, _height), "white")
    d = _Draw(canvas)
    left, right = MARGIN + 4, WIDTH - MARGIN - 4
    y = MARGIN + 6

    dw = d.textlength(date_text, font=date_f)
    size = 84
    while size > 48 and d.textlength(day_name, font=day_f) + dw + 24 > right - left:
        size -= 4
        day_f = _font(size, True)
    if d.textlength(day_name, font=day_f) + dw + 24 <= right - left:
        d.text((left, y), day_name, font=day_f, fill=0)
        base = y + int(size * 0.98)  # approximate baseline, so the date sits on the same line
        d.text((right - dw, base - 40), date_text, font=date_f, fill=0)
        y += int(size * 1.2)
    else:  # a long list title: up to two lines of it, then the date on its own line
        title_lines = _wrap(d, day_name, day_f, right - left)
        if len(title_lines) > 2:
            title_lines = [title_lines[0], _ellipsize(d, title_lines[1], day_f, right - left)]
        for line in title_lines:
            d.text((left, y), line, font=day_f, fill=0)
            y += int(size * 1.12)
        d.text((left, y), date_text, font=date_f, fill=0)
        y += 54
    if subtitle:
        d.text((left, y), subtitle, font=sub_f, fill=0)
        y += 38
    if schedule:
        # Today's calendar: no boxes (events aren't tasks), time on the left like the task rows.
        d.line((left, y, right, y), fill=0, width=4)
        y += 8
        sched_head, sched_time, sched_f = _font(22, True), _font(26, True), _font(28)
        d.text((left, y), _heading("SCHEDULE"), font=sched_head, fill=0)
        y += 32
        for when, what in schedule:
            if when:
                d.text((left, y + 2), when, font=sched_time if when != "all day" else _font(20, True), fill=0)
            what_lines = _wrap(d, what, sched_f, right - left - 112)
            if len(what_lines) > 2:
                what_lines = [what_lines[0], _ellipsize(d, what_lines[1], sched_f, right - left - 112)]
            for i, line in enumerate(what_lines):
                d.text((left + 112, y), line, font=sched_f, fill=0)
                y += 34
            y += 4
        y += 4
    d.line((left, y, right, y), fill=0, width=4)
    y += 10
    legend = "✓ done    → move    ✗ drop"
    lw = d.textlength(legend, font=legend_f)
    d.text(((WIDTH - lw) / 2, y), legend, font=legend_f, fill=0)
    y += 40
    d.line((left, y, right, y), fill=0, width=2)
    y += 12

    box = 44
    box_x = right - box
    swatch = 30 if any(r.color in PATTERNS for r in rows) else 0  # white tags: no empty box by the checkbox
    time_w = max(112, max((d.textlength(r.time, font=time_f) for r in rows if r.time), default=0) + 18)
    text_x = left + time_w
    text_max = box_x - 16 - text_x - (swatch + 10 if swatch else 0)
    note_f = _font(24)
    if not rows:
        d.text((text_x, y), "Nothing due. Nice.", font=text_f, fill=0)
        y += 56
    for row in rows:
        top = y
        font = text_bold_f if row.urgent else text_f
        text = f"! {row.text}" if row.urgent else row.text
        lines = _wrap(d, text, font, text_max)
        if len(lines) > 3:  # a row stays short; the full text is in the app
            lines = lines[:2] + [_ellipsize(d, lines[2], font, text_max)]
        tag_w = d.textlength(row.tag, font=tag_f) + 12 if row.tag else 0
        tag_on_own_line = bool(row.tag) and d.textlength(lines[-1], font=font) + tag_w > text_max
        tag_fill = ACCENT if (accent and row.late) else 0
        for i, line in enumerate(lines):
            d.text((text_x, y), line, font=font, fill=0)
            if i == len(lines) - 1 and row.tag and not tag_on_own_line:
                d.text((text_x + d.textlength(line, font=font) + 12, y + 8), row.tag, font=tag_f, fill=tag_fill)
            y += 40
        if tag_on_own_line:
            d.text((text_x, y - 4), row.tag, font=tag_f, fill=tag_fill)
            y += 28
        if row.note:
            for line in _wrap(d, f"→ next: {row.note}", note_f, text_max)[:2]:
                d.text((text_x, y - 2), line, font=note_f, fill=0)
                y += 30
        if row.time:
            d.text((left, top + 2), row.time, font=time_f, fill=0)
        else:
            d.line((left, top + 32, left + time_w - 20, top + 32), fill=0, width=2)
        row_h = max(y - top, box + 8)
        d.rectangle((box_x, top + (row_h - box) // 2 - 2, box_x + box, top + (row_h - box) // 2 - 2 + box),
                    outline=0, width=3)
        if row.color in PATTERNS:
            sy = top + (row_h - swatch) // 2 - 2
            draw_tag(canvas, (box_x - 12 - swatch, sy, box_x - 12, sy + swatch), row.color)
        y = top + row_h + 8
        _dotted(d, y - 4, left, right)
        y += 8

    if waiting:
        y += 4
        d.line((left, y, right, y), fill=0, width=2)
        y += 10
        head = f"{waiting_head} ({len(waiting)}): "
        waiting = [w if len(w) <= 40 else w[:39].rstrip() + "…" for w in waiting]  # one long name can't hide the rest
        body_lines = _wrap(d, head + " · ".join(waiting), wait_f, right - left)
        if len(body_lines) > 2:
            body_lines = [body_lines[0], _ellipsize(d, body_lines[1], wait_f, right - left)]
        for i, line in enumerate(body_lines):
            if i == 0 and line.startswith(head.strip()):
                d.text((left, y), head, font=wait_bold_f, fill=0)
                d.text((left + d.textlength(head, font=wait_bold_f), y), line[len(head):], font=wait_f, fill=0)
            else:
                d.text((left, y), line, font=wait_f, fill=0)
            y += 34

    if win:
        y += 8
        ww = d.textlength(win, font=wait_bold_f)
        d.text(((WIDTH - ww) / 2, y), win, font=wait_bold_f, fill=0)
        y += 34
    if journal:
        # Journal mode: the label goes on the left page; these get filled in by hand.
        y += 14
        lead = "Today's win:"
        d.text((left, y), lead, font=wait_bold_f, fill=0)
        d.line((left + d.textlength(lead, font=wait_bold_f) + 12, y + 28, right, y + 28), fill=0, width=2)
        y += 52
        hint = "Notes on the right page → snap both pages"
        hw = d.textlength(hint, font=foot_f)
        d.text(((WIDTH - hw) / 2, y), hint, font=foot_f, fill=0)
        y += 30
    y += 10
    d.line((left, y, right, y), fill=0, width=4)
    y += 12
    d.text((left, y), f"#{code}", font=code_f, fill=0)
    if footer:
        fw = d.textlength(footer, font=foot_f)
        d.text((right - fw, y + 5), footer, font=foot_f, fill=0)
    y += 36 + MARGIN
    if y > _height:  # longer than the canvas: draw it again on one that fits (never crop into black)
        return render_day(day_name, date_text, rows, code, footer, subtitle, waiting, accent, waiting_head,
                          win, schedule, journal, _height=y)
    return canvas.crop((0, 0, WIDTH, y))


# --- color tags on any label ------------------------------------------------------------------
# Thermal printers print black. A tag's color decides which label roll it goes on (when one is
# loaded) and, on plain white labels, which pattern it gets, so tags stay easy to tell apart.
PATTERNS = {"red": "solid", "orange": "diagonal", "yellow": "dots", "green": "hstripes",
            "blue": "vstripes", "pink": "checker", "purple": "crosshatch"}


def draw_tag(canvas: Image.Image, box: tuple[int, int, int, int], color: str, name: str = "",
             font=None) -> None:
    """A patterned block for a tag color, with the tag's name on it (readable on any pattern)."""
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = x1 - x0, y1 - y0
    pattern = PATTERNS.get(color, "outline")
    tile = Image.new("L", (w, h), 0 if pattern == "solid" else 255)
    t = _Draw(tile)  # drawn on its own tile, so patterns never spill past the block
    if pattern == "diagonal":
        for k in range(-h, w, 12):
            t.line((k, h, k + h, 0), fill=0, width=5)
    elif pattern == "dots":
        for yy in range(6, h - 3, 11):
            for xx in range(6 + (yy // 11 % 2) * 5, w - 3, 11):
                t.ellipse((xx - 2, yy - 2, xx + 2, yy + 2), fill=0)
    elif pattern == "hstripes":
        for yy in range(4, h, 10):
            t.line((0, yy, w, yy), fill=0, width=4)
    elif pattern == "vstripes":
        for xx in range(4, w, 10):
            t.line((xx, 0, xx, h), fill=0, width=4)
    elif pattern == "checker":
        for yy in range(0, h, 12):
            for xx in range(yy // 12 % 2 * 12, w, 24):
                t.rectangle((xx, yy, xx + 11, yy + 11), fill=0)
    elif pattern == "crosshatch":
        for k in range(-h, w, 14):
            t.line((k, h, k + h, 0), fill=0, width=2)
            t.line((k, 0, k + h, h), fill=0, width=2)
    t.rectangle((0, 0, w - 1, h - 1), outline=0, width=3)
    if name and font is not None:
        label = name.upper()
        lw = t.textlength(label, font=font)
        while lw > w - 16 and font.size > 14:  # smaller type before cutting the name short
            font = _font(int(font.size) - 2, True)
            lw = t.textlength(label, font=font)
        while lw > w - 16 and len(label) > 2:
            label = label[:-2] + "…"
            lw = t.textlength(label, font=font)
        fh = font.size
        cx, cy = w / 2, h / 2
        if pattern == "solid":
            t.text((cx - lw / 2, cy - fh * 0.6), label, font=font, fill=255)
        else:
            t.rectangle((cx - lw / 2 - 6, cy - fh * 0.62, cx + lw / 2 + 6, cy + fh * 0.62), fill=255, outline=0, width=2)
            t.text((cx - lw / 2, cy - fh * 0.6), label, font=font, fill=0)
    canvas.paste(tile.convert(canvas.mode), (x0, y0))


@dataclass
class Sticker:
    number: int           # the row read-back matches (shown next to the checkbox)
    text: str
    tag: str = ""         # tag name shown on the band ("" = no band)
    color: str = "white"
    when: str = ""        # "Tue 3:00p", "today", "overdue 2d"
    minutes: int | None = None
    next_step: str = ""
    urgent: bool = False
    big: bool = False     # bigger type, for reading across a room (Settings → Sticker size)


def _fit_text(d: ImageDraw.ImageDraw, text: str, width: int, sizes: tuple[int, ...], max_lines: int,
              bold: bool = True):
    for size in sizes:
        font = _font(size, bold)
        lines = _wrap(d, text, font, width)
        if len(lines) <= max_lines:
            return font, lines
    font = _font(sizes[-1], bold)
    lines = _wrap(d, text, font, width)[:max_lines]
    lines[-1] = _ellipsize(d, lines[-1], font, width)
    return font, lines


def _minutes(m: int) -> str:
    """45 -> "45m", 90 -> "1h 30m", 120 -> "2h"."""
    h, m = divmod(int(m), 60)
    return f"{h}h {m}m" if h and m else f"{h}h" if h else f"{m}m"


def _meta_line(s: Sticker) -> str:
    bits = [b for b in (s.when, f"~{_minutes(s.minutes)}" if s.minutes else "") if b]
    return "  ·  ".join(bits)


def render_sticker(s: Sticker, code: str) -> Image.Image:
    """One task on its own small label, to stick where the task happens (the bill, the door, the car)."""
    canvas = Image.new("L", (WIDTH, 800), 255)
    d = _Draw(canvas)
    left, right = MARGIN + 4, WIDTH - MARGIN - 4
    y = MARGIN
    band_h = 52
    if s.tag:
        draw_tag(canvas, (left, y, right, y + band_h), s.color, s.tag, _font(28, True))
        y += band_h + 12
    box = 54
    text_x = left + box + 18
    sizes = (64, 56, 48, 40) if s.big else (52, 46, 40, 34)
    font, lines = _fit_text(d, ("! " if s.urgent else "") + s.text, right - text_x, sizes, 3)
    top = y
    d.rectangle((left, top + 4, left + box, top + 4 + box), outline=0, width=5)
    num_f = _font(22, True)
    d.text((left + box / 2 - d.textlength(str(s.number), font=num_f) / 2, top + box + 8), str(s.number),
           font=num_f, fill=0)
    for line in lines:
        d.text((text_x, y), line, font=font, fill=0)
        y += int(font.size * 1.18)
    y = max(y, top + box + 36) + 4
    small = _font(30 if s.big else 26)
    if s.next_step:
        for line in _wrap(d, f"next: {s.next_step}", small, right - text_x)[:2]:
            d.text((text_x, y), line, font=small, fill=0)
            y += small.size + 6
    meta = _meta_line(s)
    foot_f = _font(22, True)
    y += 6
    d.line((left, y, right, y), fill=0, width=2)
    y += 8
    meta_f = _font(30 if s.big else 24, True)
    if meta:
        d.text((left, y), meta, font=meta_f, fill=0)
    cw = d.textlength(f"#{code}", font=foot_f)
    d.text((right - cw, y + 2), f"#{code}", font=foot_f, fill=0)
    y += meta_f.size + 8 + MARGIN // 2
    return canvas.crop((0, 0, WIDTH, y)).convert("RGB")


def stack_with_cuts(images: list[Image.Image]) -> Image.Image:
    """Preview of several stickers as they'll come out: one after another, with cut marks."""
    gap = 26
    h = sum(i.height for i in images) + gap * max(0, len(images) - 1)
    out = Image.new("RGB", (WIDTH, max(h, 1)), "white")
    d = ImageDraw.Draw(out)
    y = 0
    for n, img in enumerate(images):
        out.paste(img, (0, y))
        y += img.height
        if n < len(images) - 1:
            for x in range(0, WIDTH, 16):
                d.line((x, y + gap // 2, x + 8, y + gap // 2), fill=(160, 160, 160), width=2)
            y += gap
    return out


def render_strips(title: str, date_text: str, strips: list[Sticker], code: str) -> Image.Image:
    """One label with tear-off strips: cut or tear along the dashed lines and stick each task up."""
    canvas = Image.new("L", (WIDTH, 300 + 260 * max(1, len(strips))), 255)
    d = _Draw(canvas)
    left, right = MARGIN + 4, WIDTH - MARGIN - 4
    y = MARGIN
    head_f, date_f = _font(40, True), _font(28, True)
    d.text((left, y), title, font=head_f, fill=0)
    dw = d.textlength(date_text, font=date_f)
    d.text((right - dw, y + 10), date_text, font=date_f, fill=0)
    y += 56
    tip = "✂ cut on the dashes · stick each one where it happens"
    d.text((left, y), tip, font=_font(22), fill=0)
    y += 34
    # The tag band only when a strip has a tag: an empty one looks like a second checkbox.
    band_w, box = (120 + 14 if any(s.tag for s in strips) else 0), 46
    tag_f, code_f = _font(20, True), _font(18, True)
    for s in strips:
        _dashes(d, y, left, right)
        y += 14
        top = y
        text_x = left + band_w + box + 14
        sizes = (48, 40, 34) if s.big else (40, 34, 30)
        font, lines = _fit_text(d, ("! " if s.urgent else "") + s.text, right - text_x, sizes, 2)
        y_text = top + 4
        for line in lines:
            d.text((text_x, y_text), line, font=font, fill=0)
            y_text += int(font.size * 1.18)
        meta = _meta_line(s)
        if s.next_step:
            meta = (meta + "  ·  " if meta else "") + f"next: {s.next_step}"
        if meta:
            meta_f = _font(26 if s.big else 22)
            for line in _wrap(d, meta, meta_f, right - text_x)[:2]:
                d.text((text_x, y_text + 2), line, font=meta_f, fill=0)
                y_text += meta_f.size + 6
        bottom = max(y_text, top + box + 34) + 6
        if s.tag:
            draw_tag(canvas, (left, top, left + band_w - 14, bottom - 6), s.color, s.tag, tag_f)
        bx = left + band_w
        d.rectangle((bx, top + 4, bx + box, top + 4 + box), outline=0, width=4)
        d.text((bx + box / 2 - d.textlength(str(s.number), font=code_f) / 2, top + box + 10), str(s.number),
               font=code_f, fill=0)
        y = bottom + 6
    _dashes(d, y, left, right)
    y += 14
    d.text((left, y), f"#{code}", font=_font(24, True), fill=0)
    y += 34 + MARGIN // 2
    return canvas.crop((0, 0, WIDTH, y)).convert("RGB")


def _dashes(d: ImageDraw.ImageDraw, y: int, left: int, right: int) -> None:
    d.text((left - 4, y - 14), "✂", font=_font(22), fill=0)
    for x in range(left + 26, right, 18):
        d.line((x, y, x + 10, y), fill=0, width=3)


def render_focus(text: str, steps: list[str], code: str, tag: str = "", color: str = "white",
                 when: str = "", minutes: int | None = None, start_by: str = "") -> Image.Image:
    """Just one thing: a single task, big, with its tiny steps. For when the whole list is too much."""
    canvas = Image.new("L", (WIDTH, 2400), 255)
    d = _Draw(canvas)
    left, right = MARGIN + 4, WIDTH - MARGIN - 4
    y = MARGIN
    head_f = _font(30, True)
    d.text((left, y), _heading("JUST ONE THING"), font=head_f, fill=0)
    if tag:
        draw_tag(canvas, (right - 220, y - 4, right, y + 40), color, tag, _font(22, True))
    y += 54
    d.line((left, y, right, y), fill=0, width=5)
    y += 18
    box = 64
    font, lines = _fit_text(d, text, right - left - box - 22, (64, 56, 48, 40), 4)
    d.rectangle((left, y + 6, left + box, y + 6 + box), outline=0, width=6)
    d.text((left + box / 2 - d.textlength("1", font=_font(24, True)) / 2, y + box + 14), "1",
           font=_font(24, True), fill=0)
    top = y
    for line in lines:
        d.text((left + box + 22, y), line, font=font, fill=0)
        y += int(font.size * 1.15)
    y = max(y, top + box + 44) + 8
    about = (f"about {minutes} min" if minutes < 60 else f"about {_minutes(minutes)}") if minutes else ""
    info = "  ·  ".join(b for b in (when, about,
                                    f"start by {start_by}" if start_by else "") if b)
    for line in _wrap(d, info, _font(28, True), right - left) if info else []:
        d.text((left, y), line, font=_font(28, True), fill=0)
        y += 38
    y += 6 if info else 0
    if steps:
        d.text((left, y), "Tiny steps:", font=_font(28, True), fill=0)
        y += 40
        step_f = _font(30)
        for step in steps[:8]:
            d.rectangle((left + 6, y + 6, left + 34, y + 34), outline=0, width=3)
            for i, line in enumerate(_wrap(d, step, step_f, right - left - 52)[:2]):
                d.text((left + 50, y), line, font=step_f, fill=0)
                y += 38
            y += 8
    else:
        d.text((left, y), "First tiny step: ______________________", font=_font(28), fill=0)
        y += 46
    y += 6
    d.line((left, y, right, y), fill=0, width=3)
    y += 10
    d.text((left, y), "Done? Tick it. That counts.", font=_font(24, True), fill=0)
    cw = d.textlength(f"#{code}", font=_font(22, True))
    d.text((right - cw, y + 2), f"#{code}", font=_font(22, True), fill=0)
    y += 36 + MARGIN // 2
    return canvas.crop((0, 0, WIDTH, y)).convert("RGB")
