import pytest

from nextbox.auth import Accounts, AuthError
from nextbox.cli import main


def test_password_rules(tmp_path):
    a = Accounts(tmp_path)
    with pytest.raises(AuthError):
        a.set_password("mike", "short", create=True)
    with pytest.raises(AuthError):
        a.set_password("Bad Name!", "long enough", create=True)
    a.set_password("mike", "long enough", create=True)
    with pytest.raises(AuthError):
        a.set_password("mike", "long enough", create=True)
    assert a.verify("mike", "long enough") and not a.verify("mike", "wrong one!")
    assert not a.verify("nobody", "long enough")
    assert "long enough" not in (tmp_path / "users.json").read_text()
    assert oct((tmp_path / "users.json").stat().st_mode)[-3:] == "600"


def test_cli_user_add(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "hunter2hunter2")
    assert main(["user", "add", "mike"]) == 0
    assert main(["user", "list"]) == 0
    assert "mike" in capsys.readouterr().out
    assert Accounts(tmp_path).verify("mike", "hunter2hunter2")


def test_cli_roles_and_recovery(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "hunter2hunter2")
    assert main(["user", "add", "mike"]) == 0
    assert main(["user", "add", "sam", "admin"]) == 0
    assert main(["user", "disable", "sam"]) == 0
    assert main(["user", "role", "mike", "user"]) == 1   # last superadmin is protected
    assert main(["user", "list"]) == 0
    out = capsys.readouterr().out
    assert "superadmin" in out and "disabled" in out
