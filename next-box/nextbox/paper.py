"""Print on paper: lay a ticket out on a Letter or A4 page as a PDF, for any printer.

Stickers keep their real size (the same as on a 62 mm label) in a grid with dashed cut lines, so a
sheet of full-page sticker paper becomes a sheet of stickers. Lists and cards are printed a bit
bigger than on a label, and long ones continue on the next page.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw

DPI = 300
PAGES = {"letter": (2550, 3300), "a4": (2480, 3508)}  # pixels at 300 dpi
MARGIN = 150  # half an inch
GAP = 60      # between stickers: room to cut
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
    pages, page, x, y, row = [], _new_page(paper), MARGIN, MARGIN, []

    def close_row():
        if row:
            h = max(img.height for _, img in row)
            d = ImageDraw.Draw(page)
            for rx, img in row:
                _dashed_box(d, rx - GAP // 2, y - GAP // 2, rx + img.width + GAP // 2, y + h + GAP // 2)
        return max((img.height for _, img in row), default=0)

    for img in stickers:
        img = img.convert("L")
        if img.width > pw - 2 * MARGIN:
            img = img.resize((pw - 2 * MARGIN, round(img.height * (pw - 2 * MARGIN) / img.width)), Image.LANCZOS)
        if x + img.width > pw - MARGIN:  # next row
            y += close_row() + GAP
            x, row = MARGIN, []
        if y + img.height > ph - MARGIN:  # next page
            pages.append(page)
            page, x, y, row = _new_page(paper), MARGIN, MARGIN, []
        page.paste(img, (x, y))
        row.append((x, img))
        x += img.width + GAP
    close_row()
    pages.append(page)
    return pages


def single_pages(img: Image.Image, paper: str = "letter") -> list[Image.Image]:
    """A list, strips or card: 3.5 inches wide, continued on more pages if it's long."""
    pw, ph = PAGES[paper]
    img = img.convert("L")
    img = img.resize((SINGLE_WIDTH, round(img.height * SINGLE_WIDTH / img.width)), Image.LANCZOS)
    usable = ph - 2 * MARGIN
    pages = []
    for top in range(0, img.height, usable):
        page = _new_page(paper)
        piece = img.crop((0, top, img.width, min(img.height, top + usable)))
        page.paste(piece, (MARGIN, MARGIN))
        _dashed_box(ImageDraw.Draw(page), MARGIN - 30, MARGIN - 30, MARGIN + img.width + 30, MARGIN + piece.height + 30)
        pages.append(page)
    return pages


def to_pdf(pages: list[Image.Image]) -> bytes:
    buf = io.BytesIO()
    first, rest = pages[0].convert("RGB"), [p.convert("RGB") for p in pages[1:]]
    first.save(buf, "PDF", resolution=DPI, save_all=True, append_images=rest)
    return buf.getvalue()


def ticket_pdf(png: bytes, parts: list[Image.Image] | None, paper: str = "letter") -> bytes:
    if paper not in PAGES:
        raise ValueError("paper is letter or a4")
    if parts:
        return to_pdf(sticker_sheets(parts, paper))
    return to_pdf(single_pages(Image.open(io.BytesIO(png)), paper))
