"""Printer drivers: turn a ticket image into the bytes a thermal printer understands, and send them.

Drivers:
  brother_ql  Brother QL / PT label printers (raster), network or USB, optional black+red rolls
  escpos      ESC/POS receipt-style printers (Epson TM, Star, most 58/80 mm printers)
  zpl         Zebra and other ZPL label printers
  tspl        TSC, Munbyn, iDPRT, Xprinter and other TSPL label printers
  cups        any printer installed on this computer (Dymo, Rollo, USB printers with a driver)

Every real send goes through `_send_raster`, the single choke point tests guard.
"""
from __future__ import annotations

import fcntl
import math
import shutil
import socket
import subprocess
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

from .config import Settings

PORT = 9100
COLORS = ("white", "red", "orange", "yellow", "green", "blue", "pink", "purple", "clear", "other")

# Brother QL / PT models and label sizes (the brother_ql ids). Only the QL-800 series prints red.
BROTHER_MODELS = ("QL-500", "QL-550", "QL-560", "QL-570", "QL-580N", "QL-600", "QL-650TD", "QL-700", "QL-710W",
                  "QL-720NW", "QL-800", "QL-810W", "QL-820NWB", "QL-1050", "QL-1060N", "QL-1100", "QL-1110NWB",
                  "QL-1115NWB", "PT-E550W", "PT-P700", "PT-P750W", "PT-P900W", "PT-P950NW")
WIDE_MODELS = ("QL-1050", "QL-1060N", "QL-1100", "QL-1110NWB", "QL-1115NWB")  # the only ones for 102 mm labels
# id, what it says on the box, printable width in dots
BROTHER_LABELS = (
    ("62", "62 mm continuous (DK-22205)", 696), ("29", "29 mm continuous", 306), ("38", "38 mm continuous", 413),
    ("50", "50 mm continuous", 554), ("54", "54 mm continuous", 590), ("12", "12 mm continuous", 106),
    ("102", "102 mm continuous (QL-1000 series)", 1164),
    ("62x29", "62 × 29 mm labels", 696), ("62x100", "62 × 100 mm labels", 696), ("29x90", "29 × 90 mm labels", 306),
    ("29x42", "29 × 42 mm labels", 306), ("17x54", "17 × 54 mm labels", 165), ("17x87", "17 × 87 mm labels", 165),
    ("23x23", "23 × 23 mm labels", 202), ("39x90", "38 × 90 mm labels", 413), ("39x48", "39 × 48 mm labels", 425),
    ("52x29", "52 × 29 mm labels", 578), ("102x51", "102 × 51 mm labels (QL-1000 series)", 1164),
    ("102x152", "102 × 152 mm labels (QL-1000 series)", 1164), ("d24", "24 mm round labels", 236),
    ("d58", "58 mm round labels", 618), ("pt12", "12 mm P-touch tape", 70), ("pt18", "18 mm P-touch tape", 112),
    ("pt24", "24 mm P-touch tape", 128), ("pt36", "36 mm P-touch tape", 454),
)

# The Brother rolls most people have, shown first in Set up a printer's label picker: id, plain name, the code on
# the box, what it's for. Every other size is under "More sizes".
COMMON_ROLLS = (
    ("62", "62 mm continuous roll", "DK-22205", "One long roll, cut to fit each label"),
    ("102x152", "102 × 152 mm labels (4 × 6 in)", "DK-1241", "Shipping labels"),
    ("62x100", "62 × 100 mm labels", "DK-1202", "Shipping labels"),
    ("29x90", "29 × 90 mm labels", "DK-1201", "Address labels"),
    ("102", "102 mm continuous roll", "DK-22243", "One wide roll, cut to fit each label"),
)

