"""Move Next Box to another PC (`nextbox move ...`; README "Move Next Box to another PC", SPEC §33).

- `pack` (old PC): ONE file with a consistent copy of the database (always as SQLite inside), secret.key, the
  .env and a manifest (counts and checksums; never a secret value or a username), locked with a passphrase.
- `info`: the file's plain header (which Next Box made it, on which day, on which PC), without the passphrase.
- `unpack` (new PC): unlocks it into a private temporary folder, checks every file against the manifest,
  keeps what was here before in backups/, puts everything in place and checks the database and the vault.
- The MOVED-AWAY marker (scripts/move-out.sh writes it in the data folder) keeps the old PC from coming back by
  accident: `nextbox serve` exits with 78, install.sh leaves the service off, doctor says so first.

The file ("NBMOVE", format 1) is the STREAM construction (Hoang, Reyhanitabar, Rogaway and Vizár, 2015; the one
Tink's streaming AEAD and age use) over AES-256-GCM:
  header  b"NBMOVE" + a 4-byte big-endian length + that much JSON: format, app_version, made_on, from_host,
          kdf {name: scrypt, n, r, p, salt}, cipher, chunk, nonce_prefix, check. Nothing secret.
  key     scrypt(passphrase, 16-byte random salt, n=2**15, r=8, p=1) → 48 bytes. Bytes 0-31 are the AES-256 key;
          bytes 32-47 are `check`, which tells a wrong passphrase from a damaged file. Testing a guess against
          `check` costs the same scrypt as trying to decrypt, so it gives nothing away.
  body    a tar stream (manifest.json, nextbox.db, secret.key, .env) in 1 MiB chunks, each sealed with AES-256-GCM:
          nonce = 7-byte random prefix ‖ 4-byte chunk number ‖ 1 byte (1 on the last chunk, else 0), and the whole
          header as associated data. A changed byte anywhere (header included), chunks swapped, dropped or added,
          or a file cut short at any point (also exactly between two chunks) fails, and only whole, checked chunks
          are ever read. The database is streamed through, never held in memory.
"""
from __future__ import annotations

import base64
import contextlib
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import struct
import sys
import tarfile
import tempfile
import time
import unicodedata
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from dotenv import dotenv_values

from .config import PROJECT_DIR, Settings, app_version

MAGIC = b"NBMOVE"
FORMAT = 1
CHUNK = 1 << 20          # 1 MiB of plaintext per sealed chunk
TAG = 16                 # AES-GCM's tag on every chunk
KDF = {"name": "scrypt", "n": 2 ** 15, "r": 8, "p": 1}
MIN_PASSPHRASE = 10
DB, KEY, ENV, MANIFEST = "nextbox.db", "secret.key", ".env", "manifest.json"
MEMBERS = (MANIFEST, DB, KEY, ENV)  # all a move file ever holds: png/, backups/, practice/, locks stay behind
MARKER = "MOVED-AWAY"
MOVED_EXIT = 78          # EX_CONFIG. install.sh's unit has RestartPreventExitStatus=78, so systemd doesn't retry
TURN_OFF = ("service", "tray", "cloudflared")
EXAMPLE_PRINTER_IP = "192.168.1.50"

WRONG = "That passphrase didn't open this move file."
DAMAGED = "This move file is damaged or incomplete. Copy it again."
NOT_A_MOVE_FILE = "This isn't a Next Box move file."
RUNNING = ("Stop Next Box first so nothing changes after the copy: systemctl --user stop nextbox "
           "(scripts/move-out.sh does this for you).")
RUNNING_HERE = ("Next Box is running on this PC. Stop it first: systemctl --user stop nextbox "
                "(scripts/move-in.sh --replace does this for you).")
UPDATE_FIRST = "Update this copy first: scripts/update.sh"


class MoveError(Exception):
    """One plain sentence for the person (never a traceback, never a secret)."""


# --- small helpers ---------------------------------------------------------------------------------------
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(text, size: int) -> bytes:
    try:
        data = base64.b64decode(str(text), validate=True)
    except (ValueError, TypeError):
        raise MoveError(DAMAGED) from None
    if len(data) != size:
        raise MoveError(DAMAGED)
    return data


def _host() -> str:
    """This PC's short name (shown in the header, so the new PC can say where the file came from)."""
    return (socket.gethostname().split(".")[0] or "this PC")[:64]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def _private_file(path: Path, data: bytes) -> None:
    """A new file only this user can read (never over an existing one)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _free_path(path: Path) -> Path:
    """`path`, or path-2, path-3 ... before the suffix: never an existing file."""
    stem, suffix = (path.name[:-len(path.suffix)], path.suffix) if path.suffix else (path.name, "")
    candidate, n = path, 1
    while candidate.exists() or candidate.is_symlink():
        n += 1
        candidate = path.with_name(f"{stem}-{n}{suffix}")
    return candidate


def env_path() -> Path:
    """The .env this copy's Settings load (the same rule as config.load_settings)."""
    return Path(os.environ.get("NEXTBOX_ENV_FILE", PROJECT_DIR / ".env"))


