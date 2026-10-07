"""The database learning kit: a practice database, safe read-only queries on the real one."""
import os
import stat

import pytest

from nextbox import cli, dbtools
from nextbox.auth import Accounts


@pytest.fixture
def practice(tmp_path):
    counts = dbtools.build_practice(tmp_path)
    return tmp_path, counts


def test_practice_database_is_full_of_fake_data(practice):
    data_dir, counts = practice
    assert counts["households"] == 4 and counts["users"] == 9
    assert counts["tickets"] > 50 and counts["events"] > 50 and counts["feedback"] == 4
    assert (dbtools.practice_dir(data_dir) / "nextbox.db").exists()
    # Building it again starts fresh instead of piling up.
    assert dbtools.build_practice(data_dir) == counts


def test_queries_are_read_only_on_the_real_database(tmp_path):
    Accounts(tmp_path).create("sam", "sam password")
    _, rows, _ = dbtools.run_sql(tmp_path, "SELECT username FROM users")
    assert rows == [["sam"]]
    with pytest.raises(PermissionError):
        dbtools.run_sql(tmp_path, "DELETE FROM users", write=True)
    with pytest.raises(Exception, match=r"readonly|read-only|read only"):
        dbtools.run_sql(tmp_path, "DELETE FROM users")
    assert dbtools.run_sql(tmp_path, "SELECT COUNT(*) FROM users")[1] == [[1]]
    # The connection is usable for normal writes afterwards.
    Accounts(tmp_path).create("kim", "kim password")


def test_practice_writes_are_allowed(practice):
    data_dir, _ = practice
    dbtools.run_sql(data_dir, "INSERT INTO households (id, name, created, plan) VALUES ('lab1', 'Lab', 0, 'free')",
                    practice=True, write=True)
    assert dbtools.run_sql(data_dir, "SELECT name FROM households WHERE id = 'lab1'", practice=True)[1] == [["Lab"]]


def test_secrets_are_never_shown(tmp_path):
    a = Accounts(tmp_path)
    a.create("sam", "sam password")
    a.set_connection("sam", {"provider": "todoist", "secret": "sealed-token-bytes"})
    cols, rows, _ = dbtools.run_sql(tmp_path, "SELECT * FROM users")
    shown = dict(zip(cols, rows[0]))
    assert shown["hash"] == shown["salt"] == "•••• (hidden)"
    assert "sealed-token-bytes" not in str(rows) and "todoist" in str(shown["connection"])
    a.create_session("sam")
    assert dbtools.run_sql(tmp_path, "SELECT token_hash FROM sessions")[1] == [["•••• (hidden)"]]


def test_schema_tables_and_format(practice):
    data_dir, _ = practice
    assert "ix_tickets_by_time" in dbtools.schema(data_dir, "tickets", practice=True)
    with pytest.raises(KeyError):
        dbtools.schema(data_dir, "nope")
    names = [n for n, _, _ in dbtools.tables(data_dir, practice=True)]
    assert "feedback_identities" in names
    out = dbtools.format_table(["a", "b"], [[1, None]])
    assert "NULL" in out and out.splitlines()[0].startswith("a")


@pytest.mark.skipif(bool(os.environ.get("DATABASE_URL")), reason="SQLite backups only")
def test_backup_is_a_private_copy(tmp_path):
    Accounts(tmp_path).create("sam", "sam password")
    dest = dbtools.backup(tmp_path)
    assert dest.parent == tmp_path / "backups"
    assert oct(stat.S_IMODE(dest.stat().st_mode)) == "0o600"
    assert dbtools.run_sql(f"sqlite:///{dest}", "SELECT username FROM users")[1] == [["sam"]]


def test_cli_db_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(tmp_path / "none.env"))
    assert cli.main(["db", "demo"]) == 0
    assert cli.main(["db", "sql", "--practice", "SELECT COUNT(*) AS n FROM households"]) == 0
    assert "4" in capsys.readouterr().out
    assert cli.main(["db", "sql", "DELETE FROM users"]) != 0
    out = capsys.readouterr()
    assert "read" in (out.out + out.err).lower()
