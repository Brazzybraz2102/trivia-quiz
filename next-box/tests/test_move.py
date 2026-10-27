"""Moving Next Box to another PC: `nextbox move pack/info/unpack`, the MOVED-AWAY guard and the two scripts.

Runs on SQLite and on PostgreSQL (DATABASE_URL set): there the old PC's database is PostgreSQL, so pack copies it
into the SQLite file inside the move file, and the new PC gets SQLite (setup-postgres.sh moves it back).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import tarfile
import time
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nextbox import cli, db, dbtools, jobs, move, movecheck, printer
from nextbox.auth import Accounts
from nextbox.config import PROJECT_DIR, Settings
from nextbox.journal import Journal
from nextbox.mylist import LocalTasks
from nextbox.printers import Printers
from nextbox.store import Store
from nextbox.vault import Vault

from conftest import SAMPLE, FakeTodoist

PG = os.environ.get("DATABASE_URL", "")
PASS = "maple orbit candle seventeen"
SECRETS = {"NEXTBOX_KEY": "SENTINEL-nbkey-7Q2x", "ANTHROPIC_API_KEY": "sk-ant-SENTINEL-9XZq",
           "TODOIST_TOKEN": "SENTINEL-todoist-55k", "GOOGLE_CLIENT_SECRET": "SENTINEL-google-31m",
           "DBPW": "SENTINEL-dbpw-88p"}
ENV_TEXT = f"""# Next Box settings
NEXTBOX_KEY={SECRETS['NEXTBOX_KEY']}
ANTHROPIC_API_KEY={SECRETS['ANTHROPIC_API_KEY']}
TODOIST_TOKEN={SECRETS['TODOIST_TOKEN']}
PRINTER_IP=192.168.68.110
DATABASE_URL=postgresql://nextbox:{SECRETS['DBPW']}@127.0.0.1:5432/nextbox
export NEXTBOX_DATA_DIR=/home/braznarkelai/.local/share/nextbox
NEXTBOX_PUBLIC_URL=https://nextbox.braznarkel.com
NEXTBOX_HOSTING=Cloudflare
GOOGLE_CLIENT_SECRET={SECRETS['GOOGLE_CLIENT_SECRET']}
"""
LINKED_TOKEN = "SENTINEL-linked-token-4w"
CAL_SECRET = "https://cal.example.com/SENTINEL-cal-link"
JOURNAL_WORDS = "SENTINEL private journal words"
NAMES = ("boss", "mike", "mei")


def asks(*answers):
    """A getpass stand-in that answers in turn and remembers what it was asked."""
    it = iter(answers)
    asked = []

    def ask(prompt=""):
        asked.append(prompt)
        return next(it)
    ask.asked = asked
    return ask


def no(_port):
    return False


@pytest.fixture(autouse=True)
def no_real_env(monkeypatch, tmp_path):
    """install.sh runs these tests on the real Next Box PC: never read its .env (its DATABASE_URL would point
    the tests at the real database)."""
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.setattr("nextbox.config.load_dotenv", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def quick_kdf(monkeypatch):
    """Real move files use scrypt n=2**17 (about half a second a try); most tests here use the smallest one a
    move file may have, 2**15, to stay quick. test_the_real_scrypt_setting_round_trips uses the real one."""
    monkeypatch.setitem(move.KDF, "n", movecheck.KDF_N_RANGE[0])


def free_port() -> int:
    """A port nothing listens on (the real Next Box may be on 8787 while install.sh runs these tests)."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def no_printer_knocks(monkeypatch):
    """Nothing here may connect to a printer: the reachability check is faked (and records what it was asked)."""
    knocked = []

    def fake(target, timeout=2.0):
        knocked.append(getattr(target, "address", target))
        return False
    monkeypatch.setattr(printer, "is_reachable", fake)
    return knocked


def sqlite_url(folder: Path) -> str:
    return f"sqlite:///{folder / 'nextbox.db'}"


def seed(data: Path) -> dict:
    """The old PC: two households, prefs and marks, My list, a journal page, printers, a sealed linked app and
    calendar, a printed ticket and Home Assistant's auto print for today."""
    accounts = Accounts(data)
    accounts.set_password("boss", "boss password", create=True)  # first account: superadmin (prints for HA)
    accounts.create("mike", "correct horse")
    accounts.set_prefs("mike", {"mark_style": "bullet_journal", "text_size": "large", "max_rows": 7})
    chen = accounts.create_household("Chen family")
    accounts.create("mei", "mei password", household_id=chen)
    vault = Vault(data)
    accounts.set_connection("mike", {"provider": "todoist", "config": {}, "account": "mike@example.com",
                                     "secret": vault.seal({"token": LINKED_TOKEN})})
    accounts.set_calendars("mike", [{"id": "c1", "provider": "ics", "name": "Family", "config": {},
                                     "secret": vault.seal({"url": CAL_SECRET}), "account": ""}])
    mine = LocalTasks(data, "mike")
    dentist = mine.create({"content": "Call the dentist", "due_date": "2026-10-28", "tags": ["call"]})
    done = mine.create({"content": "Water the plants"})
    mine.close_task(done["id"])
    Journal(db.database(data), "mike").add("2026-10-26", notes=JOURNAL_WORDS)
    printers = Printers(data, Settings(data_dir=data))
    printers.add({"name": "Label printer", "driver": "brother_ql", "address": "192.168.68.110"})
    printers.add({"name": "Kitchen", "driver": "escpos", "address": "192.168.1.60"}, chen)
    printers.add({"name": "Desk USB", "driver": "escpos", "address": "/dev/usb/lp0"})
    # Home Assistant's once-a-day print, as README "Home Assistant" tests it (dry): a ticket and today's claim.
    ctx = jobs.Context(settings=Settings(dry_run=True, data_dir=data, server_key="k"), store=Store(data),
                       tasks_factory=lambda: FakeTodoist([dict(t) for t in SAMPLE]), user="boss")
    first = jobs.print_today(ctx, source="auto", dry_run=True)
    assert first["status"] != "skipped" and (data / "png" / f"{first['id']}.png").exists()
    # What stays behind: the PNG cache (made by the print), backups, the practice database, the print lock,
    # the app link.
    (data / "backups").mkdir(exist_ok=True)
    (data / "backups" / "nextbox-20261001.db").write_bytes(b"old backup")
    (data / "practice").mkdir(exist_ok=True)
    (data / "practice" / "nextbox.db").write_bytes(b"practice")
    (data / ".printer.lock").write_bytes(b"")
    (data / "app").symlink_to(PROJECT_DIR)
    return {"dentist": dentist["id"], "ticket": first["id"]}


@pytest.fixture
def old(tmp_path):
    data = tmp_path / "old"
    data.mkdir()
    env = tmp_path / "old.env"
    env.write_text(ENV_TEXT)
    ids = seed(data)
    return {"data": data, "env": env, "settings": Settings(data_dir=data, server_key="k"), **ids}


def make_file(old, tmp_path, **kw) -> move.Packed:
    return move.pack(old["settings"], tmp_path, ask=asks(PASS, PASS), answers=no, env_file=old["env"],
                     say=lambda *_: None, **kw)


def unpack(new: Path, file: Path, env: Path, **kw) -> move.Unpacked:
    kw.setdefault("ask", asks(PASS))
    return move.unpack(Settings(data_dir=new), file, answers=no, env_file=env, say=lambda *_: None, **kw)


def members(file: Path, passphrase: str = PASS) -> list[str]:
    """The names inside a move file (decrypted here the way unpack does)."""
    with open(file, "rb") as fh:
        header, raw = move.read_header(fh)
        key, _ = move._keys(passphrase, header["kdf"])
        opener = move._Opener(fh, key, move._unb64(header["nonce_prefix"], 7), raw, header["chunk"])
        with tarfile.open(fileobj=opener, mode="r|") as tar:
            names = [m.name for m in tar]
        opener.drain()
    return names


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ------------------------------------------------------------------ the round trip
def test_everything_moves_and_works_on_the_new_pc(old, tmp_path, no_printer_knocks, monkeypatch):
    packed = make_file(old, tmp_path)
    assert packed.path.name == f"nextbox-move-{date.today():%Y-%m-%d}.nbmove" and packed.path.parent == tmp_path
    assert mode(packed.path) == 0o600
    c = packed.manifest["counts"]
    assert (c["accounts"], c["households"], c["printers"], c["tasks"], c["tasks_open"], c["journal_pages"]) == \
        (3, 2, 3, 2, 1, 1)
    assert packed.manifest["db_was"] == ("postgresql" if PG else "sqlite") and packed.manifest["had_public_url"]

    new, env = tmp_path / "new", tmp_path / "project" / ".env"
    env.parent.mkdir()
    result = unpack(new, packed.path, env)
    assert result.accounts == 3 and (result.secrets, result.secrets_open) == (2, 2)
    assert result.home_assistant and result.public_url and result.env_written and result.set_aside is None
    assert result.was_away is None
    # MOVED-IN says which move file is in place here (its random salt), so the same file isn't put in twice.
    assert sorted(p.name for p in new.iterdir()) == ["MOVED-IN", "nextbox.db", "secret.key"]
    assert mode(new / "MOVED-IN") == 0o600
    assert mode(new) == 0o700 and mode(new / "nextbox.db") == 0o600 and mode(new / "secret.key") == 0o600
    assert mode(env) == 0o600

    # Printers: each network one is knocked on (a connection test only), the USB one is named as staying behind.
    assert sorted(no_printer_knocks) == ["192.168.1.60", "192.168.68.110"]
    assert sorted((n, a, ok) for n, a, ok in result.printers if ok is not None) == [
        ("Kitchen", "192.168.1.60", False), ("Label printer", "192.168.68.110", False)]
    assert [n for n, _, ok in result.printers if ok is None] == ["Desk USB"]
    text = "\n".join(move.summary(result, ip="192.168.68.50"))
    assert "3 accounts, 2 households, 3 printers, 2 My list tasks (1 still to do), 1 journal page" in text
    assert "2 of 2 saved connection(s) unlock here" in text
    assert "This PC may not be on the same network as the printer." in text
    assert "nextbox_print_today_url: http://192.168.68.50:8787/print/today" in text
    assert "scripts/go-online.sh" in text and "Desk USB was plugged into the old PC" in text
    if PG:
        assert "scripts/setup-postgres.sh" in text

    # The new PC's database has it all (read straight from the SQLite file, whatever DATABASE_URL says).
    url = sqlite_url(new)
    accounts = Accounts(url)
    assert {u["username"] for u in accounts.all_users()} == set(NAMES)
    prefs = accounts.get("mike")["prefs"]
    assert (prefs["mark_style"], prefs["text_size"], prefs["max_rows"]) == ("bullet_journal", "large", 7)
    assert accounts.get("mei")["household_id"] != "home"
    assert accounts.verify("mike", "correct horse") and not accounts.verify("mike", "wrong")
    with db.database(url).connect() as conn:
        tasks = {r.content: r.done_at is None for r in conn.execute(select(db.tasks.c.content, db.tasks.c.done_at))}
        configs = [r[0] for r in conn.execute(select(db.printers.c.config))]
    assert tasks == {"Call the dentist": True, "Water the plants": False}
    assert [p["notes"] for p in Journal(db.database(url), "mike").pages("2026-10-01", "2026-10-31")] == [JOURNAL_WORDS]
    assert {p["name"] for p in configs} == {"Label printer", "Kitchen", "Desk USB"}
    # The moved secret.key still opens the linked app and the calendar.
    vault = Vault(new)
    with db.database(url).connect() as conn:
        connection, calendars = conn.execute(select(db.users.c.connection, db.users.c.calendars)
                                             .where(db.users.c.username == "mike")).one()
    assert vault.open(connection["secret"]) == {"token": LINKED_TOKEN}
    assert vault.open(calendars[0]["secret"]) == {"url": CAL_SECRET}
    # Home Assistant walking in again today: the move kept today's claim, so it says skipped (no second print).
    ctx = jobs.Context(settings=Settings(dry_run=True, data_dir=new, server_key="k"), store=Store(new, url=url),
                       tasks_factory=lambda: FakeTodoist([dict(t) for t in SAMPLE]), user="boss")
    again = jobs.print_today(ctx, source="auto", dry_run=True)
    assert again["status"] == "skipped" and "already ran" in again["reason"]
    # The tickets' pictures: png/ (a cache) stayed on the old PC, so the web app shows them from the database.
    assert not (new / "png" / f"{old['ticket']}.png").exists()
    from nextbox.server import create_app
    monkeypatch.delenv("DATABASE_URL", raising=False)  # the new PC runs on the moved SQLite file
    settings = Settings(data_dir=new, server_key="k", dry_run=True)
    web = TestClient(create_app(jobs.Context(settings=settings, store=Store(new, url=url))), base_url="http://testserver")
    assert web.post("/auth/login", json={"username": "boss", "password": "boss password"}).status_code == 200
    web.post("/auth/consent", json={"terms": True, "agree": True})
    picture = web.get(f"/printed/{old['ticket']}/png")
    assert picture.status_code == 200 and picture.headers["content-type"] == "image/png"
    assert picture.content.startswith(b"\x89PNG") and picture.content == Store(new, url=url).get_png(old["ticket"])
    assert web.get(f"/printed/{old['ticket']}/png?download=1").headers["content-disposition"].startswith("attachment")
    assert web.get("/printed/ZZZZ-ZZZZ/png").status_code == 404

    # The .env: secrets kept as they were, DATABASE_URL and NEXTBOX_DATA_DIR off, each saying why.
    moved = env.read_text()
    for value in SECRETS.values():
        assert value in moved
    assert "\nDATABASE_URL=" not in moved and "# DATABASE_URL=postgresql://" in moved
    assert "scripts/setup-postgres.sh" in moved
    assert "# export NEXTBOX_DATA_DIR=/home/braznarkelai/" in moved and f"NEXTBOX_DATA_DIR={new}" in moved
    assert "PRINTER_IP=192.168.68.110" in moved.splitlines()

    if PG:  # and scripts/setup-postgres.sh can take it back to PostgreSQL on the new PC
        target = PG.rpartition("/")[0] + "/nbmove1"
        db.metadata.drop_all(db.database(target))
        db.forget_engine(target)
        copied = dbtools.copy_database(url, target)
        assert copied["users"] == 3 and copied["journal"] == 1
        assert {u["username"] for u in Accounts(target).all_users()} == set(NAMES)
        db.metadata.drop_all(db.database(target))