def size_text(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1000
    return ""  # pragma: no cover


def server_answers(port: int, timeout: float = 2.0) -> bool:
    """Is a Next Box answering on this computer's port? (GET /version on 127.0.0.1, never through a proxy.)"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{int(port)}/version", timeout=timeout) as r:
            return r.status == 200 and "version" in json.loads(r.read(4096) or b"{}")
    except Exception:
        return False


def _version(text) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)\S*", str(text or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def newer_than_here(made_by: str, here: str | None = None) -> bool:
    """True when the file was made by a newer Next Box than this copy. An older one is fine: the database
    is brought up to date when the server starts (db._add_missing_columns, upkeep.run_startup_fixes)."""
    mine, theirs = _version(here or app_version()), _version(made_by)
    return mine is not None and theirs is not None and theirs > mine


# --- the passphrase --------------------------------------------------------------------------------------
def normal_passphrase(text: str) -> str:
    """The same words however they were typed: Unicode NFC, no spaces around them, single spaces between."""
    return " ".join(unicodedata.normalize("NFC", text or "").split())


def _keys(passphrase: str, kdf: dict) -> tuple[bytes, bytes]:
    out = Scrypt(salt=_unb64(kdf["salt"], 16), length=48, n=kdf["n"], r=kdf["r"], p=kdf["p"]).derive(
        normal_passphrase(passphrase).encode())
    return out[:32], out[32:]


def new_passphrase(ask: Callable[[str], str] | None = None, say: Callable[[str], None] = print) -> str:
    ask = ask or getpass.getpass
    say("Pick a passphrase to lock the move file: at least 10 characters. Four random words are easy to type")
    say("and hard to guess. You type it again on the new PC. Nothing shows while you type.")
    for _ in range(3):
        first = normal_passphrase(ask("Passphrase: "))
        if len(first) < MIN_PASSPHRASE:
            say(f"That's {len(first)} characters. Use at least {MIN_PASSPHRASE}, e.g. four random words.")
            continue
        if normal_passphrase(ask("The same passphrase again: ")) != first:
            say("The two didn't match. Try again.")
            continue
        return first
    raise MoveError("No passphrase was set, so no move file was made.")


# --- the file format -------------------------------------------------------------------------------------
def _header_bytes(header: dict) -> bytes:
    body = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    return MAGIC + struct.pack(">I", len(body)) + body


def read_header(fh) -> tuple[dict, bytes]:
    """The plain header and its exact bytes (the associated data of every chunk)."""
    start = fh.read(len(MAGIC) + 4)
    if start[:len(MAGIC)] != MAGIC:
        raise MoveError(NOT_A_MOVE_FILE)
    if len(start) < len(MAGIC) + 4:
        raise MoveError(DAMAGED)
    (size,) = struct.unpack(">I", start[len(MAGIC):])
    if not 0 < size <= 65536:
        raise MoveError(DAMAGED)
    body = fh.read(size)
    if len(body) != size:
        raise MoveError(DAMAGED)
    try:
        header = json.loads(body)
        assert isinstance(header, dict) and isinstance(header.get("kdf"), dict)
        assert isinstance(header.get("app_version"), str) and isinstance(header.get("format"), int)
    except (ValueError, AssertionError):
        raise MoveError(DAMAGED) from None
    return header, start + body


def _check_header(header: dict) -> None:
    """Refuse a file this copy can't (or mustn't) open, before anything is asked or decrypted."""
    made_by = header["app_version"]
    if newer_than_here(made_by) or header["format"] > FORMAT:
        raise MoveError(f"This move file was made by Next Box {made_by}, which is newer than this copy "
                        f"({app_version()}). {UPDATE_FIRST}")
    kdf = header["kdf"]
    try:
        n, r, p, chunk = int(kdf["n"]), int(kdf["r"]), int(kdf["p"]), int(header["chunk"])
        ok = (header["format"] == FORMAT and kdf.get("name") == "scrypt" and header.get("cipher") == "AES-256-GCM"
              and 2 ** 14 <= n <= 2 ** 20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16
              and 4096 <= chunk <= 16 * CHUNK)
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok:  # also keeps a made-up header from asking scrypt for gigabytes
        raise MoveError(DAMAGED)


def _nonce(prefix: bytes, n: int, last: bool) -> bytes:
    if n >= 2 ** 32:
        raise MoveError(DAMAGED)
    return prefix + struct.pack(">I", n) + (b"\x01" if last else b"\x00")


class _Sealer:
    """A write-only stream: whatever is written is sealed in CHUNK-sized pieces. finish() seals the last one."""

    def __init__(self, fh, key: bytes, prefix: bytes, aad: bytes, chunk: int = CHUNK):
        self.fh, self.aead, self.prefix, self.aad, self.chunk = fh, AESGCM(key), prefix, aad, chunk
        self.buf = bytearray()
        self.n = 0

    def _seal(self, data: bytes, last: bool) -> None:
        self.fh.write(self.aead.encrypt(_nonce(self.prefix, self.n, last), data, self.aad))
        self.n += 1

    def write(self, data) -> int:
        self.buf += data
        while len(self.buf) > self.chunk:  # more than one chunk: the next one isn't the last
            self._seal(bytes(self.buf[:self.chunk]), last=False)
            del self.buf[:self.chunk]
        return len(data)

    def finish(self) -> None:
        self._seal(bytes(self.buf), last=True)
        self.buf.clear()


class _Opener:
    """A read-only stream of the checked plaintext. Each chunk is opened (authenticated) before any of it is
    handed on; the chunk after it is read first, to know whether this one must be the last."""

    def __init__(self, fh, key: bytes, prefix: bytes, aad: bytes, chunk: int = CHUNK):
        self.fh, self.aead, self.prefix, self.aad = fh, AESGCM(key), prefix, aad
        self.size = chunk + TAG
        self.next = fh.read(self.size)
        self.buf = bytearray()
        self.n = 0
        self.done = False

    def _more(self) -> bool:
        if self.done:
            return False
        block, self.next = self.next, self.fh.read(self.size)
        last = not self.next
        if len(block) < TAG:
            raise MoveError(DAMAGED)
        try:
            self.buf += self.aead.decrypt(_nonce(self.prefix, self.n, last), block, self.aad)
        except InvalidTag:
            raise MoveError(DAMAGED) from None
        self.n += 1
        self.done = last
        return True

    def read(self, size: int = -1) -> bytes:
        while (size is None or size < 0 or len(self.buf) < size) and self._more():
            pass
        take = len(self.buf) if size is None or size < 0 else min(size, len(self.buf))
        out = bytes(self.buf[:take])
        del self.buf[:take]
        return out

    def drain(self) -> None:
        """Open every chunk that's left, so a file cut short (or with anything after its end) is refused."""
        while self._more():
            self.buf.clear()
        if not self.done:
            raise MoveError(DAMAGED)


