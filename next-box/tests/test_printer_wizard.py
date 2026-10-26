"""Set up a printer: the walkthrough that replaced Admin → Printers' raw form as the normal way to add a printer.

Mike's real printer, from this week: a Brother QL-1110NWB on Wi-Fi at 192.168.68.110 (Bonjour name
BRW5413795FE5CF.local), announcing _pdl-datastream._tcp on 9100 (usb_MDL=QL-1110NWB, ty="Brother QL-1110NWB",
product="(Brother QL-1110NWB)"), _ipp._tcp and _printer._tcp, and a status page saying "Media Type  102mm x 152mm /
4.0" x 6.0"". Setting it up by hand went wrong four ways: he couldn't find its address (a search grabbed a TV's), the
label size stayed at the 62 mm default (the printer flashed red), "Preview only" was locked on, and the form asked
for driver, model, label size, color, print width in dots and label height in mm.

Nothing here reaches a real network or a printer: the search runs against a stand-in zeroconf module or a stand-in
avahi-browse, the status page is a tiny HTTP server on 127.0.0.1, a fixture refuses any other connection, and the
conftest printer guard (no_real_printer) still fails any test that tries to print.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nextbox import discover, jobs, printer
from nextbox.auth import Accounts
from nextbox.printer import BROTHER_LABELS, COMMON_ROLLS
from nextbox.printers import PrinterError, validate
from nextbox.server import create_app

from conftest import rows

WEB = Path(__file__).resolve().parent.parent / "web"
ROOT = WEB.parent


# --- no network but local stand-ins -------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def only_local_network(monkeypatch):
    """Any connection, packet or name lookup that isn't this computer's own loopback fails the test."""
    real_connect, real_sendto, real_getaddrinfo = socket.socket.connect, socket.socket.sendto, socket.getaddrinfo

    def local(host) -> bool:
        try:
            return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
        except ValueError:
            return str(host) == "localhost"

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and self.type == socket.SOCK_STREAM and not local(address[0]):
            raise AssertionError(f"tests must not connect to {address[0]}")
        return real_connect(self, address)  # (a UDP "connect" sends nothing: printers._own_addresses uses one)

    def sendto(self, data, *args):
        if not local(args[-1][0]):
            raise AssertionError(f"tests must not send to {args[-1][0]}")
        return real_sendto(self, data, *args)

    def getaddrinfo(host, *args, **kwargs):
        try:
            ipaddress.ip_address(str(host))
        except ValueError:
            if host not in ("localhost", None) and host != socket.gethostname():
                raise AssertionError(f"tests must not look up {host}") from None
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


# --- what Mike's printer announces ------------------------------------------------------------------------------------
MIKE_TXT = ('"txtvers=1" "qtotal=1" "pdl=application/octet-stream,application/vnd.brother-hbp" '
            '"ty=Brother QL-1110NWB" "product=(Brother QL-1110NWB)" "usb_MFG=Brother" "usb_MDL=QL-1110NWB" '
            '"note=" "priority=75"')
MIKE_AVAHI = [
    "+;wlan0;IPv4;Brother\\032QL-1110NWB;_pdl-datastream._tcp;local",
    f"=;wlan0;IPv4;Brother\\032QL-1110NWB;_pdl-datastream._tcp;local;BRW5413795FE5CF.local;192.168.68.110;9100;{MIKE_TXT}",
    f"=;wlan0;IPv6;Brother\\032QL-1110NWB;_pdl-datastream._tcp;local;BRW5413795FE5CF.local;fe80::3e2a:f4ff:fe8d:8c2b;9100;{MIKE_TXT}",
    f"=;wlan0;IPv4;Brother\\032QL-1110NWB;_ipp._tcp;local;BRW5413795FE5CF.local;192.168.68.110;631;{MIKE_TXT}",
    f"=;wlan0;IPv4;Brother\\032QL-1110NWB;_printer._tcp;local;BRW5413795FE5CF.local;192.168.68.110;515;{MIKE_TXT}",
]


def test_avahi_output_is_read_like_mikes_printer():
    found = discover.merge(discover.parse_avahi(MIKE_AVAHI))
    assert len(found) == 1  # three services and an IPv6 line: one printer
    f = found[0]
    assert (f.address, f.host, f.name, f.model) == ("192.168.68.110", "BRW5413795FE5CF.local", "Brother QL-1110NWB",
                                                    "QL-1110NWB")
    assert (f.driver, f.brother_model, f.port) == ("brother_ql", "QL-1110NWB", 9100)
    assert sorted(f.services) == ["_ipp._tcp", "_pdl-datastream._tcp", "_printer._tcp"]


def test_the_model_comes_from_any_of_its_announcements():
    for txt in ({"usb_MDL": "QL-1110NWB"}, {"ty": "Brother QL-1110NWB"}, {"product": "(Brother QL-1110NWB)"}):
        f = discover.merge([{"service": "_ipp._tcp", "name": "x", "host": "h.local", "address": "192.168.1.9", "txt": txt}])
        assert f[0].brother_model == "QL-1110NWB" and f[0].driver == "brother_ql", txt
    assert discover.brother_model("Brother QL-1100") == "QL-1100"  # not mistaken for the QL-1110NWB or the other way
    assert discover.brother_model("QL-1110NWB") == "QL-1110NWB"
    assert discover.brother_model("Brother QL-820NWB") == "QL-820NWB"
    assert discover.guess_driver("Zebra ZD421") == "zpl" and discover.guess_driver("MUNBYN ITPP941") == "tspl"
    assert discover.guess_driver("EPSON TM-m30II") == "escpos"
    assert discover.guess_driver("HP OfficeJet Pro 9010") is None  # a regular printer


def test_a_regular_printer_is_listed_after_the_label_printer_and_link_local_addresses_are_left_out():
    records = discover.parse_avahi(MIKE_AVAHI) + [
        {"service": "_ipp._tcp", "name": "HP OfficeJet", "host": "HP1.local", "address": "192.168.68.20",
         "txt": {"ty": "HP OfficeJet Pro 9010"}},
        {"service": "_ipp._tcp", "name": "Lost printer", "host": "x.local", "address": "169.254.3.4", "txt": {}},
    ]
    found = discover.merge(records)
    assert [f.address for f in found] == ["192.168.68.110", "192.168.68.20"]
    assert found[1].driver is None and found[1].name == "HP OfficeJet Pro 9010"


# --- the two ways to search, stubbed ------------------------------------------------------------------------------
class FakeInfo:
    def __init__(self, server, address, port, props):
        self.server, self.port, self.properties, self._address = server, port, props, address

    def parsed_addresses(self, version=None):
        return [self._address]


def fake_zeroconf(monkeypatch, heard, slow=0.0):
    """A stand-in python-zeroconf: the browser hears `heard` [(type, name, FakeInfo)] at once."""
    mod = types.ModuleType("zeroconf")
    mod.IPVersion = types.SimpleNamespace(V4Only="v4", All="all")
    mod.ServiceStateChange = types.SimpleNamespace(Added="added", Removed="removed")
    made = []

    class Zeroconf:
        def __init__(self, ip_version=None):
            self.closed = False
            made.append(self)

        def get_service_info(self, type_, name, timeout=3000):
            if slow:
                time.sleep(min(slow, timeout / 1000))
                return None
            return next((i for t, n, i in heard if (t, n) == (type_, name)), None)

        def close(self):
            self.closed = True

    class ServiceBrowser:
        def __init__(self, zc, types_, handlers=None):
            self.cancelled = False
            for t, n, _ in heard:
                if t in types_:
                    for h in handlers:
                        h(zeroconf=zc, service_type=t, name=n, state_change="added")

        def cancel(self):
            self.cancelled = True

    mod.Zeroconf, mod.ServiceBrowser = Zeroconf, ServiceBrowser
    monkeypatch.setitem(sys.modules, "zeroconf", mod)
    return made


MIKE_PROPS = {b"usb_MDL": b"QL-1110NWB", b"ty": b"Brother QL-1110NWB", b"product": b"(Brother QL-1110NWB)", b"note": None}


def test_zeroconf_finds_mikes_printer(monkeypatch):
    info = FakeInfo("BRW5413795FE5CF.local.", "192.168.68.110", 9100, MIKE_PROPS)
    made = fake_zeroconf(monkeypatch, [
        ("_pdl-datastream._tcp.local.", "Brother QL-1110NWB._pdl-datastream._tcp.local.", info),
        ("_ipp._tcp.local.", "Brother QL-1110NWB._ipp._tcp.local.",
         FakeInfo("BRW5413795FE5CF.local.", "192.168.68.110", 631, MIKE_PROPS)),
    ])
    start = time.monotonic()
    found, how = discover.find(limit=0.4)
    assert how == "zeroconf" and time.monotonic() - start < 2
    assert [(f.address, f.host, f.name, f.brother_model, f.port) for f in found] == [
        ("192.168.68.110", "BRW5413795FE5CF.local", "Brother QL-1110NWB", "QL-1110NWB", 9100)]
    assert made and all(z.closed for z in made)  # it stops listening when it's done


def test_zeroconf_stops_at_the_time_limit(monkeypatch):
    info = FakeInfo("a.local.", "192.168.68.110", 9100, MIKE_PROPS)
    heard = [("_ipp._tcp.local.", f"Printer {i}._ipp._tcp.local.", info) for i in range(20)]
    fake_zeroconf(monkeypatch, heard, slow=5.0)  # every printer takes ages to answer
    start = time.monotonic()
    found, _ = discover.find(limit=1.0)
    assert time.monotonic() - start < 1.8 and found == []


@pytest.fixture
def fake_avahi(tmp_path, monkeypatch):
    """A stand-in avahi-browse on PATH (and no zeroconf), printing Mike's lines; `slow` keeps it running."""
    monkeypatch.setattr(discover, "_have_zeroconf", lambda: False)

    def make(lines, slow=False):
        out = tmp_path / "avahi-lines.txt"
        out.write_text("\n".join(lines) + "\n")
        exe = tmp_path / "bin" / "avahi-browse"
        exe.parent.mkdir(exist_ok=True)
        exe.write_text(f"#!/bin/sh\ncat '{out}'\n" + ("exec sleep 30\n" if slow else ""))
        exe.chmod(0o755)
        monkeypatch.setenv("PATH", f"{exe.parent}{os.pathsep}{os.environ.get('PATH', '')}")
        return exe
    return make


