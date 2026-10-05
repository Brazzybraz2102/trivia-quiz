"""Local user accounts, roles and sign-in sessions. Everything lives in the data dir on the desktop.

Passwords are hashed with scrypt. Session tokens are random and only their SHA-256 is stored,
so a copy of the data dir can't be used to sign in.

Roles: user < admin < superadmin. Admins support people (reset passwords, disable accounts,
read activity); superadmins also debug (diagnostics, error log, per-user debug mode).
"""
from __future__ import annotations

import contextlib
import fcntl
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
ROLES = ("user", "admin", "superadmin")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}

# Per-user preferences: name -> (default, validator). Unknown keys are rejected.
PREFS = {
    "always_dry_run": (False, lambda v: isinstance(v, bool)),
    "max_rows": (10, lambda v: isinstance(v, int) and 3 <= v <= 20),
    "show_waiting": (True, lambda v: isinstance(v, bool)),
    "time_24h": (False, lambda v: isinstance(v, bool)),
    "default_filter": ("", lambda v: isinstance(v, str) and len(v) <= 200),
    "auto_apply": (True, lambda v: isinstance(v, bool)),
    "confidence": (0.7, lambda v: isinstance(v, (int, float)) and 0.5 <= v <= 0.95),
}


def default_prefs() -> dict:
    return {k: d for k, (d, _) in PREFS.items()}


