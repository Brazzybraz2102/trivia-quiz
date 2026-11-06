"""What a move file and this PC can tell before Next Box's packages are installed (standard library only).

scripts/move-in.sh runs this file with the system's own python3, before anything is installed or changed:
    python3 -I nextbox/movecheck.py FILE DATA_DIR [--replace] [--replace-newer]
so an empty or damaged file, a file from a newer Next Box, a time zone that isn't the old PC's, a move file
that's already in place, or a Next Box here with newer lists than the file are all found in seconds. It
prints one plain message and exits with one of the codes below (move-in.sh says what to do for each).
nextbox/move.py uses the same functions, so `nextbox move unpack` refuses the same things.
    python3 -I nextbox/movecheck.py --status [CODE_DIR]
is `nextbox move status` for a PC without a .venv: one plain report of this PC (where the move stands), safe to
paste into a chat. README "Move Next Box to another PC", SPEC §33.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAGIC = b"NBMOVE"
FORMAT = 1
DB, KEY = "nextbox.db", "secret.key"
MOVED_IN = "MOVED-IN"     # in the data folder: which move file was put in place here, and when
SLACK = 60                # seconds: two PCs' clocks are never exactly the same

# What a move file is locked with: only these are ever opened, so a made-up header can't ask scrypt for
# gigabytes of memory or minutes of work (128 * n * r bytes: 128 MiB now, 256 MiB at most).
KDF_N, KDF_R, KDF_P = 2 ** 17, 8, 1
KDF_N_RANGE = (2 ** 15, 2 ** 18)

DAMAGED = "This move file is damaged or incomplete. Copy it again."
EMPTY = "The move file is empty: the copy didn't finish. Copy it again."
NOT_A_MOVE_FILE = "This isn't a Next Box move file."
UPDATE_FIRST = ("Get the newest Next Box code first (README \"Move Next Box to another PC\", step 1; "
                "where Next Box is already installed: ~/.local/share/nextbox/app/scripts/update.sh)")

# Exit codes (also `nextbox move unpack`'s): what scripts/move-in.sh tells Mike to do.
GO, FAILED, NEWER_FILE, BAD_FILE, OTHER_ZONE, ALREADY_HERE, HAS_DATA, NEWER_HERE = 0, 1, 3, 4, 5, 6, 7, 8


class MoveError(Exception):
    """One plain sentence for the person (never a traceback, never a secret)."""
    code = FAILED


class NewerFile(MoveError):
    code = NEWER_FILE


class BadFile(MoveError):
    code = BAD_FILE


class OtherZone(MoveError):
    code = OTHER_ZONE


class AlreadyHere(MoveError):
    code = ALREADY_HERE


class HasData(MoveError):
    code = HAS_DATA


class NewerHere(MoveError):
    code = NEWER_HERE


# --- the plain header ----------------------------------------------------------------------------------------
_ZONE = re.compile(r"[A-Za-z0-9_+./-]{1,64}")
# Every text in the header is checked before it's shown, so a made-up file can't put terminal codes on the screen.
TEXTS = {"app_version": re.compile(r"\d+\.\d+\.\d+[0-9A-Za-z.+-]{0,24}"),
         "made_on": re.compile(r"\d{4}-\d{2}-\d{2}"),
         "from_host": re.compile(r"[A-Za-z0-9._-]{1,64}"),
         "tz": _ZONE,
         "utc_offset": re.compile(r"[+-]\d{2}:?\d{2}")}
REQUIRED = ("app_version", "made_on", "from_host")


def read_header(fh) -> tuple[dict, bytes]:
    """The plain header and its exact bytes (the associated data of every chunk)."""
    start = fh.read(len(MAGIC) + 4)
    if not start:
        raise BadFile(EMPTY)
    if not (start[:len(MAGIC)] == MAGIC or (len(start) < len(MAGIC) and MAGIC.startswith(start))):
        raise MoveError(NOT_A_MOVE_FILE)
    if len(start) < len(MAGIC) + 4:
        raise BadFile(DAMAGED)
    (size,) = struct.unpack(">I", start[len(MAGIC):])
    if not 0 < size <= 65536:
        raise BadFile(DAMAGED)
    body = fh.read(size)
    if len(body) != size:
        raise BadFile(DAMAGED)
    try:
        header = json.loads(body)
        assert isinstance(header, dict) and isinstance(header.get("kdf"), dict)
        assert isinstance(header.get("format"), int) and not isinstance(header["format"], bool)
        for name, pattern in TEXTS.items():
            value = header.get(name)
            assert (value is None and name not in REQUIRED) or (isinstance(value, str) and pattern.fullmatch(value))
        for name in ("made_ts", "data_ts"):
            value = header.get(name)
            assert value is None or (type(value) is int and 0 <= value < 2 ** 40)
    except (ValueError, AssertionError):
        raise BadFile(DAMAGED) from None
    return header, start + body


def open_header(path) -> dict:
    path = Path(path).expanduser()
    if not path.is_file():
        raise BadFile(f"Can't find {path}.")
    with open(path, "rb") as fh:
        return read_header(fh)[0]


def version_tuple(text) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)[0-9A-Za-z.+-]*", str(text or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def here_version() -> str | None:
    """This copy's version, from pyproject.toml beside the nextbox folder."""
    try:
        import tomllib
        with open(Path(__file__).resolve().parent.parent / "pyproject.toml", "rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except Exception:
        return None


def newer_than_here(made_by: str, here: str | None) -> bool:
    """True when the file was made by a newer Next Box than this copy. An older one is fine: the database
    is brought up to date when the server starts (db._add_missing_columns, upkeep.run_startup_fixes)."""
    mine, theirs = version_tuple(here), version_tuple(made_by)
    return mine is not None and theirs is not None and theirs > mine


def check_header(header: dict, here: str | None) -> None:
    """Refuse a file this copy can't (or mustn't) open, before anything is asked or decrypted."""
    made_by = header["app_version"]
    if newer_than_here(made_by, here) or header["format"] > FORMAT:
        raise NewerFile(f"This move file was made by Next Box {made_by}, which is newer than this copy "
                        f"({here or '?'}). {UPDATE_FIRST}")
    kdf = header["kdf"]
    try:
        n, r, p, chunk = (kdf["n"], kdf["r"], kdf["p"], header["chunk"])
        ok = (all(type(x) is int for x in (n, r, p, chunk)) and header["format"] == FORMAT
              and kdf.get("name") == "scrypt" and header.get("cipher") == "AES-256-GCM"
              and KDF_N_RANGE[0] <= n <= KDF_N_RANGE[1] and n & (n - 1) == 0 and r == KDF_R and p == KDF_P
              and 4096 <= chunk <= 16 << 20)
    except (KeyError, TypeError):
        ok = False
    if not ok:
        raise BadFile(DAMAGED)


# --- time zones ------------------------------------------------------------------------------------------------
def _zone_name(text: str) -> str | None:
    text = (text or "").strip().lstrip(":")
    if "zoneinfo/" in text:
        text = text.split("zoneinfo/", 1)[1]
    return text if text and not text.startswith("/") and _ZONE.fullmatch(text) else None


def local_zone() -> str | None:
    """The time zone this PC's clock uses, e.g. America/New_York: what "today", due dates and Home Assistant's
    once-a-day print go by. None when it can't be told."""
    if os.environ.get("TZ"):
        return _zone_name(os.environ["TZ"])
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        r = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"], capture_output=True, text=True,
                           timeout=5)
        if r.returncode == 0 and _zone_name(r.stdout):
            return _zone_name(r.stdout)
    with contextlib.suppress(OSError):
        name = _zone_name(os.readlink("/etc/localtime"))
        if name:
            return name
    with contextlib.suppress(OSError):
        return _zone_name(Path("/etc/timezone").read_text())
    return None


