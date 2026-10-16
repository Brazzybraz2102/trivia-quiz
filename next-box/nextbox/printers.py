"""The printers this Next Box can use (admin-managed), and which one a ticket goes to.

Thermal printers print black; label *color* comes from the roll loaded in each printer. So
"overdue tasks on red labels" means: send overdue tickets to the printer loaded with red labels.
A Brother QL with a black+red roll can also print the overdue tags in red ink.
"""
from __future__ import annotations

import ipaddress
import re
import secrets
import socket
from dataclasses import asdict, fields

from sqlalchemy import and_, delete, func, insert, select, update

from . import db
from .config import Settings
from .printer import COLORS, DRIVERS, PrinterConfig, _host_port, legacy_config

# Why a ticket is being printed. People map each reason to a label color in Settings.
REASONS = {
    "overdue": "Overdue (expired) tasks",
    "urgent": "Urgent tasks (p1/p2)",
    "today": "Daily list",
    "list": "Lists",
    "task": "Single task",
    "note": "Notes",
    "new_task": "New task from the hotkey or web",
    "focus": "Just one thing",
    "strips": "Tear-off strips",
}
DEFAULT_RULES = {"overdue": "red"}


class PrinterError(ValueError):
    pass


PRINTER_PORTS = (9100, 515, 631)  # raw printing, LPD, IPP: all a household outside Home may use
_HOSTNAME = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*")
_BAD_ADDRESS = ("address: an IP or host (optionally :port), a printer device like /dev/usb/lp0, "
                "usb://0x04f9:0x20a7 for Brother USB, or the CUPS printer name")


def _check_host_port(address: str) -> tuple[str, int]:
    """192.168.1.50 or printer.lan, optionally :port. Not '...', '-' or port 99999."""
    host, port = _host_port(address)
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        if not (_HOSTNAME.fullmatch(host) and re.search(r"[A-Za-z]", host)):
            raise PrinterError(f"'{host or address}' isn't a printer address. It looks like 192.168.1.50 "
                               "or printer.lan") from None
    if not 1 <= port <= 65535:
        raise PrinterError("the port is a number from 1 to 65535 (most label printers use 9100)")
    return host, port


def _own_addresses() -> set[str]:
    """This computer's own addresses, so a printer address can't point back at Next Box."""
    out: set[str] = set()
    try:
        out |= {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None)}
    except (OSError, UnicodeError):
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # a documentation address: connecting a UDP socket sends nothing
            out.add(s.getsockname()[0])
    except OSError:
        pass
    return out


def _check_reachable_by_others(host: str, port: int) -> None:
    """Households other than Home (and not staff) may only point Next Box at a network printer:
    not at this computer, its local services, or odd ports."""
    if port not in PRINTER_PORTS:
        raise PrinterError("use the printer's own port: 9100 (most label printers), 515 or 631")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise PrinterError(f"can't find {host}. Check the printer's address") from None
    own = _own_addresses()
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or str(ip) in own:
            raise PrinterError("that address isn't a printer Next Box can use")


def _clean(data: dict) -> dict:
    allowed = {f.name for f in fields(PrinterConfig)}
    return {k: v for k, v in data.items() if k in allowed}


def validate(data: dict, restricted: bool = False) -> PrinterConfig:
    """`restricted`: the household isn't Home and the person isn't staff, so only a network
    printer on a printer port is allowed (see _check_reachable_by_others)."""
    data = _clean(data)
    driver = data.get("driver")
    if driver not in DRIVERS:
        raise PrinterError(f"driver must be one of {', '.join(DRIVERS)}")
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 40:
        raise PrinterError("give the printer a name (up to 40 characters)")
    address = str(data.get("address", "")).strip()
    forms = {
        "network": r"[A-Za-z0-9.-]{1,100}(:\d{1,5})?",                              # 192.168.1.50[:9100]
        "device": r"/dev/(usb/lp\d+|lp\d+|ttyUSB\d+|ttyACM\d+|rfcomm\d+)",         # /dev/usb/lp0
        "usb": r"usb://0x[0-9a-fA-F]{4}:0x[0-9a-fA-F]{4}(/[\w.-]+)?",                # usb://0x04f9:0x20a7
        "cups": r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}",                                # CUPS queue name
    }
    allowed = ["cups"] if driver == "cups" else ["network", "device"] + (["usb"] if driver == "brother_ql" else [])
    if restricted:
        if "network" not in allowed:
            raise PrinterError("this household can add network printers (an IP address) only")
        allowed = ["network"]
    form = next((f for f in allowed if address and re.fullmatch(forms[f], address)), None)
    if form is None:
        raise PrinterError("an IP address like 192.168.1.50 (optionally :9100)" if restricted else _BAD_ADDRESS)
    if form == "network":
        host, port = _check_host_port(address)
        if restricted:
            _check_reachable_by_others(host, port)
    color = data.get("stock_color", "white")
    if color not in COLORS:
        raise PrinterError(f"label color must be one of {', '.join(COLORS)}")
    ink = data.get("ink", "black")
    if ink not in ("black", "black_red") or (ink == "black_red" and driver != "brother_ql"):
        raise PrinterError("black+red ink is only for Brother QL printers with a black+red roll")
    if ink == "black_red" and not str(data.get("model") or "QL-1110NWB").upper().startswith("QL-8"):
        # Only the QL-800 series has the second (red) print head; a QL-1100/1110 prints black only.
        raise PrinterError("only the Brother QL-800, QL-810W and QL-820NWB print red. "
                           "Other Brother models print black only; use colored label rolls instead")
    try:
        width = int(data.get("width_px") or DRIVERS[driver]["width"])
        dpi = int(data.get("dpi") or DRIVERS[driver]["dpi"])
        height = float(data.get("label_height_mm") or 0)
        gap = float(data.get("gap_mm") if data.get("gap_mm") is not None else 2)
    except (TypeError, ValueError) as exc:
        raise PrinterError("width, dpi and label sizes must be numbers") from exc
    if not 96 <= width <= 2400 or dpi not in (152, 180, 200, 203, 300, 600) or not 0 <= height <= 1000:
        raise PrinterError("check the width (96–2400 dots), dpi and label height")
    return PrinterConfig(**{**data, "name": name, "address": address, "stock_color": color, "ink": ink,
                            "width_px": width, "dpi": dpi, "label_height_mm": height, "gap_mm": gap,
                            "id": data.get("id") or secrets.token_hex(3)})


