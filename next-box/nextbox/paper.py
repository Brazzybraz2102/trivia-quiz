"""Print on paper: lay a ticket out on a Letter, A4, A5 or half-letter page as a PDF, for any printer.
A5 and half-letter are journal page sizes: trim the page and paste the list into the journal.

Stickers keep their real size (the same as on a 62 mm label) in a grid with dashed cut lines, so a
sheet of full-page sticker paper becomes a sheet of stickers. Lists and cards are printed a bit
bigger than on a label, and long ones continue on the next page.
"""
from __future__ import annotations

import io
import time
from datetime import date

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


# --- the journal on paper -----------------------------------------------------------------------
# "Download my journal" as a PDF and "Print my journal page": a page for each day with its photos,
# typed notes and wins. Drawn with Pillow like the tickets above, at 200 dpi (plenty for a photo of
# handwriting), and written one page at a time, so a year of journal never has to fit in memory.
JOURNAL_DPI = 200
POINTS = {"letter": (612.0, 792.0), "a4": (595.28, 841.89), "a5": (419.53, 595.28), "half_letter": (396.0, 612.0)}
INK, SOFT, RULE, SHADE = (27, 27, 27), (98, 98, 98), (205, 205, 198), (247, 243, 226)


def _pdf_text(text: str) -> bytes:
    """A PDF string that keeps any character: UTF-16 with a byte order mark, as hex."""
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode() + b">"