def test_avahi_browse_finds_mikes_printer(fake_avahi):
    fake_avahi(MIKE_AVAHI)
    found, how = discover.find(limit=2.0)
    assert how == "avahi" and [f.address for f in found] == ["192.168.68.110"]
    assert found[0].brother_model == "QL-1110NWB"


def test_avahi_browse_is_stopped_at_the_time_limit_and_what_it_said_still_counts(fake_avahi):
    fake_avahi(MIKE_AVAHI, slow=True)
    start = time.monotonic()
    found, how = discover.find(limit=1.0)
    assert time.monotonic() - start < 2.5
    assert how == "avahi" and [f.address for f in found] == ["192.168.68.110"]


def test_a_search_that_cant_start_falls_back_or_says_so(monkeypatch, fake_avahi):
    def broken(*a, **k):
        raise OSError("no multicast here")
    fake_zeroconf(monkeypatch, [])
    monkeypatch.setattr(discover, "_have_zeroconf", lambda: True)
    monkeypatch.setattr(discover, "browse_zeroconf", broken)
    fake_avahi(MIKE_AVAHI)
    found, how = discover.find(limit=1.0)  # zeroconf couldn't listen: avahi-browse instead
    assert how == "avahi" and [f.address for f in found] == ["192.168.68.110"]
    monkeypatch.setattr(discover.shutil, "which", lambda name: None)
    with pytest.raises(discover.FindError) as err:
        discover.find(limit=0.2)
    assert str(err.value) == discover.COULDNT


