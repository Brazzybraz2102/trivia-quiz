"""Moving the data to a hosting company's PostgreSQL (DEPLOY.md step 3): `nextbox db copy-to` (--check, sslmode, the
refusal of a database with data, pictures, counts, never the address), scripts/cloud-migrate.sh (valid bash, a stub
run that never shows the address, the next steps) and the pins DEPLOY.md and README must keep.

On PostgreSQL (DATABASE_URL set) the target is the nbmove3 database, emptied first; on SQLite it's a fresh file."""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, insert, select, text

from nextbox import cli, db, dbtools
from nextbox.auth import Accounts

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_DIR / "scripts" / "cloud-migrate.sh"
PG = os.environ.get("DATABASE_URL", "")
PASSWORD = "SENTINEL-db-password-7"
AWAY = f"postgresql://nextbox:{PASSWORD}@dpg-abc123.oregon-postgres.render.com/nextbox_x1"
PHOTO, DRAWING, PNG = b"\xff\xd8\xff\xe0 a jpeg" * 300, b"\x89PNG 1-bit drawing" * 100, b"\x89PNG a ticket" * 50


def target_url(tmp_path: Path) -> str:
    """Where the copy goes: nbmove3 on PostgreSQL (its tables dropped first), else a new SQLite file."""
    if PG:
        url = PG.rpartition("/")[0] + "/nbmove3"
        db.metadata.drop_all(db.database(url))
        db.forget_engine(url)
        return url
    return f"sqlite:///{tmp_path / 'cloud.db'}"