def test_the_file_holds_only_what_moves(old, tmp_path, monkeypatch):
    assert any((old["data"] / "png").iterdir())  # the PNG cache is there on the old PC...
    monkeypatch.setattr(movecheck, "local_zone", lambda: "America/New_York")
    before = int(datetime.now().timestamp())
    packed = make_file(old, tmp_path)
    assert members(packed.path) == ["manifest.json", "nextbox.db", "secret.key", ".env"]  # ...but not in the file
    assert set(packed.manifest["files"]) == {"nextbox.db", "secret.key", ".env"}
    with open(packed.path, "rb") as fh:
        header, _ = move.read_header(fh)
    assert set(header) == {"format", "app_version", "made_on", "from_host", "kdf", "cipher", "chunk",
                           "nonce_prefix", "check", "tz", "utc_offset", "made_ts", "data_ts"}
    assert header["kdf"]["r"] == 8 and header["kdf"]["p"] == 1 and header["kdf"]["n"] == move.KDF["n"]
    assert header["cipher"] == "AES-256-GCM" and header["chunk"] == 1 << 20
    # The old PC's time zone, and when the newest change in its lists was (the seed, a moment ago).
    assert header["tz"] == "America/New_York" and re.fullmatch(r"[+-]\d{4}", header["utc_offset"])
    assert before - 120 <= header["data_ts"] <= header["made_ts"] + 1 and header["made_ts"] >= before - 1
    assert not list(old["data"].glob(".move-out-*"))  # the temporary copy is gone


def test_the_real_scrypt_setting_round_trips(old, tmp_path, monkeypatch):
    """n=2**17 (128 MiB, about half a second a try), r=8, p=1: what every real move file uses."""
    monkeypatch.setitem(move.KDF, "n", movecheck.KDF_N)
    assert movecheck.KDF_N == 2 ** 17 and (movecheck.KDF_R, movecheck.KDF_P) == (8, 1)
    packed = make_file(old, tmp_path)
    with open(packed.path, "rb") as fh:
        header, _ = move.read_header(fh)
    assert header["kdf"]["n"] == 2 ** 17
    assert unpack(tmp_path / "new", packed.path, tmp_path / "new.env").accounts == 3


def test_secrets_and_journal_never_show(old, tmp_path, monkeypatch, capsys):
    """Through the real command: nothing from .env is printed, and the header and manifest name no secret
    and no one."""
    monkeypatch.setattr(move, "server_answers", no)
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(old["data"]))
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(old["env"]))
    monkeypatch.setattr("getpass.getpass", asks(PASS, PASS))
    path_file = tmp_path / "where"
    assert cli.main(["move", "pack", "--out", str(tmp_path), "--path-file", str(path_file)]) == 0
    file = Path(path_file.read_text().strip())
    assert cli.main(["move", "info", str(file)]) == 0
    new, env = tmp_path / "new", tmp_path / "new.env"
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(new))
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(env))
    monkeypatch.setattr("getpass.getpass", asks(PASS))
    assert cli.main(["move", "unpack", str(file)]) == 0
    out = capsys.readouterr()
    shown = out.out + out.err
    assert "Saved the move file" in shown and "Moved Next Box here from" in shown
    assert "Made by Next Box" in shown and "can open it" in shown
    assert (new / "nextbox.db").exists() and SECRETS["NEXTBOX_KEY"] in env.read_text()
    raw = file.read_bytes()
    with open(file, "rb") as fh:
        _, head = move.read_header(fh)
    manifest = json.dumps(move.unpack(Settings(data_dir=tmp_path / "again"), file, ask=asks(PASS), answers=no,
                                      env_file=tmp_path / "again.env", say=lambda *_: None).manifest)
    for secret in [*SECRETS.values(), LINKED_TOKEN, CAL_SECRET, JOURNAL_WORDS, "correct horse"]:
        assert secret not in shown, secret
        assert secret.encode() not in raw, secret  # the header too: everything else is encrypted
        assert secret not in manifest, secret
    for name in NAMES:  # no usernames in the header or the manifest
        assert f'"{name}"' not in manifest and name.encode() not in head


# ------------------------------------------------------------------ refusing what it should
def test_a_wrong_passphrase_is_one_plain_sentence(old, tmp_path):
    packed = make_file(old, tmp_path)
    new, env = tmp_path / "new", tmp_path / "new.env"
    with pytest.raises(move.MoveError) as err:
        unpack(new, packed.path, env, ask=asks("maple orbit candle eighteen"))
    # The last try says what to do when it's forgotten.
    assert str(err.value) == ("That passphrase didn't open this move file.\nForgot it? On the PC the file was made "
                              "on, run scripts/move-out.sh again: it makes a new move file with a new passphrase.")
    assert not (new / "nextbox.db").exists() and not env.exists()
    ask = asks("wrong one here", "also not right", PASS)
    said = []
    result = move.unpack(Settings(data_dir=new), packed.path, ask=ask, tries=3, answers=no, env_file=env,
                         say=said.append)
    assert result.accounts == 3 and len(ask.asked) == 3
    assert said.count("That passphrase didn't open this move file. Try again.") == 2
    # How it was typed doesn't matter: spaces around and between the words, or a different Unicode form.
    shutil.rmtree(new)
    env.unlink()
    assert unpack(new, packed.path, env, ask=asks("  maple  orbit candle seventeen ")).accounts == 3


def test_a_changed_byte_or_a_cut_file_is_refused(old, tmp_path, monkeypatch):
    monkeypatch.setattr(move, "CHUNK", 4096)  # small chunks, so the database spans many
    packed = make_file(old, tmp_path)
    good = packed.path.read_bytes()
    with open(packed.path, "rb") as fh:
        _, head = move.read_header(fh)
    body = len(good) - len(head)
    block = 4096 + 16
    assert body > 3 * block

    def tried(data: bytes, name: str) -> str:
        path = tmp_path / name
        path.write_bytes(data)
        new = tmp_path / f"new-{name}"
        with pytest.raises(move.MoveError) as err:
            unpack(new, path, tmp_path / f"{name}.env")
        assert not (new / "nextbox.db").exists() and not list(new.glob(".move-in-*"))  # nothing left behind
        return str(err.value)

    damaged = "This move file is damaged or incomplete. Copy it again."
    flip = bytearray(good)
    flip[len(head) + block + 7] ^= 1
    assert tried(bytes(flip), "flip") == damaged
    host = good.index(b'"from_host":"') + len('"from_host":"')  # the plain header is checked too
    flip = bytearray(good)
    flip[host] = ord("Z") if flip[host] != ord("Z") else ord("Y")
    assert tried(bytes(flip), "header") == damaged
    assert tried(good[:-10], "short") == damaged
    assert tried(good[:len(head) + 2 * block], "boundary") == damaged  # cut exactly between two chunks
    assert tried(good + good[-block:], "longer") == damaged
    swapped = good[:len(head)] + good[len(head) + block:len(head) + 2 * block] + \
        good[len(head):len(head) + block] + good[len(head) + 2 * block:]
    assert tried(swapped, "swapped") == damaged
    assert tried(good[:len(head)], "empty") == damaged
    assert tried(b"PK\x03\x04 not a move file", "zip") == "This isn't a Next Box move file."
    assert tried(good[:20], "header-cut") == damaged
    # A copy that didn't finish: empty, or cut inside the first bytes. Never "not a move file" or "newer".
    assert tried(b"", "zero") == "The move file is empty: the copy didn't finish. Copy it again."
    assert tried(good[:3], "three") == damaged and tried(good[:8], "eight") == damaged
    assert unpack(tmp_path / "fine", packed.path, tmp_path / "fine.env").accounts == 3  # the original still works


def test_move_info_says_which_kind_of_problem_it_is(old, tmp_path, capsys):
    """Its exit code tells move-in.sh what to say: 3 = get newer code, 4 = copy the file again."""
    packed = make_file(old, tmp_path)
    (tmp_path / "empty.nbmove").write_bytes(b"")
    (tmp_path / "cut.nbmove").write_bytes(packed.path.read_bytes()[:20])
    (tmp_path / "zip.nbmove").write_bytes(b"PK\x03\x04 zip")
    assert cli.main(["move", "info", str(packed.path)]) == 0
    for name in ("empty", "cut"):
        assert cli.main(["move", "info", str(tmp_path / f"{name}.nbmove")]) == movecheck.BAD_FILE == 4
    assert cli.main(["move", "info", str(tmp_path / "zip.nbmove")]) == 1
    assert cli.main(["move", "info", str(tmp_path / "missing.nbmove")]) == 4
    out = capsys.readouterr()
    assert "Copy it again" in out.err and "newest" not in out.out + out.err


