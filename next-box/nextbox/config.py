"""Settings come from nextbox/.env (or the process environment). Secrets live nowhere else."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent


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
    host: str = "0.0.0.0"
    port: int = 8787
    retention_days: int = 90  # tickets and scans older than this are deleted at startup

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
        }


def load_settings(env_file: Path | None = None) -> Settings:
    env_file = env_file or Path(os.environ.get("NEXTBOX_ENV_FILE", PROJECT_DIR / ".env"))
    load_dotenv(env_file, override=False)
    e = os.environ.get
    return Settings(
        todoist_token=e("TODOIST_TOKEN", ""),
        printer_ip=e("PRINTER_IP", ""),
        server_key=e("NEXTBOX_KEY", ""),
        anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
        dry_run=_truthy(e("NEXTBOX_DRY_RUN")),
        data_dir=Path(e("NEXTBOX_DATA_DIR", str(Path.home() / ".local" / "share" / "nextbox"))).expanduser(),
        label=e("NEXTBOX_LABEL", "62"),
        vision_model=e("NEXTBOX_VISION_MODEL", "claude-sonnet-5-5"),
        host=e("NEXTBOX_HOST", "0.0.0.0"),
        port=int(e("NEXTBOX_PORT", "8787")),
        retention_days=int(e("NEXTBOX_RETENTION_DAYS", "90")),
    )
