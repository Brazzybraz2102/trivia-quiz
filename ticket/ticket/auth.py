"""Local user accounts and sign-in sessions. Everything lives in the data dir on the desktop.

Passwords are hashed with scrypt. Session tokens are random and only their SHA-256 is stored,
so a copy of the data dir can't be used to sign in.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

SESSION_DAYS = 30
MIN_PASSWORD = 8
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{2,32}$")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthError(ValueError):
    pass


class Accounts:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._users_path = self.root / "users.json"
        self._sessions_path = self.root / "sessions.json"
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    # --- storage ----------------------------------------------------------
    def _load(self, path: Path) -> dict:
        return json.loads(path.read_text()) if path.exists() else {}

    def _save(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    # --- users ------------------------------------------------------------
    def list_users(self) -> list[str]:
        return sorted(self._load(self._users_path))

    def has_users(self) -> bool:
        return bool(self._load(self._users_path))

    def set_password(self, username: str, password: str, create: bool = False) -> None:
        username = username.strip().lower()
        if not USERNAME_RE.match(username):
            raise AuthError("username: 2-32 chars, lowercase letters, digits, . _ -")
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
        with self._lock:
            users = self._load(self._users_path)
            if create and username in users:
                raise AuthError(f"user {username} already exists")
            if not create and username not in users:
                raise AuthError(f"no user {username}")
            salt = secrets.token_bytes(16)
            users[username] = {"salt": salt.hex(), "hash": _hash_password(password, salt),
                               "created": users.get(username, {}).get("created", int(time.time()))}
            self._save(self._users_path, users)
        self.revoke_user_sessions(username)  # a password change signs out every device

    def remove_user(self, username: str) -> None:
        with self._lock:
            users = self._load(self._users_path)
            if users.pop(username.lower(), None) is None:
                raise AuthError(f"no user {username}")
            self._save(self._users_path, users)
        self.revoke_user_sessions(username.lower())

    def verify(self, username: str, password: str) -> bool:
        user = self._load(self._users_path).get(username.strip().lower())
        # Hash even for unknown users so response time doesn't reveal which names exist.
        salt = bytes.fromhex(user["salt"]) if user else b"\0" * 16
        digest = _hash_password(password, salt)
        return bool(user) and hmac.compare_digest(digest, user["hash"])

    # --- rate limit -------------------------------------------------------
    def too_many_failures(self, client: str, limit: int = 5, window: int = 300) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(client, []) if now - t < window]
        self._failures[client] = recent
        return len(recent) >= limit

    def record_failure(self, client: str) -> None:
        self._failures.setdefault(client, []).append(time.time())

    def clear_failures(self, client: str) -> None:
        self._failures.pop(client, None)

    # --- sessions ---------------------------------------------------------
    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            sessions = self._prune(self._load(self._sessions_path))
            sessions[_token_hash(token)] = {"user": username.lower(),
                                            "expires": int(time.time()) + SESSION_DAYS * 86400}
            self._save(self._sessions_path, sessions)
        return token

    def session_user(self, token: str | None) -> str | None:
        if not token:
            return None
        s = self._load(self._sessions_path).get(_token_hash(token))
        if not s or s["expires"] < time.time():
            return None
        if s["user"] not in self._load(self._users_path):
            return None
        return s["user"]

    def end_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            sessions = self._load(self._sessions_path)
            if sessions.pop(_token_hash(token), None) is not None:
                self._save(self._sessions_path, sessions)

    def revoke_user_sessions(self, username: str) -> None:
        with self._lock:
            sessions = self._load(self._sessions_path)
            kept = {k: v for k, v in sessions.items() if v["user"] != username}
            if kept != sessions:
                self._save(self._sessions_path, kept)

    @staticmethod
    def _prune(sessions: dict) -> dict:
        now = time.time()
        return {k: v for k, v in sessions.items() if v["expires"] > now}
