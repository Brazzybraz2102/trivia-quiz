"""Moving Next Box to another PC: `nextbox move pack/info/unpack`, the MOVED-AWAY guard and the two scripts.

Runs on SQLite and on PostgreSQL (DATABASE_URL set): there the old PC's database is PostgreSQL, so pack copies it
into the SQLite file inside the move file, and the new PC gets SQLite (setup-postgres.sh moves it back).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tarfile
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nextbox import cli, db, dbtools, jobs, move, printer
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
    assert first["status"] != "skipped"
    # What stays behind: the PNG cache (made by the print), backups, the practice database, the print lock,
    # the app link.
    (data / "backups").mkdir(exist_ok=True)
    (data / "backups" / "nextbox-20261001.db").write_bytes(b"old backup")
    (data / "practice").mkdir(exist_ok=True)
    (data / "practice" / "nextbox.db").write_bytes(b"practice")
    (data / ".printer.lock").write_bytes(b"")
    (data / "app").symlink_to(PROJECT_DIR)
    return {"dentist": dentist["id"]}


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
def test_everything_moves_and_works_on_the_new_pc(old, tmp_path, no_printer_knocks):
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
    assert sorted(p.name for p in new.iterdir()) == ["nextbox.db", "secret.key"]
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


def test_the_file_holds_only_what_moves(old, tmp_path):
    assert any((old["data"] / "png").iterdir())  # the PNG cache is there on the old PC...
    packed = make_file(old, tmp_path)
    assert members(packed.path) == ["manifest.json", "nextbox.db", "secret.key", ".env"]  # ...but not in the file
    assert set(packed.manifest["files"]) == {"nextbox.db", "secret.key", ".env"}
    with open(packed.path, "rb") as fh:
        header, _ = move.read_header(fh)
    assert set(header) == {"format", "app_version", "made_on", "from_host", "kdf", "cipher", "chunk",
                           "nonce_prefix", "check"}
    assert header["kdf"]["n"] == 2 ** 15 and header["kdf"]["r"] == 8 and header["kdf"]["p"] == 1
    assert header["cipher"] == "AES-256-GCM" and header["chunk"] == 1 << 20
    assert not list(old["data"].glob(".move-out-*"))  # the temporary copy is gone


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
    assert str(err.value) == "That passphrase didn't open this move file."
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
    assert unpack(tmp_path / "fine", packed.path, tmp_path / "fine.env").accounts == 3  # the original still works


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
    assert cli.main(["move", "info", str(newer.path)]) == 1
    assert "Update this copy first: scripts/update.sh" in capsys.readouterr().out
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


# ------------------------------------------------------------------ the MOVED-AWAY guard
def test_after_moving_away_the_old_pc_stays_off(tmp_path, monkeypatch, capsys):
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


@pytest.mark.parametrize("name", ["move-in.sh", "move-out.sh", "install.sh", "update.sh"])
def test_the_scripts_are_valid_bash_and_never_show_env(name):
    path = SCRIPTS / name
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0
    assert os.access(path, os.X_OK)
    text = path.read_text()
    assert "cat .env" not in text and "source .env" not in text and ". .env" not in text


def _fake_bin(folder: Path, log: Path, **behaviour) -> Path:
    """systemctl, sudo, loginctl and scp that only log their arguments (state for is-active in files)."""
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
  *" is-enabled "*) [[ -f "{state}/$unit" ]] && exit 0 || exit 1 ;;
  *" stop "*|*" disable --now "*) rm -f "{state}/$unit" ;;
  *" start "*|*" enable --now "*) touch "{state}/$unit" ;;
esac
exit 0
""")
    (folder / "sudo").write_text(f'#!/usr/bin/env bash\necho "sudo $*" >> "{log}"\n"$@"\n')
    (folder / "loginctl").write_text(f'#!/usr/bin/env bash\necho "loginctl $*" >> "{log}"\n')
    (folder / "scp").write_text(f'#!/usr/bin/env bash\necho "scp $*" >> "{log}"\ncp "${{1#*:}}" "$2"\n')
    # Test machines often run as root: move-in.sh refuses root, so `id -u` says an ordinary user here.
    (folder / "id").write_text('#!/usr/bin/env bash\n[[ "${1:-}" == -u ]] && { echo 1000; exit 0; }\n'
                               'exec /usr/bin/id "$@"\n')
    for f in ("systemctl", "sudo", "loginctl", "scp", "id"):
        (folder / f).chmod(0o755)
    return state