def _seal_folder(fh, folder: Path, names: list[str], passphrase: str, made_on: str) -> None:
    salt, prefix = secrets.token_bytes(16), secrets.token_bytes(7)
    kdf = {**KDF, "salt": _b64(salt)}
    key, check = _keys(passphrase, kdf)
    header = {"format": FORMAT, "app_version": app_version(), "made_on": made_on, "from_host": _host(),
              "kdf": kdf, "cipher": "AES-256-GCM", "chunk": CHUNK, "nonce_prefix": _b64(prefix),
              "check": _b64(check)}
    raw = _header_bytes(header)
    fh.write(raw)
    sink = _Sealer(fh, key, prefix, raw, CHUNK)
    with tarfile.open(fileobj=sink, mode="w|", format=tarfile.USTAR_FORMAT) as tar:
        for name in names:
            path = folder / name
            info = tarfile.TarInfo(name)  # owner 0, no user or group name: the file names nobody
            info.size, info.mode, info.mtime = path.stat().st_size, 0o600, int(time.time())
            with open(path, "rb") as src:
                tar.addfile(info, src)
    sink.finish()


# --- what's in a database ----------------------------------------------------------------------------------
COUNTED = (("accounts", "users", ""), ("households", "households", ""), ("printers", "printers", ""),
           ("tasks", "tasks", ""), ("tasks_open", "tasks", " WHERE done_at IS NULL"), ("journal_pages", "journal", ""))


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def counts(conn: sqlite3.Connection) -> dict:
    """How many of each thing (an older database without a table has none of it)."""
    have = _tables(conn)
    return {key: conn.execute(f'SELECT COUNT(*) FROM "{table}"{where}').fetchone()[0] if table in have else 0
            for key, table, where in COUNTED}