def test_with_nothing_to_search_with_it_says_so(monkeypatch):
    monkeypatch.setattr(discover, "_have_zeroconf", lambda: False)
    monkeypatch.setattr(discover.shutil, "which", lambda name: None)
    with pytest.raises(discover.FindError) as err:
        discover.find(limit=0.2)
    assert str(err.value) == discover.NOT_AVAILABLE and "Type the printer's address" in str(err.value)


# --- who may search, and read a roll ------------------------------------------------------------------------------
@pytest.fixture
def site(ctx, monkeypatch):
    """boss (staff) and ann (an admin) and mike (a plain user) in Home; kim with her own household (its admin) and
    lee, whom she invited."""
    monkeypatch.setattr(printer, "is_reachable", lambda *a, **k: False)  # /status never knocks on a printer
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("ann", "ann password", role="admin")
    a.create("mike", "mike password")
    app = create_app(ctx)

    def client():
        return TestClient(app, base_url="http://testserver")

    def login(name, pw):
        c = client()
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c

    boss = login("boss", "boss password")
    own = boss.post("/admin/invites", json={"own_household": True}).json()["code"]
    assert client().post("/auth/signup", json={"username": "kim", "password": "kim password", "invite": own,
                                               "agree": True}).status_code == 200
    kim = login("kim", "kim password")
    code = kim.post("/admin/invites", json={}).json()["code"]
    assert client().post("/auth/signup", json={"username": "lee", "password": "lee password", "invite": code,
                                               "agree": True}).status_code == 200
    people = {n: login(n, f"{n} password") for n in ("ann", "mike", "lee")}
    return ctx, {"boss": boss, "kim": kim, **people}


def mikes_printer():
    return [discover.Found(address="192.168.68.110", host="BRW5413795FE5CF.local", name="Brother QL-1110NWB",
                           model="QL-1110NWB", driver="brother_ql", brother_model="QL-1110NWB",
                           services=["_pdl-datastream._tcp"])]


