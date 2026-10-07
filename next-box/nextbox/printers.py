"""The printers this Next Box can use (admin-managed), and which one a ticket goes to.

Thermal printers print black; label *color* comes from the roll loaded in each printer. So
"overdue tasks on red labels" means: send overdue tickets to the printer loaded with red labels.
A Brother QL with a black+red roll can also print the overdue tags in red ink.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import asdict, fields

from sqlalchemy import and_, delete, func, insert, select, update

from . import db
from .config import Settings
from .printer import COLORS, DRIVERS, PrinterConfig, legacy_config

# Why a ticket is being printed. People map each reason to a label color in Settings.
REASONS = {
    "overdue": "Overdue (expired) tasks",
    "urgent": "Urgent tasks (p1/p2)",
    "today": "Daily list",
    "list": "Lists",
    "task": "Single task",
    "note": "Notes",
    "new_task": "New task from the hotkey or web",
}
DEFAULT_RULES = {"overdue": "red"}


class PrinterError(ValueError):
    pass


def _clean(data: dict) -> dict:
    allowed = {f.name for f in fields(PrinterConfig)}
    return {k: v for k, v in data.items() if k in allowed}


def validate(data: dict) -> PrinterConfig:
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
        "cups": r"[A-Za-z0-9_.-]{1,100}",                                           # CUPS queue name
    }
    allowed = ["cups"] if driver == "cups" else ["network", "device"] + (["usb"] if driver == "brother_ql" else [])
    if not address or not any(re.fullmatch(forms[f], address) for f in allowed):
        raise PrinterError("address: an IP or host (optionally :port), a printer device like /dev/usb/lp0, "
                           "usb://0x04f9:0x20a7 for Brother USB, or the CUPS printer name")
    color = data.get("stock_color", "white")
    if color not in COLORS:
        raise PrinterError(f"label color must be one of {', '.join(COLORS)}")
    ink = data.get("ink", "black")
    if ink not in ("black", "black_red") or (ink == "black_red" and driver != "brother_ql"):
        raise PrinterError("black+red ink is only for Brother QL printers with a black+red roll")
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

    def add(self, data: dict, household: str = "home") -> PrinterConfig:
        self.all(household)  # make sure a legacy .env printer is saved first
        cfg = validate({**data, "id": None})
        self._insert(cfg, household)
        return cfg

    def update(self, pid: str, data: dict, household: str = "home") -> PrinterConfig:
        current = self.get(pid, household)
        if current is None:
            raise KeyError(pid)
        cfg = validate({**asdict(current), **data, "id": pid})
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
    if not printers:
        return None, ""
    rules = {**DEFAULT_RULES, **(prefs.get("color_rules") or {})}
    want = rules.get(reason, "any")
    default = next((p for p in printers if p.id == prefs.get("default_printer")), printers[0])
    if want in ("any", "", None):
        return default, ""
    matches = [p for p in printers if p.stock_color == want]
    if matches:
        return (default if default in matches else matches[0]), ""
    return default, f"No printer has {want} labels loaded, so this went to {default.name}."
