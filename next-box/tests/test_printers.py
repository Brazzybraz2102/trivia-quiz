"""Any thermal printer: drivers, fitting tickets to each printer, and label-color routing."""
import dataclasses
import os
import socket
import stat
import threading

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from nextbox import jobs, printer
from nextbox.auth import Accounts
from nextbox.printer import PrinterConfig, escpos_bytes, tspl_bytes, zpl_bytes
from nextbox.printers import PrinterError, Printers, choose, validate
from nextbox.render import ACCENT, finalize
from nextbox.server import create_app


def checker(w=20, h=3):
    img = Image.new("1", (w, h), 1)
    img.putpixel((0, 0), 0)          # one black dot top-left
    img.putpixel((w - 1, h - 1), 0)  # and bottom-right
    return img


# --- encoders ----------------------------------------------------------------
def test_escpos_raster_command():
    data = escpos_bytes(checker(), cut=True)
    assert data.startswith(b"\x1b@\x1dv0\x00") and data.endswith(b"\x1bd\x04\x1dVB\x00")
    header = data[2:10]
    assert header[4:6] == bytes([3, 0]) and header[6:8] == bytes([3, 0])  # 3 bytes wide (20 dots), 3 rows
    rows = data[10:19]
    assert rows[0] == 0x80 and rows[8] == 0x10  # black dots are 1 bits (x=19 -> 4th bit of byte 3)


def test_escpos_splits_tall_tickets_into_bands():
    data = escpos_bytes(Image.new("1", (8, 600), 1), cut=False)
    assert data.count(b"\x1dv0\x00") == 3


def test_zpl_graphic_field():
    text = zpl_bytes(checker(), label_height_dots=100).decode()
    assert text.startswith("^XA^PW24^LL100^FO0,0^GFA,9,9,3,") and text.endswith("^FS^XZ")
    assert text.split(",")[-1].startswith("800000")


def test_tspl_bitmap_prints_zero_bits():
    data = tspl_bytes(checker(), dpi=203)
    head, _, rest = data.partition(b"BITMAP 0,0,3,3,0,")
    assert b"SIZE 3.0 mm" in head and rest.endswith(b"\r\nPRINT 1,1\r\n")
    assert rest[0] == 0x7F  # TSPL: a 0 bit is a printed dot


def test_network_send_delivers_the_bytes():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = []

    def accept():
        conn, _ = srv.accept()
        got.append(b"".join(iter(lambda: conn.recv(4096), b"")))
        conn.close()
    t = threading.Thread(target=accept)
    t.start()
    payload = zpl_bytes(checker())
    printer._send_tcp(f"127.0.0.1:{srv.getsockname()[1]}", payload)
    t.join(5)
    srv.close()
    assert got == [payload]


