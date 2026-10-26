"""Find a printer on the home network, and ask a Brother which labels are in it (Set up a printer, SPEC §14).

Both only listen or read. Nothing here ever sends anything to a printer to print: real prints go through
printer._send_raster only.

- find(): Bonjour (mDNS) browsing for the services printers announce: _pdl-datastream._tcp (raw printing,
  port 9100), _printer._tcp (LPD) and _ipp._tcp. It uses python-zeroconf, or avahi-browse when zeroconf isn't
  installed, for about TIME_LIMIT seconds. A printer's model comes from its announcement (usb_MDL, ty, product).
- read_media(): a Brother's own status page (http://<printer>/general/status.html, else /) says what's loaded
  ("Media Type  102mm x 152mm / 4.0" x 6.0""). media_label() turns that into a label id from
  printer.BROTHER_LABELS. A short timeout, a size cap, and only an address on the home network.
"""
from __future__ import annotations

import html
import ipaddress
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field

import httpx

from .printer import BROTHER_LABELS, BROTHER_MODELS, _host_port
from .printers import PrinterError, _own_addresses, validate

SERVICES = ("_pdl-datastream._tcp", "_printer._tcp", "_ipp._tcp")
TIME_LIMIT = 4.0       # seconds for the whole search
RESOLVE_TIME = 1.0     # of which the last second asks for the address of each printer heard (zeroconf)
NOT_AVAILABLE = ("Next Box can't search the network on this computer (it needs python-zeroconf, or avahi-browse). "
                 "Type the printer's address instead.")
COULDNT = "Next Box couldn't search the network just now. Type the printer's address instead."


class FindError(RuntimeError):
    """The search couldn't run at all (nothing to search with). Shown to the person as it is."""


@dataclass
class Found:
    """One printer heard on the network (its services merged by address)."""
    address: str                  # IPv4, what Next Box saves
    host: str = ""                # its Bonjour name, e.g. BRW5413795FE5CF.local
    name: str = ""                # what it calls itself, e.g. "Brother QL-1110NWB"
    model: str = ""               # from its announcement: usb_MDL, else ty, else product
    driver: str | None = None     # a guess at the kind: brother_ql, zpl, tspl, escpos, or None (a regular printer?)
    brother_model: str | None = None  # a printer.BROTHER_MODELS id, when it's one
    port: int = 9100              # raw printing, when it says so
    services: list[str] = field(default_factory=list)


# --- what it is, from what it announces ----------------------------------------------------------------------------
_BROTHER = sorted(BROTHER_MODELS, key=len, reverse=True)  # QL-1110NWB before QL-1100


def brother_model(text: str) -> str | None:
    """The Brother model id in a printer's own words ("Brother QL-1110NWB", "(Brother QL-1110NWB)", "QL-1110NWB")."""
    up = str(text or "").upper()
    return next((m for m in _BROTHER if re.search(rf"(?<![A-Z0-9-]){re.escape(m)}(?![A-Z0-9])", up)), None)


def guess_driver(text: str) -> str | None:
    """Which Next Box driver fits, from the model name. None: not a label printer we know (a regular printer)."""
    up = str(text or "").upper()
    if brother_model(up):
        return "brother_ql"
    if re.search(r"\bZEBRA\b|\bZTC\b|\b(ZD|ZT|GK|GX|ZQ)\d{3}", up):
        return "zpl"
    if re.search(r"\b(TSC|MUNBYN|IDPRT|XPRINTER|TSPL)\b", up):
        return "tspl"
    if re.search(r"\bTM-[A-Z0-9]|\bSTAR (TSP|MC|SP)|\bRECEIPT\b|\bESC/?POS\b", up):
        return "escpos"
    return None


def _model(txt: dict) -> tuple[str, str]:
    """(model, display name) from TXT records. Brother: usb_MDL=QL-1110NWB, ty=Brother QL-1110NWB,
    product=(Brother QL-1110NWB)."""
    low = {str(k).lower(): str(v or "").strip() for k, v in txt.items()}
    product = low.get("product", "").strip("()").strip()
    model = low.get("usb_mdl") or low.get("ty") or product
    name = low.get("ty") or product or " ".join(x for x in (low.get("usb_mfg"), low.get("usb_mdl")) if x)
    return model, name


def _usable_ip(addr: str) -> bool:
    """An IPv4 address a printer at home would have (not link-local 169.254.x.x, loopback or multicast)."""
    try:
        ip = ipaddress.IPv4Address(addr)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified)