def test_only_staff_and_home_admins_may_search(site, monkeypatch):
    ctx, p = site
    calls = []
    monkeypatch.setattr(discover, "find", lambda: (calls.append(1) or (mikes_printer(), "zeroconf")))
    for name in ("boss", "ann"):
        r = p[name].post("/admin/printers/discover")
        assert r.status_code == 200, (name, r.text)
        assert r.json()["printers"] == [dataclasses.asdict(mikes_printer()[0])]
        assert r.json()["how"] == "zeroconf" and r.json()["why"] == ""
    assert len(calls) == 2
    for name in ("mike", "lee"):  # plain users: never
        assert p[name].post("/admin/printers/discover").status_code == 403, name
    r = p["kim"].post("/admin/printers/discover")  # a friend's household: their printers aren't on this network
    assert r.status_code == 403 and r.json()["detail"] == jobs.PRINT_AWAY
    assert len(calls) == 2  # none of those searched
    logged = [e for e in rows(ctx.settings.data_dir, "events") if e["action"] == "printer_discover"]
    assert [(e["user"], e["kind"], e["detail"]) for e in logged] == [("boss", "audit", {"found": 1, "how": "zeroconf"}),
                                 ("ann", "audit", {"found": 1, "how": "zeroconf"})]


def test_a_search_that_cant_run_says_why_instead_of_failing(site, monkeypatch):
    _, p = site
    monkeypatch.setattr(discover, "_have_zeroconf", lambda: False)
    monkeypatch.setattr(discover.shutil, "which", lambda name: None)
    r = p["boss"].post("/admin/printers/discover")
    assert r.status_code == 200 and r.json() == {"printers": [], "how": None, "why": discover.NOT_AVAILABLE}


def test_the_search_sends_nothing_to_a_printer(site, monkeypatch):
    """It only listens: the stand-in browser is all it uses, and the conftest guard would fail any print."""
    _, p = site
    fake_zeroconf(monkeypatch, [("_pdl-datastream._tcp.local.", "Brother QL-1110NWB._pdl-datastream._tcp.local.",
                                 FakeInfo("BRW5413795FE5CF.local.", "192.168.68.110", 9100, MIKE_PROPS))])
    monkeypatch.setattr(discover, "TIME_LIMIT", 0.3)
    r = p["ann"].post("/admin/printers/discover")
    assert r.status_code == 200 and [f["address"] for f in r.json()["printers"]] == ["192.168.68.110"]


# --- which labels are in it: the printer's own status page --------------------------------------------------------
def brother_page(media: str, model: str = "QL-1110NWB") -> str:
    """Like Mike's printer's Status page. Its menu has a "Media Type" link too, which mustn't be taken for the roll."""
    return f"""<!DOCTYPE html><html><head><title>Brother {model}</title><style>dt{{color:#333}}</style></head><body>
<div id="modelName"><h1>{model}</h1></div>
<ul id="navi"><li><a href="/general/status.html">Status</a></li><li><a href="/printer/media.html">Media Type</a></li></ul>
<dl class="items">
<dt>Device Status</dt><dd><span class="moni moniOk">READY</span></dd>
<dt>Media Status</dt><dd>Not Empty</dd>
<dt>Media Type</dt><dd>{media}</dd>
</dl></body></html>"""


MIKES_MEDIA = "102mm x 152mm / 4.0&quot; x 6.0&quot;"


class StatusServer:
    """A tiny stand-in for a printer's web page on 127.0.0.1: {path: html}, an optional delay, and what was asked."""

    def __init__(self):
        self.pages, self.delay, self.asked = {}, 0.0, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.asked.append(self.path)
                if outer.delay:
                    time.sleep(outer.delay)
                page = outer.pages.get(self.path)
                try:
                    if page is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = page.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def status_page(monkeypatch):
    """The stand-in page, and discover allowed to read it on 127.0.0.1 (a test-only setting)."""
    srv = StatusServer()
    monkeypatch.setattr(discover, "HTTP_PORT", srv.port)
    monkeypatch.setattr(discover, "ALLOW_LOOPBACK", True)
    yield srv
    srv.close()


def test_mikes_status_page_says_4x6_labels(site, status_page):
    _, p = site
    status_page.pages["/general/status.html"] = brother_page(MIKES_MEDIA)
    r = p["ann"].get("/admin/printers/media", params={"address": "127.0.0.1"})
    assert r.status_code == 200, r.text
    assert r.json() == {"read": True, "why": "", "media": '102mm x 152mm / 4.0" x 6.0"', "label": "102x152",
                        "name": "102 × 152 mm labels (QL-1000 series)", "model": "QL-1110NWB", "status": "READY",
                        "media_status": "Not Empty"}
    assert status_page.asked == ["/general/status.html"]  # one look, and nothing sent to print