def test_cups_hands_the_png_to_lp(tmp_path, monkeypatch):
    log = tmp_path / "lp.log"
    fake = tmp_path / "lp"
    fake.write_text(f'#!/bin/sh\necho "$@" > {log}\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    printer._send_cups(Image.new("1", (10, 10), 1), PrinterConfig("c", "Dymo", "cups", "DYMO_LW", 696))
    assert log.read_text().startswith("-d DYMO_LW -o fit-to-page")


# --- fitting & red ink -----------------------------------------------------------
def test_finalize_fits_width_and_keeps_red_only_for_two_color():
    img = Image.new("RGB", (696, 100), "white")
    for x in range(100, 300):
        for y in range(20, 60):
            img.putpixel((x, y), ACCENT)
    mono = finalize(img, 384)
    assert mono.mode == "1" and mono.size == (384, 55)
    assert mono.getpixel((110, 22)) == 0  # red prints black on a one-color printer
    red = finalize(img, 696, red=True)
    assert red.getpixel((150, 30)) == (255, 0, 0)


# --- validation & routing ----------------------------------------------------------
def test_validation():
    ok = validate({"name": "Kitchen", "driver": "escpos", "address": "192.168.1.60:9100", "stock_color": "yellow"})
    assert ok.width_px == 576 and ok.dpi == 203
    for bad in ({"name": "x", "driver": "laser", "address": "1.2.3.4"},
                {"name": "x", "driver": "escpos", "address": "/etc/passwd"},
                {"name": "x", "driver": "escpos", "address": "file:///etc/passwd"},
                {"name": "x", "driver": "zpl", "address": "usb://0x04f9:0x20a7"},
                {"name": "x", "driver": "cups", "address": "192.168.1.5:9100/../x"},
                {"name": "x", "driver": "escpos", "address": "1.2.3.4; rm -rf"},
                {"name": "x", "driver": "zpl", "address": "1.2.3.4", "ink": "black_red"},
                {"name": "", "driver": "zpl", "address": "1.2.3.4"},
                {"name": "x", "driver": "zpl", "address": "1.2.3.4", "stock_color": "plaid"}):
        with pytest.raises(PrinterError):
            validate(bad)


def test_choose_by_label_color():
    white = PrinterConfig("w", "Office", "brother_ql", "1.1.1.1", 696)
    red = PrinterConfig("r", "Red roll", "escpos", "1.1.1.2", 576, stock_color="red")
    prefs = {"color_rules": {"overdue": "red", "note": "yellow"}}
    assert choose([white, red], prefs, "overdue") == (red, "")
    assert choose([white, red], prefs, "today") == (white, "")
    p, note = choose([white, red], prefs, "note")
    assert p is white and "No printer has yellow labels" in note
    assert choose([white, red], {**prefs, "default_printer": "r"}, "today")[0] is red
    assert choose([], prefs, "today") == (None, "")


def test_first_run_turns_env_printer_into_the_list(tmp_path, ctx):
    ctx.settings.printer_ip = "192.168.1.50"
    reg = Printers(tmp_path, ctx.settings)
    assert [p.address for p in reg.all()] == ["192.168.1.50"]
    reg.add({"name": "Red", "driver": "zpl", "address": "192.168.1.70", "stock_color": "red"})
    assert [p.stock_color for p in Printers(tmp_path, ctx.settings).all()] == ["white", "red"]


# --- end to end through printing -------------------------------------------------------
@pytest.fixture
def two_printers(ctx, tmp_path):
    reg = Printers(tmp_path / "p", ctx.settings)
    office = reg.add({"name": "Office QL", "driver": "brother_ql", "address": "192.168.1.50", "ink": "black_red"})
    red = reg.add({"name": "Red roll", "driver": "escpos", "address": "192.168.1.60", "stock_color": "red"})
    return dataclasses.replace(ctx, printers=reg), office, red


def test_overdue_goes_to_red_labels(two_printers):
    ctx, office, red = two_printers
    r = jobs.print_task(ctx, "3")  # SAMPLE task 3 is overdue
    assert r["reason"] == "overdue" and r["printer"] == {"id": red.id, "name": "Red roll", "color": "red"}
    assert Image.open(r["png"]).width == 576  # fitted to the 80 mm receipt printer
    assert jobs.print_task(ctx, "1")["printer"]["id"] == office.id  # urgent, no rule -> default


def test_split_overdue_prints_two_tickets(two_printers):
    ctx, office, red = two_printers
    ctx = dataclasses.replace(ctx, prefs={**ctx.prefs, "split_overdue": True})
    r = jobs.print_today(ctx)
    assert r["printer"]["id"] == office.id and len(r["also"]) == 1
    extra = r["also"][0]
    assert extra["title"] == "Overdue" and extra["printer"]["color"] == "red"
    assert [m["task_id"] for m in extra["manifest"]] == ["3"]
    assert "3" not in [m["task_id"] for m in r["manifest"]]


def test_two_color_roll_prints_overdue_tags_in_red(two_printers):
    ctx, office, red = two_printers
    ctx = dataclasses.replace(ctx, prefs={**ctx.prefs, "color_rules": {}})  # everything to the QL
    img = Image.open(jobs.print_today(ctx)["png"]).convert("RGB")
    raw = img.tobytes()
    reds = sum(1 for i in range(0, len(raw), 3) if raw[i:i + 3] == b"\xff\x00\x00")
    assert reds > 50


# --- server ---------------------------------------------------------------------
def test_printer_admin_and_test_print(ctx, monkeypatch):
    a = Accounts(ctx.settings.data_dir)
    a.set_password("mike", "super secret", create=True)
    a.create("helper", "helper pass", role="admin")
    a.create("sam", "sam password")
    app = create_app(ctx)

    def login(n, p):
        c = TestClient(app, base_url="http://testserver")
        c.post("/auth/login", json={"username": n, "password": p})
        return c
    helper, sam = login("helper", "helper pass"), login("sam", "sam password")
    body = {"name": "Kitchen", "driver": "zpl", "address": "192.168.1.70", "stock_color": "red"}
    assert sam.post("/admin/printers", json=body).status_code == 403
    pid = helper.post("/admin/printers", json=body).json()["id"]
    opts = sam.get("/printing-options").json()
    assert opts["printers"][0]["stock_color"] == "red" and "address" not in opts["printers"][0]
    assert "overdue" in opts["reasons"] and "red" in opts["colors"]
    monkeypatch.setattr(printer, "is_reachable", lambda p, timeout=2.0: True)
    assert helper.get("/admin/printers/status").json() == {pid: True}
    t = helper.post(f"/admin/printers/{pid}/test", params={"dry_run": True}).json()
    assert t["status"] == "dry_run" and t["printer"]["id"] == pid and t["title"] == "TEST PRINT"
    assert helper.patch(f"/admin/printers/{pid}", json={"stock_color": "plaid"}).status_code == 400
    assert sam.patch("/settings", json={"color_rules": {"overdue": "green"}}).status_code == 200
    assert sam.patch("/settings", json={"color_rules": {"whenever": "red"}}).status_code == 400
    helper.delete(f"/admin/printers/{pid}")
    assert sam.get("/printing-options").json()["printers"] == []