def _header_file(path: Path, **changes) -> Path:
    """A move file whose plain header has `changes` (the body doesn't matter: it's refused before that)."""
    header = {"format": 1, "app_version": "0.14.11", "made_on": "2026-10-27", "from_host": "desktop",
              "kdf": {"name": "scrypt", "n": 2 ** 17, "r": 8, "p": 1, "salt": "AAAAAAAAAAAAAAAAAAAAAA=="},
              "cipher": "AES-256-GCM", "chunk": 1 << 20, "nonce_prefix": "AAAAAAAAAA==",
              "check": "AAAAAAAAAAAAAAAAAAAAAA=="}
    for key, value in changes.items():
        if isinstance(value, dict):
            header[key] = {**header[key], **value}
        else:
            header[key] = value
    body = json.dumps(header).encode()
    path.write_bytes(b"NBMOVE" + struct.pack(">I", len(body)) + body + b"\x00" * 64)
    return path


def test_a_made_up_header_cant_ask_scrypt_for_gigabytes(tmp_path, monkeypatch):
    """Only what this format writes is opened: r=8, p=1, n from 2**15 to 2**18 (256 MiB at most). Anything else
    is refused before scrypt runs and before the passphrase is asked."""
    asked = []
    monkeypatch.setattr(move, "_keys", lambda *a: asked.append(a) or (b"k" * 32, b"c" * 16))
    for i, kdf in enumerate(({"n": 2 ** 20, "r": 32, "p": 16}, {"n": 2 ** 19}, {"r": 32}, {"p": 2}, {"r": 1},
                             {"n": 2 ** 14}, {"n": 3 * 2 ** 15}, {"n": True}, {"n": "131072"})):
        file = _header_file(tmp_path / f"big{i}.nbmove", kdf=kdf)
        with pytest.raises(move.MoveError, match="damaged or incomplete"):
            move.unpack(Settings(data_dir=tmp_path / "new"), file, ask=asks(PASS), answers=no)
        assert movecheck.main([str(file), str(tmp_path / "new")]) == movecheck.BAD_FILE
    assert not asked
    for n in (2 ** 15, 2 ** 16, 2 ** 17, 2 ** 18):  # these are fine (the passphrase is asked)
        file = _header_file(tmp_path / f"ok{n}.nbmove", kdf={"n": n})
        with pytest.raises(move.MoveError):
            move.unpack(Settings(data_dir=tmp_path / "new"), file, ask=asks(PASS), answers=no, tries=1)
    assert len(asked) == 4


def test_a_made_up_header_cant_put_terminal_codes_on_the_screen(tmp_path, capsys):
    """`move info` (which move-in.sh runs before the passphrase) shows header texts only when they're plain."""
    esc = "\x1b[2J\x1b[H\x1b[32mThis copy can open it. Passphrase is: hunter2"
    for change in ({"from_host": "vm" + esc}, {"app_version": "0.14.11\x1b[8m"}, {"made_on": "2026-10-27\x1b[8m"},
                   {"tz": "America/New_York\x1b[2J"}, {"from_host": "my pc"}, {"app_version": "0.14.11 beta"},
                   {"made_ts": "soon"}, {"data_ts": -5}, {"format": True}):
        file = _header_file(tmp_path / "evil.nbmove", **change)
        assert cli.main(["move", "info", str(file)]) == movecheck.BAD_FILE, change
        out = capsys.readouterr()
        assert "\x1b" not in out.out + out.err and "damaged" in out.err
    assert cli.main(["move", "info", str(_header_file(tmp_path / "fine.nbmove", tz="Europe/London"))]) == 0
    assert "time zone Europe/London" in capsys.readouterr().out
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", move._host())


def test_a_newer_next_box_file_is_refused_an_older_one_is_fine(old, tmp_path, monkeypatch, capsys):
    with monkeypatch.context() as m:  # only these two packs pretend to be another version
        m.setattr(move, "app_version", lambda: "99.0.0")
        newer = make_file(old, tmp_path)
        m.setattr(move, "app_version", lambda: "0.10.0")
        older = move.pack(old["settings"], tmp_path / "older.nbmove", ask=asks(PASS, PASS), answers=no,
                          env_file=old["env"], say=lambda *_: None)
    with pytest.raises(move.MoveError, match=r"made by Next Box 99\.0\.0, which is newer.*scripts/update\.sh"):
        unpack(tmp_path / "new", newer.path, tmp_path / "new.env")
    assert move.info(newer.path)["newer"] and not move.info(older.path)["newer"]
    assert cli.main(["move", "info", str(newer.path)]) == movecheck.NEWER_FILE == 3
    assert "Get the newest Next Box code first" in capsys.readouterr().out
    assert unpack(tmp_path / "new", older.path, tmp_path / "new.env").accounts == 3


def test_an_older_database_is_brought_up_to_date_when_the_server_starts(tmp_path, monkeypatch):
    """What an older Next Box packed (tables and columns missing) works once this version starts on it."""
    monkeypatch.delenv("DATABASE_URL", raising=False)  # this one is about the SQLite file itself
    data = tmp_path / "old"
    accounts = Accounts(data)
    accounts.set_password("boss", "boss password", create=True)
    accounts.create("mike", "correct horse")
    LocalTasks(data, "mike").create({"content": "Call the dentist", "due_date": "2026-10-28"})
    Journal(db.database(data), "mike").add("2026-10-26", notes="Old version note")
    db.reset_engines()
    with sqlite3.connect(data / "nextbox.db") as conn:  # as 0.10.0 left it: no wins or slides, older columns
        for table in ("wins", "task_slides", "tags"):
            conn.execute(f'DROP TABLE "{table}"')
        for table, column in (("tasks", "repeat_day"), ("journal", "win"), ("journal", "photo_sha"),
                              ("sessions", "last_used"), ("invites", "note"), ("users", "reset_by")):
            conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
    monkeypatch.setattr(move, "app_version", lambda: "0.10.0")
    packed = move.pack(Settings(data_dir=data), tmp_path, ask=asks(PASS, PASS), answers=no,
                       env_file=tmp_path / "none.env", say=lambda *_: None)
    monkeypatch.setattr(move, "app_version", lambda: "0.14.11")
    new = tmp_path / "new"
    result = unpack(new, packed.path, tmp_path / "new.env")
    assert result.accounts == 2 and not result.env_written
    # The server starting on it: create_all adds the tables, _add_missing_columns the columns, upkeep the rest.
    from nextbox.server import create_app
    settings = Settings(data_dir=new, server_key="k", dry_run=True)
    c = TestClient(create_app(jobs.Context(settings=settings, store=Store(new))), base_url="http://testserver")
    assert c.post("/auth/login", json={"username": "mike", "password": "correct horse"}).status_code == 200
    c.post("/auth/consent", json={"terms": True, "agree": True})
    assert any(t["content"] == "Call the dentist" for t in c.get("/list").json()["tasks"])
    assert c.get("/journal").status_code == 200
    assert c.post("/list/dump", json={"text": "After the move tomorrow"}).status_code == 200
    with sqlite3.connect(new / "nextbox.db") as conn:
        have = {r[1] for r in conn.execute("PRAGMA table_info(journal)")}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"win", "photo_sha"} <= have and {"wins", "task_slides", "tags"} <= tables


def test_data_already_here_is_refused_and_replace_keeps_it(old, tmp_path):
    packed = make_file(old, tmp_path)
    new, env = tmp_path / "new", tmp_path / "project.env"
    new.mkdir()
    here = Accounts(sqlite_url(new))
    here.set_password("someone", "their password", create=True)
    Vault(new)  # this PC's own key
    their_key = (new / "secret.key").read_bytes()
    env.write_text("NEXTBOX_KEY=this-pcs-own-key\n")
    db.forget_engine(sqlite_url(new))
    with pytest.raises(move.MoveError) as err:
        unpack(new, packed.path, env)
    assert "already has a Next Box with 1 account(s)" in str(err.value) and "--replace" in str(err.value)
    assert Accounts(sqlite_url(new)).get("someone")  # untouched
    db.forget_engine(sqlite_url(new))
    result = unpack(new, packed.path, env, replace=True, now=None)
    kept = result.set_aside
    assert kept.parent == new / "backups" and kept.name.startswith("before-move-") and mode(kept) == 0o700
    assert (kept / "secret.key").read_bytes() == their_key and (kept / "nextbox.db").exists()
    assert not (new / "nextbox.db-wal").exists() and not (new / "nextbox.db-shm").exists()
    assert result.accounts == 3 and (new / "secret.key").read_bytes() != their_key
    with sqlite3.connect(kept / "nextbox.db") as conn:
        assert conn.execute("SELECT username FROM users").fetchall() == [("someone",)]
    assert result.env_kept_as == tmp_path / "project.env.before-move"
    assert result.env_kept_as.read_text() == "NEXTBOX_KEY=this-pcs-own-key\n" and mode(result.env_kept_as) == 0o600
    assert SECRETS["NEXTBOX_KEY"] in env.read_text()


def test_a_fresh_empty_install_counts_as_empty(old, tmp_path):
    """install.sh ran first on the new PC: a database with no accounts, and the secret.key it made."""
    packed = make_file(old, tmp_path)
    new, env = tmp_path / "new", tmp_path / "new.env"
    new.mkdir()
    assert not Accounts(sqlite_url(new)).has_users()
    Vault(new)
    db.forget_engine(sqlite_url(new))
    result = unpack(new, packed.path, env)
    assert result.accounts == 3 and sorted(p.name for p in result.set_aside.iterdir()) == ["nextbox.db",
                                                                                         "secret.key"]
    # A secret.key with no database beside it might be needed (e.g. by PostgreSQL), so that one asks.
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "secret.key").write_bytes(b"x")
    with pytest.raises(move.MoveError, match="its secret.key"):
        unpack(lone, packed.path, tmp_path / "lone.env")


def test_the_env_is_kept_when_it_is_the_same_and_backed_up_when_not(old, tmp_path):
    packed = make_file(old, tmp_path)
    env = tmp_path / "proj.env"
    unpack(tmp_path / "a", packed.path, env)
    first = env.read_text()
    shutil.rmtree(tmp_path / "a")
    env.write_text(move.rewrite_env(ENV_TEXT, tmp_path / "a"))  # already the same as the moved one
    assert unpack(tmp_path / "a", packed.path, env).env_kept_as is None
    assert env.read_text() == first and not (tmp_path / "proj.env.before-move").exists()
    # Lines a person would recognise: each switched-off line has its reason just above it.
    lines = first.splitlines()
    at = lines.index(f"# DATABASE_URL=postgresql://nextbox:{SECRETS['DBPW']}@127.0.0.1:5432/nextbox")
    assert lines[at - 2].startswith("# Moved from another PC") and "setup-postgres.sh" in lines[at - 1]
    at = lines.index("# export NEXTBOX_DATA_DIR=/home/braznarkelai/.local/share/nextbox")
    assert lines[at - 1].startswith("# Moved from another PC: that PC's data folder")
    # On the usual data folder no NEXTBOX_DATA_DIR line is added.
    assert "NEXTBOX_DATA_DIR=" not in move.rewrite_env("NEXTBOX_KEY=x\n", Path.home() / ".local/share/nextbox")