DRIVERS = {
    "brother_ql": {"name": "Brother QL / PT label printer", "address": "192.168.1.50 or usb://0x04f9:0x20a7",
                   "width": 696, "dpi": 300, "models": list(BROTHER_MODELS), "wide_models": list(WIDE_MODELS),
                   "labels": [{"id": i, "name": n, "width": w} for i, n, w in BROTHER_LABELS],
                   "common": [{"id": i, "name": n, "code": c, "use": u} for i, n, c, u in COMMON_ROLLS]},
    "escpos": {"name": "ESC/POS receipt printer (Epson, Star, 58/80 mm)", "address": "192.168.1.60 or /dev/usb/lp0",
               "width": 576, "dpi": 203},
    "zpl": {"name": "ZPL label printer (Zebra and compatibles)", "address": "192.168.1.70", "width": 812, "dpi": 203},
    "tspl": {"name": "TSPL label printer (TSC, Munbyn, iDPRT, Xprinter)", "address": "192.168.1.80 or /dev/usb/lp0",
             "width": 812, "dpi": 203},
    "cups": {"name": "Any printer installed on this computer (CUPS)", "address": "printer name from lpstat -p",
             "width": 696, "dpi": 300},
}


@dataclass
class PrinterConfig:
    id: str
    name: str
    driver: str
    address: str
    width_px: int
    dpi: int = 300
    stock_color: str = "white"       # color of the label roll loaded
    ink: str = "black"               # "black_red" only for Brother QL with a black+red roll
    model: str = "QL-1110NWB"        # brother_ql
    label: str = "62"                # brother_ql label id
    label_height_mm: float = 0.0     # die-cut labels for zpl/tspl; 0 = continuous / fit content
    gap_mm: float = 2.0              # tspl gap between labels
    cut: bool = True
    extra: dict = field(default_factory=dict)

    def public(self, admin: bool = False) -> dict:
        d = asdict(self)
        if not admin:
            for k in ("address", "model", "label", "extra"):
                d.pop(k, None)
        return d


def legacy_config(settings: Settings) -> PrinterConfig:
    """The single printer from .env (PRINTER_IP) that older setups used."""
    return PrinterConfig(id="default", name="Label printer", driver="brother_ql",
                         address=settings.printer_ip, width_px=696, dpi=300,
                         model=settings.printer_model, label=settings.label)


# --- addresses & reachability -------------------------------------------------------------
def _host_port(address: str) -> tuple[str, int]:
    host, _, port = address.strip().rpartition(":") if address.count(":") == 1 else (address.strip(), "", "")
    return (host or address.strip()), int(port) if port.isdigit() else PORT


def _is_device(address: str) -> bool:
    return address.startswith("/dev/")


def is_reachable(target: str | PrinterConfig, timeout: float = 2.0) -> bool:
    if isinstance(target, PrinterConfig):
        cfg = target
        if cfg.driver == "cups":
            return bool(shutil.which("lpstat")) and subprocess.run(
                ["lpstat", "-p", cfg.address], capture_output=True, timeout=5).returncode == 0
        if _is_device(cfg.address):
            return Path(cfg.address).exists()
        if cfg.address.startswith("usb://"):
            return True  # can't probe USB without claiming it; the send reports problems
        address = cfg.address
    else:
        address = target
    if not address:
        return False
    try:
        host, port = _host_port(address)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError, OverflowError):  # a bad address saved before it was checked: just "not reachable"
        return False


# --- image -> printer language ----------------------------------------------------------
def to_mono(img: Image.Image) -> Image.Image:
    """1-bit, 0 = black."""
    return img if img.mode == "1" else img.convert("L").point(lambda p: 0 if p < 128 else 255, mode="1")


def _rows(img: Image.Image, black_is_one: bool = True) -> tuple[int, list[bytes]]:
    """Packed rows, MSB first. Pads the width to whole bytes with white."""
    mono = to_mono(img)
    w, h = mono.size
    bpr = math.ceil(w / 8)
    padded = Image.new("1", (bpr * 8, h), 1)
    padded.paste(mono, (0, 0))
    raw = padded.tobytes()  # PIL "1": 1 = white
    rows = [raw[i * bpr:(i + 1) * bpr] for i in range(h)]
    if black_is_one:
        rows = [bytes(b ^ 0xFF for b in r) for r in rows]
    return bpr, rows