def _sealed_secrets(conn: sqlite3.Connection) -> list[str]:
    """Every saved linked-app and calendar secret (sealed by the Vault), to check they still open."""
    if "users" not in _tables(conn):
        return []
    cols = [c for c in ("connection", "calendars") if c in {r[1] for r in conn.execute("PRAGMA table_info(users)")}]
    found: list[str] = []
    if not cols:
        return found
    for row in conn.execute(f"SELECT {', '.join(cols)} FROM users"):
        for value in row:
            try:
                data = json.loads(value) if isinstance(value, str) else value
            except ValueError:
                continue
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict) and isinstance(item.get("secret"), str) and item["secret"]:
                    found.append(item["secret"])
    return found


def _vault_opens(tokens: list[str], key: bytes | str | None) -> int:
    if not tokens or not key:
        return 0
    try:
        f = Fernet(key.strip() if isinstance(key, bytes) else key.strip().encode())
    except (ValueError, TypeError):
        return 0
    opened = 0
    for token in tokens:
        try:
            f.decrypt(token.encode())
            opened += 1
        except (InvalidToken, ValueError, TypeError):
            pass
    return opened


def _accounts_in(path: Path) -> int:
    """Accounts in a SQLite file here: 0 for an empty or brand-new one, -1 when it can't be read."""
    try:
        with contextlib.closing(sqlite3.connect(path, timeout=10)) as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] if "users" in _tables(conn) else 0
    except sqlite3.Error:
        return -1


def _tidy_sqlite(path: Path) -> None:
    """One self-contained file: no -wal or -shm beside it."""
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _snapshot(url: str, dest: Path) -> str:
    """A consistent copy of the database as one SQLite file at `dest`. Returns what the database was."""
    from . import db, dbtools
    if dbtools.is_postgres(url):
        target = f"sqlite:///{dest}"
        try:
            dbtools.copy_database(url, target)  # one REPEATABLE READ snapshot, streamed table by table
        finally:
            db.forget_engine(target)
        _tidy_sqlite(dest)
        return "postgresql"
    src = Path(url.removeprefix("sqlite:///"))
    with contextlib.closing(sqlite3.connect(src, timeout=30)) as s, contextlib.closing(sqlite3.connect(dest)) as d:
        s.backup(d)  # SQLite's online backup: consistent even if something is writing
    _tidy_sqlite(dest)
    return "sqlite"


# --- pack --------------------------------------------------------------------------------------------------
@dataclass
class Packed:
    path: Path
    size: int
    manifest: dict


def pack(settings: Settings, out: Path | str | None = None, *, while_running: bool = False,
         ask: Callable[[str], str] | None = None, say: Callable[[str], None] = print,
         answers: Callable[[int], bool] | None = None, env_file: Path | None = None,
         now: datetime | None = None) -> Packed:
    """Make the move file. Asks for the passphrase (twice). Never overwrites a file.
    `answers(port)`: is Next Box running here (default server_answers)."""
    from . import db, dbtools
    if not while_running and (answers or server_answers)(settings.port):
        raise MoveError(RUNNING)
    data = Path(settings.data_dir)
    url = db.url_for(data)
    if not dbtools.is_postgres(url) and not Path(url.removeprefix("sqlite:///")).exists():
        raise MoveError(f"There's no Next Box database in {data}, so there's nothing to move.")
    now = now or datetime.now()
    target = _free_path(_out_path(out, now))
    env = env_file or env_path()
    passphrase = new_passphrase(ask, say)
    say("Copying everything into the move file...")
    tmp = Path(tempfile.mkdtemp(prefix=".move-out-", dir=data))
    made = None
    try:
        os.chmod(tmp, 0o700)
        db_was = _snapshot(url, tmp / DB)
        with contextlib.closing(sqlite3.connect(tmp / DB)) as conn:
            found = counts(conn)
        names = [DB]
        for name, src in ((KEY, data / KEY), (ENV, env)):
            if src.is_file():
                _private_file(tmp / name, src.read_bytes())
                names.append(name)
        values = dotenv_values(tmp / ENV) if ENV in names else {}
        manifest = {"format": FORMAT, "app_version": app_version(), "made_at": now.isoformat(timespec="seconds"),
                    "from_host": _host(), "db_was": db_was, "counts": found,
                    "had_public_url": bool((values.get("NEXTBOX_PUBLIC_URL") or "").strip()),
                    "files": {name: {"sha256": _sha256(tmp / name), "size": (tmp / name).stat().st_size}
                              for name in names}}
        _private_file(tmp / MANIFEST, json.dumps(manifest, indent=2, sort_keys=True).encode())
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        made = target
        with os.fdopen(fd, "wb") as fh:
            _seal_folder(fh, tmp, [MANIFEST, *names], passphrase, now.date().isoformat())
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(target, 0o600)
    except BaseException:
        if made is not None:
            made.unlink(missing_ok=True)  # never leave half a file that looks like a move file
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return Packed(target, target.stat().st_size, manifest)