def test_pack_refuses_while_next_box_is_running(old, tmp_path, monkeypatch, capsys):
    with pytest.raises(move.MoveError) as err:
        move.pack(old["settings"], tmp_path, ask=asks(PASS, PASS), answers=lambda port: port == 8787,
                  env_file=old["env"])
    assert str(err.value) == ("Stop Next Box first so nothing changes after the copy: systemctl --user stop "
                              "nextbox (scripts/move-out.sh does this for you).")
    assert not list(tmp_path.glob("*.nbmove"))
    assert make_file(old, tmp_path, while_running=True).path.exists()  # still consistent (online backup)
    # The real check asks /version on 127.0.0.1: a server that answers there counts.
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(old["data"]))
    monkeypatch.setattr(move, "server_answers", lambda port: True)
    assert cli.main(["move", "pack", "--out", str(tmp_path)]) == 1
    assert "Stop Next Box first" in capsys.readouterr().err
    # And on the new PC: never put a database under a running Next Box.
    with pytest.raises(move.MoveError, match="Next Box is running on this PC"):
        move.unpack(Settings(data_dir=tmp_path / "new"), next(tmp_path.glob("*.nbmove")), ask=asks(PASS),
                    answers=lambda port: True)


def test_the_passphrase_is_asked_twice_and_must_be_long_enough(old, tmp_path):
    said = []
    with pytest.raises(move.MoveError, match="No passphrase was set"):
        move.pack(old["settings"], tmp_path, ask=asks("short", "maple orbit candle", "maple orbit candles", "x"),
                  answers=no, env_file=old["env"], say=said.append)
    assert any("That's 5 characters" in s for s in said) and "The two didn't match. Try again." in said
    assert not list(tmp_path.glob("*.nbmove")) and not list(old["data"].glob(".move-out-*"))
    a = make_file(old, tmp_path)
    b = make_file(old, tmp_path)  # never over the first: -2
    assert b.path.name == a.path.name.replace(".nbmove", "-2.nbmove") and a.path.exists()


def test_an_easy_passphrase_is_refused_and_four_random_words_are_offered(old, tmp_path):
    """The file holds every secret, so length alone isn't enough."""
    for weak in ("aaaaaaaaaa", "abababababab", "1234567890", "1234 5678 90", "password12", "Sunshine2026"):
        assert move.passphrase_problem(weak), weak
    for fine in (PASS, "maple orbit candle", "Tr0ub4dor&3xyzab", "correct horse battery staple"):
        assert move.passphrase_problem(fine) is None, fine
    said = []
    with pytest.raises(move.MoveError, match="No passphrase was set"):
        move.pack(old["settings"], tmp_path, ask=asks("aaaaaaaaaa", "password12", "1234567890"), answers=no,
                  env_file=old["env"], say=said.append)
    assert any("same few characters" in s for s in said) and any("Only digits" in s for s in said)
    assert any("three or more words" in s for s in said)
    example = next(s for s in said if s.startswith("For example"))
    words = example.split(": ", 1)[1].split()
    assert len(words) == 4 and all(w in move.WORDS for w in words) and move.passphrase_problem(" ".join(words)) is None
    assert len(set(move.WORDS)) == len(move.WORDS) >= 400


def test_a_closed_terminal_or_a_kill_leaves_no_plain_copy_behind(old, tmp_path, monkeypatch):
    """SIGHUP (the ssh session dropped) or SIGTERM stops pack and unpack like Ctrl+C: the temporary folder with
    the plain .env, database and secret.key is removed, and no half-written move file is left."""
    real = move._snapshot
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGHUP, signal.SIGTERM)}
    for sig in (signal.SIGHUP, signal.SIGTERM):
        def hung_up(url, dest, sig=sig):
            real(url, dest)
            os.kill(os.getpid(), sig)
            time.sleep(1)  # the signal arrives here
            raise AssertionError("not stopped")
        with monkeypatch.context() as m, pytest.raises(KeyboardInterrupt):
            m.setattr(move, "_snapshot", hung_up)
            make_file(old, tmp_path)
        assert not list(old["data"].glob(".move-out-*")) and not list(tmp_path.glob("*.nbmove"))
    assert {sig: signal.getsignal(sig) for sig in before} == before  # put back as they were
    packed = make_file(old, tmp_path)
    new = tmp_path / "new"

    def cut(*a, **k):
        os.kill(os.getpid(), signal.SIGHUP)
        time.sleep(1)
    monkeypatch.setattr(move, "_verify", cut)
    with pytest.raises(KeyboardInterrupt):
        unpack(new, packed.path, tmp_path / "new.env")
    assert not list(new.glob(".move-in-*")) and not (new / "nextbox.db").exists()


def test_what_a_killed_run_left_behind_is_removed_next_time(old, tmp_path):
    """A kill -9 can't be caught: the next pack or unpack removes temporary folders an hour old or more."""
    stale, fresh = old["data"] / ".move-out-abc123", old["data"] / ".move-in-def456"
    for folder in (stale, fresh):
        folder.mkdir()
        (folder / ".env").write_text("NEXTBOX_KEY=x\n")
    hours_ago = time.time() - 2 * 3600
    os.utime(stale, (hours_ago, hours_ago))
    make_file(old, tmp_path)
    assert not stale.exists() and fresh.exists()  # a fresh one may belong to a run that's still going