def utc_offset() -> str:
    return datetime.now().astimezone().strftime("%z")


# Old names some PCs still use, for systems whose zone data has only the real ones.
OLD_NAMES = {"US/Eastern": "America/New_York", "US/Central": "America/Chicago", "US/Mountain": "America/Denver",
             "US/Pacific": "America/Los_Angeles", "US/Arizona": "America/Phoenix", "US/Alaska": "America/Anchorage",
             "US/Hawaii": "Pacific/Honolulu", "US/East-Indiana": "America/Indiana/Indianapolis",
             "US/Michigan": "America/Detroit", "Canada/Eastern": "America/Toronto",
             "Canada/Central": "America/Winnipeg", "Canada/Mountain": "America/Edmonton",
             "Canada/Pacific": "America/Vancouver", "GB": "Europe/London", "Europe/Kiev": "Europe/Kyiv"}


def same_zone(a: str | None, b: str | None) -> bool:
    """Do two zones keep the same clock (now and for the next year)? Unknown on either side: nothing to compare."""
    if not a or not b or a == b:
        return True
    try:
        from zoneinfo import ZoneInfo
        za, zb = ZoneInfo(OLD_NAMES.get(a, a)), ZoneInfo(OLD_NAMES.get(b, b))
    except Exception:
        return False
    start = datetime.now(timezone.utc)
    return all((start + timedelta(days=d)).astimezone(za).utcoffset() ==
               (start + timedelta(days=d)).astimezone(zb).utcoffset() for d in range(0, 400, 5))


