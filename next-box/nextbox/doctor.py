"""`nextbox doctor`: check the setup one step at a time and say exactly what to fix.

Never prints secrets: it only says whether each one is set.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import urllib.request

from . import printer
from .auth import Accounts
from .config import Settings
from .printers import Printers

OK, BAD, NOTE = "✓", "✗", "•"


def _service_state() -> str:
    if not shutil.which("systemctl"):
        return "no-systemd"
    r = subprocess.run(["systemctl", "--user", "is-active", "nextbox.service"], capture_output=True, text=True)
    state = r.stdout.strip()
    return state if state in ("active", "inactive", "failed", "activating", "deactivating") else "no-systemd"


def _last_error() -> str:
    if not shutil.which("journalctl"):
        return ""
    r = subprocess.run(["journalctl", "--user", "-u", "nextbox", "-n", "30", "--no-pager", "-o", "cat"],
                       capture_output=True, text=True)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip() and not ln.startswith(("Started", "Stopped"))]
    return lines[-1][:200] if lines else ""


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; this just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "<this computer's IP>"


def _page_answers(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def run_doctor(settings: Settings, accounts: Accounts) -> int:
    problems = 0

    def say(mark: str, text: str, fix: str = "") -> None:
        nonlocal problems
        problems += mark == BAD
        print(f"{mark} {text}")
        if fix:
            print(f"    → {fix}")

    from . import db as _db
    from .dbtools import describe_location, is_postgres
    url = _db.url_for(settings.data_dir)
    kind = "PostgreSQL" if is_postgres(url) else "SQLite"
    try:
        with _db.database(settings.data_dir).connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        say(OK, f"database: {kind} at {describe_location(settings.data_dir)}")
    except Exception as exc:
        say(BAD, f"can't open the {kind} database: {str(getattr(exc, 'orig', exc)).splitlines()[0][:160]}",
            "sudo systemctl start postgresql" if kind == "PostgreSQL" else "check the data folder's permissions")
        return 1
    say(OK if settings.server_key else BAD, "NEXTBOX_KEY is set" if settings.server_key else "NEXTBOX_KEY is missing",
        "" if settings.server_key else "run scripts/install.sh again; it makes one for you")
    users = accounts.list_users() if accounts.has_users() else []
    say(OK if users else BAD, f"{len(users)} sign-in account(s)" if users else "no sign-in accounts yet",
        "" if users else ".venv/bin/nextbox user add <your name>")

    state = _service_state()
    if state == "no-systemd":
        say(NOTE, "no background service here (that's fine if you start it with .venv/bin/nextbox serve)")
    elif state == "active":
        say(OK, "the background service is running")
    else:
        err = _last_error()
        say(BAD, f"the background service is {state}" + (f": {err}" if err else ""),
            "fix the line above, then: systemctl --user restart nextbox")

    if _page_answers(settings.port):
        say(OK, f"the web app answers on this computer: http://localhost:{settings.port}/app")
        say(NOTE, f"on your phone (same Wi-Fi): http://{_lan_ip()}:{settings.port}/app")
        if settings.host not in ("0.0.0.0", "::", ""):
            say(BAD, f"NEXTBOX_HOST is {settings.host}, so phones can't reach it", "set NEXTBOX_HOST=0.0.0.0 in .env")
        if shutil.which("ufw"):
            r = subprocess.run(["ufw", "status"], capture_output=True, text=True)
            if "Status: active" in r.stdout and str(settings.port) not in r.stdout:
                say(BAD, "the firewall is on and doesn't allow port 8787, so phones can't connect",
                    f"sudo ufw allow from 192.168.0.0/16 to any port {settings.port} proto tcp")
    else:
        say(BAD, f"nothing answers at http://localhost:{settings.port}", "see the service line above")

    printers = Printers(settings.data_dir, settings).all()
    if settings.dry_run:
        say(NOTE, "preview only (NEXTBOX_DRY_RUN=1): nothing is sent to a printer")
    if not printers:
        say(NOTE, "no printer added yet: add one in the web app under Admin → Printers")
    for p in printers:
        up = printer.is_reachable(p)
        say(OK if up else BAD, f"printer {p.name} ({p.address}) {'answers' if up else 'does not answer'}",
            "" if up else "turn it on, check it's on the same Wi-Fi, and check the address "
                          "(hold the printer's info button to print its settings)")
    print()
    print("All good." if not problems else f"{problems} thing(s) to fix above.")
    return 1 if problems else 0
