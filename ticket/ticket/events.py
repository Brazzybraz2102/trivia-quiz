"""Activity, audit and error log (events.jsonl in the data dir) plus server-wide switches.

Events never contain passwords, tokens or .env values. Full tracebacks are kept only for
errors, so a superadmin can debug what a beta tester hit.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

MAX_EVENTS = 5000
_SERVER_DEFAULTS = {
    "printing_paused": False,     # every print becomes a dry run (kill switch)
    "auto_print_enabled": True,   # Home Assistant's once-a-day print
    "announcement": "",           # banner shown to everyone who's signed in
}


class Events:
    def __init__(self, root: Path):
        self.path = Path(root) / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, user: str, action: str, *, ok: bool = True, kind: str = "activity",
            detail: dict | None = None, error: str = "", trace: str = "") -> dict:
        """kind: activity | audit (admin actions) | error | feedback | auth"""
        ev = {"ts": round(time.time(), 3), "user": user, "kind": kind, "action": action, "ok": ok}
        if detail:
            ev["detail"] = detail
        if error:
            ev["error"] = error[:500]
        if trace:
            ev["trace"] = trace[-6000:]
        with self._lock:
            with open(self.path, "a") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self._trim()
        return ev

    def _trim(self) -> None:
        if self.path.stat().st_size < 4_000_000:
            return
        lines = self.path.read_text().splitlines()
        if len(lines) > MAX_EVENTS:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines[-MAX_EVENTS:]) + "\n")
            os.replace(tmp, self.path)

    def query(self, *, user: str | None = None, kind: str | None = None, ok: bool | None = None,
              limit: int = 200) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in reversed(self.path.read_text().splitlines()):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if user and ev.get("user") != user:
                continue
            if kind and ev.get("kind") != kind:
                continue
            if ok is not None and ev.get("ok") != ok:
                continue
            out.append(ev)
            if len(out) >= limit:
                break
        return out


class ServerSettings:
    def __init__(self, root: Path):
        self.path = Path(root) / "server_settings.json"
        self._lock = threading.Lock()

    def get(self) -> dict:
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        return {**_SERVER_DEFAULTS, **{k: v for k, v in data.items() if k in _SERVER_DEFAULTS}}

    def update(self, changes: dict) -> dict:
        for k, v in changes.items():
            if k not in _SERVER_DEFAULTS:
                raise ValueError(f"unknown server setting {k}")
            if type(v) is not type(_SERVER_DEFAULTS[k]):
                raise ValueError(f"invalid value for {k}")
            if k == "announcement" and len(v) > 300:
                raise ValueError("announcement is limited to 300 characters")
        with self._lock:
            data = {**self.get(), **changes}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self.path)
        return data