def _copy_scripts(root: Path) -> Path:
    (root / "scripts").mkdir(parents=True)
    for name in ("move-in.sh", "move-out.sh"):
        shutil.copy2(SCRIPTS / name, root / "scripts" / name)
    return root


def test_move_out_sh_switches_everything_off_and_undo_puts_it_back(tmp_path):
    """move-out.sh's own steps, with a stand-in for .venv/bin/nextbox (the real one runs in the end-to-end move)."""
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
  echo "$HOME/nextbox-move-2026-10-27.nbmove" > "$4"; echo locked > "$HOME/nextbox-move-2026-10-27.nbmove"; exit 0
fi
if [[ "$1 $2" == "move marker" ]]; then
  case "$3" in
    set) touch "$M"; shift 3; while [[ $# -gt 0 ]]; do [[ "$1" == --off ]] && echo "$2" >> "$M.off"; shift; done ;;
    show) [[ -f "$M" ]] || exit 1; [[ "${{4:-}}" == --date ]] && echo 2026-10-27; [[ "${{4:-}}" == --turned-off ]] && tr '\\n' ' ' < "$M.off"; true ;;
    clear) rm -f "$M" "$M.off" ;;
  esac
fi
""")
    nb.chmod(0o755)
    fake = tmp_path / "bin"
    state = _fake_bin(fake, log)
    for unit in ("nextbox", "cloudflared"):
        (state / unit).touch()
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "USER": "mike"}

    def run(*args, answer="MOVE\n", **extra):
        return subprocess.run(["bash", str(root / "scripts" / "move-out.sh"), *args], input=answer, text=True,
                              capture_output=True, env={**env, **extra}, timeout=60)

    r = run(answer="no\n")
    assert r.returncode == 1 and "Nothing changed." in r.stdout and not log.exists()
    r = run(PACK_FAILS="1")
    assert r.returncode == 1 and "No move file was made, so Next Box is back on here" in r.stdout
    assert (state / "nextbox").exists() and not (home / "marker").exists()
    log.unlink()
    r = run()
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
    for words in ("Next Box goes offline here", "one locked file", "Nothing is deleted",
                  "scripts/move-out.sh --undo", f"The move file: {home}/nextbox-move-2026-10-27.nbmove",
                  "USB stick", "scripts/move-in.sh mike@", "Cloudflare would keep sending some visitors"):
        assert words in out, words
    # --undo: asks first, warns about two copies, then turns back on exactly what was turned off.
    log.unlink()
    r = run("--undo", answer="nope\n")
    assert r.returncode == 1 and "two copies" in r.stdout and (home / "marker").exists()
    r = run("--undo", answer="UNDO\n")
    assert r.returncode == 0, r.stdout + r.stderr
    steps = log.read_text().splitlines()
    assert "nextbox move marker clear" in steps and "systemctl --user enable --now nextbox.service" in steps
    assert "install-tray.sh " in steps and "sudo systemctl enable --now cloudflared" in steps
    assert (state / "nextbox").exists() and (state / "cloudflared").exists() and not (home / "marker").exists()
    r = run("--undo")
    assert r.returncode == 0 and "nothing to undo" in r.stdout


def test_move_in_sh_checks_the_pc_first_and_says_what_to_do(tmp_path):
    root = _copy_scripts(tmp_path / "nb")
    home, log = tmp_path / "home", tmp_path / "log"
    home.mkdir()
    fake = tmp_path / "bin"
    state = _fake_bin(fake, log, user_session=1)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "USER": "mike",
           "NEXTBOX_PORT": str(free_port())}

    def run(*args, **extra):
        return subprocess.run(["bash", str(root / "scripts" / "move-in.sh"), *args], text=True, input="",
                              capture_output=True, env={**env, **extra}, timeout=60)

    r = run()
    assert r.returncode == 2 and "Usage: scripts/move-in.sh <move file>" in r.stdout
    r = run(str(home / "x.nbmove"))
    assert r.returncode == 1 and "systemctl --user doesn't work in this terminal" in r.stdout
    _fake_bin(fake, log)  # a working user session from here on
    uname = fake / "uname"
    uname.write_text("#!/usr/bin/env bash\necho Darwin\n")
    uname.chmod(0o755)
    r = run(str(home / "x.nbmove"))
    assert r.returncode == 1 and "This PC runs Darwin, but Next Box needs Linux" in r.stdout
    uname.unlink()
    (state / "nextbox").touch()  # a Next Box is already running here
    r = run(str(home / "x.nbmove"))
    assert r.returncode == 1 and "Next Box is already running on this PC." in r.stdout
    assert f"scripts/move-in.sh {home / 'x.nbmove'} --replace" in r.stdout
    (state / "nextbox").unlink()
    r = run(str(home / "x.nbmove"))
    assert r.returncode == 1 and "Can't find" in r.stdout and "scripts/move-in.sh ~/nextbox-move-" in r.stdout
    assert not (root / ".venv").exists()  # nothing was set up before the checks passed
    # Typed on the old PC by mistake: it says so.
    (home / ".local" / "share" / "nextbox").mkdir(parents=True)
    (home / ".local" / "share" / "nextbox" / "MOVED-AWAY").write_text("{}")
    r = run(str(home / "x.nbmove"))
    assert r.returncode == 1 and "This is the PC Next Box moved away from. Run this on the new PC." in r.stdout


def test_move_in_sh_names_the_program_holding_the_port(tmp_path):
    import socket
    root = _copy_scripts(tmp_path / "nb")
    home = tmp_path / "home"
    home.mkdir()
    fake = tmp_path / "bin"
    _fake_bin(fake, tmp_path / "log")
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
        r = subprocess.run(["bash", str(root / "scripts" / "move-in.sh"), str(home / "x.nbmove")], text=True,
                           input="", capture_output=True, timeout=60,
                           env={"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home), "USER": "mike",
                                "NEXTBOX_PORT": str(port)})
    finally:
        holder.close()
    assert r.returncode == 1
    assert f"Port {port}, which Next Box uses, is taken by ticket (process 4242)." in r.stdout
    assert "systemctl --user disable --now ticket.service" in r.stdout


# ------------------------------------------------------------------ docs
def test_the_docs_explain_the_move():
    readme = (PROJECT_DIR / "README.md").read_text()
    section = readme.split("## Move Next Box to another PC")[1].split("\n## ")[0]
    for words in ("scripts/move-out.sh", "scripts/move-in.sh", "--undo", "USB stick", "scp",
                  "git clone --depth 1 -b claude/ticket-todoist-printer-nldz8a "
                  "https://github.com/brazzybraz2102/trivia-quiz.git ~/nb-src",
                  "mv ~/nb-src/next-box ~/next-box", "scripts/go-online.sh", "nextbox_print_today_url",
                  "scripts/install-tray.sh", "After a week", "passphrase", "update.sh"):
        assert words in section, words
    spec = (PROJECT_DIR / "SPEC.md").read_text()
    assert "## 33. Move to another PC" in spec and "RestartPreventExitStatus=78" in spec
    assert "AES-256-GCM" in spec and "scrypt" in spec
    assert "nextbox move pack" in (PROJECT_DIR / "DATABASE.md").read_text()