UTC_NAMES = {"UTC", "Etc/UTC", "Etc/Universal", "Universal", "Etc/Zulu", "Zulu", "Etc/GMT", "GMT", "UCT", "Etc/UCT"}


def zone_problem(header: dict, here: str | None = None) -> str | None:
    """The plain fix when this PC's clock is in another time zone than the old PC's was."""
    there = header.get("tz")
    if not there:  # a file from before time zones were recorded: nothing to compare
        return None
    here = here or local_zone()
    if same_zone(there, here):
        return None
    return (f"This PC's clock uses the time zone {here}, but the old PC used {there}. Then \"today\" and Home "
            f"Assistant's once-a-day print would change day at the wrong hour. Set this PC to the old PC's time "
            f"zone with: sudo timedatectl set-timezone {there}\nThen run this again.")


# --- what's already on this PC ---------------------------------------------------------------------------------
def accounts_in(path: Path) -> int:
    """Accounts in a SQLite file here: 0 for an empty or brand-new one, -1 when it can't be read."""
    try:
        with contextlib.closing(sqlite3.connect(path, timeout=10)) as conn:  # only reads
            have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] if "users" in have else 0
    except sqlite3.Error:
        return -1


def existing_data(data: Path) -> tuple[bool, int]:
    """(must --replace be given, accounts in the database here). A database with no accounts (install.sh ran
    first) and its secret.key are empty: no account means no saved connection that key could unlock."""
    if (data / DB).exists():
        found = accounts_in(data / DB)
        return found != 0, found
    return (data / KEY).exists(), 0  # a key with no database beside it: we can't tell it's unused


# When someone last did something: signing in, a phone being used, a task, a journal page, a print. Not the
# events log (the server writes to it by itself).
EPOCH_COLUMNS = (("users", "created"), ("users", "last_login"), ("users", "last_seen"), ("sessions", "created"),
                 ("sessions", "last_used"), ("tasks", "created"), ("tasks", "done_at"), ("journal", "updated"),
                 ("wins", "ts"), ("task_slides", "last_moved"), ("friendships", "created"),
                 ("friendships", "accepted"), ("friend_notes", "created"), ("friend_notes", "seen"))
LOCAL_COLUMNS = (("tickets", "created_at"), ("scans", "created_at"))  # this PC's local time, as text


def last_activity(path: Path) -> int | None:
    """The newest of those times in a SQLite file (seconds since 1970), None when there's nothing."""
    newest: list[float] = []
    if not Path(path).is_file():
        return None
    try:
        conn = sqlite3.connect(path, timeout=10)  # only reads (a read-only one would leave -wal/-shm behind)
    except sqlite3.Error:
        return None
    with contextlib.closing(conn):
        for table, column in EPOCH_COLUMNS + LOCAL_COLUMNS:
            try:
                value = conn.execute(f'SELECT MAX("{column}") FROM "{table}"').fetchone()[0]
            except sqlite3.Error:
                continue  # an older database without that table or column
            if value is None:
                continue
            try:
                newest.append(datetime.fromisoformat(value).timestamp() if isinstance(value, str) else float(value))
            except (ValueError, TypeError, OverflowError):
                continue
    return int(max(newest)) if newest else None


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d at %H:%M")