@pytest.mark.parametrize("media,label", [("62mm", "62"), ("62mm x 100mm", "62x100"), ("29mm x 90mm", "29x90")])
def test_other_rolls_are_read_too(site, status_page, media, label):
    _, p = site
    status_page.pages["/general/status.html"] = brother_page(media, model="QL-820NWB")
    r = p["boss"].get("/admin/printers/media", params={"address": "127.0.0.1:9100"})  # a saved address keeps its port
    assert r.status_code == 200 and (r.json()["read"], r.json()["label"], r.json()["model"]) == (True, label, "QL-820NWB")


def test_a_size_next_box_doesnt_offer_is_said_plainly(site, status_page):
    _, p = site
    status_page.pages["/general/status.html"] = brother_page("103mm x 164mm / 4.07&quot; x 6.4&quot;")  # DK-1247
    out = p["ann"].get("/admin/printers/media", params={"address": "127.0.0.1"}).json()
    assert out["read"] is False and out["label"] is None and out["media"].startswith("103mm x 164mm")
    assert out["why"] == "Next Box doesn't have a setting for that size yet (103mm x 164mm / 4.07\" x 6.4\")."


def test_an_older_page_at_the_top_level_is_read_when_status_html_isnt_there(site, status_page):
    _, p = site
    status_page.pages["/"] = brother_page("62mm")
    out = p["ann"].get("/admin/printers/media", params={"address": "127.0.0.1"}).json()
    assert out["label"] == "62" and status_page.asked == ["/general/status.html", "/"]


def test_a_page_without_media_type_says_so(site, status_page):
    _, p = site
    status_page.pages["/general/status.html"] = "<html><body><h1>Welcome</h1></body></html>"
    out = p["ann"].get("/admin/printers/media", params={"address": "127.0.0.1"}).json()
    assert out["read"] is False and out["why"] == "The printer's page doesn't say which labels are in it."


def test_a_printer_that_doesnt_answer_in_time_is_a_short_wait(site, status_page, monkeypatch):
    _, p = site
    monkeypatch.setattr(discover, "MEDIA_TIMEOUT", 0.5)
    status_page.pages["/general/status.html"] = brother_page(MIKES_MEDIA)
    status_page.delay = 3.0
    start = time.monotonic()
    out = p["ann"].get("/admin/printers/media", params={"address": "127.0.0.1"}).json()
    assert time.monotonic() - start < 2.0
    assert out["read"] is False and out["why"] == "The printer didn't answer in time. Is it switched on?"


def test_a_printer_with_no_web_page_says_so(site, monkeypatch):
    _, p = site
    with socket.socket() as s:  # a free port with nothing on it
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(discover, "HTTP_PORT", port)
    monkeypatch.setattr(discover, "ALLOW_LOOPBACK", True)
    out = p["ann"].get("/admin/printers/media", params={"address": "127.0.0.1"}).json()
    assert out["read"] is False and out["why"].startswith("The printer's web page didn't answer")


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.10.20", "8.8.8.8", "0.0.0.0", "224.0.0.251",
                                     "usb://0x04f9:0x20a7", "/dev/usb/lp0", "not an address!", "", "192.168.1.5:99999"])
def test_addresses_it_wont_read(site, monkeypatch, address):
    """Read only from a printer on the home network: never this computer (loopback, without the test-only
    setting), link-local, the internet, multicast, or anything printers.validate refuses."""
    _, p = site
    srv = StatusServer()
    try:
        monkeypatch.setattr(discover, "HTTP_PORT", srv.port)
        r = p["ann"].get("/admin/printers/media", params={"address": address})
        assert r.status_code == 400, (address, r.text)
        assert srv.asked == []
    finally:
        srv.close()
    with pytest.raises(PrinterError):
        discover.lan_address(address)


def test_only_staff_and_home_admins_may_read_a_roll(site, status_page):
    _, p = site
    status_page.pages["/general/status.html"] = brother_page(MIKES_MEDIA)
    for name in ("mike", "lee"):
        assert p[name].get("/admin/printers/media", params={"address": "127.0.0.1"}).status_code == 403
    r = p["kim"].get("/admin/printers/media", params={"address": "127.0.0.1"})
    assert r.status_code == 403 and r.json()["detail"] == jobs.PRINT_AWAY
    assert status_page.asked == []
    assert p["boss"].get("/admin/printers/media", params={"address": "127.0.0.1"}).json()["label"] == "102x152"


