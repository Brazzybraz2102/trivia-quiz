from PIL import Image

from nextbox import printer
from nextbox.config import Settings


def test_dry_run_never_sends(tmp_path):
    img = Image.new("1", (696, 100), 1)
    assert printer.print_image(img, Settings(printer_ip="10.0.0.9"), dry_run=True) is False
    assert printer.print_image(img, Settings(printer_ip="10.0.0.9", dry_run=True), dry_run=False) is False


def test_raster_conversion_works_for_ql1110(tmp_path):
    # Build the raster instructions without sending them anywhere.
    from brother_ql.conversion import convert
    from brother_ql.raster import BrotherQLRaster

    img = Image.new("RGB", (696, 300), "white")
    data = convert(qlr=BrotherQLRaster("QL-1110NWB"), images=[img], label="62", rotate="0",
                   threshold=70.0, dither=False, compress=True, red=False, cut=True)
    assert len(data) > 100


def test_unreachable_printer():
    assert printer.is_reachable("") is False
    assert printer.is_reachable("192.0.2.1", timeout=0.2) is False
