"""Settings come from nextbox/.env (or the process environment). Secrets live nowhere else.

Hosted mode (NEXTBOX_HOSTED=1): this Next Box runs at a hosting company (Render, see render.yaml). Every
request comes from the internet, there is no printer here, no home network and no systemd. The host hands
over its port as PORT, and the data folder is scratch space (the database is PostgreSQL: DATABASE_URL).
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent

DEFAULT_FORWARDED_ALLOW_IPS = "127.0.0.1,::1"  # uvicorn: whose X-Forwarded-* headers to believe (cli.py serve)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    todoist_token: str = ""
    printer_ip: str = ""
    server_key: str = ""
    anthropic_api_key: str = ""
    dry_run: bool = False
    data_dir: Path = Path.home() / ".local" / "share" / "nextbox"
    label: str = "62"
    printer_model: str = "QL-1110NWB"
    vision_model: str = "claude-sonnet-5-5"
    coach_model: str = "claude-opus-5-5"  # "Break it down" and the weekly look-back (nextbox/coach.py)
    host: str = "0.0.0.0"
    port: int = 8787
    retention_days: int = 90  # tickets and scans older than this are deleted at startup
    credit: str = "Created by Mike"  # the small mark at the bottom of every page
    # NEXTBOX_HOSTED=1: this Next Box runs at a hosting company. Every request comes from the internet, and
    # there's no printer, home network or systemd there (SPEC §35).
    hosted: bool = False
    forwarded_allow_ips: str = DEFAULT_FORWARDED_ALLOW_IPS  # NEXTBOX_FORWARDED_ALLOW_IPS ("*" at a host)

    def redacted(self) -> dict:
        """Safe to print: never includes secret values."""
        return {
            "todoist_token": "set" if self.todoist_token else "MISSING",
            "printer_ip": self.printer_ip or "MISSING",
            "server_key": "set" if self.server_key else "MISSING",
            "anthropic_api_key": "set" if self.anthropic_api_key else "MISSING",
            "dry_run": self.dry_run,
            "data_dir": str(self.data_dir),
            "label": self.label,
            "vision_model": self.vision_model,
            "coach_model": self.coach_model,
            "hosted": self.hosted,
        }


def app_version() -> str:
    """This copy's version, for `nextbox --version` and doctor. pyproject.toml first: the installed
    metadata can lag behind an update."""
    try:
        import tomllib
        with open(PROJECT_DIR / "pyproject.toml", "rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except Exception:
        try:
            from importlib.metadata import version
            return version("nextbox")
        except Exception:
            return "dev"


def _writable(folder: Path) -> bool:
    """Can Next Box make this folder (if needed) and write in it?"""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        return os.access(folder, os.W_OK)
    except OSError:
        return False


def _data_dir(explicit: str | None, hosted: bool) -> Path:
    """NEXTBOX_DATA_DIR when set. Otherwise ~/.local/share/nextbox; at a hosting company, where the home
    folder may be missing or read-only, a folder under the temp dir instead (the data folder only caches
    ticket images there: everything that matters is in the PostgreSQL database)."""
    if explicit:
        return Path(explicit).expanduser()
    try:
        default = Path.home() / ".local" / "share" / "nextbox"
    except RuntimeError:  # no home folder at all (a container running as a bare user id)
        default = None
    if default is not None and (not hosted or _writable(default)):
        return default
    return Path(tempfile.gettempdir()) / "nextbox"


def load_settings(env_file: Path | None = None) -> Settings:
    env_file = env_file or Path(os.environ.get("NEXTBOX_ENV_FILE", PROJECT_DIR / ".env"))
    load_dotenv(env_file, override=False)
    e = os.environ.get
    hosted = _truthy(e("NEXTBOX_HOSTED"))
    return Settings(
        todoist_token=e("TODOIST_TOKEN", ""),
        printer_ip=e("PRINTER_IP", ""),
        server_key=e("NEXTBOX_KEY", ""),
        anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
        dry_run=_truthy(e("NEXTBOX_DRY_RUN")),
        data_dir=_data_dir(e("NEXTBOX_DATA_DIR"), hosted),
        label=e("NEXTBOX_LABEL", "62"),
        vision_model=e("NEXTBOX_VISION_MODEL", "claude-sonnet-5-5"),
        coach_model=e("NEXTBOX_COACH_MODEL", "claude-opus-5-5"),
        host=e("NEXTBOX_HOST", "0.0.0.0"),
        # PORT is how a hosting company says which port to listen on (Render: 10000). NEXTBOX_PORT wins.
        port=int(e("NEXTBOX_PORT") or e("PORT") or "8787"),
        retention_days=int(e("NEXTBOX_RETENTION_DAYS", "90")),
        credit=e("NEXTBOX_CREDIT", "Created by Mike")[:80],
        hosted=hosted,
        forwarded_allow_ips=(e("NEXTBOX_FORWARDED_ALLOW_IPS") or "").strip() or DEFAULT_FORWARDED_ALLOW_IPS,
    )