def settle(url: str) -> None:
    """After a test: drop what it made in the target (PostgreSQL), so the next one starts empty."""
    if PG:
        db.metadata.drop_all(db.database(url))
    db.forget_engine(url)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A Next Box at home with people's data in every kind of table, pictures included. On PostgreSQL that's the
    test database (DATABASE_URL), as it would be on a PC after setup-postgres.sh."""
    data = tmp_path / "home"
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(data))
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(tmp_path / "none.env"))
    accounts = Accounts(data)
    accounts.set_password("mike", "mike password", create=True)
    accounts.create("sam", "sam password")
    other = accounts.create_household("Lee's place")
    accounts.create("lee", "lee password", role="admin", household_id=other)
    household = accounts.household_of("mike")
    now = int(time.time())
    with db.database(data).begin() as conn:
        conn.execute(insert(db.tasks).values(owner="mike", household_id=household, content="Water plants",
                                             tags=[], steps=[], created=now))
        conn.execute(insert(db.journal).values(id="j1", owner="mike", household_id=household, day="2026-11-20",
                                               created=now, updated=now, notes="Dentist at 3", photo=PHOTO))
        conn.execute(insert(db.friend_notes).values(id="n1", sender="sam", recipient="mike", kind="draw",
                                                    image=DRAWING, created=now))
        conn.execute(insert(db.tickets).values(id="t1", household_id=household, by="mike",
                                               created_at="2026-11-20T08:00:00", record={"type": "today"}, png=PNG))
        conn.execute(insert(db.events).values(ts=1.0, user="mike", kind="print", action="today", ok=True))
        conn.execute(insert(db.server_settings).values(key="_upkeep:orphans-v1", value=True))
    return data


def rows(url: str, table) -> list[dict]:
    with db.database(url).connect() as conn:
        return [dict(r) for r in conn.execute(select(table)).mappings()]


# --- the address ------------------------------------------------------------------------------------------------
def test_cloud_url_adds_sslmode_only_for_a_server_on_another_computer():
    assert dbtools.cloud_url(AWAY) == AWAY + "?sslmode=require"
    assert dbtools.cloud_url("postgres://u:p@dpg-x.frankfurt-postgres.render.com/db") == \
        "postgresql://u:p@dpg-x.frankfurt-postgres.render.com/db?sslmode=require"
    assert dbtools.cloud_url(AWAY + "?sslmode=require") == AWAY + "?sslmode=require"  # as Render gives it
    assert dbtools.cloud_url(AWAY + "?sslmode=verify-full") == AWAY + "?sslmode=verify-full"  # a stricter choice stays
    assert dbtools.cloud_url(" " + AWAY + "\n") == AWAY + "?sslmode=require"
    for local in ("postgresql://nextbox:pw@127.0.0.1:5432/nextbox", "postgresql://nextbox:pw@localhost/nextbox"):
        assert dbtools.cloud_url(local) == local  # setup-postgres.sh's own server: as before
    assert dbtools.cloud_url("sqlite:////tmp/x.db") == "sqlite:////tmp/x.db"
    for bad in ("", "not an address", "https://nextbox.braznarkel.com"):
        with pytest.raises(ValueError, match="postgresql://"):
            dbtools.cloud_url(bad)
    with pytest.raises(ValueError) as err:
        dbtools.cloud_url("mysql://u:SECRET-pw@h/db")
    assert "SECRET-pw" not in str(err.value) and "PostgreSQL" in str(err.value)


def test_mask_secret_hides_the_address_and_the_password():
    url = AWAY + "?sslmode=require"
    said = f"connection to {url} failed: FATAL password {PASSWORD} rejected (Background on this error at: https://x)"
    masked = dbtools.mask_secret(said, url)
    assert PASSWORD not in masked and "render.com" not in masked and "Background" not in masked
    assert masked == "connection to <database address> failed: FATAL password <password> rejected"
    assert dbtools.mask_secret("see postgresql://a:b@c/d now") == "see <database address> now"
    assert dbtools.mask_secret(RuntimeError("plain words"), url) == "plain words"


# --- --check and the copy ---------------------------------------------------------------------------------------
def test_check_only_looks_and_says_what_it_found(home, tmp_path, capsys):
    target = target_url(tmp_path)
    try:
        assert cli.main(["db", "copy-to", "--check", target]) == 0
        out = capsys.readouterr().out
        assert "Reachable:  yes" in out and "Empty:      yes" in out
        if PG:
            assert "Encrypted:  yes (SSL)" in out and "PostgreSQL" in out
            assert "nbtest" not in out  # never the address or anything from it
        else:
            assert "Encrypted:  not needed" in out and "SQLite" in out
        plain = create_engine(db._engine_url(target))
        assert inspect(plain).get_table_names() == []  # --check created nothing
        plain.dispose()

        # A Next Box that started there once: its own bookkeeping rows don't make it "not empty"...
        with db.database(target).begin() as conn:
            conn.execute(insert(db.server_settings).values(key="_upkeep:orphans-v1", value=True))
            conn.execute(insert(db.events).values(ts=2.0, user="?", kind="auth", action="login", ok=False))
        assert cli.main(["db", "copy-to", "--check", target]) == 0
        out = capsys.readouterr().out
        assert "Empty:      yes. Next Box started there once and left 2 bookkeeping rows" in out
        # ...but an account does (NEXTBOX_FIRST_ADMIN, or someone used it): plain words, exit 1, nothing copied.
        Accounts(target).set_password("boss", "boss password", create=True)
        assert cli.main(["db", "copy-to", "--check", target]) == 1
        out = capsys.readouterr().out
        assert "Empty:      NO. It already has 1 account and 1 household. Copy only into an empty database" in out
        assert rows(target, db.tasks) == []
    finally:
        settle(target)


def test_copy_moves_every_table_with_its_pictures_and_reports_counts(home, tmp_path, capsys, monkeypatch):
    target = target_url(tmp_path)
    try:
        with db.database(target).begin() as conn:  # started once at the hosting company: upkeep's flags, a bot's try
            conn.execute(insert(db.server_settings).values(key="_upkeep:dates-v1", value=True))
            conn.execute(insert(db.events).values(ts=2.0, user="?", kind="auth", action="login", ok=False))
        monkeypatch.setattr("sys.stdin", io.StringIO(target + "\n"))  # "-": the address never on a command line
        assert cli.main(["db", "copy-to", "-"]) == 0
        out, err = capsys.readouterr()
        assert err == ""
        assert "Copied, table by table:" in out and "Checked: the new database has the same rows" in out
        assert re.search(r"users\s+3\n", out) and re.search(r"journal\s+1\n", out)
        assert re.search(r"friend_notes\s+1\n", out) and re.search(r"tickets\s+1\n", out)
        assert re.search(r"events\s+1\n", out) and re.search(r"server_settings\s+\d+\n", out)
        assert "(nothing in: " in out and "scans" in out and "total" in out
        assert "nbtest" not in out and "nbmove3" not in out and "cloud.db" not in out  # never the address

        assert {u["username"] for u in Accounts(target).all_users()} == {"mike", "sam", "lee"}
        assert rows(target, db.journal)[0]["photo"] == PHOTO
        assert rows(target, db.friend_notes)[0]["image"] == DRAWING
        assert rows(target, db.tickets)[0]["png"] == PNG
        keys = {r["key"] for r in rows(target, db.server_settings)}
        assert "_upkeep:orphans-v1" in keys and "_upkeep:dates-v1" not in keys  # the home rows, not the hosted copy's
        assert [r["user"] for r in rows(target, db.events)] == ["mike"]  # the hosted copy's own rows are gone
        if PG:  # the id counters moved past the copied ids
            with db.database(target).begin() as conn:
                conn.execute(insert(db.tasks).values(owner="sam", household_id="h", content="New", tags=[],
                                                     steps=[], created=1))
            assert sorted(r["id"] for r in rows(target, db.tasks)) == [1, 2]

        # Again: the database has people's data now, so it's refused in plain words and nothing changes.
        monkeypatch.setattr("sys.stdin", io.StringIO(target))
        assert cli.main(["db", "copy-to", "-"]) == 1
        out, err = capsys.readouterr()
        assert out == ""
        tasks = "2 My list tasks" if PG else "1 My list task"  # (PostgreSQL: the counter test above added one)
        assert (f"error: That database isn't empty: it already has 3 accounts, 2 households, {tasks}, "
                "1 journal page, 1 ticket, 1 friends' note and 5 tags, so nothing was copied. Copy only into an "
                "empty database.") in err
        assert "nbtest" not in err and "cloud.db" not in err
        assert len(rows(target, db.tasks)) == (2 if PG else 1)
    finally:
        settle(target)


def test_a_database_that_doesnt_answer_is_said_without_the_address(home, capsys, monkeypatch):
    nobody = f"postgresql://nextbox:{PASSWORD}@127.0.0.1:1/nextbox"  # nothing listens on port 1
    assert cli.main(["db", "copy-to", "--check", nobody]) == 1
    out = capsys.readouterr().out
    assert out.startswith("Reachable:  no.") and "Nothing was copied." in out
    assert PASSWORD not in out and "Background on this error" not in out
    monkeypatch.setattr("sys.stdin", io.StringIO(nobody))
    assert cli.main(["db", "copy-to", "-"]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "error: couldn't copy into that database." in err and "Nothing was copied" in err
    assert PASSWORD not in err and "Background on this error" not in err
    assert cli.main(["db", "copy-to", "--check", "not-an-address"]) == 1
    assert "error: That doesn't look like a database address" in capsys.readouterr().err


def test_copy_database_still_takes_the_sqlite_file_itself(tmp_path, monkeypatch):
    """scripts/setup-postgres.sh and `move pack` call copy_database with two addresses, as before."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    src, dst = f"sqlite:///{tmp_path / 'a.db'}", f"sqlite:///{tmp_path / 'b.db'}"
    Accounts(src).set_password("mike", "mike password", create=True)
    counts = dbtools.copy_database(src, dst)
    assert counts["users"] == 1 and counts["households"] == 1 and counts["scans"] == 0
    with pytest.raises(RuntimeError, match="isn't empty: it already has 1 account and 1 household"):
        dbtools.copy_database(src, dst)


