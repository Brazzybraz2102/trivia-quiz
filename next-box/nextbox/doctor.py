"""`nextbox doctor`: check the setup one step at a time and say exactly what to fix.

Never prints secrets: it only says whether each one is set. `nextbox doctor --json` gives the same checks
as JSON for the top-bar status icon. Neither one calls Anthropic or a linked app, or prints a label.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request

from . import printer
from .auth import Accounts
from .config import Settings, app_version
from .printers import EXAMPLE_PRINTER_IP, Printers

OK, BAD, NOTE = "✓", "✗", "•"
MARKS = {OK: "ok", BAD: "bad", NOTE: "note"}  # in `nextbox doctor --json`


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


def run_doctor(settings: Settings, accounts: Accounts | None, as_json: bool = False) -> int:
    """Plain: each line as it's checked, then a summary. `as_json` (`nextbox doctor --json`, read by the
    top-bar status icon, tray/nextbox_tray.py): the same checks as one JSON object, and nothing else.
    `accounts` None: opened here once the database answers. `nextbox doctor --json` does that, so a database
    that's down is a ✗ with its fix in the JSON, not a traceback."""
    checks: list[tuple[str, str, str]] = []

    def say(mark: str, text: str, fix: str = "") -> None:
        checks.append((mark, text, fix))
        if not as_json:
            print(f"{mark} {text}")
            if fix:
                print(f"    → {fix}")

    if as_json:
        with contextlib.redirect_stdout(sys.stderr):  # a stray print can't break the JSON
            try:
                _checks(settings, accounts, say)
            except Exception as exc:  # still one answer; the error's kind only, since its words could hold anything
                say(BAD, f"doctor stopped with an error ({type(exc).__name__})",
                    "run .venv/bin/nextbox doctor to see it")
        print(json.dumps(json_report(settings, checks)))
        return 1 if any(mark == BAD for mark, _, _ in checks) else 0
    if not _checks(settings, accounts, say):
        return 1
    problems = sum(mark == BAD for mark, _, _ in checks)
    print()
    print("All good." if not problems else f"{problems} thing(s) to fix above.")
    return 1 if problems else 0


def json_report(settings: Settings, checks: list[tuple[str, str, str]]) -> dict:
    """What `nextbox doctor --json` prints. Like the plain lines it says only whether a secret is set."""
    from .providers import oauth
    return {"version": app_version(), "port": settings.port, "public_url": oauth.public_url() or None,
            "checks": [{"mark": MARKS[mark], "text": text, "fix": fix} for mark, text, fix in checks],
            "problems": sum(mark == BAD for mark, _, _ in checks)}


def _checks(settings: Settings, accounts: Accounts | None, say) -> bool:
    """Every check, in order, through `say(mark, text, fix)`. False when it had to stop early."""
    say(NOTE, f"Next Box {app_version()}")
    from .move import UNDO_HINT, moved_on, read_marker
    marker = read_marker(settings.data_dir)
    if marker is not None:  # scripts/move-out.sh: everything else is off here on purpose, so that's all to say
        say(BAD, f"Next Box was moved to another PC{moved_on(marker)}, so it stays off on this one",
            f"use Next Box on the new PC. {UNDO_HINT}")
        return True
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
        return False
    if accounts is None:
        accounts = Accounts(settings.data_dir)
    say(OK if settings.server_key else BAD, "NEXTBOX_KEY is set" if settings.server_key else "NEXTBOX_KEY is missing",
        "" if settings.server_key else "run scripts/install.sh again; it makes one for you")
    if settings.anthropic_api_key:
        say(OK, "ANTHROPIC_API_KEY is set")
    else:
        say(BAD, "ANTHROPIC_API_KEY is missing: Snap (marks and handwriting) and the AI helpers won't work",
            "put it in .env (console.anthropic.com → API keys), then: systemctl --user restart nextbox")
    from .scan import heic_supported
    if not heic_supported():
        say(NOTE, "iPhone HEIC photos can't be opened (people are asked to send a JPEG)",
            "to open them, in the Next Box folder: .venv/bin/pip install -c constraints.txt -e '.[heic]', "
            "then: systemctl --user restart nextbox")
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
    from .movecheck import UTC_NAMES, local_zone
    if local_zone() in UTC_NAMES:  # a server's usual setting: "today" would turn over at the wrong hour
        say(NOTE, "this PC's clock is set to UTC, so \"today\", due dates and Home Assistant's once-a-day print "
                  "change day at midnight UTC",
            "if you live somewhere else: sudo timedatectl set-timezone America/New_York (your own zone: "
            "timedatectl list-timezones), then: systemctl --user restart nextbox")

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

    from .providers import oauth
    for app, st in oauth.setup_status().items():
        name = oauth.APPS[app]["name"]
        if st["ready"]:
            say(OK, f"Sign in with {name} is on (sign-in comes back to {st['redirect_uri']})")
        else:
            say(NOTE, f"Sign in with {name} is off (needs {', '.join(st['missing'])} in .env)")
    if oauth.public_url() and not os.environ.get("NEXTBOX_HOSTING", "").strip():
        say(NOTE, "NEXTBOX_PUBLIC_URL is set but NEXTBOX_HOSTING isn't, so the Privacy and About pages "
                  "may not name the service that carries traffic",
            "using a Cloudflare Tunnel? Add NEXTBOX_HOSTING=Cloudflare to .env, then: systemctl --user restart nextbox")
    printers = Printers(settings.data_dir, settings).all()
    if settings.dry_run:
        say(NOTE, "preview only (NEXTBOX_DRY_RUN=1): nothing is sent to a printer")
    # The example address isn't a hardware fault: nobody has typed the printer's address yet.
    added = [p for p in printers if p.address != EXAMPLE_PRINTER_IP]
    if settings.printer_ip == EXAMPLE_PRINTER_IP and not added:
        say(BAD, "PRINTER_IP is still the example address",
            "put your printer's IP in .env (hold its info button to print it) or add it in Admin → Printers, "
            "delete NEXTBOX_DRY_RUN=1, then: systemctl --user restart nextbox. "
            "No label printer? Delete the PRINTER_IP line instead")
    elif settings.printer_ip == EXAMPLE_PRINTER_IP:  # printers were added in the web app: the line is left over
        say(NOTE, "PRINTER_IP in .env is still the example address, so it isn't used",
            "delete that line, or put your printer's IP there")
    elif not printers:
        say(NOTE, "no printer added yet: add one in the web app under Admin → Printers")
    for p in printers:
        if p.address == EXAMPLE_PRINTER_IP:  # knocking on it would only time out
            if settings.printer_ip != EXAMPLE_PRINTER_IP:
                say(BAD, f"printer {p.name} still has the example address ({p.address})",
                    "change it to your printer's address, or remove it, in Admin → Printers")
            continue
        up = printer.is_reachable(p)
        say(OK if up else BAD, f"printer {p.name} ({p.address}) {'answers' if up else 'does not answer'}",
            "" if up else "turn it on, check it's on the same Wi-Fi, and check the address "
                          "(hold the printer's info button to print its settings)")
    return True
