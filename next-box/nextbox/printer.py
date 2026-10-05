"""Send images to the Brother QL-1110NWB over the network (raw TCP 9100)."""
from __future__ import annotations

import socket

from PIL import Image

from .config import Settings

PORT = 9100


def is_reachable(ip: str, timeout: float = 2.0) -> bool:
    if not ip:
        return False
    try:
        with socket.create_connection((ip, PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _send_raster(img: Image.Image, settings: Settings) -> None:
    # Imported lazily so dry-run and tests never load the printer backend.
    from brother_ql.backends.helpers import send
    from brother_ql.conversion import convert
    from brother_ql.raster import BrotherQLRaster

    qlr = BrotherQLRaster(settings.printer_model)
    qlr.exception_on_warning = True
    instructions = convert(
        qlr=qlr,
        images=[img.convert("RGB")],
        label=settings.label,
        rotate="0",
        threshold=70.0,
        dither=False,
        compress=True,
        red=False,
        dpi_600=False,
        hq=True,
        cut=True,
    )
    result = send(
        instructions=instructions,
        printer_identifier=f"tcp://{settings.printer_ip}:{PORT}",
        backend_identifier="network",
        blocking=True,
    )
    if isinstance(result, dict) and result.get("outcome") == "error":
        raise RuntimeError(f"printer reported an error: {result}")


def print_image(img: Image.Image, settings: Settings, dry_run: bool) -> bool:
    """Returns True if the label went to the printer, False for a dry run."""
    if dry_run or settings.dry_run:
        return False
    if not settings.printer_ip:
        raise RuntimeError("PRINTER_IP is not set")
    _send_raster(img, settings)
    return True