def already_here(data: Path, header: dict) -> str | None:
    """The day this very move file was put in place here (its random salt is in MOVED-IN), else None."""
    try:
        moved = json.loads((data / MOVED_IN).read_text())
        salt = header["kdf"]["salt"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if isinstance(moved, dict) and isinstance(salt, str) and moved.get("salt") == salt:
        return str(moved.get("moved_in") or "")[:10] or "?"
    return None


def write_moved_in(data: Path, header: dict, now: datetime | None = None) -> None:
    record = {"salt": header["kdf"]["salt"], "made_on": header.get("made_on"), "from_host": header.get("from_host"),
              "moved_in": (now or datetime.now()).isoformat(timespec="seconds")}
    tmp = data / f".{MOVED_IN}-{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(record, indent=2) + "\n")
    os.replace(tmp, data / MOVED_IN)


def check_here(header: dict, data: Path, *, replace: bool = False, replace_newer: bool = False) -> tuple[bool, int]:
    """May this move file be put in place in `data`? Returns (something is set aside, accounts here).
    - the same file again: nothing to do (unless replace_newer);
    - a Next Box with accounts here: only with --replace;
    - and when someone used that one after the file's last change: only with --replace-newer, because the
      file's lists are older than this PC's."""
    data = Path(data).expanduser()
    when = already_here(data, header)
    if when and not replace_newer:
        raise AlreadyHere(f"This move file is already in place here (since {when}).")
    must, found = existing_data(data)
    if must and not (replace or replace_newer):
        what = f"{found} account(s)" if found > 0 else "its secret.key" if found == 0 else "a database"
        raise HasData(f"This PC already has a Next Box with {what} in {data}. To put the moved one in its "
                      f"place, run this again with --replace (what's here now is kept in {data / 'backups'}).")
    if must and found > 0 and not replace_newer:
        mine = last_activity(data / DB)
        theirs = header.get("data_ts") or header.get("made_ts")
        if mine and theirs and mine > theirs + SLACK:
            raise NewerHere(
                f"Next Box on this PC was used on {_when(mine)}, after the last change in this move file "
                f"({_when(theirs)}). The file is older than what's on this PC: putting it in place would set "
                f"aside everything added here since. Use a newer move file. Only if this PC's Next Box was just "
                f"a test copy: run this again with --replace-newer (what's here now is kept in {data / 'backups'}).")
    return must, found


# --- nextbox move status: one plain report of this PC, safe to paste into a chat -------------------------------
# `python3 -I nextbox/movecheck.py --status [CODE_DIR]` (no .venv needed) and `nextbox move status` print the same
# lines: versions, yes/no, service states, host names, addresses, days, time zones and the tunnel's id (the
# token's "t" field). Never a .env value, a key, a token or a password. scripts/status.sh picks whichever works.
SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,80}")
UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_+/=.-]{20,}")
STATUS_KEYS = ("NEXTBOX_DATA_DIR", "NEXTBOX_PORT", "NEXTBOX_PUBLIC_URL", "PRINTER_IP", "DATABASE_URL")
EXAMPLE_PRINTER = "192.168.1.50"
DEFAULT_PORT = 8787
# The tunnel id when the token file can't be read here: the one sudo command that shows only the id.
SUDO_HINT = ("unknown: the token file needs sudo; to see it: sudo journalctl -u cloudflared -n 300 --no-pager -o cat "
             "| grep -o 'tunnelID=[0-9a-f-]*' | tail -1")


def _run(*cmd, timeout: float = 5) -> tuple[int, str]:
    """(exit code, stdout) of a command; (127, "") when it isn't there or doesn't answer."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def _mask(text: str) -> str:
    return TOKEN_RE.sub("<token>", text)


def home_path(path) -> str:
    """A path as Mike would type it from his home folder: ~/next-box rather than /home/mike/next-box
    (quoted, so a space in a folder name pastes)."""
    import shlex
    path, home = str(path), str(Path.home()).rstrip("/")
    if path == home:
        return "~"
    if home and path.startswith(home + "/"):
        return "~/" + shlex.quote(path[len(home) + 1:])
    return shlex.quote(path)


def read_env_keys(path: Path, keys=STATUS_KEYS) -> dict:
    """A few settings from a .env, used only to derive safe facts (a port, a host, set/not set)."""
    out = {}
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m and m[1] in keys:
            out[m[1]] = m[2].strip().strip('"').strip("'")
    return out


def env_facts(code: Path, env: dict | None = None) -> dict:
    """The settings the report needs: the .env beside the code (or NEXTBOX_ENV_FILE), then the environment on top
    (`nextbox move status` has loaded the .env into it already)."""
    facts = dict(env) if env is not None else read_env_keys(Path(os.environ.get("NEXTBOX_ENV_FILE") or code / ".env"))
    for key in STATUS_KEYS:
        if os.environ.get(key):
            facts[key] = os.environ[key]
    return facts


def tunnel_id_of(token: str) -> str | None:
    """The tunnel id (the "t" field) in a Cloudflare tunnel token. Nothing else of the token is ever returned."""
    try:
        token = (token or "").strip()
        data = json.loads(base64.b64decode(token + "=" * (-len(token) % 4)))
        tid = str(data.get("t", ""))
        return tid if UUID.fullmatch(tid) else None
    except Exception:
        return None


def code_version(code: Path) -> str | None:
    try:
        import tomllib
        with open(code / "pyproject.toml", "rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except Exception:
        return None


def _version_answer(url: str, timeout: float) -> tuple[str, str | None]:
    """("yes" | "other" | "no ...", the version it says) for GET <url>/version (127.0.0.1 never through a proxy)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if "127.0.0.1" in url \
        else urllib.request.build_opener()
    try:
        with opener.open(f"{url.rstrip('/')}/version", timeout=timeout) as r:
            data = json.loads(r.read(4096) or b"{}")
            v = str(data.get("version") or "") if isinstance(data, dict) else ""
            return ("yes", v) if v and TEXTS["app_version"].fullmatch(v) else ("other", None)
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        why = {502: "the tunnel is up, but on a PC where Next Box isn't running", 503: "Next Box is starting or busy",
               530: "no PC is connected to the tunnel"}.get(code)
        return (f"no ({code}{': ' + why if why else ''})", None)
    except Exception as exc:
        return (f"no ({type(exc).__name__})", None)


