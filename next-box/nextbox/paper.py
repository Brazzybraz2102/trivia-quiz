"""Print on paper: lay a ticket out on a Letter, A4, A5 or half-letter page as a PDF, for any printer.
A5 and half-letter are journal page sizes: trim the page and paste the list into the journal.

Stickers keep their real size (the same as on a 62 mm label) in a grid with dashed cut lines, so a
sheet of full-page sticker paper becomes a sheet of stickers. Lists and cards are printed a bit
bigger than on a label, and long ones continue on the next page.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw

DPI = 300
PAGES = {"letter": (2550, 3300), "a4": (2480, 3508),        # pixels at 300 dpi
         "a5": (1748, 2480), "half_letter": (1650, 2550)}  # journal pages
MARGIN = 150  # half an inch
GAP = 60      # between stickers: room to cut
# Journal-size pages get narrower margins, so two stickers still fit side by side.
STICKER_MARGIN = {"a5": 100, "half_letter": 100}
STICKER_GAP = {"a5": 40, "half_letter": 40}
SINGLE_WIDTH = 1050  # 3.5 inches: a list or card on paper


def _dashed_box(d: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, y1: int) -> None:
    for x in range(x0, x1, 24):
        d.line((x, y0, min(x + 12, x1), y0), fill=150, width=2)
        d.line((x, y1, min(x + 12, x1), y1), fill=150, width=2)
    for y in range(y0, y1, 24):
        d.line((x0, y, x0, min(y + 12, y1)), fill=150, width=2)
        d.line((x1, y, x1, min(y + 12, y1)), fill=150, width=2)


def _new_page(paper: str) -> Image.Image:
    return Image.new("L", PAGES[paper], 255)


def sticker_sheets(stickers: list[Image.Image], paper: str = "letter") -> list[Image.Image]:
    """Real-size stickers in rows, cut lines around each, as many pages as needed. Every box in a
    row is the row's height, so the cut lines line up."""
    pw, ph = PAGES[paper]
    margin, gap = STICKER_MARGIN.get(paper, MARGIN), STICKER_GAP.get(paper, GAP)
    pages, page, x, y, row = [], _new_page(paper), margin, margin, []

    def close_row():
        if row:
            h = max(img.height for _, img in row)
            d = ImageDraw.Draw(page)
            for rx, img in row:
                _dashed_box(d, rx - gap // 2, y - gap // 2, rx + img.width + gap // 2, y + h + gap // 2)
        return max((img.height for _, img in row), default=0)

    for img in stickers:
        img = img.convert("L")
        if img.width > pw - 2 * margin:
            img = img.resize((pw - 2 * margin, round(img.height * (pw - 2 * margin) / img.width)), Image.LANCZOS)
        if x + img.width > pw - margin:  # next row
            y += close_row() + gap
            x, row = margin, []
        if y + img.height > ph - margin:  # next page: this row's cut lines first
            close_row()
            pages.append(page)
            page, x, y, row = _new_page(paper), margin, margin, []
        page.paste(img, (x, y))
        row.append((x, img))
        x += img.width + gap
    close_row()
    pages.append(page)
    return pages


def _break_at(img: Image.Image, top: int, usable: int) -> int:
    """Where this page's piece should end: a blank row of pixels near the bottom, so a page never
    cuts through a line of text or a checkbox. Falls back to a straight cut if there's none."""
    end = top + usable
    if end >= img.height:
        return img.height
    px = img.load()
    for y in range(end, max(top + usable // 2, end - 300), -1):
        if all(px[x, y] >= 250 for x in range(0, img.width, 2)):
            return y
    return end


def single_pages(img: Image.Image, paper: str = "letter", code: str = "") -> list[Image.Image]:
    """A list, strips or card: 3.5 inches wide, continued on more pages if it's long."""
    pw, ph = PAGES[paper]
    img = img.convert("L")
    img = img.resize((SINGLE_WIDTH, round(img.height * SINGLE_WIDTH / img.width)), Image.LANCZOS)
    usable = ph - 2 * MARGIN
    if usable < img.height <= usable * 1.3:  # a little too long: shrink it onto one page instead
        img = img.resize((round(img.width * usable / img.height), usable), Image.LANCZOS)
    cuts, top = [], 0
    while top < img.height:
        end = _break_at(img, top, usable)
        cuts.append((top, end))
        top = end
    pages = []
    for n, (top, end) in enumerate(cuts, start=1):
        page = _new_page(paper)
        piece = img.crop((0, top, img.width, end))
        page.paste(piece, (MARGIN, MARGIN))
        d = ImageDraw.Draw(page)
        _dashed_box(d, MARGIN - 30, MARGIN - 30, MARGIN + img.width + 30, MARGIN + piece.height + 30)
        if len(cuts) > 1:  # which page this is, so a dropped sheet finds its place
            from .render import _font
            d.text((MARGIN - 30, MARGIN + piece.height + 44), f"page {n} of {len(cuts)}" + (f" · #{code}" if code else ""),
                   font=_font(28), fill=90)
        pages.append(page)
    return pages


def to_pdf(pages: list[Image.Image]) -> bytes:
    buf = io.BytesIO()
    first, rest = pages[0].convert("RGB"), [p.convert("RGB") for p in pages[1:]]
    first.save(buf, "PDF", resolution=DPI, save_all=True, append_images=rest)
    return buf.getvalue()


def ticket_pdf(png: bytes, parts: list[Image.Image] | None, paper: str = "letter", code: str = "") -> bytes:
    if paper not in PAGES:
        raise ValueError("paper is letter, a4, a5 or half_letter")
    if parts:
        return to_pdf(sticker_sheets(parts, paper))
    return to_pdf(single_pages(Image.open(io.BytesIO(png)), paper, code))