class Printers:
    """Each household's printers, in the database."""

    def __init__(self, root, settings: Settings):
        self.db = db.database(root)
        self.settings = settings

    def all(self, household: str = "home") -> list[PrinterConfig]:
        with self.db.connect() as conn:
            rows = [r[0] for r in conn.execute(select(db.printers.c.config).where(
                db.printers.c.household_id == household).order_by(db.printers.c.position, db.printers.c.id))]
        if not rows and household == "home" and self.settings.printer_ip and not self._seeded():
            # First run after upgrading: the .env printer becomes the household's first printer
            # (once; if it's removed later it stays removed).
            legacy = legacy_config(self.settings)
            self._insert(legacy, household)
            return [legacy]
        return [PrinterConfig(**_clean(r)) for r in rows]

    def _seeded(self) -> bool:
        with self.db.connect() as conn:
            return bool(conn.execute(select(db.server_settings.c.key).where(
                db.server_settings.c.key == "_printers_seeded")).first())

    def _insert(self, cfg: PrinterConfig, household: str) -> None:
        with self.db.begin() as conn:
            pos = conn.execute(select(func.count()).select_from(db.printers).where(
                db.printers.c.household_id == household)).scalar()
            conn.execute(insert(db.printers).values(id=cfg.id, household_id=household, position=pos,
                                                    config=asdict(cfg)))
            if not conn.execute(select(db.server_settings.c.key).where(
                    db.server_settings.c.key == "_printers_seeded")).first():
                conn.execute(insert(db.server_settings).values(key="_printers_seeded", value=True))

    def get(self, pid: str, household: str = "home") -> PrinterConfig | None:
        return next((p for p in self.all(household) if p.id == pid), None)

    def add(self, data: dict, household: str = "home", restricted: bool = False) -> PrinterConfig:
        self.all(household)  # make sure a legacy .env printer is saved first
        cfg = validate({**data, "id": None}, restricted=restricted)
        self._insert(cfg, household)
        return cfg

    def update(self, pid: str, data: dict, household: str = "home", restricted: bool = False) -> PrinterConfig:
        current = self.get(pid, household)
        if current is None:
            raise KeyError(pid)
        cfg = validate({**asdict(current), **data, "id": pid}, restricted=restricted)
        with self.db.begin() as conn:
            conn.execute(update(db.printers).where(and_(db.printers.c.id == pid,
                                                        db.printers.c.household_id == household)).values(config=asdict(cfg)))
        return cfg

    def remove(self, pid: str, household: str = "home") -> None:
        self.all(household)
        with self.db.begin() as conn:
            conn.execute(delete(db.printers).where(and_(db.printers.c.id == pid,
                                                        db.printers.c.household_id == household)))

    def delete_for_household(self, household: str) -> None:
        with self.db.begin() as conn:
            conn.execute(delete(db.printers).where(db.printers.c.household_id == household))


def choose(printers: list[PrinterConfig], prefs: dict, reason: str) -> tuple[PrinterConfig | None, str]:
    """Pick the printer for this ticket. Returns (printer, note for the person or "")."""
    rules = {**DEFAULT_RULES, **(prefs.get("color_rules") or {})}
    return choose_color(printers, prefs, rules.get(reason, "any"))


def choose_color(printers: list[PrinterConfig], prefs: dict, want: str | None) -> tuple[PrinterConfig | None, str]:
    """The printer loaded with `want` labels, else the person's default printer (with a note)."""
    if not printers:
        return None, ""
    default = next((p for p in printers if p.id == prefs.get("default_printer")), printers[0])
    if want in ("any", "", None):
        return default, ""
    matches = [p for p in printers if p.stock_color == want]
    if matches:
        return (default if default in matches else matches[0]), ""
    if want == "white":
        return default, ""  # plain labels: the pattern shows the tag
    return default, f"No printer has {want} labels loaded, so this went to {default.name}."
