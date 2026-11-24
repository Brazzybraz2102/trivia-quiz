"""The cloud container and the platform files: Dockerfile, .dockerignore, render.yaml, scripts/cloud/start.sh,
and the settings they rely on (NEXTBOX_HOSTED, PORT, NEXTBOX_SECRET_KEY as any text, NEXTBOX_FORWARDED_ALLOW_IPS)."""
from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from nextbox import cli, config, vault
from nextbox.config import PROJECT_DIR, Settings, load_settings

START = PROJECT_DIR / "scripts" / "cloud" / "start.sh"
NO_ENV_FILE = Path("/nonexistent/.env")  # load_settings reads the process environment only


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("NEXTBOX_PORT", "PORT", "NEXTBOX_HOSTED", "NEXTBOX_DATA_DIR", "NEXTBOX_FORWARDED_ALLOW_IPS",
                 "NEXTBOX_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)


# --- settings ------------------------------------------------------------------------------------------

def test_the_hosts_port_is_used_when_nextbox_port_is_not_set(clean_env, monkeypatch):
    assert load_settings(NO_ENV_FILE).port == 8787
    monkeypatch.setenv("PORT", "10000")  # what Render hands over
    assert load_settings(NO_ENV_FILE).port == 10000
    monkeypatch.setenv("NEXTBOX_PORT", "9000")  # Next Box's own setting wins
    assert load_settings(NO_ENV_FILE).port == 9000
    monkeypatch.setenv("NEXTBOX_PORT", "")  # set but empty counts as not set
    assert load_settings(NO_ENV_FILE).port == 10000


def test_hosted_comes_from_nextbox_hosted_and_shows_in_redacted(clean_env, monkeypatch):
    assert load_settings(NO_ENV_FILE).hosted is False
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("NEXTBOX_HOSTED", value)
        assert load_settings(NO_ENV_FILE).hosted is True
    monkeypatch.setenv("NEXTBOX_HOSTED", "0")
    assert load_settings(NO_ENV_FILE).hosted is False
    shown = Settings(server_key="shh", hosted=True).redacted()
    assert shown["hosted"] is True and shown["server_key"] == "set" and "shh" not in str(shown)


def test_forwarded_allow_ips_default_and_override(clean_env, monkeypatch):
    assert load_settings(NO_ENV_FILE).forwarded_allow_ips == "127.0.0.1,::1"
    monkeypatch.setenv("NEXTBOX_FORWARDED_ALLOW_IPS", " * ")
    assert load_settings(NO_ENV_FILE).forwarded_allow_ips == "*"
    monkeypatch.setenv("NEXTBOX_FORWARDED_ALLOW_IPS", "")
    assert load_settings(NO_ENV_FILE).forwarded_allow_ips == "127.0.0.1,::1"


def test_hosted_data_dir_falls_back_to_the_temp_dir_when_home_is_not_writable(clean_env, monkeypatch, tmp_path):
    (tmp_path / "not-a-folder").write_text("")
    monkeypatch.setenv("HOME", str(tmp_path / "not-a-folder"))  # ~/.local/share/nextbox can't be made
    assert load_settings(NO_ENV_FILE).data_dir == tmp_path / "not-a-folder" / ".local" / "share" / "nextbox"  # as before
    monkeypatch.setenv("NEXTBOX_HOSTED", "1")
    assert load_settings(NO_ENV_FILE).data_dir == Path(tempfile.gettempdir()) / "nextbox"
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(tmp_path / "chosen"))  # an explicit folder is always used
    assert load_settings(NO_ENV_FILE).data_dir == tmp_path / "chosen"


