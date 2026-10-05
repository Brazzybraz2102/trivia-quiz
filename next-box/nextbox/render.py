"""Draw labels for 62 mm continuous tape (696 px printable width at 300 dpi)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 696
MARGIN = 16
FONT_DIRS = [
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/dejavu-sans-fonts"),
    Path("/usr/share/fonts/TTF"),
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        if (d / name).exists():
            return ImageFont.truetype(str(d / name), size)
    return ImageFont.load_default(size=size)


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
            # hard-break words longer than a full line
            while draw.textlength(word, font=font) > max_width and len(word) > 1:
                cut = len(word)
                while cut > 1 and draw.textlength(word[:cut], font=font) > max_width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def render_label(title: str, subtitle: str = "", rows: list[Row] | None = None,
                 body: str = "", code: str = "", legend: bool = True) -> Image.Image:
    """Return a 1-bit image. Rows get numbered checkboxes; body is free text."""
    title_f, sub_f, row_f, meta_f, small_f = _font(46, True), _font(26), _font(34), _font(24, True), _font(20)
    canvas = Image.new("L", (WIDTH, 4000), 255)
    d = ImageDraw.Draw(canvas)
    y = MARGIN
    for line in _wrap(d, title, title_f, WIDTH - 2 * MARGIN):
        d.text((MARGIN, y), line, font=title_f, fill=0)
        y += 54
    if subtitle:
        d.text((MARGIN, y), subtitle, font=sub_f, fill=0)
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
    img = canvas.crop((0, 0, WIDTH, y))
    return img.point(lambda p: 0 if p < 128 else 255, mode="1")


@dataclass
class DayRow:
    text: str
    time: str = ""        # "9:00a"; blank rows get a write-in line
    urgent: bool = False  # p1/p2: bold with a leading "!"
    tag: str = ""         # "overdue 2d", "↻"


def _dotted(d: ImageDraw.ImageDraw, y: int, x0: int, x1: int) -> None:
    for x in range(x0, x1, 8):
        d.line((x, y, x + 3, y), fill=0, width=2)


def render_day(day_name: str, date_text: str, rows: list[DayRow], code: str, footer: str = "",
               subtitle: str = "", waiting: list[str] | None = None) -> Image.Image:
    """The daily sheet: big day name, legend on top, time column, checkboxes on the right.
    Read-back matches rows top to bottom against the manifest, so row order is the contract."""
    day_f, date_f, sub_f = _font(84, True), _font(40, True), _font(28)
    legend_f, time_f = _font(28), _font(28, True)
    text_f, text_bold_f, tag_f = _font(31), _font(31, True), _font(22)
    wait_bold_f, wait_f, code_f, foot_f = _font(25, True), _font(25), _font(26, True), _font(20)

    canvas = Image.new("L", (WIDTH, 5000), 255)
    d = ImageDraw.Draw(canvas)
    left, right = MARGIN + 4, WIDTH - MARGIN - 4
    y = MARGIN + 6

    dw = d.textlength(date_text, font=date_f)
    size = 84
    while size > 48 and d.textlength(day_name, font=day_f) + dw + 24 > right - left:
        size -= 4
        day_f = _font(size, True)
    d.text((left, y), day_name, font=day_f, fill=0)
    base = y + int(size * 0.98)  # approximate baseline, so the date sits on the same line
    d.text((right - dw, base - 40), date_text, font=date_f, fill=0)
    y += int(size * 1.2)
    if subtitle:
        d.text((left, y), subtitle, font=sub_f, fill=0)
        y += 38
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
    time_w = 112
    text_x = left + time_w
    text_max = box_x - 16 - text_x
    if not rows:
        d.text((text_x, y), "Nothing due. Nice.", font=text_f, fill=0)
        y += 56
    for row in rows:
        top = y
        font = text_bold_f if row.urgent else text_f
        text = f"! {row.text}" if row.urgent else row.text
        lines = _wrap(d, text, font, text_max)
        tag_w = d.textlength(row.tag, font=tag_f) + 12 if row.tag else 0
        tag_on_own_line = bool(row.tag) and d.textlength(lines[-1], font=font) + tag_w > text_max
        for i, line in enumerate(lines):
            d.text((text_x, y), line, font=font, fill=0)
            if i == len(lines) - 1 and row.tag and not tag_on_own_line:
                d.text((text_x + d.textlength(line, font=font) + 12, y + 8), row.tag, font=tag_f, fill=0)
            y += 40
        if tag_on_own_line:
            d.text((text_x, y - 4), row.tag, font=tag_f, fill=0)
            y += 28
        if row.time:
            d.text((left, top + 2), row.time, font=time_f, fill=0)
        else:
            d.line((left, top + 32, left + time_w - 20, top + 32), fill=0, width=2)
        row_h = max(y - top, box + 8)
        d.rectangle((box_x, top + (row_h - box) // 2 - 2, box_x + box, top + (row_h - box) // 2 - 2 + box),
                    outline=0, width=3)
        y = top + row_h + 8
        _dotted(d, y - 4, left, right)
        y += 8

    if waiting:
        y += 4
        d.line((left, y, right, y), fill=0, width=2)
        y += 10
        head = f"Also waiting ({len(waiting)}): "
        body_lines = _wrap(d, head + " · ".join(waiting), wait_f, right - left)[:2]
        full = " ".join(body_lines)
        if len(full) < len(head + " · ".join(waiting)):
            body_lines[-1] = body_lines[-1].rstrip(" ·") + "…"
        for i, line in enumerate(body_lines):
            if i == 0 and line.startswith(head.strip()):
                d.text((left, y), head, font=wait_bold_f, fill=0)
                d.text((left + d.textlength(head, font=wait_bold_f), y), line[len(head):], font=wait_f, fill=0)
            else:
                d.text((left, y), line, font=wait_f, fill=0)
            y += 34

    y += 10
    d.line((left, y, right, y), fill=0, width=4)
    y += 12
    d.text((left, y), f"#{code}", font=code_f, fill=0)
    if footer:
        fw = d.textlength(footer, font=foot_f)
        d.text((right - fw, y + 5), footer, font=foot_f, fill=0)
    y += 36 + MARGIN
    img = canvas.crop((0, 0, WIDTH, y))
    return img.point(lambda p: 0 if p < 128 else 255, mode="1")
