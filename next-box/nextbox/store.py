"""Print history, label manifests, scan results and the daily auto-print guard.

This is not a task database. It records what was printed (so a photo of a label can be
matched back to Todoist task IDs) and what read-back is still waiting for confirmation.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
from datetime import date
from pathlib import Path
from typing import Iterator

ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def normalize_id(label_id: str) -> str:
    return label_id.strip().lstrip("#").upper()


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        for sub in ("printed", "png", "scans"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        # File lock so the CLI and the server can't race each other.
        with open(self.root / ".lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _write(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, path)

    @staticmethod
    def new_id(day: date | None = None) -> str:
        """e.g. 260930-X5C8: printed date + 4 unambiguous characters (no 0/O, 1/I)."""
        day = day or date.today()
        return f"{day:%y%m%d}-" + "".join(secrets.choice(ID_ALPHABET) for _ in range(4))

    # --- auto guard -------------------------------------------------------
    def claim_auto(self, day: str, dry_run: bool) -> bool:
        """True the first time it's called for `day`; False after. Real and dry runs are
        tracked separately so testing never uses up the real print for the day."""
        key = "dry" if dry_run else "real"
        path = self.root / "auto.json"
        with self._lock():
            state = json.loads(path.read_text()) if path.exists() else {}
            if state.get(key) == day:
                return False
            state[key] = day
            self._write(path, state)
            return True

    def release_auto(self, day: str, dry_run: bool) -> None:
        """Only used when nothing was sent to the printer (e.g. Todoist was down)."""
        key = "dry" if dry_run else "real"
        path = self.root / "auto.json"
        with self._lock():
            state = json.loads(path.read_text()) if path.exists() else {}
            if state.get(key) == day:
                state.pop(key)
                self._write(path, state)

    # --- printed labels ---------------------------------------------------
    def png_path(self, label_id: str) -> Path:
        return self.root / "png" / f"{label_id}.png"

    def save_printed(self, record: dict) -> None:
        with self._lock():
            self._write(self.root / "printed" / f"{record['id']}.json", record)

    def get_printed(self, label_id: str) -> dict | None:
        path = self.root / "printed" / f"{normalize_id(label_id)}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def list_printed(self, limit: int = 50) -> list[dict]:
        records = [json.loads(p.read_text()) for p in (self.root / "printed").glob("*.json")]
        records.sort(key=lambda r: r["created_at"], reverse=True)
        return records[:limit]

    # --- scans ------------------------------------------------------------
    def save_scan(self, record: dict) -> None:
        with self._lock():
            self._write(self.root / "scans" / f"{record['id']}.json", record)

    def get_scan(self, scan_id: str) -> dict | None:
        path = self.root / "scans" / f"{scan_id}.json"
        return json.loads(path.read_text()) if path.exists() else None

    @contextlib.contextmanager
    def edit_scan(self, scan_id: str) -> Iterator[dict]:
        path = self.root / "scans" / f"{scan_id}.json"
        with self._lock():
            if not path.exists():
                raise KeyError(scan_id)
            record = json.loads(path.read_text())
            yield record
            self._write(path, record)

    def list_scans(self, limit: int = 20) -> list[dict]:
        records = [json.loads(p.read_text()) for p in (self.root / "scans").glob("*.json")]
        records.sort(key=lambda r: r["created_at"], reverse=True)
        return records[:limit]