def rank(role: str) -> int:
    return ROLES.index(role) if role in ROLES else 0


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
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._failures: dict[str, list[float]] = {}

    @contextlib.contextmanager
    def _lock(self):
        """Thread lock plus a file lock, so the CLI and the server never clobber each other."""
        with self._thread_lock:
            if self._depth:  # already holding the file lock in this thread
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            with open(self.root / ".accounts.lock", "w") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
                    fcntl.flock(fh, fcntl.LOCK_UN)

    # --- storage ----------------------------------------------------------
    def _load(self, path: Path) -> dict:
        return json.loads(path.read_text()) if path.exists() else {}

    def _save(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _users(self) -> dict:
        users = self._load(self._users_path)
        for u in users.values():  # older records predate roles and prefs
            u.setdefault("role", "user")
            u.setdefault("disabled", False)
            u.setdefault("beta", False)
            u.setdefault("debug", False)
            u.setdefault("must_change", False)
            u["prefs"] = {**default_prefs(), **u.get("prefs", {})}
        return users

    # --- users ------------------------------------------------------------
    @staticmethod
    def _check_name(username: str) -> str:
        username = username.strip().lower()
        if not USERNAME_RE.match(username):
            raise AuthError("username: 2-32 chars, lowercase letters, digits, . _ -")
        return username

    @staticmethod
    def _check_password(password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"password must be at least {MIN_PASSWORD} characters")

    def list_users(self) -> list[str]:
        return sorted(self._load(self._users_path))

    def has_users(self) -> bool:
        return bool(self._load(self._users_path))

    def get(self, username: str) -> dict | None:
        """Public view of a user: never includes the password hash or salt."""
        u = self._users().get(username.strip().lower())
        if not u:
            return None
        out = {"username": username.strip().lower(),
               **{k: v for k, v in u.items() if k not in {"salt", "hash", "connection"}}}
        conn = u.get("connection")
        out["connection"] = ({k: v for k, v in conn.items() if k != "secret"} if conn else None)
        return out

    # --- to-do app connection (secret is sealed by the Vault) ----------------
    def connection(self, username: str) -> dict | None:
        """Includes the sealed secret: server-side use only."""
        u = self._users().get(username.strip().lower())
        return u.get("connection") if u else None

    def set_connection(self, username: str, connection: dict | None) -> None:
        with self._lock():
            users = self._users()
            if username not in users:
                raise AuthError(f"no user {username}")
            if connection is None:
                users[username].pop("connection", None)
            else:
                users[username]["connection"] = connection
            self._save(self._users_path, users)

    def owner(self) -> str | None:
        """The oldest active superadmin: whose to-do app Home Assistant and the CLI use."""
        supers = [(u.get("created", 0), name) for name, u in self._users().items()
                  if u["role"] == "superadmin" and not u["disabled"]]
        return min(supers)[1] if supers else None

    def all_users(self) -> list[dict]:
        return [self.get(name) for name in self.list_users()]

    def create(self, username: str, password: str, role: str = "user", beta: bool = False,
               must_change: bool = False) -> None:
        username = self._check_name(username)
        self._check_password(password)
        if role not in ROLES:
            raise AuthError(f"role must be one of {', '.join(ROLES)}")
        with self._lock():
            users = self._users()
            if username in users:
                raise AuthError(f"user {username} already exists")
            salt = secrets.token_bytes(16)
            users[username] = {"salt": salt.hex(), "hash": _hash_password(password, salt),
                               "created": int(time.time()), "role": role, "disabled": False,
                               "beta": beta, "debug": False, "must_change": must_change,
                               "last_login": None, "prefs": default_prefs()}
            self._save(self._users_path, users)

    def set_password(self, username: str, password: str, create: bool = False,
                     must_change: bool = False) -> None:
        if create:
            # First account ever is the superadmin, so a fresh install can't lock itself out.
            role = "user" if self.has_users() else "superadmin"
            return self.create(username, password, role=role)
        username = self._check_name(username)
        self._check_password(password)
        with self._lock():
            users = self._users()
            if username not in users:
                raise AuthError(f"no user {username}")
            salt = secrets.token_bytes(16)
            users[username].update(salt=salt.hex(), hash=_hash_password(password, salt),
                                   must_change=must_change)
            self._save(self._users_path, users)
        self.revoke_user_sessions(username)  # a password change signs out every device

    def update(self, username: str, **fields) -> dict:
        allowed = {"role", "disabled", "beta", "debug"}
        if set(fields) - allowed:
            raise AuthError(f"can't change {', '.join(set(fields) - allowed)}")
        if "role" in fields and fields["role"] not in ROLES:
            raise AuthError(f"role must be one of {', '.join(ROLES)}")
        username = username.strip().lower()
        with self._lock():
            users = self._users()
            if username not in users:
                raise AuthError(f"no user {username}")
            demoting = fields.get("role", "superadmin") != "superadmin" or fields.get("disabled")
            if users[username]["role"] == "superadmin" and demoting and self._superadmins(users) <= 1:
                raise AuthError("can't demote or disable the last superadmin")
            users[username].update(fields)
            self._save(self._users_path, users)
        if fields.get("disabled"):
            self.revoke_user_sessions(username)
        return self.get(username)

    def set_prefs(self, username: str, changes: dict) -> dict:
        for k, v in changes.items():
            if k not in PREFS:
                raise AuthError(f"unknown setting {k}")
            if not PREFS[k][1](v):
                raise AuthError(f"invalid value for {k}")
        with self._lock():
            users = self._users()
            users[username]["prefs"].update(changes)
            self._save(self._users_path, users)
            return users[username]["prefs"]

    @staticmethod
    def _superadmins(users: dict) -> int:
        return sum(1 for u in users.values() if u["role"] == "superadmin" and not u["disabled"])

    def remove_user(self, username: str) -> None:
        username = username.strip().lower()
        with self._lock():
            users = self._users()
            if username not in users:
                raise AuthError(f"no user {username}")
            if users[username]["role"] == "superadmin" and self._superadmins(users) <= 1:
                raise AuthError("can't remove the last superadmin")
            users.pop(username)
            self._save(self._users_path, users)
        self.revoke_user_sessions(username)

    def password_ok(self, username: str, password: str) -> bool:
        """Right password, whether or not the account is turned off."""
        user = self._users().get(username.strip().lower())
        # Hash even for unknown users so response time doesn't reveal which names exist.
        salt = bytes.fromhex(user["salt"]) if user else b"\0" * 16
        digest = _hash_password(password, salt)
        return bool(user) and hmac.compare_digest(digest, user["hash"])

    def verify(self, username: str, password: str) -> bool:
        user = self._users().get(username.strip().lower())
        return self.password_ok(username, password) and not user["disabled"]

    def touch_login(self, username: str) -> None:
        with self._lock():
            users = self._users()
            users[username]["last_login"] = int(time.time())
            self._save(self._users_path, users)

    @staticmethod
    def temp_password() -> str:
        return "-".join(secrets.token_hex(2) for _ in range(3))  # e.g. 3f9a-07c2-b1e4

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
    def create_session(self, username: str, ip: str = "", agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        with self._lock():
            sessions = self._prune(self._load(self._sessions_path))
            sessions[_token_hash(token)] = {"user": username.lower(), "created": int(time.time()),
                                            "expires": int(time.time()) + SESSION_DAYS * 86400,
                                            "ip": ip, "agent": agent[:120]}
            self._save(self._sessions_path, sessions)
        return token

    def session_user(self, token: str | None) -> str | None:
        if not token:
            return None
        h = _token_hash(token)
        s = self._load(self._sessions_path).get(h)
        now = time.time()
        if not s or s["expires"] < now:
            return None
        user = self._users().get(s["user"])
        if not user or user["disabled"]:
            return None
        if s["expires"] - now < (SESSION_DAYS - 1) * 86400:
            # Sliding expiry: anyone who uses the app at least monthly stays signed in.
            with self._lock():
                sessions = self._load(self._sessions_path)
                if h in sessions:
                    sessions[h]["expires"] = int(now) + SESSION_DAYS * 86400
                    self._save(self._sessions_path, sessions)
        return s["user"]

    def sessions_for(self, username: str, current: str | None = None) -> list[dict]:
        cur = _token_hash(current) if current else None
        out = []
        for h, s in self._prune(self._load(self._sessions_path)).items():
            if s["user"] == username:
                out.append({"id": h[:12], "created": s.get("created"), "ip": s.get("ip", ""),
                            "agent": s.get("agent", ""), "current": h == cur})
        return sorted(out, key=lambda s: s["created"] or 0, reverse=True)

    def end_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock():
            sessions = self._load(self._sessions_path)
            if sessions.pop(_token_hash(token), None) is not None:
                self._save(self._sessions_path, sessions)

    def revoke_user_sessions(self, username: str, keep: str | None = None) -> int:
        keep_h = _token_hash(keep) if keep else None
        with self._lock():
            sessions = self._load(self._sessions_path)
            kept = {k: v for k, v in sessions.items() if v["user"] != username or k == keep_h}
            if kept != sessions:
                self._save(self._sessions_path, kept)
            return len(sessions) - len(kept)

    @staticmethod
    def _prune(sessions: dict) -> dict:
        now = time.time()
        return {k: v for k, v in sessions.items() if v["expires"] > now}