def test_a_database_that_cant_be_read_says_so_before_the_passphrase(old, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nextbox:wrong-password@127.0.0.1:1/nextbox")
    ask = asks(PASS, PASS)
    with pytest.raises(move.MoveError) as err:
        move.pack(old["settings"], tmp_path, ask=ask, answers=no, env_file=old["env"], say=lambda *_: None)
    assert str(err.value) == "Couldn't read the database. Check it with: .venv/bin/nextbox doctor"
    assert not ask.asked and not list(tmp_path.glob("*.nbmove"))
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(old["data"]))
    monkeypatch.setattr(move, "server_answers", no)
    assert cli.main(["move", "pack", "--out", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "Couldn't read the database" in err and "wrong-password" not in err


def test_photos_are_copied_a_few_rows_at_a_time(tmp_path):
    """PostgreSQL → SQLite (pack, setup-postgres.sh): tables with pictures are read 5 rows at a time, so a
    journal full of 1-2 MB photos never sits in memory 500 at once (measured: 496 MB → 205 MB peak)."""
    assert dbtools.batch_for(db.journal, 500) == dbtools.batch_for(db.tickets, 500) == 5
    assert dbtools.batch_for(db.users, 500) == 500 and dbtools.batch_for(db.journal, 2) == 2
    used = []
    real = dbtools.batch_for
    try:
        dbtools.batch_for = lambda table, batch: used.append(table.name) or real(table, batch)
        src, dst = f"sqlite:///{tmp_path / 'a.db'}", f"sqlite:///{tmp_path / 'b.db'}"
        assert dbtools.copy_database(src, dst)["journal"] == 0
    finally:
        dbtools.batch_for = real
    assert "journal" in used and "tickets" in used  # copy_database asks it for every table


def test_temporary_folders_are_cleaned_up_when_something_fails(old, tmp_path, monkeypatch):
    def broken(url, dest):
        dest.write_bytes(b"half a copy")
        raise OSError(28, "No space left on device")
    with monkeypatch.context() as m, pytest.raises(OSError):
        m.setattr(move, "_snapshot", broken)
        make_file(old, tmp_path)
    assert not list(old["data"].glob(".move-out-*")) and not list(tmp_path.glob("*.nbmove"))
    packed = make_file(old, tmp_path)
    new = tmp_path / "new"

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(move, "_verify", full)
    with pytest.raises(OSError):
        unpack(new, packed.path, tmp_path / "new.env")
    assert not list(new.glob(".move-in-*")) and not (new / "nextbox.db").exists()


# ------------------------------------------------------------------ the new PC: its clock, and what's already there
def test_a_new_pc_in_another_time_zone_is_refused_with_the_exact_fix(old, tmp_path, monkeypatch):
    """"today" and Home Assistant's once-a-day guard (Store.claim_auto) go by this PC's own date: a server on
    UTC would turn over at 8 PM in New York, so the evening walk-in would print a second ticket."""
    monkeypatch.setattr(movecheck, "local_zone", lambda: "America/New_York")
    packed = make_file(old, tmp_path)
    new, env = tmp_path / "new", tmp_path / "new.env"
    for here in ("Etc/UTC", "Asia/Tokyo"):
        monkeypatch.setattr(movecheck, "local_zone", lambda here=here: here)
        ask = asks(PASS)
        with pytest.raises(move.OtherZone) as err:
            unpack(new, packed.path, env, ask=ask)
        text = str(err.value)
        assert f"This PC's clock uses the time zone {here}, but the old PC used America/New_York" in text
        assert "sudo timedatectl set-timezone America/New_York" in text and "run this again" in text
        assert not ask.asked and not (new / "nextbox.db").exists() and not env.exists()
        assert movecheck.main([str(packed.path), str(new)]) == movecheck.OTHER_ZONE == 5
    # Another name for the same clock is fine (US/Eastern is America/New_York), and so is the same zone.
    monkeypatch.setattr(movecheck, "local_zone", lambda: "US/Eastern")
    assert movecheck.main([str(packed.path), str(new)]) == movecheck.GO
    assert movecheck.same_zone("UTC", "Etc/UTC") and not movecheck.same_zone("Europe/London", "Etc/UTC")
    monkeypatch.setattr(movecheck, "local_zone", lambda: "America/New_York")
    assert unpack(new, packed.path, env).accounts == 3


def test_the_zone_is_read_from_this_pc(monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    assert movecheck.local_zone() == "Pacific/Kiritimati"
    monkeypatch.setenv("TZ", ":/usr/share/zoneinfo/America/New_York")
    assert movecheck.local_zone() == "America/New_York"
    monkeypatch.delenv("TZ")
    assert movecheck.local_zone() is None or re.fullmatch(r"[A-Za-z0-9_+./-]+", movecheck.local_zone())


def test_doctor_points_out_a_clock_on_utc(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXTBOX_KEY", "k")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(movecheck, "local_zone", lambda: "Etc/UTC")
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "• this PC's clock is set to UTC" in out and "sudo timedatectl set-timezone" in out
    monkeypatch.setattr(movecheck, "local_zone", lambda: "America/New_York")
    cli.main(["doctor"])
    assert "clock is set to UTC" not in capsys.readouterr().out


def test_the_same_move_file_twice_changes_nothing(old, tmp_path):
    """Re-running move-in.sh with the same file (the history line still works a week later) used to suggest
    --replace, which quietly put the old lists back over what was added on the new PC since."""
    packed = make_file(old, tmp_path)
    new, env = tmp_path / "new", tmp_path / "new.env"
    unpack(new, packed.path, env)
    db.forget_engine(sqlite_url(new))
    LocalTasks(sqlite_url(new), "mike").create({"content": "Added on the new PC after the move"})
    db.forget_engine(sqlite_url(new))
    for replace in (False, True):
        ask = asks(PASS)
        with pytest.raises(move.AlreadyHere) as err:
            unpack(new, packed.path, env, ask=ask, replace=replace)
        assert str(err.value) == f"This move file is already in place here (since {date.today()}). Nothing to do."
        assert not ask.asked
    assert movecheck.main([str(packed.path), str(new), "--replace"]) == movecheck.ALREADY_HERE == 6
    with sqlite3.connect(new / "nextbox.db") as conn:
        assert ("Added on the new PC after the move",) in conn.execute("SELECT content FROM tasks").fetchall()
    assert not (new / "backups").exists()


def test_an_older_move_file_never_quietly_replaces_newer_lists(old, tmp_path):
    """move-out.sh run twice on the old PC makes a second file from the same old lists. With --replace on the new
    PC it would have put aside a week of work there: now that needs --replace-newer, said plainly."""
    first = make_file(old, tmp_path)
    second = make_file(old, tmp_path)  # the old PC's lists, again (another salt)
    new, env = tmp_path / "new", tmp_path / "new.env"
    unpack(new, first.path, env)
    db.forget_engine(sqlite_url(new))
    week = LocalTasks(sqlite_url(new), "mike").create({"content": "Week of work on the new PC"})
    with sqlite3.connect(new / "nextbox.db") as conn:  # ...added a day after the file was made
        conn.execute("UPDATE tasks SET created = created + 86400 WHERE id = ?", (int(week["id"].split(":")[-1]),))
    db.forget_engine(sqlite_url(new))
    with pytest.raises(move.HasData, match="--replace"):
        unpack(new, second.path, env)
    with pytest.raises(move.NewerHere) as err:
        unpack(new, second.path, env, replace=True)
    text = str(err.value)
    assert "after the last change in this move file" in text and "The file is older than what's on this PC" in text
    assert "--replace-newer" in text and "test copy" in text
    assert movecheck.main([str(second.path), str(new), "--replace"]) == movecheck.NEWER_HERE == 8
    with sqlite3.connect(new / "nextbox.db") as conn:
        assert ("Week of work on the new PC",) in conn.execute("SELECT content FROM tasks").fetchall()
    # Said explicitly (a test copy): it goes ahead, and what was here is kept in backups/.
    result = unpack(new, second.path, env, replace_newer=True)
    with sqlite3.connect(result.set_aside / "nextbox.db") as conn:
        assert ("Week of work on the new PC",) in conn.execute("SELECT content FROM tasks").fetchall()
    # Older lists here than the file (an earlier test install with an account): --replace is enough.
    other = tmp_path / "other"
    other.mkdir()
    Accounts(sqlite_url(other)).set_password("tester", "test password", create=True)
    with sqlite3.connect(other / "nextbox.db") as conn:
        conn.execute("UPDATE users SET created = created - 86400")
    db.forget_engine(sqlite_url(other))
    assert unpack(other, second.path, tmp_path / "other.env", replace=True).accounts == 3


def test_moving_back_to_the_old_pc_turns_it_on_again(old, tmp_path, monkeypatch):
    """The way back is the same move the other way round: the old PC's MOVED-AWAY marker goes, so install.sh
    starts Next Box there again, and move-in.sh turns back on what move-out.sh turned off."""
    (tmp_path / "out").mkdir()
    away = make_file(old, tmp_path / "out")
    move.set_marker(old["data"], file=away.path.name, off=["service", "cloudflared"])
    monkeypatch.delenv("DATABASE_URL", raising=False)  # from here on, the PCs run on their SQLite files
    new, env = tmp_path / "new", tmp_path / "new.env"
    unpack(new, away.path, env)
    db.forget_engine(sqlite_url(new))
    LocalTasks(sqlite_url(new), "mike").create({"content": "Made on the new PC"})
    db.forget_engine(sqlite_url(new))
    back = move.pack(Settings(data_dir=new), tmp_path / "back.nbmove", ask=asks(PASS, PASS), answers=no,
                     env_file=env, say=lambda *_: None)
    move.set_marker(new, file=back.path.name, off=["service"])  # the new PC's move-out.sh
    # Back on the old PC (its data folder; DATABASE_URL commented out in the moved .env, so SQLite here).
    home = tmp_path / "old-again"
    shutil.copytree(old["data"], home, symlinks=True) if not PG else _sqlite_copy(old["data"], home)
    assert move.read_marker(home)["turned_off"] == ["cloudflared", "service"]
    with pytest.raises(move.HasData):
        unpack(home, back.path, tmp_path / "old.env")
    result = unpack(home, back.path, tmp_path / "old.env", replace=True)
    assert result.was_away["turned_off"] == ["cloudflared", "service"] and move.read_marker(home) is None
    assert "Next Box had moved away from this PC on" in "\n".join(move.summary(result, ip="192.168.68.50"))
    with sqlite3.connect(home / "nextbox.db") as conn:
        assert ("Made on the new PC",) in conn.execute("SELECT content FROM tasks").fetchall()


def _sqlite_copy(data: Path, home: Path) -> None:
    """On PostgreSQL the old PC's lists are in the server: make its data folder a SQLite copy of them."""
    shutil.copytree(data, home, symlinks=True)
    dbtools.copy_database(PG, sqlite_url(home))
    db.forget_engine(sqlite_url(home))


def test_another_port_goes_into_the_env_and_the_next_steps(old, tmp_path):
    """Port 8787 taken by another program on the server: --port 8788 instead of stopping that program."""
    packed = make_file(old, tmp_path)
    env = tmp_path / "new.env"
    result = unpack(tmp_path / "new", packed.path, env, port=8788)
    assert result.port == 8788 and "NEXTBOX_PORT=8788" in env.read_text().splitlines()
    assert sum(line.startswith("NEXTBOX_PORT=") for line in env.read_text().splitlines()) == 1
    assert mode(env) == 0o600
    text = "\n".join(move.summary(result, ip="192.168.68.50"))
    assert "http://192.168.68.50:8788/app" in text and "nextbox_print_today_url: http://192.168.68.50:8788/" in text
    assert "public hostname's service to: http://localhost:8788" in text
    move.set_env_value(env, "NEXTBOX_PORT", "8790")
    assert [line for line in env.read_text().splitlines() if "NEXTBOX_PORT" in line] == ["NEXTBOX_PORT=8790"]


# ------------------------------------------------------------------ the MOVED-AWAY guard
def test_after_moving_away_the_old_pc_stays_off(tmp_path, monkeypatch, capsys, no_desktop_notifications):
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXTBOX_KEY", "k")
    assert cli.main(["move", "marker", "show"]) == 1
    assert cli.main(["move", "marker", "set", "--file", "/home/x/nextbox-move-2026-10-27.nbmove",
                     "--off", "service"]) == 0
    assert cli.main(["move", "marker", "set", "--off", "tray"]) == 0
    marker = json.loads((tmp_path / "MOVED-AWAY").read_text())
    assert marker["file"] == "nextbox-move-2026-10-27.nbmove" and marker["turned_off"] == ["service", "tray"]
    assert mode(tmp_path / "MOVED-AWAY") == 0o600
    capsys.readouterr()
    assert cli.main(["move", "marker", "show", "--turned-off"]) == 0
    assert capsys.readouterr().out.strip() == "service tray"
    assert cli.main(["move", "marker", "show", "--date"]) == 0
    day = capsys.readouterr().out.strip()
    assert day == marker["moved_at"][:10]
    assert cli.main(["move", "marker", "show", "--name"]) == 0
    assert capsys.readouterr().out.strip() == "nextbox-move-2026-10-27.nbmove"

    # nextbox serve exits with 78 (install.sh's unit doesn't restart on 78), saying when and how to undo.
    assert cli.main(["serve"]) == 78
    err = capsys.readouterr().err
    assert f"Next Box was moved to another PC on {day}" in err and "scripts/move-out.sh --undo" in err
    from nextbox.server import build_default_app
    with pytest.raises(SystemExit) as stopped:
        build_default_app()
    assert stopped.value.code == 78
    # Nothing else that prints or changes data runs either (the hotkey's `clip` included).
    for argv in (["today"], ["--dry-run", "text", "hi"], ["add", "Milk"], ["user", "list"], ["clip"]):
        assert cli.main(argv) == 78, argv
    capsys.readouterr()
    # `clip` (the hotkey) says it as a desktop notification. Tests never pop a real one (conftest).
    assert no_desktop_notifications == [("Next Box", move.moved_away_message(move.read_marker(tmp_path), False))]
    # doctor says it first, and nothing else (plain and --json, which the status square reads).
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[1].startswith(f"✗ Next Box was moved to another PC on {day}")
    assert "scripts/move-out.sh --undo" in out[2] and "1 thing(s) to fix above." in out[-1]
    assert cli.main(["doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checks"][1]["mark"] == "bad" and report["problems"] == 1
    # --undo clears it.
    assert cli.main(["move", "marker", "clear"]) == 0
    assert cli.main(["move", "marker", "show"]) == 1 and move.read_marker(tmp_path) is None


def test_a_marker_that_cant_be_read_still_keeps_it_off(tmp_path):
    (tmp_path / "MOVED-AWAY").write_text("not json")
    marker = move.read_marker(tmp_path)
    assert marker == {"moved_at": "", "file": "", "turned_off": []}
    assert move.moved_away_message(marker).startswith("Next Box was moved to another PC, so it doesn't start")


# ------------------------------------------------------------------ the scripts
SCRIPTS = PROJECT_DIR / "scripts"


def test_install_sh_respects_the_marker_and_never_loops():
    install = (SCRIPTS / "install.sh").read_text()
    assert "RestartPreventExitStatus=78" in install and "Restart=always" in install
    check = install.index(".venv/bin/nextbox move marker show")
    assert check < install.index("Checking everything works") < install.index('if [[ $MOVED == yes ]]; then\n  '
                                                                             'systemctl --user disable --now')
    moved = install.index("if [[ $MOVED == yes ]]; then\n  systemctl --user disable --now")
    assert moved < install.index("systemctl --user enable --now nextbox.service")
    assert "scripts/move-out.sh --undo" in install[moved:install.index("systemctl --user enable --now")]
    assert move.MOVED_EXIT == 78
    update = (SCRIPTS / "update.sh").read_text()
    assert "--exclude '.env.before-move*'" in update
    # The status square isn't added back on a PC Next Box moved away from (move-out.sh removed it).
    tray = (SCRIPTS / "install-tray.sh").read_text()
    assert tray.index('move marker show') < tray.index("# Start at every login")
    assert tray.index("--remove)") < tray.index('move marker show')  # --remove still works
    # Python 3.11+ (picked the same way as move-in.sh), and the phone address from the route and .env's port.
    assert "source scripts/python.sh" in install and "pick_python" in install and "install_packages" in install
    assert "python3 -m venv" not in install and "hostname -I" not in install and "localhost:8787" not in install
    assert ".venv/bin/nextbox move address" in install and "to any port $PORT proto tcp" in install


@pytest.mark.parametrize("name", ["move-in.sh", "move-out.sh", "install.sh", "update.sh", "go-online.sh",
                                  "python.sh"])
def test_the_scripts_are_valid_bash_and_never_show_env(name):
    path = SCRIPTS / name
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0
    assert os.access(path, os.X_OK) or name == "python.sh"  # python.sh is only ever sourced
    text = path.read_text()
    assert "cat .env" not in text and "source .env" not in text and ". .env" not in text


def test_python_3_11_is_required_everywhere():
    """SQLAlchemy 2.1.3 and websockets 17.2 (constraints.txt) need Python 3.11: 3.10 used to pass the check and
    then fail in pip, blamed on the internet."""
    import tomllib
    assert tomllib.loads((PROJECT_DIR / "pyproject.toml").read_text())["project"]["requires-python"] == ">=3.11"
    helper = (SCRIPTS / "python.sh").read_text()
    assert "sys.version_info < (3, 11)" in helper and "python3.11 python3.11-venv" in helper
    assert "Requires-Python" in helper and "pypi.org" in helper
    for name in ("move-in.sh", "install.sh"):
        assert "(3, 10)" not in (SCRIPTS / name).read_text()


def _fake_bin(folder: Path, log: Path, **behaviour) -> Path:
    """systemctl, sudo, loginctl, scp, id, hostname and sleep that only log (state for is-active in files)."""
    folder.mkdir(parents=True, exist_ok=True)
    state = folder / "state"
    state.mkdir(exist_ok=True)
    (folder / "systemctl").write_text(f"""#!/usr/bin/env bash
echo "systemctl $*" >> "{log}"
args=" $* "
unit="${{@: -1}}"; unit="${{unit%.service}}"
case "$args" in
  *" show-environment "*) exit {behaviour.get('user_session', 0)} ;;
  *" is-active "*) [[ -f "{state}/$unit" ]] && exit 0 || exit 3 ;;
  *" is-enabled "*) [[ -f "{state}/$unit" || -f "{state}/$unit.enabled" ]] && exit 0 || exit 1 ;;
  *" disable --now "*) rm -f "{state}/$unit" "{state}/$unit.enabled" ;;
  *" disable "*) rm -f "{state}/$unit.enabled" ;;
  *" stop "*) rm -f "{state}/$unit" ;;
  *" enable --now "*) touch "{state}/$unit" "{state}/$unit.enabled" ;;
  *" start "*|*" restart "*) touch "{state}/$unit" ;;
  *" cat "*) [[ -f "{state}/$unit.unit" ]] && cat "{state}/$unit.unit" || exit 1 ;;
esac
exit 0
""")
    (folder / "sudo").write_text(f'#!/usr/bin/env bash\necho "sudo $*" >> "{log}"\n"$@"\n')
    (folder / "loginctl").write_text(f'#!/usr/bin/env bash\necho "loginctl $*" >> "{log}"\n')
    (folder / "scp").write_text(f'#!/usr/bin/env bash\necho "scp $*" >> "{log}"\ncp "${{1#*:}}" "$2"\n')
    # Test machines often run as root: the scripts refuse root, so `id -u` says an ordinary user here.
    (folder / "id").write_text('#!/usr/bin/env bash\n[[ "${1:-}" == -u ]] && { echo "${FAKE_UID:-1000}"; exit 0; }\n'
                               'exec /usr/bin/id "$@"\n')
    (folder / "hostname").write_text('#!/usr/bin/env bash\necho "${FAKE_HOSTNAME:-oldpc}"\n')
    (folder / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")  # the scripts' waits, made instant
    for f in ("systemctl", "sudo", "loginctl", "scp", "id", "hostname", "sleep"):
        (folder / f).chmod(0o755)
    return state


def _copy_scripts(root: Path) -> Path:
    """A copy of the code the scripts need without .venv: the scripts, movecheck.py (move-in.sh runs it with the
    system's python3 before anything is installed) and pyproject.toml (this copy's version)."""
    (root / "scripts").mkdir(parents=True)
    (root / "nextbox").mkdir()
    for name in ("move-in.sh", "move-out.sh", "python.sh", "go-online.sh"):
        shutil.copy2(SCRIPTS / name, root / "scripts" / name)
    shutil.copy2(PROJECT_DIR / "nextbox" / "movecheck.py", root / "nextbox" / "movecheck.py")
    shutil.copy2(PROJECT_DIR / "pyproject.toml", root / "pyproject.toml")
    return root


def _move_out_pc(tmp_path: Path):
    """The old PC for move-out.sh: a stand-in for .venv/bin/nextbox (the real one runs in the end-to-end move),
    the status square, and stand-ins for systemctl and friends. Returns (run, root, home, log, state)."""
    root = _copy_scripts(tmp_path / "nb")
    home, log = tmp_path / "home", tmp_path / "log"
    (home / ".config" / "autostart").mkdir(parents=True)
    (home / ".config" / "autostart" / "nextbox-tray.desktop").write_text("[Desktop Entry]\n")
    (root / "scripts" / "install-tray.sh").write_text(f"""#!/usr/bin/env bash
echo "install-tray.sh $*" >> "{log}"
[[ "${{1:-}}" == --remove ]] && rm -f "$HOME/.config/autostart/nextbox-tray.desktop"
exit 0
""")
    (root / "scripts" / "install-tray.sh").chmod(0o755)
    (root / ".venv" / "bin").mkdir(parents=True)
    nb = root / ".venv" / "bin" / "nextbox"
    nb.write_text(f"""#!/usr/bin/env bash
echo "nextbox $*" >> "{log}"
M="$HOME/marker"
if [[ "$1 $2" == "move pack" ]]; then
  [[ -n "${{PACK_FAILS:-}}" ]] && {{ echo "Stop Next Box first" >&2; exit 1; }}
  [[ -n "${{PACK_HANGS_UP:-}}" ]] && {{ kill -HUP $PPID; exit 1; }}
  echo "$HOME/nextbox-move-2026-10-27.nbmove" > "$4"; echo locked > "$HOME/nextbox-move-2026-10-27.nbmove"
  echo "Saved the move file: $HOME/nextbox-move-2026-10-27.nbmove (225.6 KB)"; exit 0
fi
[[ "$1 $2" == "move address" ]] && {{ echo "192.168.68.50 8787"; exit 0; }}
if [[ "$1 $2" == "move marker" ]]; then
  case "$3" in
    set) touch "$M"; shift 3; while [[ $# -gt 0 ]]; do [[ "$1" == --off ]] && echo "$2" >> "$M.off"; shift; done ;;
    show) [[ -f "$M" ]] || exit 1; [[ "${{4:-}}" == --date ]] && echo 2026-10-27
          [[ "${{4:-}}" == --name ]] && echo nextbox-move-2026-10-27.nbmove
          [[ "${{4:-}}" == --turned-off ]] && tr '\\n' ' ' < "$M.off" 2>/dev/null; true ;;
    clear) rm -f "$M" "$M.off" ;;
  esac
fi
""")
    nb.chmod(0o755)
    # --forget reads the data folder from Settings; the stand-in says the usual one.
    py = root / ".venv" / "bin" / "python"
    py.write_text('#!/usr/bin/env bash\n[[ "$1" == -c ]] && { echo "$HOME/.local/share/nextbox"; exit 0; }\n'
                  'cat >/dev/null; exit 1\n')
    py.chmod(0o755)
    fake = tmp_path / "bin"
    state = _fake_bin(fake, log)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "USER": "mike"}

    def run(*args, answer="MOVE\n", **extra):
        return subprocess.run(["bash", str(root / "scripts" / "move-out.sh"), *args], input=answer, text=True,
                              capture_output=True, env={**env, **extra}, timeout=60)
    return run, root, home, log, state


def test_move_out_sh_switches_everything_off_and_undo_puts_it_back(tmp_path):
    run, root, home, log, state = _move_out_pc(tmp_path)
    for unit in ("nextbox", "cloudflared", "ssh"):
        (state / unit).touch()

    r = run(answer="no\n")
    assert r.returncode == 1 and "Nothing changed." in r.stdout
    assert log.read_text().splitlines() == ["nextbox move marker show"]  # it only looked
    r = run(PACK_FAILS="1")
    assert r.returncode == 1 and "No move file was made, so Next Box is back on here" in r.stdout
    assert (state / "nextbox").exists() and not (home / "marker").exists()
    log.unlink()
    r = run(answer="move\n")  # any case
    assert r.returncode == 0, r.stdout + r.stderr
    steps = log.read_text().splitlines()
    pack = next(x for x in steps if x.startswith("nextbox move pack --path-file "))
    order = [steps.index(s) for s in ("systemctl --user stop nextbox.service", pack,
                                      "nextbox move marker set --file nextbox-move-2026-10-27.nbmove --off service",
                                      "systemctl --user disable nextbox.service",
                                      "install-tray.sh --remove",
                                      "nextbox move marker set --off tray",
                                      "sudo systemctl disable --now cloudflared",
                                      "nextbox move marker set --off cloudflared")]
    assert order == sorted(order)
    assert not (state / "nextbox").exists() and not (state / "cloudflared").exists()
    out = r.stdout
    get = ("rm -rf ~/nb-src && git clone -q --depth 1 -b claude/ticket-todoist-printer-nldz8a "
           "https://github.com/brazzybraz2102/trivia-quiz.git ~/nb-src && mkdir -p ~/next-box && "
           "cp -a ~/nb-src/next-box/. ~/next-box/ && rm -rf ~/nb-src")
    for words in ("Next Box goes offline here", "one locked file", "Nothing is deleted",
                  "scripts/move-out.sh --undo", f"The move file: {home}/nextbox-move-2026-10-27.nbmove\n",
                  "USB stick", "Cloudflare would keep sending some visitors",
                  # one line to paste on the new PC: the code and the move, over the network or from the stick
                  f"{get} && ~/next-box/scripts/move-in.sh mike@192.168.68.50:{home}/nextbox-move-2026-10-27.nbmove",
                  f"{get} && ~/next-box/scripts/move-in.sh ~/nextbox-move-2026-10-27.nbmove",
                  "Are you sure you want to continue connecting\", type yes"):
        assert words in out, words
    assert out.count("225.6 KB") == 1  # the size, once (pack says it)

    # A second run: it says Next Box already moved, and only goes on when asked again.
    log.unlink()
    r = run(answer="\n")
    assert r.returncode == 1 and "Nothing changed." in r.stdout
    assert ("Next Box already moved away from this PC on 2026-10-27 (file nextbox-move-2026-10-27.nbmove)."
            in r.stdout) and "only if the first one or its passphrase is lost" in r.stdout
    assert "This moves Next Box off this PC" not in r.stdout and "move pack" not in log.read_text()
    r = run()
    assert r.returncode == 0 and "move pack" in log.read_text()

    # --undo: asks first, warns about two copies and says how to move back instead, then turns back on exactly
    # what was turned off.
    log.unlink()
    r = run("--undo", answer="nope\n")
    assert r.returncode == 1 and "two copies" in r.stdout and (home / "marker").exists()
    assert "move-in.sh <the file it made> --replace" in r.stdout
    r = run("--undo", answer="undo\n", DISPLAY=":0")
    assert r.returncode == 0, r.stdout + r.stderr
    steps = log.read_text().splitlines()
    assert "nextbox move marker clear" in steps and "systemctl --user enable --now nextbox.service" in steps
    assert "install-tray.sh " in steps and "sudo systemctl enable --now cloudflared" in steps
    assert (state / "nextbox").exists() and (state / "cloudflared").exists() and not (home / "marker").exists()
    assert f"Check it with: {root}/.venv/bin/nextbox doctor" in r.stdout
    r = run("--undo")
    assert r.returncode == 0 and "nothing to undo" in r.stdout


def test_move_out_sh_turns_off_a_tunnel_that_only_starts_with_the_pc(tmp_path):
    """cloudflared enabled but not running at moving time would connect again at the next boot, and Cloudflare
    would split visitors between the two PCs."""
    run, root, home, log, state = _move_out_pc(tmp_path)
    (state / "nextbox").touch()
    (state / "cloudflared.enabled").touch()
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "sudo systemctl disable --now cloudflared" in log.read_text().splitlines()
    assert not (state / "cloudflared.enabled").exists() and "cloudflared" in (home / "marker.off").read_text()


def test_move_out_sh_leaves_a_tunnel_on_that_also_carries_other_websites(tmp_path):
    """One tunnel can carry several websites from this PC. Turning it off would take them down too: then only
    Next Box's address leaves it (in the dashboard), and the new PC gets a tunnel of its own."""
    run, root, home, log, state = _move_out_pc(tmp_path)
    (state / "nextbox").touch()
    (state / "cloudflared").touch()
    (root / ".env").write_text("NEXTBOX_KEY=k\nNEXTBOX_PUBLIC_URL=https://nextbox.example.com\n")
    r = run(answer="MOVE\nn\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Does this PC's tunnel also carry other websites" in r.stdout
    assert "The tunnel stays on here" in r.stdout and "delete only nextbox.example.com" in r.stdout
    assert "Create a tunnel" in r.stdout and "nextbox.example.com → HTTP → localhost:8787" in r.stdout
    assert (state / "cloudflared").exists() and "disable --now cloudflared" not in log.read_text()
    assert "cloudflared" not in (home / "marker.off").read_text()  # --undo won't touch it either
    # go-online.sh recorded a shared tunnel: no question, it stays on.
    run, root, home, log, state = _move_out_pc(tmp_path / "again")
    (state / "nextbox").touch()
    (state / "cloudflared").touch()
    (root / ".env").write_text("NEXTBOX_KEY=k\nNEXTBOX_TUNNEL=shared\n")
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Does this PC's tunnel" not in r.stdout and "delete only nextbox.braznarkel.com" in r.stdout
    assert (state / "cloudflared").exists() and "disable --now cloudflared" not in log.read_text()


def test_move_out_sh_moving_back_says_replace(tmp_path):
    """Next Box came to this PC with a move: the PC it came from still has its moving-day lists, so the line to
    paste there ends with --replace (without it, move-in.sh stops there)."""
    run, root, home, log, state = _move_out_pc(tmp_path)
    (state / "nextbox").touch()
    r = run()
    assert r.returncode == 0 and "--replace" not in r.stdout
    data = home / ".local" / "share" / "nextbox"
    data.mkdir(parents=True)
    (data / "MOVED-IN").write_text("{}")
    (home / "marker").unlink()
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "~/next-box/scripts/move-in.sh ~/nextbox-move-2026-10-27.nbmove --replace" in r.stdout
    assert "puts its old lists aside" in r.stdout


def test_move_out_sh_puts_next_box_back_on_when_the_terminal_closes(tmp_path):
    run, root, home, log, state = _move_out_pc(tmp_path)
    (state / "nextbox").touch()
    r = run(PACK_HANGS_UP="1")
    assert r.returncode == 129
    assert (state / "nextbox").exists() and not (home / "marker").exists()  # on again, and not marked as moved
    assert "systemctl --user start nextbox.service" in log.read_text().splitlines()


def test_move_out_sh_refuses_root(tmp_path):
    run, root, home, log, state = _move_out_pc(tmp_path)
    r = run(FAKE_UID="0")
    assert r.returncode == 1 and "Don't run this with sudo or as root" in r.stdout and not log.exists()


def test_move_out_sh_forget_deletes_only_on_the_pc_next_box_moved_away_from(tmp_path):
    """After a week: one guarded command instead of rm -rf, which on the wrong PC would delete the live lists."""
    run, root, home, log, state = _move_out_pc(tmp_path)
    data = home / ".local" / "share" / "nextbox"
    data.mkdir(parents=True)
    (data / "nextbox.db").write_bytes(b"lists")
    (data / "app").symlink_to(root)
    unit = home / ".config" / "systemd" / "user" / "nextbox.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")
    (home / "nextbox-move-2026-10-27.nbmove").write_bytes(b"locked")
    fake = tmp_path / "bin"
    (fake / "cloudflared").write_text(f'#!/usr/bin/env bash\necho "cloudflared $*" >> "{log}"\n')
    (fake / "cloudflared").chmod(0o755)

    r = run("--forget", answer="oldpc\n")  # typed on the PC Next Box runs on: no marker there
    assert r.returncode == 1 and "nothing was deleted" in r.stdout
    assert (data / "nextbox.db").exists() and root.exists() and unit.exists()

    (home / "marker").touch()
    (home / "marker.off").write_text("service\ncloudflared\n")
    (data / "MOVED-AWAY").write_text("{}")
    r = run("--forget", answer="newpc\n")  # not this PC's name
    assert r.returncode == 1 and "Nothing was deleted." in r.stdout and "(oldpc)" in r.stdout
    assert (data / "nextbox.db").exists() and root.exists()

    r = run("--forget", answer="oldpc\ny\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not data.exists() and not root.exists() and not unit.exists()
    assert not (home / "nextbox-move-2026-10-27.nbmove").exists()
    assert not (home / ".config" / "autostart" / "nextbox-tray.desktop").exists()
    steps = log.read_text().splitlines()
    assert "sudo cloudflared service uninstall" in steps and "systemctl --user daemon-reload" in steps
    assert "Last step: delete the move file on the new PC" in r.stdout


def _move_in(tmp_path: Path, **behaviour):
    """The new PC for move-in.sh (no .venv yet). Returns (run, root, home, state)."""
    root = _copy_scripts(tmp_path / "nb")
    home = tmp_path / "home"
    home.mkdir()
    fake = tmp_path / "bin"
    state = _fake_bin(fake, tmp_path / "log", **behaviour)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "USER": "mike", "TZ": "America/New_York",
           "NEXTBOX_PORT": str(free_port())}

    def run(*args, cwd=None, **extra):
        return subprocess.run(["bash", str(root / "scripts" / "move-in.sh"), *args], text=True, input="",
                              capture_output=True, env={**env, **extra}, timeout=60, cwd=cwd)
    return run, root, home, state


def test_move_in_sh_checks_the_pc_first_and_says_what_to_do(tmp_path):
    run, root, home, state = _move_in(tmp_path, user_session=1)
    r = run()
    assert r.returncode == 2 and "Usage: scripts/move-in.sh <move file>" in r.stdout
    r = run(str(home / "x.nbmove"))
    assert r.returncode == 1 and "systemctl --user doesn't work in this terminal" in r.stdout
    _fake_bin(tmp_path / "bin", tmp_path / "log")  # a working user session from here on
    uname = tmp_path / "bin" / "uname"
    uname.write_text("#!/usr/bin/env bash\necho Darwin\n")
    uname.chmod(0o755)
    r = run(str(home / "x.nbmove"))
    assert r.returncode == 1 and "This PC runs Darwin, but Next Box needs Linux" in r.stdout
    uname.unlink()
    r = run(str(home / "x.nbmove"), FAKE_UID="0")
    assert r.returncode == 1 and "Don't run this as root or with sudo." in r.stdout
    (state / "nextbox").touch()  # a Next Box is already running here: that's looked at once the file is checked
    r = run(str(home / "x.nbmove"))
    assert r.returncode == 1 and "Can't find" in r.stdout and "scripts/move-in.sh ~/nextbox-move-" in r.stdout
    (state / "nextbox").unlink()
    assert not (root / ".venv").exists()  # nothing was set up before the checks passed
    # Typed on the old PC by mistake (or moving back): it says both ways back.
    (home / ".local" / "share" / "nextbox").mkdir(parents=True)
    (home / ".local" / "share" / "nextbox" / "MOVED-AWAY").write_text("{}")
    r = run(str(home / "x.nbmove"))
    assert r.returncode == 1 and "Next Box moved away from this PC before" in r.stdout
    assert "scripts/move-out.sh --undo" in r.stdout and f"scripts/move-in.sh {home / 'x.nbmove'} --replace" in r.stdout
    r = run(str(home / "x.nbmove"), "--replace")
    assert r.returncode == 1 and "Can't find" in r.stdout and "moved away" not in r.stdout


def test_move_in_sh_finds_a_file_named_from_the_folder_mike_is_in(tmp_path):
    """From a USB stick's folder: a path like ./nextbox-move-....nbmove is looked up there, not in the code folder.
    And an empty (half-copied) file is caught in seconds, before anything is installed."""
    run, root, home, state = _move_in(tmp_path)
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / "nextbox-move-2026-10-27.nbmove").write_bytes(b"")
    for name in ("nextbox-move-2026-10-27.nbmove", "./nextbox-move-2026-10-27.nbmove"):
        r = run(name, cwd=usb)
        assert r.returncode == 1 and "Can't find" not in r.stdout, r.stdout
        assert "The move file is empty: the copy didn't finish. Copy it again." in r.stdout
        assert "Copy the move file again" in r.stdout and "newest" not in r.stdout
    assert not (root / ".venv").exists()


def test_move_in_sh_python_3_10_is_too_old(tmp_path):
    run, root, home, state = _move_in(tmp_path)
    for name in ("python3", "python3.11", "python3.12", "python3.13", "python3.14"):  # Ubuntu 22.04: 3.10 only
        fake = tmp_path / "bin" / name
        fake.write_text('#!/usr/bin/env bash\n[[ "${1:-}" == -V ]] && { echo "Python 3.10.12"; exit 0; }\nexit 1\n')
        fake.chmod(0o755)
    r = run(str(home / "x.nbmove"))
    assert r.returncode == 1
    assert "Next Box needs Python 3.11 or newer, and this PC has: Python 3.10.12" in r.stdout
    assert "Ubuntu 22.04 (its Python 3.10 is too old): sudo apt install -y python3.11 python3.11-venv" in r.stdout


def test_move_in_sh_check_says_whether_this_pc_is_ready(tmp_path):
    """--check before moving out of the old PC: Python, port, time zone, internet. Nothing is changed."""
    run, root, home, state = _move_in(tmp_path)
    offline = {"HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9", "NO_PROXY": "",
               "no_proxy": ""}
    r = run("--check", **offline)
    assert "✓ Time zone: America/New_York." in r.stdout
    assert r.returncode == 1 and "can't reach pypi.org" in r.stdout
    r = run("--check", TZ="Etc/UTC", **offline)
    assert "This PC's clock is set to Etc/UTC" in r.stdout and "sudo timedatectl set-timezone" in r.stdout
    assert not (root / ".venv").exists() and not (home / ".local").exists()


def test_move_in_sh_checks_the_file_against_this_pc_before_installing(old, tmp_path, monkeypatch):
    """With the system's own Python, in seconds: the old PC's time zone, the same file again, and a Next Box
    here with accounts. The pip install (minutes) only starts once all of that is fine."""
    monkeypatch.setattr(movecheck, "local_zone", lambda: "America/New_York")
    packed = make_file(old, tmp_path)
    run, root, home, state = _move_in(tmp_path)
    r = run(str(packed.path), TZ="Asia/Tokyo")
    assert r.returncode == 1 and "but the old PC used America/New_York" in r.stdout
    assert "sudo timedatectl set-timezone America/New_York" in r.stdout and "Nothing was changed." in r.stdout
    data = home / ".local" / "share" / "nextbox"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    data.mkdir(parents=True)
    Accounts(sqlite_url(data)).set_password("tester", "test password", create=True)  # an earlier test install
    db.forget_engine(sqlite_url(data))
    r = run(str(packed.path))
    assert r.returncode == 1 and "already has a Next Box with 1 account(s)" in r.stdout
    assert "Only if that Next Box was just a test copy" in r.stdout and f"{packed.path} --replace" in r.stdout
    shutil.rmtree(data)
    unpack(data, packed.path, tmp_path / "new.env")  # moved in already
    db.forget_engine(sqlite_url(data))
    (state / "nextbox").touch()  # ...and running
    r = run(str(packed.path), "--replace")  # the same command again, from the history
    assert r.returncode == 0 and "This move file is already in place here" in r.stdout
    assert "Nothing was changed." in r.stdout and not (data / "backups").exists()
    assert not (root / ".venv").exists()
    # An earlier run stopped after the files were in place, before the install (Next Box isn't running):
    # running it again finishes the setup instead of saying "nothing to do".
    (state / "nextbox").unlink()
    log = tmp_path / "steps"
    (root / "scripts" / "install.sh").write_text(f"""#!/usr/bin/env bash
echo "install.sh" >> "{log}"
mkdir -p .venv/bin
printf '#!/usr/bin/env bash\\necho "nextbox $*" >> "{log}"\\n' > .venv/bin/nextbox
chmod 755 .venv/bin/nextbox
""")
    (root / "scripts" / "install.sh").chmod(0o755)
    r = run(str(packed.path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already in place here. Finishing the setup..." in r.stdout and "Nothing was changed." not in r.stdout
    steps = log.read_text().splitlines()
    assert steps[0] == "install.sh" and "nextbox doctor" in steps and "nextbox move next-steps" in steps
    assert not any("unpack" in x for x in steps) and not (data / "backups").exists()


def test_move_in_sh_sets_aside_an_empty_running_next_box_by_itself(old, tmp_path, monkeypatch):
    """install.sh ran first on the new PC (no accounts, the service running): no --replace needed, as README
    says. That Next Box is only stopped once the packages are in, so a failed download leaves it running. And a
    pip failure only blames the internet when pypi.org can't be reached."""
    monkeypatch.setattr(movecheck, "local_zone", lambda: "America/New_York")
    packed = make_file(old, tmp_path)
    run, root, home, state = _move_in(tmp_path)
    data = home / ".local" / "share" / "nextbox"
    data.mkdir(parents=True)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert not Accounts(sqlite_url(data)).has_users()  # an empty database, like install.sh's
    db.forget_engine(sqlite_url(data))
    (state / "nextbox").touch()
    offline = {"HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9", "NO_PROXY": "",
               "no_proxy": "", "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    r = subprocess.run(["bash", str(root / "scripts" / "move-in.sh"), str(packed.path)], text=True, input="",
                       capture_output=True, timeout=300,
                       env={"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "HOME": str(home), "USER": "mike",
                            "TZ": "America/New_York", "NEXTBOX_PORT": str(free_port()), **offline})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "--replace" not in r.stdout and "already has a Next Box" not in r.stdout
    assert "Setting up Next Box's Python packages" in r.stdout
    assert "This PC can't reach pypi.org" in r.stdout and "Nothing was moved yet." in r.stdout
    log = (tmp_path / "log").read_text()
    assert "systemctl --user stop nextbox.service" not in log and (state / "nextbox").exists()  # still running
    assert (data / "nextbox.db").exists() and not (data / "backups").exists()


def test_move_in_sh_names_the_program_holding_the_port_and_offers_another_port(tmp_path):
    run, root, home, state = _move_in(tmp_path)
    fake = tmp_path / "bin"
    holder = socket.socket()  # an old program sitting on Next Box's port (like Mike's ticket.service did)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    (fake / "ss").write_text(f'#!/usr/bin/env bash\necho \'LISTEN 0 128 0.0.0.0:{port} 0.0.0.0:* '
                             'users:(("ticket",pid=4242,fd=3))\'\n')
    (fake / "ps").write_text('#!/usr/bin/env bash\n[[ "$2" == uunit= ]] && echo ticket.service || echo user@1000.service\n')
    for f in ("ss", "ps"):
        (fake / f).chmod(0o755)
    try:
        r = run(str(home / "x.nbmove"), NEXTBOX_PORT=str(port))
        free = run(str(home / "x.nbmove"), "--port", str(free_port()), NEXTBOX_PORT=str(port))
    finally:
        holder.close()
    assert r.returncode == 1
    assert f"Port {port}, which Next Box uses, is taken by ticket (process 4242)." in r.stdout
    assert "If you don't need it: systemctl --user disable --now ticket.service" in r.stdout
    assert "If you don't know what ticket is, don't stop it. Run Next Box on another port instead:" in r.stdout
    assert re.search(rf"scripts/move-in.sh {re.escape(str(home / 'x.nbmove'))} --port \d+", r.stdout)
    assert free.returncode == 1 and "Can't find" in free.stdout  # with --port the port check passes


def _go_online(tmp_path: Path):
    root = _copy_scripts(tmp_path / "nb")
    (root / ".env").write_text("NEXTBOX_KEY=k\n")
    log = tmp_path / "log"
    state = _fake_bin(tmp_path / "bin", log)
    for name, body in (("cloudflared", f'echo "cloudflared $*" >> "{log}"'),
                       ("curl", '[[ -n "${CURL_OK:-}" ]] && { echo \'{"version": "0.14.11"}\'; exit 0; }; exit 22')):
        (tmp_path / "bin" / name).write_text(f"#!/usr/bin/env bash\n{body}\n")
        (tmp_path / "bin" / name).chmod(0o755)
    env = {"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "HOME": str(tmp_path), "USER": "mike"}

    def token(tunnel: str) -> str:
        import base64
        return base64.b64encode(json.dumps({"a": "account-id-1234", "t": tunnel, "s": "SENTINEL-tunnel-secret"})
                                .encode()).decode()

    def run(pasted: str, **extra):
        return subprocess.run(["bash", str(root / "scripts" / "go-online.sh"), "https://nextbox.example.com"],
                              input=f"sudo cloudflared service install {pasted}\n", text=True, capture_output=True,
                              env={**env, **extra}, timeout=60)
    return run, root, state, log, token


def test_go_online_checks_the_address_really_reaches_next_box(tmp_path):
    run, root, state, log, token = _go_online(tmp_path)
    r = run(token("tunnel-nextbox"), CURL_OK="1")  # nothing running yet: installs it
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Done: https://nextbox.example.com reaches Next Box on this PC" in r.stdout
    assert any(s.startswith("sudo cloudflared service install ") for s in log.read_text().splitlines())
    assert "NEXTBOX_PUBLIC_URL=https://nextbox.example.com" in (root / ".env").read_text().splitlines()
    # The tunnel runs, the address doesn't answer: never "Done", but what to check in the dashboard.
    (state / "cloudflared").touch()
    (state / "cloudflared.unit").write_text(f"ExecStart=/usr/bin/cloudflared tunnel run --token {token('tunnel-nextbox')}\n")
    r = run(token("tunnel-nextbox"))
    assert r.returncode == 1 and "Done" not in r.stdout and "It's Next Box's tunnel." in r.stdout
    assert "nextbox.example.com → HTTP → localhost:8787" in r.stdout
    # A server that already runs another tunnel for something else: Next Box's address goes on that one.
    (state / "cloudflared.unit").write_text(f"ExecStart=/usr/bin/cloudflared tunnel run --token {token('tunnel-other')}\n")
    (root / ".env").write_text("NEXTBOX_KEY=k\nNEXTBOX_PORT=8788\n")
    r = run(token("tunnel-nextbox"))
    assert r.returncode == 1 and "Done" not in r.stdout
    assert "This PC already runs another Cloudflare tunnel" in r.stdout
    assert "nextbox.example.com → HTTP → localhost:8788" in r.stdout
    # The address leaves Next Box's old tunnel first (Cloudflare refuses a name that's still in use), and the
    # shared tunnel is remembered, so move-out.sh leaves it on if Next Box moves away later.
    assert r.stdout.index("delete nextbox.example.com") < r.stdout.index("Add: nextbox.example.com")
    assert (root / ".env").read_text().splitlines().count("NEXTBOX_TUNNEL=shared") == 1
    r = run(token("tunnel-nextbox"))
    assert (root / ".env").read_text().splitlines().count("NEXTBOX_TUNNEL=shared") == 1  # once, also on a re-run
    for out in (r.stdout, r.stderr):
        assert "SENTINEL" not in out and "eyJ" not in out
    script = (SCRIPTS / "go-online.sh").read_text()
    # No apt (Fedora): it says what to install and stops before asking for the token.
    assert script.index("command -v apt-get") < script.index("https://pkg.cloudflare.com (Fedora") \
        < script.index("exit 1") < script.index("Paste the command")


# ------------------------------------------------------------------ docs
def test_the_docs_explain_the_move():
    readme = (PROJECT_DIR / "README.md").read_text()
    section = readme.split("## Move Next Box to another PC")[1].split("\n## ")[0]
    for words in ("scripts/move-out.sh", "scripts/move-in.sh", "--undo", "USB stick", "scp",
                  "git clone --depth 1 -b claude/ticket-todoist-printer-nldz8a "
                  "https://github.com/brazzybraz2102/trivia-quiz.git ~/nb-src",
                  # works also when ~/next-box is already there (an earlier try): copied into it, never nested
                  "mkdir -p ~/next-box && cp -a ~/nb-src/next-box/. ~/next-box/",
                  "move-in.sh --check", "scripts/go-online.sh", "nextbox_print_today_url",
                  "scripts/install-tray.sh", "After a week", "passphrase", "update.sh", "--port 8788",
                  "--replace-newer", "timedatectl set-timezone", "python3.11", "move-out.sh --forget",
                  "Get back what was set aside", "move back"):
        assert words in section, words
    # The old bare rm -rf is gone: only the guarded --forget deletes, and the move file goes last.
    assert "rm -rf" not in section.replace("rm -rf ~/nb-src", "")
    after = section.split("### After a week")[1]
    assert after.index("move-out.sh --forget") < after.index("rm ~/nextbox-move-*.nbmove")
    assert "Python 3.10 or newer" not in readme
    spec = (PROJECT_DIR / "SPEC.md").read_text()
    assert "## 33. Move to another PC" in spec and "RestartPreventExitStatus=78" in spec
    assert "AES-256-GCM" in spec and "scrypt" in spec and "MOVED-IN" in spec and "--forget" in spec
    assert "nextbox move pack" in (PROJECT_DIR / "DATABASE.md").read_text()


def test_moving_back_says_the_tunnel_is_on_again_here():
    """move-in.sh turned this PC's tunnel back on (moving back): don't also say it has to move here."""
    back = move.next_steps(8787, False, True, ip="192.168.68.50", tunnel_here="https://nextbox.example.com/")
    assert "The internet tunnel is on again here. Check it with: curl https://nextbox.example.com/version" in back
    assert not any("has to move here" in line for line in back)
    assert any("has to move here" in line for line in move.next_steps(8787, False, True, ip="192.168.68.50"))


def test_old_time_zone_names_count_as_their_real_zone(monkeypatch):
    """A PC set to US/Eastern and one set to America/New_York keep the same clock, also where the zone data
    has only the real names."""
    import zoneinfo
    real = zoneinfo.ZoneInfo

    def only_real_names(name):
        if "/" in name and name.split("/")[0] in ("US", "Canada"):
            raise zoneinfo.ZoneInfoNotFoundError(name)
        return real(name)
    monkeypatch.setattr(zoneinfo, "ZoneInfo", only_real_names)
    assert movecheck.same_zone("US/Eastern", "America/New_York")
    assert movecheck.same_zone("America/Los_Angeles", "US/Pacific")
    assert not movecheck.same_zone("US/Eastern", "America/Chicago")