def _out_path(out, now: datetime) -> Path:
    name = f"nextbox-move-{now:%Y-%m-%d}.nbmove"
    path = Path(out).expanduser() if out else Path.home()
    if path.is_dir():
        path = path / name
    if not path.parent.is_dir():
        raise MoveError(f"There's no folder {path.parent} to save the move file in.")
    return path.parent.resolve() / path.name


# --- info --------------------------------------------------------------------------------------------------
def info(path: Path | str) -> dict:
    """The plain header, no passphrase needed."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise MoveError(f"Can't find {path}.")
    with open(path, "rb") as fh:
        header, _ = read_header(fh)
    return {"app_version": header["app_version"], "made_on": str(header.get("made_on") or "?"),
            "from_host": str(header.get("from_host") or "?"), "size": path.stat().st_size,
            "newer": newer_than_here(header["app_version"]) or header["format"] > FORMAT}


# --- unpack ------------------------------------------------------------------------------------------------
@dataclass
class Unpacked:
    manifest: dict
    data_dir: Path
    accounts: int
    secrets: int = 0
    secrets_open: int = 0
    set_aside: Path | None = None
    env_written: bool = False
    env_kept_as: Path | None = None
    printers: list = field(default_factory=list)     # [(name, address, answers: bool | None)]
    home_assistant: bool = False
    public_url: bool = False
    port: int = 8787


def _ask_passphrase(header: dict, ask, tries: int, say) -> bytes:
    expected = _unb64(header.get("check"), 16)
    for attempt in range(tries):
        key, check = _keys(ask("Passphrase for the move file: "), header["kdf"])
        if hmac.compare_digest(check, expected):
            return key
        if attempt < tries - 1:
            say(WRONG + " Try again.")
    raise MoveError(WRONG)


def _existing_data(data: Path) -> tuple[bool, int]:
    """(must --replace be given, accounts in the database here). A database with no accounts (install.sh ran
    first) and its secret.key are empty: no account means no saved connection that key could unlock."""
    if (data / DB).exists():
        found = _accounts_in(data / DB)
        return found != 0, found
    return (data / KEY).exists(), 0  # a key with no database beside it: we can't tell it's unused


def _set_aside(data: Path, stamp: str) -> Path | None:
    names = [n for n in (DB, f"{DB}-wal", f"{DB}-shm", f"{DB}-journal", KEY) if (data / n).exists()]
    if not names:
        return None
    backups = data / "backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    dest = _free_path(backups / f"before-move-{stamp}")
    dest.mkdir(mode=0o700)
    for name in names:  # the -wal/-shm go too: left behind, SQLite would replay them into the new file
        os.replace(data / name, dest / name)
    return dest


ENV_NOTES = {
    "DATABASE_URL": ["# Moved from another PC: Next Box here uses the SQLite copy in its data folder, so this is off.",
                     "# To use PostgreSQL on this PC, run scripts/setup-postgres.sh (it copies everything over)."],
    "NEXTBOX_DATA_DIR": ["# Moved from another PC: that PC's data folder. Here everything is in this PC's own one."],
}
_ENV_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def rewrite_env(text: str, data_dir: Path) -> str:
    """The moved .env for this PC: DATABASE_URL and NEXTBOX_DATA_DIR commented out, each saying why."""
    out: list[str] = []
    for line in text.splitlines():
        m = _ENV_KEY.match(line)
        if m and m[1] in ENV_NOTES:
            out += ENV_NOTES[m[1]] + ["# " + line]
        else:
            out.append(line)
    default = Path.home() / ".local" / "share" / "nextbox"
    if Path(data_dir).expanduser().resolve() != default.resolve():  # this PC keeps its data somewhere else
        out += ["# This PC's data folder (where the move put everything).", f"NEXTBOX_DATA_DIR={data_dir}"]
    return "\n".join(out) + "\n"


def _write_env(moved: bytes, target: Path, data_dir: Path) -> Path | None:
    """Put the moved .env in place (600). A different .env already here is kept as .env.before-move (600)."""
    new = rewrite_env(moved.decode("utf-8", "surrogateescape"), data_dir).encode("utf-8", "surrogateescape")
    kept = None
    if target.exists():
        old = target.read_bytes()
        if old == new:
            return None
        kept = _free_path(target.with_name(target.name + ".before-move"))
        _private_file(kept, old)
    tmp = target.with_name(f".env.move-{secrets.token_hex(4)}")
    _private_file(tmp, new)
    os.replace(tmp, target)
    os.chmod(target, 0o600)
    return kept


def _network_printers(conn, printer_ip: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(name, address) of every network printer, then the names of USB/CUPS ones (those stay with the old PC)."""
    from sqlalchemy import select

    from . import db
    network, local = [], []
    for (config,) in conn.execute(select(db.printers.c.config).order_by(db.printers.c.household_id,
                                                                            db.printers.c.position)):
        config = config if isinstance(config, dict) else {}
        name, address = str(config.get("name") or "Printer")[:40], str(config.get("address") or "").strip()
        if not address or address == EXAMPLE_PRINTER_IP:
            continue
        if config.get("driver") == "cups" or address.startswith(("/dev/", "usb://")):
            local.append(name)
        elif address not in {a for _, a in network}:
            network.append((name, address))
    if printer_ip and printer_ip != EXAMPLE_PRINTER_IP and printer_ip not in {a for _, a in network}:
        network.append(("PRINTER_IP printer", printer_ip))
    return network, local