# --- the mapping table --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("words,label", [
    ('102mm x 152mm / 4.0" x 6.0"', "102x152"), ("102mm x 152mm / 4.0&quot; x 6.0&quot;", "102x152"),
    ('4.0" x 6.0" / 102mm x 152mm', "102x152"), ("102 mm × 152 mm", "102x152"),
    ("62mm", "62"), ("62mm Continuous", "62"), ("Continuous Length Tape 62mm", "62"), ("62 mm / 2.4\"", "62"),
    ("62mm x 100mm", "62x100"), ("29mm x 90mm", "29x90"), ("62mm x 29mm", "62x29"), ("29mm x 42mm", "29x42"),
    ("17mm x 54mm", "17x54"), ("17mm x 87mm", "17x87"), ("23mm x 23mm", "23x23"), ("38mm x 90mm", "39x90"),
    ("39mm x 48mm", "39x48"), ("52mm x 29mm", "52x29"), ("102mm x 51mm", "102x51"),
    ("12mm", "12"), ("29mm", "29"), ("38mm", "38"), ("50mm", "50"), ("54mm", "54"), ("102mm", "102"),
    ("24mm dia", "d24"), ("Ø58mm", "d58"), ("58mm Round", "d58"),
    ("DK-22205", "62"), ("DK-1241", "102x152"), ("DK-1202", "62x100"), ("DK-1201", "29x90"), ("DK-22243", "102"),
    ("103mm x 164mm", None), ("DK-1247", None), ("70mm", None), ("Not Empty", None), ("", None),
])
def test_media_words_map_to_label_sizes(words, label):
    assert discover.media_label(words) == label
    if label:
        assert label in {i for i, _, _ in BROTHER_LABELS}  # always one Next Box (and brother_ql) knows


def test_every_common_roll_is_a_real_label_size_with_its_code():
    ids = {i for i, _, _ in BROTHER_LABELS}
    assert [r[0] for r in COMMON_ROLLS] == ["62", "102x152", "62x100", "29x90", "102"]
    for label, name, code, use in COMMON_ROLLS:
        assert label in ids and code.startswith("DK-") and name and use
        assert discover.media_label(code) == label  # the code on the box means the same roll
    assert dict((r[0], r[1]) for r in COMMON_ROLLS)["102x152"] == "102 × 152 mm labels (4 × 6 in)"


def test_what_the_walkthrough_saves_is_a_valid_printer():
    """The bodies Set up a printer sends (pwSave), for Mike's printer and the other kinds: no dots typed by anyone."""
    widths = {i: w for i, _, w in BROTHER_LABELS}
    mike = validate({"name": "Label printer", "driver": "brother_ql", "address": "192.168.68.110", "stock_color": "white",
                     "ink": "black", "model": "QL-1110NWB", "label": "102x152", "width_px": widths["102x152"]})
    assert (mike.label, mike.width_px, mike.model) == ("102x152", 1164, "QL-1110NWB")
    zebra = validate({"name": "Zebra", "driver": "zpl", "address": "192.168.68.40", "width_px": round(101.6 / 25.4 * 203),
                      "dpi": 203, "label_height_mm": 152.4})
    assert (zebra.width_px, zebra.label_height_mm) == (812, 152.4)
    assert validate({"name": "Till", "driver": "escpos", "address": "192.168.68.41", "width_px": 576, "dpi": 203}).width_px == 576
    assert validate({"name": "Dymo", "driver": "cups", "address": "DYMO_LabelWriter_450"}).width_px == 696


# --- the app: the steps and the ways in ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def js():
    return (WEB / "app.js").read_text()


@pytest.fixture(scope="module")
def html():
    return (WEB / "index.html").read_text()


def fn(js, name):
    m = re.search(rf"^(?:async )?function {name}\(.*?\n}}\n", js, re.S | re.M)
    assert m, name
    return m[0]


def test_the_walkthrough_is_a_dialog_with_a_heading_back_and_next(html):
    d = html[html.index('<dialog id="pwDialog"'):]
    d = d[:d.index("</dialog>")]
    assert '<dialog id="pwDialog" aria-labelledby="pwTitle" aria-describedby="pwHint">' in html
    assert '<h2 id="pwTitle" tabindex="-1"></h2>' in d
    assert '<button type="button" class="linkish" id="pwClose">Close</button>' in d
    assert '<button type="button" class="plain" id="pwBack">← Back</button>' in d
    assert '<button type="button" class="go" id="pwNext">Next</button>' in d
    assert '<p class="err" id="pwMsg" role="alert"></p>' in d
    assert 'class="quizbar" aria-hidden="true"' in d  # the bar is decoration: "Step 2 of 5" says it in words