class JpegPdf:
    """A PDF whose pages are images, written page by page to `out` (any writable binary file).
    Pillow's own PDF writer keeps every page in memory until the end."""

    def __init__(self, out, title: str = ""):
        self.out, self.pos, self.offsets, self.kids, self.next_id = out, 0, {}, [], 4  # 1 catalog, 2 pages, 3 info
        self._write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        self._object(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        self._object(3, b"<< /Producer (Next Box) /Title " + _pdf_text(title) + b" >>")

    def _write(self, data: bytes) -> None:
        self.out.write(data)
        self.pos += len(data)

    def _object(self, n: int, body: bytes, stream: bytes | None = None) -> None:
        self.offsets[n] = self.pos
        self._write(f"{n} 0 obj\n".encode() + body)
        if stream is not None:
            self._write(b"\nstream\n" + stream + b"\nendstream")
        self._write(b"\nendobj\n")

    def add(self, page: Image.Image, size_pt: tuple[float, float], quality: int = 80) -> None:
        buf = io.BytesIO()
        page.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
        jpeg = buf.getvalue()
        img, content, node = self.next_id, self.next_id + 1, self.next_id + 2
        self.next_id += 3
        w, h = size_pt
        self._object(img, (f"<< /Type /XObject /Subtype /Image /Width {page.width} /Height {page.height} /ColorSpace "
                           f"/DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>").encode(), jpeg)
        draw = f"q {w:.2f} 0 0 {h:.2f} 0 0 cm /P Do Q".encode()
        self._object(content, f"<< /Length {len(draw)} >>".encode(), draw)
        self._object(node, (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:.2f} {h:.2f}] "
                            f"/Resources << /XObject << /P {img} 0 R >> >> /Contents {content} 0 R >>").encode())
        self.kids.append(node)

    def finish(self) -> int:
        """The page tree, the cross-reference table and the trailer. Returns the number of pages."""
        if not self.kids:
            raise ValueError("a PDF needs at least one page")
        kids = " ".join(f"{k} 0 R" for k in self.kids)
        self._object(2, f"<< /Type /Pages /Kids [{kids}] /Count {len(self.kids)} >>".encode())
        start, size = self.pos, self.next_id
        rows = b"".join(f"{self.offsets[n]:010d} 00000 n \n".encode() for n in range(1, size))
        self._write(f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n" + rows)
        self._write(f"trailer\n<< /Size {size} /Root 1 0 R /Info 3 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
        return len(self.kids)


def day_title(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d:%A}, {d:%B} {d.day}, {d.year}"


def clock(ts: int) -> str:
    return time.strftime("%I:%M %p", time.localtime(ts)).lstrip("0")


class _JournalLayout:
    """Lays one day out top to bottom, starting a new sheet whenever the next piece doesn't fit."""

    def __init__(self, paper: str, day: str):
        from .render import _font
        if paper not in POINTS:
            raise ValueError("paper is letter, a4, a5 or half_letter")
        self.size_pt = POINTS[paper]
        self.w, self.h = (round(p / 72 * JOURNAL_DPI) for p in self.size_pt)
        self.margin = round((0.4 if paper in ("a5", "half_letter") else 0.55) * JOURNAL_DPI)

        def pt(n: float) -> int:  # type sizes in points, like a word processor
            return round(n / 72 * JOURNAL_DPI)
        self.body, self.small = _font(pt(10.5)), _font(pt(8))
        self.bold, self.title_font = _font(pt(10.5), bold=True), _font(pt(15), bold=True)
        self.line = round(pt(10.5) * 1.42)
        self.title = day_title(day)
        self.sheets: list[Image.Image] = []
        self._sheet()

    @property
    def width(self) -> int:
        return self.w - 2 * self.margin

    @property
    def bottom(self) -> int:
        return self.h - self.margin - round(self.line * 1.2)  # room for the footer

    def _sheet(self) -> None:
        from .render import _Draw
        more = bool(self.sheets)
        self.page = Image.new("RGB", (self.w, self.h), "white")
        self.d = _Draw(self.page)
        y = self.margin
        self.d.text((self.margin, y), self.title + (" (continued)" if more else ""), fill=INK,
                    font=self.bold if more else self.title_font)
        y += round(self.line * (1.1 if more else 1.6))
        self.d.line((self.margin, y, self.w - self.margin, y), fill=RULE, width=3)
        self.y = y + round(self.line * 0.7)
        self.sheets.append(self.page)

    def room(self, height: int) -> None:
        """Start a new sheet unless `height` more pixels fit (a nearly empty sheet always takes it)."""
        if self.y + height > self.bottom and self.y > self.margin + self.line * 3:
            self._sheet()

    def text(self, text: str, font=None, fill=INK, indent: int = 0) -> None:
        from .render import _wrap
        font = font or self.body
        for line in _wrap(self.d, text, font, self.width - indent):
            self.room(self.line)
            self.d.text((self.margin + indent, self.y), line, fill=fill, font=font)
            self.y += self.line

    def gap(self, lines: float = 0.6) -> None:
        self.y += round(self.line * lines)

    def win(self, text: str) -> None:
        from .render import _wrap
        pad = self.line // 2
        lines = _wrap(self.d, f"Your win: {text}", self.body, self.width - 2 * pad)
        self.room(min(len(lines), 3) * self.line + pad)
        top = self.y
        height = min(len(lines) * self.line + pad, self.bottom - top)
        self.d.rounded_rectangle((self.margin, top, self.w - self.margin, top + height), radius=self.line // 3, fill=SHADE)
        self.y += pad // 2
        for line in lines:
            if self.y + self.line > top + height:
                break  # a win is a line or two; anything longer is cut at the box
            self.d.text((self.margin + pad, self.y), line, fill=INK, font=self.body)
            self.y += self.line
        self.y = top + height + pad

    def photo(self, jpeg: bytes, notes_follow: bool) -> None:
        img = Image.open(io.BytesIO(jpeg))
        usable = self.bottom - (self.margin + self.line * 3)
        cap = round(usable * (0.62 if notes_follow else 0.9))
        scale = min(self.width / img.width, cap / img.height)
        if self.y + img.height * scale > self.bottom:
            left = self.bottom - self.y
            if left >= cap * 0.55:  # a little smaller still fits on this sheet
                scale = min(scale, left / img.height)
            else:
                self._sheet()
        size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        img.draft("RGB", size)  # decode a big JPEG at about the size it's drawn: much quicker
        img = img.convert("RGB").resize(size, Image.LANCZOS)
        x = self.margin + (self.width - size[0]) // 2  # a tall photo sits in the middle
        self.page.paste(img, (x, self.y))
        self.d.rectangle((x - 1, self.y - 1, x + size[0], self.y + size[1]), outline=RULE, width=2)
        self.y += size[1] + round(self.line * 0.6)

    def finish(self) -> list[Image.Image]:
        from .render import _Draw
        n = len(self.sheets)
        for i, sheet in enumerate(self.sheets, start=1):
            d = _Draw(sheet)
            y = self.h - self.margin - self.line // 2
            d.text((self.margin, y), "Next Box journal · private", fill=SOFT, font=self.small)
            if n > 1:
                label = f"{i} of {n}"
                d.text((self.w - self.margin - d.textlength(label, font=self.small), y), label, fill=SOFT, font=self.small)
        return self.sheets


def journal_day_sheets(day: dict, paper: str, photo) -> list[Image.Image]:
    """One day of the journal on paper: its win, each page's photo and typed notes, then the day's
    wins. `day` is {"day", "pages": [{"id", "notes", "has_photo", "created", "win"}], "wins": {"count",
    "items", "linked"}}; `photo(page_id)` returns a page's JPEG."""
    out = _JournalLayout(paper, day["day"])
    for p in day["pages"]:
        if p.get("win"):
            out.win(p["win"])
    for n, p in enumerate(day["pages"]):
        if n:
            out.gap(0.4)
        image = photo(p["id"]) if p.get("has_photo") else None
        notes = (p.get("notes") or "").strip()
        out.room(out.line * 2)
        out.text(("Snapped at " if image else "Written at ") + clock(p["created"]), font=out.small, fill=SOFT)
        if image:
            out.photo(image, notes_follow=bool(notes))
        if notes:
            out.text(notes)
    wins = day.get("wins") or {}
    if wins.get("count"):
        out.gap(0.8)
        out.room(out.line * 2)
        out.text(f"Wins: {wins['count']} ✓", font=out.bold)
        counted: dict[str, int] = {}
        for item in wins.get("items") or []:
            counted[item] = counted.get(item, 0) + 1
        for item, times in counted.items():
            out.text(f"✓ {item}" + (f" ×{times}" if times > 1 else ""), indent=out.line // 2)
        if wins.get("linked"):
            more = "more " if counted else ""
            out.text(f"{'+' if counted else ''}{wins['linked']} {more}in your linked app", fill=SOFT, indent=out.line // 2)
    return out.finish()


def journal_pdf(days: list[dict], paper: str, out, photo, title: str = "Next Box journal") -> int:
    """The journal as a PDF, oldest day first, written to `out`. Returns the number of pages."""
    pdf = JpegPdf(out, title)
    for day in days:
        for sheet in journal_day_sheets(day, paper, photo):
            pdf.add(sheet, POINTS[paper])
    return pdf.finish()
