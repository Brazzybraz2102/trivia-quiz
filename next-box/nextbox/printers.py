"""The printers this Next Box can use (admin-managed), and which one a ticket goes to.

Thermal printers print black; label *color* comes from the roll loaded in each printer. So
"overdue tasks on red labels" means: send overdue tickets to the printer loaded with red labels.
A Brother QL with a black+red roll can also print the overdue tags in red ink.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from dataclasses import asdict, fields
from pathlib import Path

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
    def __init__(self, root: Path, settings: Settings):
        self.path = Path(root) / "printers.json"
        self.settings = settings
        self._lock = threading.Lock()

    def _load(self) -> list[PrinterConfig]:
        if not self.path.exists():
            # First run after upgrading: the .env printer becomes the first entry.
            return [legacy_config(self.settings)] if self.settings.printer_ip else []
        return [PrinterConfig(**_clean(p)) for p in json.loads(self.path.read_text())]

    def _save(self, items: list[PrinterConfig]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(p) for p in items], indent=2))
        os.replace(tmp, self.path)

    def all(self) -> list[PrinterConfig]:
        return self._load()

    def get(self, pid: str) -> PrinterConfig | None:
        return next((p for p in self._load() if p.id == pid), None)

    def add(self, data: dict) -> PrinterConfig:
        cfg = validate({**data, "id": None})
        with self._lock:
            items = self._load()
            items.append(cfg)
            self._save(items)
        return cfg

    def update(self, pid: str, data: dict) -> PrinterConfig:
        with self._lock:
            items = self._load()
            for i, p in enumerate(items):
                if p.id == pid:
                    items[i] = validate({**asdict(p), **data, "id": pid})
                    self._save(items)
                    return items[i]
        raise KeyError(pid)

    def remove(self, pid: str) -> None:
        with self._lock:
            items = [p for p in self._load() if p.id != pid]
            self._save(items)


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
