"""Encrypts the secrets people give Next Box (to-do app tokens and passwords).

The key lives in its own file in the data dir, not in .env, so editing .env or changing
NEXTBOX_KEY never locks anyone out of their connection. Back the data dir up as a whole.

At a hosting company there is no lasting disk, so the key is the NEXTBOX_SECRET_KEY environment secret
instead: the exact content of secret.key (a Fernet key, used as it is), or any other text of 32 or more
characters, which `key_from` turns into a key the same way every time.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

MIN_SECRET_CHARS = 32
SHORT_KEY = (f"NEXTBOX_SECRET_KEY is too short: use at least {MIN_SECRET_CHARS} characters (any letters and digits), "
             "or the exact content of your secret.key file.")


def key_from(secret: str) -> bytes:
    """The Fernet key a NEXTBOX_SECRET_KEY setting means.

    A valid Fernet key (what secret.key holds: 32 bytes, base64) is used as it is, so moving a home
    Next Box's secret.key into the environment keeps every saved connection readable. Any other text of
    32+ characters is turned into one (SHA-256 → url-safe base64): a hosting company's random secret works,
    and the same text always gives the same key. Shorter text is refused with one plain sentence."""
    secret = (secret or "").strip()
    try:
        Fernet(secret.encode())
        return secret.encode()
    except (ValueError, TypeError):
        pass
    if len(secret) < MIN_SECRET_CHARS:
        raise ValueError(SHORT_KEY)
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


class Vault:
    def __init__(self, root: Path):
        env_key = os.environ.get("NEXTBOX_SECRET_KEY", "").strip()
        if env_key:  # servers without a lasting disk keep the key in an environment secret
            self.path = None
            try:
                self._f = Fernet(key_from(env_key))
            except ValueError as exc:  # too short: nothing can run without a usable key, so say so and stop
                raise SystemExit(str(exc)) from None
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