def merge(records: list[dict]) -> list[Found]:
    """One Found per printer: a printer announces several services from the same address.
    Each record: {service, name, host, address, port, txt}."""
    out: dict[str, Found] = {}
    for r in records:
        addr = str(r.get("address") or "")
        if not _usable_ip(addr):
            continue
        service = str(r.get("service") or "").removesuffix(".").removesuffix(".local")
        f = out.setdefault(addr, Found(address=addr))
        if service and service not in f.services:
            f.services.append(service)
        model, name = _model(r.get("txt") or {})
        instance = str(r.get("name") or "")
        f.model = f.model or model
        f.name = f.name or name or instance
        f.host = f.host or str(r.get("host") or "").rstrip(".")
        if service == "_pdl-datastream._tcp" and r.get("port"):
            f.port = int(r["port"])
    for f in out.values():
        words = " ".join((f.model, f.name))
        f.brother_model = brother_model(words)
        f.driver = guess_driver(words)
        if not f.name:
            f.name = f.model or f.host or f.address
    # Label printers first (a Brother first of all), then by address, so the list doesn't jump around.
    rank = {"brother_ql": 0, "zpl": 1, "tspl": 1, "escpos": 2}
    return sorted(out.values(), key=lambda f: (rank.get(f.driver or "", 3), tuple(int(x) for x in f.address.split("."))))


# --- the two ways to listen ---------------------------------------------------------------------------------------
def _txt(props: dict) -> dict:
    def s(v):
        return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else ("" if v is None else str(v))
    return {s(k): s(v) for k, v in (props or {}).items()}


def browse_zeroconf(services=SERVICES, limit: float = TIME_LIMIT) -> list[dict]:
    """python-zeroconf: listen for the services, then ask each printer heard for its address, all within `limit`."""
    from zeroconf import IPVersion, ServiceBrowser, ServiceStateChange, Zeroconf

    deadline = time.monotonic() + limit
    heard: list[tuple[str, str]] = []
    lock = threading.Lock()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change == ServiceStateChange.Added:
            with lock:
                if (service_type, name) not in heard:
                    heard.append((service_type, name))

    zc = Zeroconf(ip_version=IPVersion.V4Only)
    browser = None
    try:
        browser = ServiceBrowser(zc, [f"{s}.local." for s in services], handlers=[on_change])
        time.sleep(max(0.0, deadline - time.monotonic() - min(RESOLVE_TIME, limit / 2)))
        out = []
        with lock:
            todo = list(heard)
        for service_type, name in todo:
            left = deadline - time.monotonic()
            if left <= 0.05:
                break
            info = zc.get_service_info(service_type, name, timeout=int(left * 1000))
            if not info:
                continue
            for addr in info.parsed_addresses(IPVersion.V4Only):
                out.append({"service": service_type, "name": name.removesuffix("." + service_type),
                            "host": info.server or "", "address": addr, "port": info.port or 0,
                            "txt": _txt(info.properties)})
        return out
    finally:
        if browser is not None:
            browser.cancel()
        zc.close()


def _unescape_avahi(s: str) -> str:
    """avahi-browse -p writes odd characters as \\DDD (decimal) and \\x for the rest."""
    s = re.sub(r"\\(\d{3})", lambda m: chr(int(m[1])), s)
    return re.sub(r"\\(.)", r"\1", s)


def parse_avahi(lines) -> list[dict]:
    """Resolved lines of `avahi-browse -r -t -p -k`:
    =;eth0;IPv4;Brother\\032QL-1110NWB;_pdl-datastream._tcp;local;BRW5413795FE5CF.local;192.168.68.110;9100;"ty=..." ..."""
    out = []
    for line in lines:
        if not line.startswith("="):
            continue
        parts = line.rstrip("\n").split(";", 9)
        if len(parts) < 9 or parts[2] != "IPv4":
            continue
        txt = {}
        for item in re.findall(r'"((?:[^"\\]|\\.)*)"', parts[9] if len(parts) > 9 else ""):
            key, _, value = _unescape_avahi(item).partition("=")
            txt[key] = value
        out.append({"service": parts[4], "name": _unescape_avahi(parts[3]), "host": parts[6], "address": parts[7],
                    "port": int(parts[8]) if parts[8].isdigit() else 0, "txt": txt})
    return out