# --- scripts/cloud-migrate.sh ---------------------------------------------------------------------------------
def test_the_script_is_valid_bash_and_never_shows_env():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0
    assert os.access(SCRIPT, os.X_OK)
    text_ = SCRIPT.read_text()
    assert "cat .env" not in text_ and "source .env" not in text_ and ". .env" not in text_
    assert "read -rs" in text_  # the address comes through a hidden prompt...
    assert not any("$URL" in line or "$PASTED" in line  # ...and no line that shows anything holds it
                   for line in text_.splitlines() if line.lstrip().startswith(("echo", "printf")))


def _home_pc(tmp_path: Path):
    """The home server for cloud-migrate.sh: the script in a copy of the code, a stand-in .venv/bin/nextbox that logs
    what it's asked and what it's given on stdin (so the test can see the address went that way and nowhere else),
    and a stand-in python that names the data folder."""
    root = tmp_path / "nb"
    (root / "scripts").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts" / "cloud-migrate.sh")
    log = tmp_path / "log"
    (root / ".venv" / "bin" / "nextbox").write_text(f"""#!/usr/bin/env bash
given="$(cat)"
echo "nextbox $* <<< $given" >> "{log}"
if [[ "$*" == *--check* ]]; then
  if [[ "${{CHECK_EXIT:-0}}" != 0 ]]; then echo "Reachable:  yes (PostgreSQL 16.1)"; echo "Empty:      NO. It already has 1 account."; exit 1; fi
  echo "Reachable:  yes (PostgreSQL 16.1)"; echo "Encrypted:  yes (SSL)"; echo "Empty:      yes."; exit 0
fi
if [[ "${{COPY_EXIT:-0}}" != 0 ]]; then echo "error: the copy didn't check out" >&2; exit 1; fi
echo "Copied, table by table:"; echo "  users 3"; exit 0
""")
    (root / ".venv" / "bin" / "python").write_text(f'#!/usr/bin/env bash\necho "{tmp_path}/data"\n')
    for name in ("nextbox", "python"):
        (root / ".venv" / "bin" / name).chmod(0o755)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secret.key").write_bytes(b"SENTINEL-fernet-key=")
    (tmp_path / "bin").mkdir()  # the tests may run as root; the script refuses root, so stand in as mike
    (tmp_path / "bin" / "id").write_text('#!/usr/bin/env bash\n[[ "${1:-}" == -u ]] && echo 1000 || echo "uid=1000(mike)"\n')
    (tmp_path / "bin" / "id").chmod(0o755)

    def run(*args, pasted: str = AWAY, answer: str = "COPY", **extra):
        log.write_text("")
        env = {"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "HOME": str(tmp_path), "USER": "mike", **extra}
        return subprocess.run(["bash", str(root / "scripts" / "cloud-migrate.sh"), *args], text=True,
                              input=f"{pasted}\n{answer}\n", capture_output=True, env=env, timeout=60)
    return run, log


def test_the_script_checks_then_copies_through_stdin_and_says_the_next_steps(tmp_path):
    run, log = _home_pc(tmp_path)
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    # The address went to nextbox on stdin, twice (the check, then the copy), and nowhere else.
    assert log.read_text() == f"nextbox db copy-to --check - <<< {AWAY}\nnextbox db copy-to - <<< {AWAY}\n"
    shown = r.stdout + r.stderr
    assert PASSWORD not in shown and "oregon-postgres" not in shown and "SENTINEL-fernet-key" not in shown
    assert "Nothing shows while you paste" in r.stdout and "Type COPY to go ahead" in r.stdout
    assert "Done: your data is in the hosted database." in r.stdout
    # The key went into the Blueprint form in step 2; the end only says how to fix it, never `cat` it on screen.
    assert "cloud-migrate.sh --copy secret" in r.stdout and "cat " not in r.stdout
    assert "Never into a chat or a note." in r.stdout
    assert "Manual Deploy → Deploy latest commit" in r.stdout and "DEPLOY.md step 4" in r.stdout
    assert "https://nextbox.braznarkel.com/health" in r.stdout and "DEPLOY.md step 7" in r.stdout

    # The whole line from Render's page or a .env line pasted: the address is found in it.
    r = run(pasted=f"DATABASE_URL={AWAY}?sslmode=require   ")
    assert r.returncode == 0 and f"<<< {AWAY}?sslmode=require\n" in log.read_text()
    r = run(pasted=f"psql '{AWAY}'")
    assert r.returncode == 0 and f"<<< {AWAY}\n" in log.read_text()


def test_the_script_stops_when_the_check_fails_or_nothing_is_pasted_or_not_confirmed(tmp_path):
    run, log = _home_pc(tmp_path)
    r = run(CHECK_EXIT="1")
    assert r.returncode == 1 and "Empty:      NO" in r.stdout and "Nothing was copied. Fix what it says above" in r.stdout
    assert log.read_text().count("\n") == 1 and "copy-to - " not in log.read_text()  # only the check ran
    r = run(pasted="")
    assert r.returncode == 1 and "That wasn't a database address" in r.stdout and log.read_text() == ""
    r = run(pasted="https://nextbox.braznarkel.com/app/")
    assert r.returncode == 1 and "That wasn't a database address" in r.stdout and log.read_text() == ""
    r = run(answer="no")
    assert r.returncode == 1 and r.stdout.rstrip().endswith("Nothing was copied.") and "copy-to - " not in log.read_text()
    r = run(COPY_EXIT="1")
    assert r.returncode == 1 and "The copy didn't finish, so the hosted database is as it was" in r.stdout
    assert PASSWORD not in r.stdout + r.stderr
    r = run("--check")
    assert r.returncode == 0 and "That's the check. To copy everything into it:" in r.stdout
    assert log.read_text() == f"nextbox db copy-to --check - <<< {AWAY}\n"
    r = run("--bogus")
    assert r.returncode == 1 and r.stdout.startswith("Usage:")
    r = run("--copy", "nothing")
    assert r.returncode == 1 and r.stdout.startswith("Usage:")


def test_copy_puts_a_key_on_the_clipboard_and_never_on_screen(tmp_path):
    """--copy secret / --copy anthropic: the value goes to the clipboard tool on stdin and is never printed. With no
    clipboard (ssh), the script names the file to open instead, still without the value."""
    run, log = _home_pc(tmp_path)
    root = tmp_path / "nb"
    (root / ".env").write_text('NEXTBOX_KEY=k\nANTHROPIC_API_KEY="SENTINEL-anthropic-1"\n')
    got = tmp_path / "clipboard"
    (tmp_path / "bin" / "xclip").write_text(f'#!/usr/bin/env bash\ncat > {got}\n')
    (tmp_path / "bin" / "xclip").chmod(0o755)
    r = run("--copy", "secret", DISPLAY=":0")
    assert r.returncode == 0 and "Copied to your clipboard (not shown)" in r.stdout and "NEXTBOX_SECRET_KEY" in r.stdout
    assert got.read_text() == "SENTINEL-fernet-key=" and "SENTINEL" not in r.stdout + r.stderr
    assert log.read_text() == ""  # nothing asked for a database, nothing copied
    r = run("--copy", "anthropic", DISPLAY=":0")
    assert r.returncode == 0 and got.read_text() == "SENTINEL-anthropic-1" and "SENTINEL" not in r.stdout + r.stderr
    # No display (over ssh): the file to open, never the value.
    r = run("--copy", "secret")
    assert r.returncode == 0 and "no clipboard" in r.stdout and f"{tmp_path}/data/secret.key" in r.stdout
    assert "SENTINEL" not in r.stdout + r.stderr
    # A desktop without a clipboard command (Ubuntu's GNOME): the file opens in the text editor, still without the value.
    opened = tmp_path / "opened"
    (tmp_path / "bin" / "xclip").write_text("#!/usr/bin/env bash\nexit 1\n")
    (tmp_path / "bin" / "xdg-open").write_text(f'#!/usr/bin/env bash\necho "$1" > {opened}\n')
    (tmp_path / "bin" / "xdg-open").chmod(0o755)
    r = run("--copy", "secret", DISPLAY=":0")
    assert r.returncode == 0 and "opening in your text editor" in r.stdout and f"{tmp_path}/data/secret.key" in r.stdout
    assert opened.read_text().strip() == f"{tmp_path}/data/secret.key" and "SENTINEL" not in r.stdout + r.stderr
    (root / ".env").write_text("NEXTBOX_KEY=k\n")
    r = run("--copy", "anthropic", DISPLAY=":0")
    assert r.returncode == 1 and "There's no ANTHROPIC_API_KEY here" in r.stdout


# --- the guide --------------------------------------------------------------------------------------------------
SETTINGS_IN_THE_GUIDE = ["NEXTBOX_HOSTED", "PORT", "NEXTBOX_PORT", "NEXTBOX_SECRET_KEY", "NEXTBOX_FIRST_ADMIN",
                         "NEXTBOX_FORWARDED_ALLOW_IPS", "DATABASE_URL", "ANTHROPIC_API_KEY", "NEXTBOX_PUBLIC_URL",
                         "NEXTBOX_HOSTING", "NEXTBOX_OPERATOR", "NEXTBOX_CONTACT", "NEXTBOX_KEY"]


def test_deploy_md_names_every_setting_and_every_step():
    guide = (PROJECT_DIR / "DEPLOY.md").read_text()
    for name in SETTINGS_IN_THE_GUIDE:
        assert re.search(rf"`{name}`|\b{name}=", guide), name
    steps = re.findall(r"^## (\d)\. ", guide, re.M)
    assert steps == [str(n) for n in range(1, 9)]  # eight steps, in order
    for words in ("scripts/cloud-migrate.sh", "External Database URL", "nextbox.braznarkel.com", "render.yaml",
                  "Blueprint", "Custom Domains", "DNS only", "Print on paper", "Save label image", "/health",
                  "Manual Deploy", "Invite", "spins down", "30 days", "secret.key", "NEEDS MIKE", "--forget",
                  "cfargotunnel.com"):
        assert words in guide, words
    assert "unverified" in guide  # what couldn't be checked on the web is marked, not guessed
    assert re.search(r"\(sources?: https://render\.com/docs/", guide)  # click paths cite Render's own pages
    for secret in ("eyJ", "sk-ant-", "postgresql://nextbox:"):  # no real-looking secrets in a guide people paste from
        assert secret not in guide, secret


def test_readme_points_at_the_guide():
    readme = (PROJECT_DIR / "README.md").read_text()
    assert "## Hosted on Render" in readme and "DEPLOY.md" in readme
    online = readme[readme.index("## Online at nextbox.braznarkel.com"):]
    assert "two ways" in online.split("\n## ", 1)[0]