def test_the_five_steps_in_plain_words(js):
    for words in ('"What kind of printer?"', '"Find your printer"', '"Which labels are in it?"', '"Name it"', '"Try it"',
                  '"Brother label printer"', '"Another label printer"', '"A regular printer"', '"I don\'t have one yet"',
                  "Your printer says:", "Yes, use these", "No, let me pick", "More sizes", "Search again",
                  "Type its address instead", "BRW", "info (i) button", "router", '"Which model is it?"',
                  '"How big are the labels?"', '"How wide is the paper?"', "Did a label come out?", '"What happened?"',
                  '"Its light is flashing red"', '"Nothing happened"', '"It printed, but it looks wrong"',
                  "Print today's label", '"All set"', "No setup needed", "Print on paper",
                  "Advanced (type everything yourself)"):
        assert words in js, words
    assert "Step ${n} of 5" in fn(js, "pwDraw")
    assert '$("#pwTitle").focus();' in fn(js, "pwDraw")  # each step's heading gets focus (and is read out)
    # Never asks for dots: every width is worked out (Brother from the label, the others from mm or inches and dpi).
    wizard = js[js.index("// ---------------------------------------------------------------- Set up a printer"):
                js.index("// ---------------------------------------------------------------- usage (superadmin)")]
    assert "(dots)" not in wizard and "Print width" not in wizard and "Math.round(w / 25.4 * dpi)" in wizard


def test_the_entry_points_open_the_walkthrough(js, html):
    assert '$("#noPrinterGo").onclick = () => openPrinterSetup({ from: "settings" });' in js  # Settings, no printer
    assert '$("#labelAdd").onclick = () => openPrinterSetup({ from: "settings" });' in js     # Settings, another one
    assert '$("#prAdd").onclick = () => openPrinterSetup({ from: "admin" });' in js           # Admin → Printers
    step = js[js.index("  printer: { title:"):js.index("  preview: { title:")]                 # Get started
    assert '["Set up a printer", () => openPrinterSetup({ from: "guide" })]' in step
    assert 'if (b.dataset.p === "edit") return openPrinterSetup({ printer: p });' in js     # Edit: at the labels
    opener = fn(js, "openPrinterSetup")
    assert 'step: p.driver === "brother_ql" ? "labels"' in opener and 'back: ["kind", "find"]' in opener
    assert "if (!canAddPrinter()) return toast(noPrinterNext());" in opener  # who may add printers is unchanged
    card = html[html.index('<div class="card"><h2>Printers</h2>'):html.index('<div class="card"><h2>Invite people</h2>')]
    assert card.index('id="prAdd"') < card.index('<details id="npForm"')
    assert '<summary><b id="npTitle">Advanced (type everything yourself)</b></summary>' in card  # the full form stays
    for field in ("npDriver", "npModel", "npLabel", "npColor", "npWidth", "npHeight"):
        assert f'id="{field}"' in card
    settings = html[html.index('<div class="card" id="labelCard">'):html.index('<div class="card" id="printCard">')]
    assert 'id="labelAdd" aria-haspopup="dialog">Add a printer</button>' in settings


def test_searching_and_reading_the_roll_happen_by_themselves_but_printing_never_does(js):
    find = fn(js, "pwFind")
    assert "if (pwiz.found === null && !pwiz.searching) pwSearch();" in find  # searches on arrival
    assert 'api("/admin/printers/discover", { method: "POST" })' in fn(js, "pwSearch")
    assert "if (canRead && pwiz.read === null) return pwRead(a);" in fn(js, "pwLabels")
    assert "/admin/printers/media?address=${encodeURIComponent(address)}" in fn(js, "pwRead")
    # A real print only on the test button's tap, after "Send this to the printer?".
    test = fn(js, "pwTest")
    assert test.index('if (!confirm("Send this to the printer?")) return;') < test.index("/test?dry_run=false")
    wizard = js[js.index("// ---------------------------------------------------------------- Set up a printer"):
                js.index("// ---------------------------------------------------------------- usage (superadmin)")]
    assert wizard.count("/test?dry_run=") == 1 and "/print/" not in wizard
    today = wizard[wizard.index('if (act === "today")'):]
    today = today[:today.index("\n")]
    assert 'goto("print")' in today and "api(" not in today  # it opens Print, where the button asks first
    # The person picks the printer when there's more than one; exactly one is picked for them.
    assert "fits.length === 1 ? fits[0] : list.length === 1 ? list[0] : null" in fn(js, "pwPrePick")


def test_anything_in_the_way_of_printing_is_fixed_in_place(js):
    blockers = fn(js, "pwBlockers")
    assert "st.printing_paused" in blockers and "me?.prefs?.always_dry_run" in blockers and "st.dry_run_forced" in blockers
    assert '["Turn it off", "always"]' in blockers and '["Turn printing back on", "unpause"]' in blockers
    assert 'code: "systemctl --user restart nextbox"' in blockers and "NEXTBOX_DRY_RUN=1 is in its .env file" in blockers
    fix = fn(js, "pwFixIt")
    assert 'api("/settings", { method: "PATCH", json: { always_dry_run: false } })' in fix
    assert 'api("/super/settings", { method: "PATCH", json: { printing_paused: false } })' in fix
    # Blocked: the test button can't be pressed until it's fixed.
    assert "data-pw=\"test\"${blocks.length ? ' aria-disabled=\"true\"' : \"\"}" in fn(js, "pwShowChecks")


