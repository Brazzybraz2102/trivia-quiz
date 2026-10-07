"""Encrypts the secrets people give Next Box (to-do app tokens and passwords).

The key lives in its own file in the data dir, not in .env, so editing .env or changing
NEXTBOX_KEY never locks anyone out of their connection. Back the data dir up as a whole.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class Vault:
    def __init__(self, root: Path):
        env_key = os.environ.get("NEXTBOX_SECRET_KEY", "").strip()
        if env_key:  # servers without a lasting disk keep the key in an environment secret
            self.path = None
            self._f = Fernet(env_key.encode())
            return
        self.path = Path(root) / "secret.key"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(Fernet.generate_key())
        self._f = Fernet(self.path.read_bytes().strip())

    def seal(self, data: dict) -> str:
        return self._f.encrypt(json.dumps(data).encode()).decode()

    def open(self, token: str) -> dict:
        try:
            return json.loads(self._f.decrypt(token.encode()))
        except InvalidToken as exc:
            raise ValueError("saved connection can't be decrypted (secret.key changed?)") from exc
