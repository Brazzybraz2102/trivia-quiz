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
        d.text((MARGIN, y), "✓ done    → tomorrow    ✗ drop", font=small_f, fill=0)
        y += 28
    if code:
        w = d.textlength(f"#{code}", font=meta_f)
        d.text((WIDTH - MARGIN - w, y), f"#{code}", font=meta_f, fill=0)
        y += 32
    y += MARGIN
    img = canvas.crop((0, 0, WIDTH, y))
    return img.point(lambda p: 0 if p < 128 else 255, mode="1")