def home_assistant_used(conn, days: int = 60) -> bool:
    """Has Home Assistant (the once-a-day `source="auto"` print) been printing? Read only, nothing is sent."""
    from sqlalchemy import select

    from . import db
    since = datetime.now() - timedelta(days=days)
    if any(str(day or "") >= since.date().isoformat() for (day,) in conn.execute(select(db.auto_guard.c.day))):
        return True
    q = select(db.tickets.c.record).where(db.tickets.c.created_at >= since.isoformat(timespec="seconds"))
    return any(isinstance(rec, dict) and rec.get("source") == "auto" for (rec,) in conn.execute(q))


def _facts_from(url: str, printer_ip: str) -> tuple[list, list, bool]:
    from sqlalchemy import create_engine
    engine = create_engine(url)  # its own engine: not the shared cache, and no tables are created
    try:
        with engine.connect() as conn:
            try:
                network, local = _network_printers(conn, printer_ip)
            except Exception:
                network, local = ([("PRINTER_IP printer", printer_ip)]
                                  if printer_ip and printer_ip != EXAMPLE_PRINTER_IP else []), []
            try:
                ha = home_assistant_used(conn)
            except Exception:
                ha = False
        return network, local, ha
    finally:
        engine.dispose()


def unpack(settings: Settings, path: Path | str, *, replace: bool = False,
           ask: Callable[[str], str] | None = None, tries: int | None = None,
           say: Callable[[str], None] = print, answers: Callable[[int], bool] | None = None,
           reach: Callable[[str], bool] | None = None, env_file: Path | None = None,
           now: datetime | None = None) -> Unpacked:
    """Put a move file's Next Box on this PC. `reach(address)` checks a printer (a TCP connect only)."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise MoveError(f"Can't find {path}.")
    data = Path(settings.data_dir).expanduser()
    with open(path, "rb") as fh:
        header, raw = read_header(fh)
        _check_header(header)
        if (answers or server_answers)(settings.port):
            raise MoveError(RUNNING_HERE)
        must_replace, found = _existing_data(data)
        if must_replace and not replace:
            what = f"{found} account(s)" if found > 0 else "its secret.key" if found == 0 else "a database"
            raise MoveError(f"This PC already has a Next Box with {what} in {data}. To put the moved one in its "
                            f"place, run this again with --replace (what's here now is kept in {data / 'backups'}).")
        if tries is None:
            tries = 3 if sys.stdin is not None and sys.stdin.isatty() else 1
        key = _ask_passphrase(header, ask or getpass.getpass, max(1, tries), say)
        say("Unlocking and checking the move file...")
        data.mkdir(parents=True, exist_ok=True)
        os.chmod(data, 0o700)
        tmp = Path(tempfile.mkdtemp(prefix=".move-in-", dir=data))
        try:
            os.chmod(tmp, 0o700)
            hashes = _extract(fh, header, raw, key, tmp)
            manifest = _verify(tmp, hashes)
            with contextlib.closing(sqlite3.connect(f"file:{tmp / DB}?mode=ro", uri=True)) as conn:
                try:
                    healthy = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                    tokens = _sealed_secrets(conn)
                except sqlite3.Error:
                    healthy = False
            if not healthy:
                raise MoveError(DAMAGED)
            moved_env = dotenv_values(tmp / ENV) if (tmp / ENV).exists() else {}
            vault_key = (moved_env.get("NEXTBOX_SECRET_KEY") or "").strip() or (
                (tmp / KEY).read_bytes() if (tmp / KEY).exists() else None)
            opened = _vault_opens(tokens, vault_key)

            # Everything checks out: only now is anything here changed.
            stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
            aside = _set_aside(data, stamp)
            os.replace(tmp / DB, data / DB)
            os.chmod(data / DB, 0o600)
            if (tmp / KEY).exists():
                os.replace(tmp / KEY, data / KEY)
                os.chmod(data / KEY, 0o600)
            kept, wrote = None, False
            if (tmp / ENV).exists():
                kept = _write_env((tmp / ENV).read_bytes(), env_file or env_path(), data)
                wrote = True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    accounts = _accounts_in(data / DB)
    printer_ip = (moved_env.get("PRINTER_IP") or "").strip() if wrote else settings.printer_ip
    network, local, ha = _facts_from(f"sqlite:///{data / DB}", printer_ip)
    if reach is None:
        from .printer import is_reachable

        def reach(address: str) -> bool:
            return is_reachable(address, timeout=2.0)
    try:
        port = int(moved_env.get("NEXTBOX_PORT") or settings.port) if wrote else settings.port
    except ValueError:
        port = settings.port
    return Unpacked(manifest=manifest, data_dir=data, accounts=accounts, secrets=len(tokens), secrets_open=opened,
                    set_aside=aside, env_written=wrote, env_kept_as=kept,
                    printers=[(n, a, bool(reach(a))) for n, a in network] + [(n, "", None) for n in local],
                    home_assistant=ha, public_url=bool((moved_env.get("NEXTBOX_PUBLIC_URL") or "").strip()),
                    port=port)


def _extract(fh, header: dict, raw: bytes, key: bytes, tmp: Path) -> dict[str, str]:
    """Decrypt the tar stream into `tmp` (600 files); returns each file's sha256. Only the known names."""
    opener = _Opener(fh, key, _unb64(header.get("nonce_prefix"), 7), raw, int(header["chunk"]))
    hashes: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=opener, mode="r|") as tar:
            for member in tar:
                if member.name not in MEMBERS or member.name in hashes or not member.isreg():
                    raise MoveError(DAMAGED)
                src = tar.extractfile(member)
                digest = hashlib.sha256()
                fd = os.open(tmp / member.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as dst:
                    while block := src.read(CHUNK):
                        digest.update(block)
                        dst.write(block)
                hashes[member.name] = digest.hexdigest()
        opener.drain()
    except (tarfile.TarError, EOFError):
        raise MoveError(DAMAGED) from None
    return hashes


def _verify(tmp: Path, hashes: dict[str, str]) -> dict:
    try:
        manifest = json.loads((tmp / MANIFEST).read_text())
        files = manifest["files"]
        assert isinstance(files, dict) and DB in files
    except (OSError, ValueError, KeyError, TypeError, AssertionError):
        raise MoveError(DAMAGED) from None
    if set(files) != set(hashes) - {MANIFEST}:
        raise MoveError(DAMAGED)
    for name, meta in files.items():
        if not isinstance(meta, dict) or not hmac.compare_digest(str(meta.get("sha256", "")), hashes[name]):
            raise MoveError(DAMAGED)
    return manifest


# --- what to say -------------------------------------------------------------------------------------------
def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def what_moved(c: dict) -> str:
    tasks = _n(c.get("tasks", 0), "My list task")
    if c.get("tasks") and c.get("tasks_open") is not None and c["tasks_open"] != c["tasks"]:
        tasks += f" ({c['tasks_open']} still to do)"
    return ", ".join([_n(c.get("accounts", 0), "account"), _n(c.get("households", 0), "household"),
                      _n(c.get("printers", 0), "printer"), tasks, _n(c.get("journal_pages", 0), "journal page")])


def lan_ip() -> str:
    from .doctor import _lan_ip
    return _lan_ip()


def next_steps(port: int, home_assistant: bool, public_url: bool, ip: str | None = None) -> list[str]:
    ip = ip or lan_ip()
    lines = [f"On your phone (same Wi-Fi): http://{ip}:{port}/app  (sign in as before)"]
    if home_assistant:
        lines += ["Home Assistant prints from this PC now. In Home Assistant's secrets.yaml change this line,",
                  "then restart Home Assistant (the key stays the same):",
                  f"  nextbox_print_today_url: http://{ip}:{port}/print/today"]
    if public_url:
        lines += ["Your internet address (NEXTBOX_PUBLIC_URL) has to move here too: the old PC's tunnel is off now.",
                  "Run scripts/go-online.sh on this PC (README \"Move Next Box to another PC\", step 4)."]
    return lines


def summary(result: Unpacked, with_next_steps: bool = True, ip: str | None = None) -> list[str]:
    m = result.manifest
    made = str(m.get("made_at") or "")[:10]
    lines = [f"Moved Next Box here from {m.get('from_host') or 'the old PC'} (move file made {made}, "
             f"Next Box {m.get('app_version')}):",
             f"  {what_moved(m.get('counts') or {})}."]
    if result.accounts != (m.get("counts") or {}).get("accounts"):
        lines.append(f"  ✗ The database here has {result.accounts} account(s), not "
                     f"{(m.get('counts') or {}).get('accounts')}. Copy the move file again and run this again.")
    if result.secrets:
        mark = "✓" if result.secrets_open == result.secrets else "✗"
        lines.append(f"  {mark} Linked apps and calendars: {result.secrets_open} of {result.secrets} saved "
                     "connection(s) unlock here.")
        if result.secrets_open != result.secrets:
            lines.append("    The others can't be unlocked with the moved secret.key: those people link their app "
                         "again in Settings.")
    lines.append(f"  Data folder: {result.data_dir}")
    if result.env_written:
        lines.append("  Settings (.env) are in place." + (f" The .env that was here is kept as {result.env_kept_as.name}."
                                                          if result.env_kept_as else ""))
    else:
        lines.append("  The move file had no .env, so this PC's own is kept (scripts/install.sh makes one).")
    if m.get("db_was") == "postgresql":
        lines.append("  It was PostgreSQL there; here it's the SQLite file in the data folder "
                     "(scripts/setup-postgres.sh moves it to PostgreSQL again).")
    if result.set_aside:
        lines.append(f"  What was here before is kept in {result.set_aside}")
    if result.printers:
        lines.append("Printers, checked from this PC (a quick connection test; nothing is printed):")
        for name, address, ok in result.printers:
            if ok is None:
                lines.append(f"  • {name} was plugged into the old PC (USB or CUPS): set it up on this one too.")
            elif ok:
                lines.append(f"  ✓ {name} ({address}) answers.")
            else:
                lines.append(f"  ✗ {name} ({address}) doesn't answer. This PC may not be on the same network as "
                             "the printer.")
    if with_next_steps:
        lines.append("Next:")
        lines += ["  " + line for line in next_steps(result.port, result.home_assistant, result.public_url, ip)]
    return lines


def facts_here(settings: Settings) -> tuple[bool, bool]:
    """(Home Assistant has been printing, NEXTBOX_PUBLIC_URL is set) on this PC's own database and .env."""
    from . import db
    try:
        with db.database(settings.data_dir).connect() as conn:
            ha = home_assistant_used(conn)
    except Exception:
        ha = False
    return ha, bool(os.environ.get("NEXTBOX_PUBLIC_URL", "").strip())


# --- the MOVED-AWAY marker -----------------------------------------------------------------------------------
def marker_path(data_dir) -> Path:
    return Path(data_dir).expanduser() / MARKER


def read_marker(data_dir) -> dict | None:
    """None when Next Box wasn't moved away from this PC. A marker that can't be read still counts."""
    path = marker_path(data_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = None
    data = data if isinstance(data, dict) else {}
    off = data.get("turned_off")
    return {"moved_at": str(data.get("moved_at") or ""), "file": str(data.get("file") or ""),
            "turned_off": [x for x in off if x in TURN_OFF] if isinstance(off, list) else []}


def set_marker(data_dir, file: str | None = None, off: list[str] | tuple = (), now: datetime | None = None) -> dict:
    """Write (or add to) the marker. Kept: the first moved_at, and everything turned off so far."""
    bad = [x for x in off if x not in TURN_OFF]
    if bad:
        raise MoveError(f"--off takes {', '.join(TURN_OFF)}")
    current = read_marker(data_dir) or {}
    marker = {"moved_at": current.get("moved_at") or (now or datetime.now()).isoformat(timespec="seconds"),
              "file": Path(file).name if file else current.get("file", ""),
              "turned_off": sorted(set(current.get("turned_off") or []) | set(off))}
    path = marker_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{MARKER}-{secrets.token_hex(4)}")
    _private_file(tmp, (json.dumps(marker, indent=2) + "\n").encode())
    os.replace(tmp, path)
    return marker


def clear_marker(data_dir) -> dict | None:
    marker = read_marker(data_dir)
    marker_path(data_dir).unlink(missing_ok=True)
    return marker


def moved_on(marker: dict) -> str:
    day = str(marker.get("moved_at") or "")[:10]
    return f" on {day}" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else ""


UNDO_HINT = "To bring it back to this PC: scripts/move-out.sh --undo"


def moved_away_message(marker: dict, serving: bool = True) -> str:
    if serving:
        return (f"Next Box was moved to another PC{moved_on(marker)}, so it doesn't start here: Home Assistant and "
                f"phones would get old lists. {UNDO_HINT}")
    return (f"Next Box was moved to another PC{moved_on(marker)}, so this copy is off: use the new PC. "
            f"{UNDO_HINT}")