def test_the_printer_is_saved_at_name_it_and_the_first_is_your_usual_one(js):
    save = fn(js, "pwSave")
    assert 'pwiz.pid ? `/admin/printers/${encodeURIComponent(pwiz.pid)}` : "/admin/printers"' in save
    assert 'if (added && (pwiz.first || pwiz.usual))' in save and "default_printer: cfg.id" in save
    assert save.index("api(pwiz.pid") < save.index('pwGo("try")')
    name = fn(js, "pwName")
    assert '"Label printer"' in name and "`Brother ${pwiz.model}`" in name
    assert '(pwiz.first ? `<details id="pwMore"><summary>More: label color</summary>${color}</details>` : color)' in name


def test_closing_asks_first_when_something_was_picked(js):
    assert fn(js, "pwAsk").count("confirm(") == 1 and "if (!pwiz?.dirty) return true;" in fn(js, "pwAsk")
    keys = js[js.index('$("#pwDialog").addEventListener("keydown"'):]
    keys = keys[:keys.index("});")]
    assert 'e.key !== "Escape"' in keys and "if (pwAsk())" in keys


def test_the_troubleshooter_goes_back_to_the_right_step(js):
    fix = fn(js, "pwFix")
    assert '"Check the labels", "labels"]' in fix          # flashing red: the roll may not match
    assert '"Find it again", "find"]' in fix               # nothing happened: asleep, off, or the wrong address
    assert "doesn't match the roll" in fix                  # looks wrong: which labels


def labels_script(js, body):
    """The walkthrough's label rules, run in node with small stand-ins."""
    def const(name):  # a top-level const, to its closing "];" (or its one line)
        m = re.search(rf"^const {name} = (?:\[\n.*?^\];|[^\n]*;)\n", js, re.S | re.M)
        assert m, name
        return m[0]
    return f"""
let pwiz = {{ model: "" }};
const printOpts = {{ drivers: {{ brother_ql: {{ wide_models: ["QL-1050", "QL-1060N", "QL-1100", "QL-1110NWB", "QL-1115NWB"] }} }} }};
const pwRolls = () => printOpts.drivers.brother_ql;
{const("PW_SIZES")}{const("PW_PAPER")}{fn(js, "pwLabelFits")}{fn(js, "pwSizeOf")}
const out = {{}};
{body}
console.log(JSON.stringify(out));
"""


def run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_label_picker_only_offers_what_the_model_takes(js):
    out = run_node(labels_script(js, """
for (const m of ["QL-1110NWB", "QL-800", "PT-P750W"]) {
  pwiz.model = m;
  out[m] = ["62", "102x152", "102", "29x90", "pt24"].filter(pwLabelFits);
}
out.edit = [{ driver: "zpl", width_px: 812, dpi: 203, label_height_mm: 152.4 }, { driver: "tspl", width_px: 1200, dpi: 300, label_height_mm: 152.4 },
            { driver: "escpos", width_px: 576 }, { driver: "zpl", width_px: 700, dpi: 203, label_height_mm: 0 }].map(pwSizeOf);
"""))
    assert out["QL-1110NWB"] == ["62", "102x152", "102", "29x90"]
    assert out["QL-800"] == ["62", "29x90"]  # 102 mm labels fit the QL-1000 series only
    assert out["PT-P750W"] == ["pt24"]
    assert out["edit"] == ["4x6", "4x6", "80", "own"]  # Edit shows the size it has, or "Another size"


def test_the_docs_say_how_to_add_a_printer_now():
    readme = (ROOT / "README.md").read_text()
    assert "Settings → Add a printer and follow the steps" in readme
    spec = (ROOT / "SPEC.md").read_text()
    for words in ("Set up a printer", "/admin/printers/discover", "/admin/printers/media", "_pdl-datastream._tcp",
                  "Advanced (type everything yourself)", "Media Type"):
        assert words in spec, words
    guide = (WEB / "guide.html").read_text()
    assert "Set up a printer" in guide and "Add a printer" in guide
    deps = (ROOT / "pyproject.toml").read_text()
    assert '"zeroconf>=' in deps and re.search(r"^zeroconf==\d", (ROOT / "constraints.txt").read_text(), re.M)
    assert re.search(r"^ifaddr==\d", (ROOT / "constraints.txt").read_text(), re.M)


def test_the_conftest_printer_guard_is_still_there():
    conftest = (Path(__file__).parent / "conftest.py").read_text()
    assert "def no_real_printer(monkeypatch):" in conftest
    assert 'monkeypatch.setattr(printer, "_send_raster", boom)' in conftest
    with pytest.raises(AssertionError, match="tests must never talk to the printer"):
        printer._send_raster(None, None)
