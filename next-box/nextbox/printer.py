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

DRIVERS = {
    "brother_ql": {"name": "Brother QL / PT label printer", "address": "192.168.1.50 or usb://0x04f9:0x20a7",
                   "width": 696, "dpi": 300},
    "escpos": {"name": "ESC/POS receipt printer (Epson, Star, 58/80 mm)", "address": "192.168.1.60 or /dev/usb/lp0",
               "width": 576, "dpi": 203},
    "zpl": {"name": "ZPL label printer (Zebra and compatibles)", "address": "192.168.1.70", "width": 812, "dpi": 203},
    "tspl": {"name": "TSPL label printer (TSC, Munbyn, iDPRT, Xprinter)", "address": "192.168.1.80 or /dev/usb/lp0",
             "width": 812, "dpi": 203},
    "cups": {"name": "Any printer installed on this computer (CUPS)", "address": "printer name from `lpstat -p`",
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
    host, port = _host_port(address)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
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


def brother_bytes(img: Image.Image, cfg: PrinterConfig) -> bytes:
    from brother_ql.conversion import convert
    from brother_ql.raster import BrotherQLRaster

    qlr = BrotherQLRaster(cfg.model)
    qlr.exception_on_warning = True
    red = cfg.ink == "black_red"
    return convert(qlr=qlr, images=[img.convert("RGB")], label=cfg.label + ("red" if red and not cfg.label.endswith("red") else ""),
                   rotate="0", threshold=70.0, dither=False, compress=True, red=red, dpi_600=False,
                   hq=True, cut=cfg.cut)


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