def escpos_bytes(img: Image.Image, cut: bool = True) -> bytes:
    bpr, rows = _rows(img)
    out = bytearray(b"\x1b@")  # initialize
    for start in range(0, len(rows), 255):  # bands: many printers cap the height per command
        band = rows[start:start + 255]
        out += b"\x1dv0\x00" + bytes([bpr & 0xFF, bpr >> 8, len(band) & 0xFF, len(band) >> 8]) + b"".join(band)
    out += b"\x1bd\x04"  # feed 4 lines
    if cut:
        out += b"\x1dVB\x00"  # partial cut after feed
    return bytes(out)


def zpl_bytes(img: Image.Image, label_height_dots: int = 0) -> bytes:
    bpr, rows = _rows(img)
    data = b"".join(rows)
    h = len(rows)
    length = max(label_height_dots, h)
    return (f"^XA^PW{bpr * 8}^LL{length}^FO0,0^GFA,{len(data)},{len(data)},{bpr},{data.hex().upper()}^FS^XZ"
            .encode())


def tspl_bytes(img: Image.Image, dpi: int, label_height_mm: float = 0.0, gap_mm: float = 2.0) -> bytes:
    bpr, rows = _rows(img, black_is_one=False)  # TSPL bitmaps print the 0 bits
    w_mm = round(bpr * 8 / dpi * 25.4, 1)
    h_mm = label_height_mm or round(len(rows) / dpi * 25.4 + 2, 1)
    head = (f"SIZE {w_mm} mm,{h_mm} mm\r\nGAP {gap_mm} mm,0 mm\r\nDIRECTION 1\r\nCLS\r\n"
            f"BITMAP 0,0,{bpr},{len(rows)},0,").encode()
    return head + b"".join(rows) + b"\r\nPRINT 1,1\r\n"


def brother_label(label_id: str):
    """brother_ql's own spec for a label id: form factor, tape size in mm, printable dots."""
    from brother_ql.labels import ALL_LABELS
    return next((lab for lab in ALL_LABELS if lab.identifier == label_id), None)


def die_cut_spec(cfg: PrinterConfig):
    """brother_ql's spec when this printer has die-cut or round labels loaded, else None."""
    if cfg.driver != "brother_ql":
        return None
    from brother_ql.labels import FormFactor
    spec = brother_label(cfg.label)
    return spec if spec and spec.form_factor in (FormFactor.DIE_CUT, FormFactor.ROUND_DIE_CUT) else None


def _ink_box(img: Image.Image) -> tuple[int, int, int, int] | None:
    """The box around everything that isn't (near) white, or None for a blank image."""
    return img.convert("L").point(lambda p: 255 if p < 250 else 0).getbbox()


CODE_INFO = "nextbox_code"  # Image.info key for a ticket's #code (jobs._finish sets it with with_code)
SHRINK_ONTO_ONE = 1.3       # a ticket up to this much taller than its die-cut label is shrunk onto one


def with_code(img: Image.Image, code: str) -> Image.Image:
    """Tag a ticket image with its #code. It travels with the image to label_pages (the preview and
    the send), which prints it on every label of a ticket that's split over several."""
    img.info[CODE_INFO] = code
    return img


def _scaled(n: int, width: int) -> int:
    """`n` pixels of a ticket drawn at render.WIDTH, on a label `width` dots wide."""
    from .render import WIDTH
    return round(n * width / WIDTH)


def _stamp_size(width: int) -> int:
    """Type size of a split ticket's "n/N  #code" line: the ticket's own code size, scaled to the label."""
    return max(12, _scaled(22, width))


def stamp_height(width: int) -> int:
    """Rows kept white at the bottom of each label of a split ticket, for its "n/N  #code" line."""
    return round(_stamp_size(width) * 1.8)


def _stamp(label: Image.Image, n: int, total: int, code: str) -> None:
    """ "n/N" at the bottom left of a label and "#code" at the bottom right, like a ticket's own
    footer: each label of a split ticket can be snapped and read back on its own, and put in order."""
    from PIL import ImageDraw

    from .render import MARGIN, _font
    w, h = label.size
    size = _stamp_size(w)
    margin, bottom = max(4, _scaled(MARGIN, w)), h - round(size * 0.3)
    left, right = f"{n}/{total}", f"#{code}" if code else ""
    d = ImageDraw.Draw(label)
    font = _font(size, True)
    while size > 8 and d.textlength(left, font=font) + d.textlength(right, font=font) + size > w - 2 * margin:
        size -= 1  # a narrow label (17 mm): smaller type rather than the code running off the edge
        font = _font(size, True)
    d.text((margin, bottom), left, font=font, fill="black", anchor="ld")
    if right:
        d.text((w - margin, bottom), right, font=font, fill="black", anchor="rd")


