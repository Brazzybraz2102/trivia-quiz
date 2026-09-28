"""Settings come from ticket/.env (or the process environment). Secrets live nowhere else."""
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
    ticket_key: str = ""
    anthropic_api_key: str = ""
    dry_run: bool = False
    data_dir: Path = Path.home() / ".local" / "share" / "ticket"
    label: str = "62"
    printer_model: str = "QL-1110NWB"
    vision_model: str = "claude-sonnet-5-5"
    host: str = "0.0.0.0"
    port: int = 8787

    def redacted(self) -> dict:
        """Safe to print: never includes secret values."""
        return {
            "todoist_token": "set" if self.todoist_token else "MISSING",
            "printer_ip": self.printer_ip or "MISSING",
            "ticket_key": "set" if self.ticket_key else "MISSING",
            "anthropic_api_key": "set" if self.anthropic_api_key else "MISSING",
            "dry_run": self.dry_run,
            "data_dir": str(self.data_dir),
            "label": self.label,
            "vision_model": self.vision_model,
        }


def load_settings(env_file: Path | None = None) -> Settings:
    env_file = env_file or Path(os.environ.get("TICKET_ENV_FILE", PROJECT_DIR / ".env"))
    load_dotenv(env_file, override=False)
    e = os.environ.get
    return Settings(
        todoist_token=e("TODOIST_TOKEN", ""),
        printer_ip=e("PRINTER_IP", ""),
        ticket_key=e("TICKET_KEY", ""),
        anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
        dry_run=_truthy(e("TICKET_DRY_RUN")),
        data_dir=Path(e("TICKET_DATA_DIR", str(Path.home() / ".local" / "share" / "ticket"))).expanduser(),
        label=e("TICKET_LABEL", "62"),
        vision_model=e("TICKET_VISION_MODEL", "claude-sonnet-5-5"),
        host=e("TICKET_HOST", "0.0.0.0"),
        port=int(e("TICKET_PORT", "8787")),
    )