def browse_avahi(services=SERVICES, limit: float = TIME_LIMIT) -> list[dict]:
    """avahi-browse, one per service at once, stopped at `limit` (what it printed by then still counts)."""
    exe = shutil.which("avahi-browse")
    if not exe:
        raise FindError(NOT_AVAILABLE)
    deadline = time.monotonic() + limit
    procs = [subprocess.Popen([exe, "-r", "-t", "-p", "-k", s], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True) for s in services]
    lines: list[str] = []
    for p in procs:
        try:
            out, _ = p.communicate(timeout=max(0.05, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
        lines += (out or "").splitlines()
    return parse_avahi(lines)


def _have_zeroconf() -> bool:
    try:
        import zeroconf  # noqa: F401
    except ImportError:
        return False
    return True


_one_search = threading.Lock()


def find(limit: float | None = None) -> tuple[list[Found], str]:
    """The printers announcing themselves on this network, and how they were found ("zeroconf" or "avahi").
    One search at a time. Raises FindError when there's nothing to search with."""
    limit = TIME_LIMIT if limit is None else limit
    with _one_search:
        if _have_zeroconf():
            try:
                return merge(browse_zeroconf(SERVICES, limit)), "zeroconf"
            except OSError:  # no network to listen on, or Bonjour's port can't be shared: try avahi
                pass
        if shutil.which("avahi-browse"):
            try:
                return merge(browse_avahi(SERVICES, limit)), "avahi"
            except OSError:
                raise FindError(COULDNT) from None
        if _have_zeroconf():
            raise FindError(COULDNT)
    raise FindError(NOT_AVAILABLE)


# --- which labels are loaded --------------------------------------------------------------------------------------
# Die-cut sizes as a Brother's status page writes them (width x length, mm) → brother_ql label id. Brother sells
# the 38 × 90 mm roll that brother_ql calls 39x90.
DIE_CUT = {(102, 152): "102x152", (62, 100): "62x100", (62, 29): "62x29", (29, 90): "29x90", (29, 42): "29x42",
           (17, 54): "17x54", (17, 87): "17x87", (23, 23): "23x23", (38, 90): "39x90", (39, 90): "39x90",
           (39, 48): "39x48", (52, 29): "52x29", (102, 51): "102x51"}
CONTINUOUS = {12: "12", 29: "29", 38: "38", 50: "50", 54: "54", 62: "62", 102: "102"}
ROUND = {24: "d24", 58: "d58"}
# The code on the box, for pages (or people) that say that instead. DK-1247 (103 × 164 mm) isn't offered yet.
DK_CODES = {"DK-22205": "62", "DK-2205": "62", "DK-22251": "62", "DK-22210": "29", "DK-22225": "38", "DK-22223": "50",
            "DK-22214": "12", "DK-22243": "102", "DK-1241": "102x152", "DK-1240": "102x51", "DK-1202": "62x100",
            "DK-1209": "62x29", "DK-11209": "62x29", "DK-1201": "29x90", "DK-1208": "39x90", "DK-1204": "17x54",
            "DK-1203": "17x87", "DK-1221": "23x23", "DK-1218": "d24", "DK-11207": "d58"}
_NUM = r"(\d{1,3}(?:\.\d+)?)\s*mm"


def media_label(text: str) -> str | None:
    """A printer.BROTHER_LABELS id for what a Brother says is loaded, or None when it's a size Next Box doesn't
    offer (or not a size at all). "102mm x 152mm / 4.0" x 6.0"" → 102x152, "62mm" → 62, "62mm x 100mm" → 62x100."""
    t = html.unescape(str(text or "")).replace("×", "x").replace("Ø", "ø")
    code = re.search(r"\bDK-?\s?N?(\d{4,5})\b", t, re.I)
    if code and f"DK-{code[1]}" in DK_CODES:
        return DK_CODES[f"DK-{code[1]}"]
    pair = re.search(_NUM + r"\s*x\s*" + _NUM, t, re.I)
    if pair:
        return DIE_CUT.get((round(float(pair[1])), round(float(pair[2]))))
    one = re.search(_NUM, t, re.I)
    if not one:
        return None
    mm = round(float(one[1]))
    if re.search(r"round|dia|ø|circle", t, re.I):
        return ROUND.get(mm)
    return CONTINUOUS.get(mm)


def label_name(label_id: str | None) -> str:
    """What Next Box calls a label id (from BROTHER_LABELS), or ""."""
    return next((n for i, n, _ in BROTHER_LABELS if i == label_id), "")


def page_fields(page: str) -> dict:
    """The status page's "Device Status", "Media Status" and "Media Type", and the model if it names one."""
    page = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page)
    cells = [re.sub(r"\s+", " ", html.unescape(c)).strip() for c in re.split(r"<[^>]*>", page)]
    cells = [c for c in cells if c]
    out = {}
    for key, want in (("status", "device status"), ("media_status", "media status"), ("media", "media type")):
        seen = []  # every value next to the words: a menu link called "Media Type" mustn't win over the real one
        for i, c in enumerate(cells):
            low = c.lower().rstrip(":").strip()
            if low == want and i + 1 < len(cells):
                seen.append(cells[i + 1])
            elif low.startswith(want + ":") or low.startswith(want + " "):
                rest = c[len(want):].lstrip(" :").strip()
                if rest:
                    seen.append(rest)
        if key == "media":
            seen.sort(key=lambda v: (media_label(v) is None, not re.search(r"\d\s*mm|DK-", v, re.I)))
        if seen:
            out[key] = seen[0]
    model = brother_model(" ".join(cells))
    if model:
        out["model"] = model
    return out


# Tests and the demo's stand-in status page only: the status page is read on port 80 of an address on the home
# network. Production never changes these.
HTTP_PORT = 80
ALLOW_LOOPBACK = False
STATUS_PATHS = ("/general/status.html", "/")
MEDIA_TIMEOUT = 3.0    # seconds for the whole read
MAX_PAGE = 256 * 1024


def lan_address(address: str) -> tuple[str, str]:
    """(host, IPv4) for an address a person picked or typed, checked as Admin → Printers checks a printer's
    address (printers.validate), and only on the home network: never this computer, loopback, link-local
    (169.254.x.x), multicast or an address on the internet."""
    address = str(address or "").strip()
    validate({"driver": "brother_ql", "name": "printer", "address": address})
    if address.startswith(("/dev/", "usb://")):
        raise PrinterError("Next Box can ask a printer on the network only (an address like 192.168.1.50)")
    host, _ = _host_port(address)
    try:
        infos = socket.getaddrinfo(host, HTTP_PORT, socket.AF_INET, socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise PrinterError(f"can't find {host}. Check the printer's address") from None
    own = _own_addresses()
    for info in infos:
        ip = ipaddress.IPv4Address(info[4][0])
        if ip.is_loopback and ALLOW_LOOPBACK:
            return host, str(ip)
        if (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or str(ip) in own
                or not ip.is_private):
            raise PrinterError("that address isn't a printer on your home network")
        return host, str(ip)
    raise PrinterError(f"can't find {host}. Check the printer's address")


def _get(client: httpx.Client, ip: str, host: str, path: str, timeout: float) -> str | None:
    """The page, at most MAX_PAGE bytes, or None if it isn't there."""
    url = f"http://{ip}:{HTTP_PORT}{path}"
    with client.stream("GET", url, headers={"Host": host}, timeout=timeout) as r:
        if r.status_code != 200:
            return None
        body = b""
        for chunk in r.iter_bytes():
            body += chunk
            if len(body) >= MAX_PAGE:
                break
    return body[:MAX_PAGE].decode(r.charset_encoding or "utf-8", "replace")


def read_media(address: str) -> dict:
    """Ask a Brother which labels are in it, from its own status page. Read-only, one short look.

    Returns {"read": bool, "why": str, "media": its words, "label": a BROTHER_LABELS id or None, "name": Next
    Box's name for it, "model", "status", "media_status"}. A bad address raises PrinterError."""
    host, ip = lan_address(address)
    deadline = time.monotonic() + MEDIA_TIMEOUT
    got: dict = {}
    why = ""
    with httpx.Client(trust_env=False, follow_redirects=False) as client:  # never through a proxy
        for path in STATUS_PATHS:
            left = deadline - time.monotonic()
            if left <= 0.05:
                break
            try:
                page = _get(client, ip, host, path, left)
            except httpx.TimeoutException:
                why = "The printer didn't answer in time. Is it switched on?"
                break
            except httpx.HTTPError:
                why = "The printer's web page didn't answer. Is it switched on, and is the address right?"
                break
            if page is None:
                continue
            got = {**page_fields(page), **got}
            if got.get("media"):
                break
    label = media_label(got.get("media", "")) if got.get("media") else None
    if not why and not got.get("media"):
        why = "The printer's page doesn't say which labels are in it."
    elif got.get("media") and not label:
        why = f"Next Box doesn't have a setting for that size yet ({got['media']})."
    return {"read": bool(label), "why": "" if label else why, "media": got.get("media", ""), "label": label,
            "name": label_name(label), "model": got.get("model"), "status": got.get("status", ""),
            "media_status": got.get("media_status", "")}