def label_pages(img: Image.Image, cfg: PrinterConfig, code: str | None = None) -> list[Image.Image]:
    """The labels a ticket comes out on. Continuous rolls and other printers: the ticket as it is.

    A Brother die-cut label has one size, and brother_ql refuses an image of any other. So the
    ticket is fitted to the label's width, then a short one is padded with white below it, and one
    up to SHRINK_ONTO_ONE times too long is shrunk onto one label (as paper.single_pages does). A
    longer one is split over several labels between rows of white (just below a row's dotted line
    where there is one: paper._break_at), so a line of text or a checkbox is never cut in half, and
    each of those labels says "n/N  #code" along its bottom edge, so any one of them can be snapped
    and read back. `code` is the ticket's code (default: the one with_code put on the image).
    A round label gets the whole ticket, shrunk and centred inside its circle."""
    spec = die_cut_spec(cfg)
    if spec is None:
        return [img]
    from brother_ql.labels import FormFactor

    from .paper import _break_at
    w, h = spec.dots_printable
    work = img.convert("L" if img.mode in ("1", "L") else "RGB")
    code = str(img.info.get(CODE_INFO, "")) if code is None else code

    def page(piece: Image.Image | None, at: tuple[int, int] = (0, 0),
             stamp: tuple[int, int] | None = None) -> Image.Image:
        out = Image.new(work.mode, (w, h), "white")
        if piece is not None:
            out.paste(piece, at)
        if stamp:
            _stamp(out, *stamp, code)
        return out.point(lambda p: 0 if p < 128 else 255, mode="1") if img.mode == "1" else out

    box = _ink_box(work)
    if spec.form_factor == FormFactor.ROUND_DIE_CUT:
        if box is None:
            return [page(None)]
        work = work.crop(box)
        side = int(min(w, h) / math.sqrt(2))  # the biggest square inside the circle
        scale = min(1.0, side / work.width, side / work.height)
        if scale < 1:
            work = work.resize((max(1, round(work.width * scale)), max(1, round(work.height * scale))), Image.LANCZOS)
        return [page(work, ((w - work.width) // 2, (h - work.height) // 2))]
    if work.width != w:
        work = work.resize((w, max(1, round(work.height * w / work.width))), Image.LANCZOS)
        box = _ink_box(work)
    if work.height > h:  # white below the last line doesn't need a label of its own
        work = work.crop((0, 0, w, max(1, box[3]) if box else h))
    if work.height <= h:
        return [page(work)]
    if work.height <= h * SHRINK_ONTO_ONE:  # a little too long: one label, a little smaller, code and all
        from .render import MARGIN
        fit = h - max(2, _scaled(MARGIN, w) // 2)  # the ticket's own white edge below its last line
        small = work.resize((max(1, round(w * fit / work.height)), fit), Image.LANCZOS)
        return [page(small, ((w - small.width) // 2, 0))]
    gray, pieces, top = work.convert("L"), [], 0
    usable = h - stamp_height(w)  # the bottom of each label is kept for "n/N  #code"
    while top < work.height:
        end = _break_at(gray, top, usable)
        piece = work.crop((0, top, w, end))
        if _ink_box(piece) is not None:
            pieces.append(piece)
        top = end
    if len(pieces) < 2:
        return [page(pieces[0] if pieces else None)]
    return [page(piece, stamp=(n, len(pieces))) for n, piece in enumerate(pieces, start=1)]


def brother_bytes(img: Image.Image, cfg: PrinterConfig) -> bytes:
    """One raster job. A die-cut roll gets one page per label (label_pages), so the printer feeds
    to the next label (and cuts, if cutting is on) between them."""
    from brother_ql.conversion import convert
    from brother_ql.raster import BrotherQLRaster

    qlr = BrotherQLRaster(cfg.model)
    qlr.exception_on_warning = True
    red = cfg.ink == "black_red"
    return convert(qlr=qlr, images=[p.convert("RGB") for p in label_pages(img, cfg)],
                   label=cfg.label + ("red" if red and not cfg.label.endswith("red") else ""),
                   rotate="0", threshold=70.0, dither=False, compress=True, red=red, dpi_600=False,
                   hq=True, cut=cfg.cut)


def trouble_hint(cfg: PrinterConfig) -> str:
    """What else to check when a printer didn't take a job. A Brother QL whose roll doesn't match
    its Label size stops with a flashing red light, and then doesn't answer until it's restarted.
    Only names from BROTHER_LABELS are shown, never a typed value."""
    if cfg.driver != "brother_ql":
        return ""
    name = next((n for i, n, _ in BROTHER_LABELS if i == cfg.label), "")
    return (" If its light is flashing red, the label roll may not match the Label size in Admin → Printers"
            + (f" (this printer: {name})" if name else "") + ". Switch it off and on to clear it.")


def encode(img: Image.Image, cfg: PrinterConfig) -> bytes:
    if cfg.driver == "escpos":
        return escpos_bytes(img, cfg.cut)
    if cfg.driver == "zpl":
        return zpl_bytes(img, round(cfg.label_height_mm / 25.4 * cfg.dpi))
    if cfg.driver == "tspl":
        return tspl_bytes(img, cfg.dpi, cfg.label_height_mm, cfg.gap_mm)
    if cfg.driver == "brother_ql":
        return brother_bytes(img, cfg)
    raise ValueError(f"{cfg.driver} has no byte encoding")


# --- transports -------------------------------------------------------------------------
def _send_tcp(address: str, data: bytes) -> None:
    host, port = _host_port(address)
    with socket.create_connection((host, port), timeout=10) as s:
        s.sendall(data)


def _send_cups(img: Image.Image, cfg: PrinterConfig) -> None:
    if not shutil.which("lp"):
        raise RuntimeError("CUPS isn't installed on this computer (no `lp` command)")
    with tempfile.NamedTemporaryFile(suffix=".png") as fh:
        img.save(fh.name)
        r = subprocess.run(["lp", "-d", cfg.address, "-o", "fit-to-page", fh.name],
                           capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"CUPS refused the job: {r.stderr.strip()[:200]}")


def _send_raster(img: Image.Image, target) -> None:
    """The only function that talks to real hardware."""
    cfg = target if isinstance(target, PrinterConfig) else legacy_config(target)
    if cfg.driver == "cups":
        _send_cups(img, cfg)
        return
    if cfg.driver == "brother_ql":
        from brother_ql.backends.helpers import send

        usb = cfg.address.startswith("usb://")
        result = send(instructions=brother_bytes(img, cfg),
                      printer_identifier=cfg.address if usb else f"tcp://{_host_port(cfg.address)[0]}:{_host_port(cfg.address)[1]}",
                      backend_identifier="pyusb" if usb else "network", blocking=True)
        if isinstance(result, dict) and result.get("outcome") == "error":
            raise RuntimeError(f"printer reported an error: {result}")
        return
    data = encode(img, cfg)
    if _is_device(cfg.address):
        with open(cfg.address, "wb") as fh:
            fh.write(data)
    else:
        _send_tcp(cfg.address, data)


_thread_lock = threading.Lock()


def print_image(img: Image.Image, settings: Settings, dry_run: bool, cfg: PrinterConfig | None = None) -> bool:
    """Returns True if the ticket went to the printer, False for a dry run.

    One job at a time across every printer: overlapping sends from two people (or the CLI and
    the server) would garble or drop labels.
    """
    if dry_run or settings.dry_run:
        return False
    cfg = cfg or legacy_config(settings)
    if not cfg.address:
        raise RuntimeError("No printer address set. An admin can add one under Admin → Printers.")
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    with _thread_lock, open(Path(settings.data_dir) / ".printer.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            _send_raster(img, cfg)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return True
