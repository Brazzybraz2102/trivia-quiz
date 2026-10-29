"""What a move file and this PC can tell before Next Box's packages are installed (standard library only).

scripts/move-in.sh runs this file with the system's own python3, before anything is installed or changed:
    python3 -I nextbox/movecheck.py FILE DATA_DIR [--replace] [--replace-newer]
so an empty or damaged file, a file from a newer Next Box, a time zone that isn't the old PC's, a move file
that's already in place, or a Next Box here with newer lists than the file are all found in seconds. It
prints one plain message and exits with one of the codes below (move-in.sh says what to do for each).
nextbox/move.py uses the same functions, so `nextbox move unpack` refuses the same things.
README "Move Next Box to another PC", SPEC §33.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sqlite3
import struct
import subprocess
import sys
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
        raise AlreadyHere(f"This move file is already in place here (since {when}). Nothing to do.")
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


# --- python3 nextbox/movecheck.py FILE DATA_DIR [--replace] [--replace-newer] ----------------------------------
def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--zone"]:  # move-in.sh --check: this PC's time zone, and whether it's UTC
        zone = local_zone() or ""
        print(zone)
        return 1 if zone in UTC_NAMES else 0
    flags = {a for a in args if a.startswith("--")}
    rest = [a for a in args if not a.startswith("--")]
    if len(rest) != 2 or flags - {"--replace", "--replace-newer"}:
        print("usage: movecheck.py FILE DATA_DIR [--replace] [--replace-newer]", file=sys.stderr)
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