def test_hosted_data_dir_stays_in_home_when_it_is_writable(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NEXTBOX_HOSTED", "1")
    assert load_settings(NO_ENV_FILE).data_dir == tmp_path / ".local" / "share" / "nextbox"


def test_data_dir_without_any_home_folder_goes_under_the_temp_dir(clean_env, monkeypatch):
    """A container running as a bare user id has no home: Path.home() raises instead of answering."""
    def no_home(*_):
        raise RuntimeError("Could not determine home directory.")
    monkeypatch.setattr(config.Path, "home", classmethod(no_home))
    assert load_settings(NO_ENV_FILE).data_dir == Path(tempfile.gettempdir()) / "nextbox"
    monkeypatch.setenv("NEXTBOX_HOSTED", "1")
    assert load_settings(NO_ENV_FILE).data_dir == Path(tempfile.gettempdir()) / "nextbox"


# --- the secret key as an environment secret ----------------------------------------------------------

def test_a_fernet_key_is_used_as_it_is_and_other_text_is_derived_the_same_way_every_time():
    fernet_key = Fernet.generate_key()
    assert vault.key_from(fernet_key.decode()) == fernet_key
    assert vault.key_from(fernet_key.decode() + "\n") == fernet_key  # a trailing newline from a copied file
    text = "any-32-plus-character-text-from-the-host-xyz"
    derived = vault.key_from(text)
    assert derived == vault.key_from(text) and derived != text.encode()
    Fernet(derived)  # a valid key
    assert vault.key_from("another-32-plus-character-text-abcdef") != derived
    assert vault.key_from("x" * 32) == vault.key_from(" " + "x" * 32 + " ")  # surrounding spaces don't count


def test_a_short_secret_is_refused_with_one_plain_sentence(monkeypatch, tmp_path):
    with pytest.raises(ValueError) as exc:
        vault.key_from("x" * 31)
    assert str(exc.value) == vault.SHORT_KEY and "32 characters" in vault.SHORT_KEY
    monkeypatch.setenv("NEXTBOX_SECRET_KEY", "x" * 31)
    with pytest.raises(SystemExit) as stop:
        vault.Vault(tmp_path)
    assert str(stop.value) == vault.SHORT_KEY
    assert not (tmp_path / "secret.key").exists()


def test_vaults_from_the_same_text_open_each_others_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXTBOX_SECRET_KEY", "the-same-long-random-text-set-at-the-host-1234")
    sealed = vault.Vault(tmp_path / "a").seal({"token": "t1"})
    assert vault.Vault(tmp_path / "b").open(sealed) == {"token": "t1"}
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()  # nothing written: no secret.key
    monkeypatch.setenv("NEXTBOX_SECRET_KEY", "a-different-long-random-text-set-at-the-host-1")
    with pytest.raises(ValueError):
        vault.Vault(tmp_path / "c").open(sealed)


def test_the_home_pcs_secret_key_content_opens_the_home_pcs_secrets(monkeypatch, tmp_path):
    """Moving the data to a host: NEXTBOX_SECRET_KEY = the content of secret.key keeps every saved connection."""
    monkeypatch.delenv("NEXTBOX_SECRET_KEY", raising=False)
    home = vault.Vault(tmp_path)  # writes secret.key, as on the desktop
    sealed = home.seal({"password": "p"})
    monkeypatch.setenv("NEXTBOX_SECRET_KEY", (tmp_path / "secret.key").read_text())
    assert vault.Vault(tmp_path / "elsewhere").open(sealed) == {"password": "p"}


# --- serve ---------------------------------------------------------------------------------------------

def test_serve_passes_the_forwarded_ips_and_the_hosts_port_to_uvicorn(clean_env, monkeypatch, tmp_path):
    import uvicorn

    from nextbox import server
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXTBOX_KEY", "k")
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setattr(server, "build_default_app", lambda: "the app")
    runs: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: runs.append({"app": app, **kw}))
    assert cli.main(["serve"]) == 0
    assert runs[-1]["app"] == "the app" and runs[-1]["port"] == 10000 and runs[-1]["host"] == "0.0.0.0"
    assert runs[-1]["proxy_headers"] is True and runs[-1]["forwarded_allow_ips"] == "127.0.0.1,::1"
    assert runs[-1]["access_log"] is False
    monkeypatch.setenv("NEXTBOX_FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setenv("NEXTBOX_PORT", "8787")
    assert cli.main(["serve"]) == 0
    assert runs[-1]["forwarded_allow_ips"] == "*" and runs[-1]["port"] == 8787


# --- scripts/cloud/start.sh ----------------------------------------------------------------------------

def _stub_python(folder: Path, log: Path) -> Path:
    """A `python` that only writes what it was asked to run, and the port settings it saw."""
    folder.mkdir(parents=True, exist_ok=True)
    stub = folder / "python"
    stub.write_text(f"""#!/usr/bin/env bash
printf 'argv: %s\\n' "$*" > "{log}"
echo "NEXTBOX_HOST=${{NEXTBOX_HOST:-unset}}" >> "{log}"
echo "NEXTBOX_PORT=${{NEXTBOX_PORT:-unset}}" >> "{log}"
""")
    stub.chmod(0o755)
    return stub


def _start(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, str]:
    log = tmp_path / "python.log"
    _stub_python(tmp_path / "bin", log)
    base = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8"}
    r = subprocess.run(["bash", str(START)], env={**base, **env}, capture_output=True, text=True, timeout=30)
    return r, log.read_text() if log.exists() else ""


def test_start_sh_is_valid_bash_executable_and_never_shows_env():
    assert subprocess.run(["bash", "-n", str(START)], capture_output=True).returncode == 0
    assert START.stat().st_mode & stat.S_IXUSR
    text = START.read_text()
    assert "cat .env" not in text and "source .env" not in text and ". .env" not in text
    assert "exec python -m nextbox serve" in text


def test_start_sh_maps_port_and_starts_the_server_when_everything_is_set(tmp_path):
    r, log = _start(tmp_path, NEXTBOX_HOSTED="1", PORT="10000", DATABASE_URL="postgresql://u:p@h/db",
                    NEXTBOX_KEY="k", NEXTBOX_SECRET_KEY="s" * 32)
    assert r.returncode == 0, r.stderr
    assert "argv: -m nextbox serve" in log and "NEXTBOX_PORT=10000" in log and "NEXTBOX_HOST=0.0.0.0" in log
    # Next Box's own port setting wins over the host's.
    r, log = _start(tmp_path, NEXTBOX_HOSTED="1", PORT="10000", NEXTBOX_PORT="8787", DATABASE_URL="x",
                    NEXTBOX_KEY="k", NEXTBOX_SECRET_KEY="s" * 32)
    assert r.returncode == 0 and "NEXTBOX_PORT=8787" in log
    # No PORT at all: Next Box's default applies (nothing exported).
    r, log = _start(tmp_path, NEXTBOX_HOSTED="1", DATABASE_URL="x", NEXTBOX_KEY="k", NEXTBOX_SECRET_KEY="s" * 32)
    assert r.returncode == 0 and "NEXTBOX_PORT=unset" in log


def test_start_sh_refuses_in_hosted_mode_without_the_three_settings_and_never_prints_a_value(tmp_path):
    r, log = _start(tmp_path, NEXTBOX_HOSTED="1")
    assert r.returncode == 1 and log == ""  # python was never run
    lines = [line for line in r.stderr.splitlines() if line.strip()]
    assert [line.split(" ")[0] for line in lines[:3]] == ["DATABASE_URL", "NEXTBOX_KEY", "NEXTBOX_SECRET_KEY"]
    assert all(line.endswith(".") for line in lines) and "Render" in lines[-1]
    assert r.stdout == ""
    # One missing: one line for it, and the values of the other two never appear.
    r, log = _start(tmp_path, NEXTBOX_HOSTED="true", DATABASE_URL="postgresql://nextbox:supersecretpw@h/db",
                    NEXTBOX_KEY="keyvalue123")
    assert r.returncode == 1 and log == ""
    assert "NEXTBOX_SECRET_KEY is not set" in r.stderr
    assert "DATABASE_URL is not set" not in r.stderr and "NEXTBOX_KEY is not set" not in r.stderr
    assert "supersecretpw" not in r.stderr + r.stdout and "keyvalue123" not in r.stderr + r.stdout


def test_start_sh_leaves_the_checks_to_next_box_when_not_hosted(tmp_path):
    r, log = _start(tmp_path)  # a plain container at home: the server's own NEXTBOX_KEY check applies
    assert r.returncode == 0 and "argv: -m nextbox serve" in log
    r, log = _start(tmp_path, NEXTBOX_HOSTED="0", PORT="10000")  # the host's port counts there too
    assert r.returncode == 0 and "argv: -m nextbox serve" in log and "NEXTBOX_PORT=10000" in log


# --- render.yaml ---------------------------------------------------------------------------------------

def test_render_yaml_parses_and_names_every_setting_the_hosted_server_needs():
    yaml = pytest.importorskip("yaml")
    blueprint = yaml.safe_load((PROJECT_DIR / "render.yaml").read_text())
    assert set(blueprint) == {"services", "databases"}
    [service] = blueprint["services"]
    assert service["type"] == "web" and service["runtime"] == "docker" and service["name"] == "nextbox"
    assert service["rootDir"] == "next-box" and service["dockerfilePath"] == "./Dockerfile"
    assert service["healthCheckPath"] == "/health" and service["autoDeploy"] is True
    assert service["branch"] == "claude/ticket-todoist-printer-nldz8a"
    assert service["plan"] in ("starter", "0.5c-512mb")  # the cheapest plan that never sleeps
    assert service["domains"] == ["nextbox.braznarkel.com"]
    env = {e["key"]: e for e in service["envVars"]}
    assert set(env) == {"DATABASE_URL", "NEXTBOX_KEY", "NEXTBOX_SECRET_KEY", "NEXTBOX_HOSTED",
                        "NEXTBOX_FORWARDED_ALLOW_IPS", "NEXTBOX_HOSTING", "NEXTBOX_PUBLIC_URL", "NEXTBOX_OPERATOR",
                        "NEXTBOX_CONTACT", "TZ", "ANTHROPIC_API_KEY", "NEXTBOX_FIRST_ADMIN"}
    [database] = blueprint["databases"]
    assert env["DATABASE_URL"]["fromDatabase"] == {"name": database["name"], "property": "connectionString"}
    assert env["NEXTBOX_KEY"] == {"key": "NEXTBOX_KEY", "generateValue": True}
    assert env["NEXTBOX_SECRET_KEY"] == {"key": "NEXTBOX_SECRET_KEY", "generateValue": True}
    assert env["NEXTBOX_HOSTED"]["value"] == "1" and env["NEXTBOX_FORWARDED_ALLOW_IPS"]["value"] == "*"
    assert env["NEXTBOX_HOSTING"]["value"] == "Render"
    assert env["NEXTBOX_PUBLIC_URL"]["value"] == "https://nextbox.braznarkel.com"
    assert env["NEXTBOX_OPERATOR"]["value"] == "Braznarkel"  # what server.py falls back to anyway
    assert env["TZ"]["value"] == "America/New_York"
    # Asked for in the Dashboard, never in the file: the two secrets, and Mike's contact address (no made-up
    # address in a public repository; the pages say "ask the person who invited you" while it is empty).
    for secret in ("ANTHROPIC_API_KEY", "NEXTBOX_FIRST_ADMIN", "NEXTBOX_CONTACT"):
        assert env[secret] == {"key": secret, "sync": False}
    assert database["plan"] == "basic-256mb" and database["databaseName"] == "nextbox" and database["user"] == "nextbox"
    # Every value in the file is a string (YAML would otherwise turn "1" into a number).
    for e in service["envVars"]:
        if "value" in e:
            assert isinstance(e["value"], str)
    text = (PROJECT_DIR / "render.yaml").read_text()
    assert "https://render.com/docs/blueprint-spec" in text  # the spec it was checked against
    assert "sleeps" in text or "spins down" in text  # the free plan's limits are named
    assert "Blueprint Path" in text and "next-box/render.yaml" in text  # the file is not at the repository root
    assert "@" not in "".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


# --- Dockerfile and .dockerignore ------------------------------------------------------------------------

def test_dockerfile_pins_with_constraints_runs_as_non_root_and_starts_with_start_sh():
    text = (PROJECT_DIR / "Dockerfile").read_text()
    assert text.splitlines()[0].startswith("#")
    assert re.search(r"^FROM python:3\.12-slim", text, re.M)
    pip_lines = [line for line in text.splitlines() if "pip install" in line]
    assert pip_lines and all("-c constraints.txt" in line for line in pip_lines)
    assert "[postgres,heic]" in pip_lines[0]
    assert "fonts-dejavu-core" in text
    assert re.search(r"^USER nextbox", text, re.M)
    assert text.index("USER nextbox") > text.index("pip install")  # packages installed first, as root
    assert re.search(r'^CMD \["scripts/cloud/start.sh"\]', text, re.M)
    assert "NEXTBOX_HOSTED=1" in text
    assert "EXPOSE 10000" in text
    assert ".env" not in text.replace(".env.", "").replace(".env ", " ") or "keeps .env out" in text


def test_dockerignore_keeps_secrets_the_venv_and_tests_out_of_the_image():
    lines = [line.strip() for line in (PROJECT_DIR / ".dockerignore").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    for needed in (".env", ".env.*", ".venv", "tests", "__pycache__", ".git", "scripts/*", "!scripts/cloud"):
        assert needed in lines
    assert "web" not in lines and "nextbox" not in lines and "constraints.txt" not in lines


def test_the_container_files_do_not_hold_a_secret_value():
    """Only names of settings, never values: nothing to leak when the repo is public."""
    for name in ("Dockerfile", "render.yaml", "scripts/cloud/start.sh", ".dockerignore"):
        text = (PROJECT_DIR / name).read_text()
        assert not re.search(r"sk-ant-[A-Za-z0-9_-]{8,}", text), name
        assert not re.search(r"postgres(ql)?://\w+:\w+@", text), name
        assert "token" not in text.lower() or name == "render.yaml"  # render.yaml explains what NEXTBOX_KEY is


def test_version_is_not_bumped_by_the_container_work():
    assert config.app_version() == "2.1.3"