def tcp_answers(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError, OverflowError):
        return False


def lan_addresses() -> tuple[list[tuple[str, str]], str]:
    """This PC's IPv4 addresses as [(address, interface)], and the interface of the default route ("" if unknown)."""
    rc, out = _run("ip", "-4", "-o", "addr", "show", "scope", "global")
    found = [(m[2], m[1]) for m in re.finditer(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/\d+", out, re.M)]
    rc, route = _run("ip", "route", "show", "default")
    m = re.search(r"\bdev\s+(\S+)", route)
    return found, (m[1] if m else "")


def interface_kind(dev: str) -> str:
    if dev.startswith(("wl", "wlan", "wifi")):
        return "Wi-Fi"
    if dev.startswith(("en", "eth")):
        return "cable"
    return ""


def sqlite_query(path: Path):
    """A read-only reader of this PC's SQLite file (no tables are created, nothing is left behind)."""
    def query(sql: str) -> list:
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as conn:
            return conn.execute(sql).fetchall()
    return query


def ha_text(query) -> str:
    """Home Assistant's once-a-day print: the last day it really printed, from auto_guard (read only)."""
    if query is None:
        return "no database here"
    try:
        rows = query("SELECT key, day FROM auto_guard")
    except Exception:
        return "couldn't read auto_guard"
    real = sorted(str(day) for key, day in rows if str(key).endswith(":real") and day)
    return f"last real auto print day {real[-1]}" if real else "never printed by Home Assistant"


def printers_of(query, printer_ip: str) -> list[tuple[str, str]]:
    """(name, address) of each network printer: the .env one and the printers table's (USB/CUPS ones skipped)."""
    found: list[tuple[str, str]] = []
    if printer_ip and printer_ip != EXAMPLE_PRINTER:
        found.append(("PRINTER_IP", printer_ip))
    rows = []
    if query is not None:
        with contextlib.suppress(Exception):
            rows = query("SELECT config FROM printers")
    for (config,) in rows:
        try:
            config = json.loads(config) if isinstance(config, str) else config
        except ValueError:
            continue
        if not isinstance(config, dict):
            continue
        address = str(config.get("address") or "").strip()
        if (not address or address == EXAMPLE_PRINTER or config.get("driver") == "cups"
                or address.startswith(("/dev/", "usb://")) or address in {a for _, a in found}):
            continue
        name = re.sub(r"[^\w .-]", "", str(config.get("name") or "printer"))[:30] or "printer"
        found.append((name, address))
    return found


def cloudflared_facts(sudo: bool = True) -> dict:
    """The tunnel service on this PC: whether its unit exists, its states, its tunnel id (never the token),
    a cloudflared started by hand, and the last problem in its log (tokens masked)."""
    f: dict = {"program": bool(shutil.which("cloudflared")), "unit": False, "active": "unknown", "enabled": "unknown",
               "tunnel": None, "how": "unknown", "problem": None, "by_hand": []}
    rc, unit = _run("systemctl", "cat", "cloudflared")  # systemd's own answer, so a test's stand-in decides
    f["unit"] = rc == 0
    if f["unit"]:
        f["active"] = _run("systemctl", "is-active", "cloudflared")[1].split("\n")[0] or "unknown"
        f["enabled"] = _run("systemctl", "is-enabled", "cloudflared")[1].split("\n")[0] or "unknown"
    m = re.search(r"--token[ =]+(eyJ[A-Za-z0-9_+/=.-]{20,})", unit)  # cloudflared before 2026.7.2
    if m:
        f["tunnel"], f["how"] = tunnel_id_of(m[1]), "from the service file"
    else:
        m = re.search(r"--token-file[ =]+(\S+)", unit)  # 2026.7.2 and newer: a file only root can read
        if m:
            path = m[1].strip("'\"")
            try:
                f["tunnel"], f["how"] = tunnel_id_of(Path(path).read_text()), "from the token file"
            except OSError:
                rc, text = _run("sudo", "-n", "cat", path) if sudo else (1, "")
                if rc == 0:
                    f["tunnel"], f["how"] = tunnel_id_of(text), "from the token file"
                else:
                    f["how"] = SUDO_HINT
    if f["tunnel"] is None and f["unit"]:  # its log names it too: "tunnelID=<uuid>"
        rc, log = _run("journalctl", "-u", "cloudflared", "-n", "300", "--no-pager", "-o", "cat", timeout=10)
        ids = [t for t in re.findall(r"tunnelID=([0-9a-f-]{36})", log) if UUID.fullmatch(t)]
        if ids:
            f["tunnel"], f["how"] = ids[-1], "from its log"
    if f["unit"]:
        rc, log = _run("journalctl", "-u", "cloudflared", "-n", "40", "--no-pager", "-o", "cat", timeout=10)
        bad = [ln for ln in log.splitlines() if re.search(r"unauthorized|error|failed", ln, re.I)]
        if bad:
            f["problem"] = _mask(bad[-1].strip())[:120]
    # cloudflared started by hand in a terminal (not the service): only its process ids, never its command line
    if f["active"] != "active":
        rc, pids = _run("pgrep", "-x", "cloudflared")
        f["by_hand"] = [p for p in pids.split() if p.isdigit()] if rc == 0 else []
    return f


def _printer_answers(address: str) -> bool:
    host, _, port = address.rpartition(":") if address.count(":") == 1 else (address, "", "")
    return tcp_answers(host or address, int(port) if port.isdigit() else 9100)


def gather(code: Path, env: dict | None = None, query=None, *, now: datetime | None = None) -> dict:
    """Everything the report says, as plain facts. `query(sql)` reads this PC's database (default: the SQLite
    file in the data folder, read only); `nextbox move status` passes one for PostgreSQL."""
    code = Path(code).expanduser()
    settings = env_facts(code, env)
    data = Path(settings.get("NEXTBOX_DATA_DIR") or Path.home() / ".local" / "share" / "nextbox").expanduser()
    port = settings.get("NEXTBOX_PORT", "")
    port = int(port) if str(port).isdigit() and 0 < int(port) < 65536 else DEFAULT_PORT
    pg = bool(settings.get("DATABASE_URL"))
    host = socket.gethostname().split(".")[0]
    f: dict = {"host": host if SAFE_NAME.fullmatch(host) else "this PC", "now": (now or datetime.now()),
               "zone": local_zone() or "zone unknown", "code": code, "version": code_version(code),
               "venv": (code / ".venv" / "bin" / "nextbox").exists(), "data": data, "data_exists": data.is_dir(),
               "db": (data / DB).exists(), "pg": pg, "key": (data / KEY).exists(), "port": port}
    f["marker"] = None
    away, moved_in = data / "MOVED-AWAY", data / MOVED_IN
    if away.exists():
        try:
            m = json.loads(away.read_text())
            assert isinstance(m, dict)
            off = [x for x in (m.get("turned_off") or []) if x in ("service", "tray", "cloudflared")]
            f["marker"] = {"kind": "away", "day": str(m.get("moved_at") or "")[:10] or "?",
                           "file": re.sub(r"[^\w .-]", "", str(m.get("file") or "?"))[:80] or "?", "off": off}
        except Exception:
            f["marker"] = {"kind": "away", "day": "?", "file": "?", "off": [], "unreadable": True}
    elif moved_in.exists():
        try:
            m = json.loads(moved_in.read_text())
            assert isinstance(m, dict)
            f["marker"] = {"kind": "in", "day": str(m.get("moved_in") or "")[:10] or "?",
                           "made_on": str(m.get("made_on") or "?")[:10],
                           "from": re.sub(r"[^\w.-]", "", str(m.get("from_host") or "?"))[:64] or "?"}
        except Exception:
            f["marker"] = {"kind": "in", "day": "?", "made_on": "?", "from": "?", "unreadable": True}
    unit = Path.home() / ".config" / "systemd" / "user" / "nextbox.service"
    f["service_file"] = unit.exists()
    f["active"] = _run("systemctl", "--user", "is-active", "nextbox.service")[1].split("\n")[0] or "unknown"
    f["enabled"] = _run("systemctl", "--user", "is-enabled", "nextbox.service")[1].split("\n")[0] or "unknown"
    rc, linger = _run("loginctl", "show-user", os.environ.get("USER") or "", "-p", "Linger", "--value")
    f["linger"] = "yes" if linger.strip() == "yes" else "no" if rc == 0 else "unknown"
    f["port_answer"], f["port_version"] = _version_answer(f"http://127.0.0.1:{port}", 2)
    if query is None and f["db"] and not pg:
        query = sqlite_query(data / DB)
    f["ha"] = ha_text(query) + (" (PostgreSQL in use here: not read)" if pg and query is None else "")
    public = str(settings.get("NEXTBOX_PUBLIC_URL") or "").strip()
    f["public"] = None
    if public:
        phost = re.sub(r"^[a-z]+://", "", public, flags=re.I).split("/")[0]
        if SAFE_NAME.fullmatch(phost):
            answer, version = _version_answer(public, 8)
            f["public"] = {"host": phost, "answer": answer, "version": version}
        else:
            f["public"] = {"host": "an odd value", "answer": "not tried", "version": None}
    f["cloudflared"] = cloudflared_facts()
    f["printers"] = [(name, address, _printer_answers(address)) for name, address in
                     printers_of(query, str(settings.get("PRINTER_IP") or ""))]
    f["addresses"], f["route"] = lan_addresses()
    rc, sleep = _run("systemctl", "is-enabled", "suspend.target")
    f["sleep"] = "masked" if "masked" in sleep else ("not masked" if rc in (0, 1) and sleep else "unknown")
    return f


def verdict(f: dict) -> tuple[str, str]:
    """(what this PC looks like, the next command to run), from the facts."""
    s = f"{home_path(f['code'])}/scripts"
    cf = f["cloudflared"]
    runs = f["active"] == "active" and f["port_answer"] == "yes"
    tunnel_on = cf["active"] == "active" or cf["enabled"] == "enabled"
    m = f["marker"]
    if m and m["kind"] == "away":
        looks = f"Next Box moved away from here on {m['day']}"
        if tunnel_on:
            return (looks + ", but the internet tunnel is still on here (two PCs on one tunnel split the visitors)",
                    "sudo systemctl disable --now cloudflared")
        if f["active"] == "active":
            return looks + ", but its service is still running here", "systemctl --user disable --now nextbox"
        return looks, f"nothing here. Use the new PC; after a week: {s}/move-out.sh --forget"
    if m and m["kind"] == "in":
        if runs:
            looks = f"Next Box runs here (moved in on {m['day']})"
            if f["public"] and not tunnel_on:
                return looks + ", but the internet tunnel isn't on this PC yet", f"{s}/go-online.sh"
            if f["public"] and f["public"]["answer"] != "yes":
                return (looks + f", but https://{f['public']['host']} doesn't reach it",
                        f"{s}/go-online.sh (then check the tunnel's Replicas table in the dashboard)")
            if f["public"] and f["public"]["version"] not in (None, f["version"]):
                return (looks + f", but https://{f['public']['host']} answers with another Next Box "
                        f"({f['public']['version']}): another PC is on the tunnel",
                        "on that other PC: sudo systemctl disable --now cloudflared")
            return looks, f"nothing. Check it any time with: {home_path(f['code'])}/.venv/bin/nextbox doctor"
        if f["active"] == "active":
            return (f"Moved in on {m['day']}; the service is active, but nothing answers on port {f['port']}",
                    "journalctl --user -u nextbox -n 20 --no-pager (safe to paste), then: systemctl --user restart nextbox")
        if not f["venv"] or not f["service_file"]:
            return (f"Moved in on {m['day']}, but the setup never finished (the service isn't installed)",
                    f"finish it with the same move file: {s}/move-in.sh <the move file>")
        return (f"Moved in on {m['day']}, but Next Box's service isn't running ({f['active']})",
                f"systemctl --user restart nextbox, then this again; still off? {s}/install.sh")
    if runs:
        return ("Next Box runs here (no move marker here)",
                f"nothing. Check it any time with: {home_path(f['code'])}/.venv/bin/nextbox doctor")
    if f["active"] == "active":
        return (f"the service is active, but nothing answers on port {f['port']}",
                "journalctl --user -u nextbox -n 20 --no-pager (safe to paste), then: systemctl --user restart nextbox")
    if f["service_file"] or (f["db"] and f["venv"]):
        return ("Next Box is set up here but not running",
                f"systemctl --user restart nextbox; still off? {s}/install.sh")
    if f["version"]:
        return ("the code is here, but Next Box hasn't been moved in (or installed) yet",
                f"{s}/move-in.sh <the move file>   (a fresh Next Box instead: {s}/install.sh)")
    return "Next Box isn't on this PC", 'get the code first: README "Move Next Box to another PC", step 1'


def render(f: dict) -> list[str]:
    """The report, one 'label: value' line each, then what this PC looks like and what to run next."""
    cf, m = f["cloudflared"], f["marker"]
    lines = [f"Next Box move status on {f['host']}, {f['now'].strftime('%Y-%m-%d %H:%M')} ({f['zone']})",
             f"code: {home_path(f['code'])} = Next Box {f['version'] or 'unknown'}, .venv {'yes' if f['venv'] else 'no'}",
             f"data: {home_path(f['data'])} {'exists' if f['data_exists'] else 'MISSING'}, database "
             f"{'yes' if f['db'] else 'no'}{' (DATABASE_URL set: PostgreSQL)' if f['pg'] else ''}, "
             f"secret.key {'yes' if f['key'] else 'no'}"]
    if m is None:
        lines.append("marker: none (no move marker here)")
    elif m["kind"] == "away":
        lines.append(f"marker: MOVED-AWAY on {m['day']} (file {m['file']}; turned off: "
                     f"{', '.join(m['off']) or 'nothing recorded'})"
                     + (" (can't be read; it still counts)" if m.get("unreadable") else ""))
    else:
        lines.append(f"marker: MOVED-IN on {m['day']} (file made {m['made_on']} on {m['from']})"
                     + (" (can't be read)" if m.get("unreadable") else ""))
    lines.append(f"service: {'file yes' if f['service_file'] else 'no service file'}, {f['active']}, {f['enabled']}; "
                 f"linger {f['linger']}")
    answer = {"yes": f"yes (Next Box {f['port_version']})", "other": "something else answers"}.get(f["port_answer"],
                                                                                                   f["port_answer"])
    lines.append(f"port {f['port']} answers on this PC: {answer}")
    lines.append(f"home assistant: {f['ha']}")
    p = f["public"]
    if p is None:
        lines.append("public address: not set")
    else:
        answer = p["answer"]
        if answer == "yes":
            same = p["version"] == f["version"]
            answer = (f"yes (Next Box {p['version']}, same as here)" if same else
                      f"yes (Next Box {p['version']}: NOT this PC, which has {f['version']})")
        elif answer == "other":
            answer = "something answers, but not Next Box"
        lines.append(f"public address: set ({p['host']}); answers from here: {answer}")
    if not cf["unit"]:
        lines.append("cloudflared: no service here" + (" (the program is installed)" if cf["program"] else ""))
    else:
        lines.append(f"cloudflared: service file yes, active {cf['active']}, enabled {cf['enabled']}")
        lines.append(f"  tunnel id: {cf['tunnel'] or 'unknown'} ({cf['how']})")
        if cf["problem"]:
            lines.append(f"  last log problem: {cf['problem']}")
    if cf["by_hand"]:
        lines.append(f"  cloudflared running by hand (not as the service), process {', '.join(cf['by_hand'])}: "
                     "close that terminal (Ctrl+C there)")
    for name, address, ok in f["printers"]:
        lines.append(f"printer {name} {address}: {'answers' if ok else 'does not answer'}")
    if not f["printers"]:
        lines.append("printer: none set")
    if f["addresses"]:
        parts = []
        for ip, dev in f["addresses"]:
            kind = interface_kind(dev)
            parts.append(f"{ip} ({dev}{', ' + kind if kind else ''}{', default route' if dev == f['route'] else ''})")
        lines.append("this PC: " + ", ".join(parts))
    if f["sleep"] != "unknown":
        lines.append("sleep: suspend " + ("masked (this PC never sleeps by itself)" if f["sleep"] == "masked"
                                          else "not masked (README \"Keep the new PC awake\")"))
    looks, nxt = verdict(f)
    lines.append(f"looks like: {looks}")
    lines.append(f"next: {nxt}")
    return [_mask(line) for line in lines]


def status_report(code: Path, env: dict | None = None, query=None) -> list[str]:
    return render(gather(code, env, query))


# --- python3 nextbox/movecheck.py FILE DATA_DIR [--replace] [--replace-newer] ----------------------------------
def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--zone"]:  # move-in.sh --check: this PC's time zone, and whether it's UTC
        zone = local_zone() or ""
        print(zone)
        return 1 if zone in UTC_NAMES else 0
    if args[:1] == ["--status"] and len(args) <= 2:  # scripts/status.sh without a .venv: this PC, in plain words
        code = Path(args[1]).expanduser() if len(args) == 2 else Path(__file__).resolve().parent.parent
        for line in status_report(code):
            print(line)
        return 0
    flags = {a for a in args if a.startswith("--")}
    rest = [a for a in args if not a.startswith("--")]
    if len(rest) != 2 or flags - {"--replace", "--replace-newer"}:
        print("usage: movecheck.py FILE DATA_DIR [--replace] [--replace-newer]  |  --status [CODE_DIR]  |  --zone",
              file=sys.stderr)
        return 2
    try:
        header = open_header(rest[0])
        check_header(header, here_version())
        problem = zone_problem(header)
        if problem:
            raise OtherZone(problem)
        check_here(header, Path(rest[1]), replace="--replace" in flags, replace_newer="--replace-newer" in flags)
    except MoveError as exc:
        print(exc)
        return exc.code
    except OSError as exc:
        print(f"Couldn't read the move file: {exc.strerror or type(exc).__name__}.")
        return BAD_FILE
    return GO


if __name__ == "__main__":
    sys.exit(main())
