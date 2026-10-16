"""Accounts, households, roles and sign-in sessions, stored in the database (see db.py).

Passwords are hashed with scrypt. Session tokens are random and only their SHA-256 is stored,
so a copy of the database can't be used to sign in.

Everyone belongs to a household. Roles: user < admin (the household's owner/manager) <
superadmin (Next Box staff). Admins manage their own household; staff support everyone.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time

from sqlalchemy import and_, delete, func, insert, or_, select, update

from . import db

SESSION_DAYS = 30
SESSION_TOUCH = 600  # seconds between "last used" updates of a signed-in device
MIN_PASSWORD = 8
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{2,32}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
DEFAULT_HOUSEHOLD = "home"  # self-hosted installs: everyone shares one household
INVITE_DAYS = 7
ROLES = ("user", "admin", "superadmin")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}

# Per-user preferences: name -> (default, validator). Unknown keys are rejected.
PREFS = {
    "always_dry_run": (False, lambda v: isinstance(v, bool)),
    "max_rows": (5, lambda v: type(v) is int and 1 <= v <= 20),   # a short list gets started (not true/false)
    "gentle_words": (True, lambda v: isinstance(v, bool)),   # "waiting 3d", not "overdue 3d"
    "show_wins": (True, lambda v: isinstance(v, bool)),      # "Yesterday: 4 done" on the daily ticket
    "show_next_step": (True, lambda v: isinstance(v, bool)), # a task's first unticked tiny step under it
    "show_calendar": (True, lambda v: isinstance(v, bool)),  # today's events at the top of the daily ticket
    "setup_dismissed": ([], lambda v: isinstance(v, list) and len(v) <= 20
                        and all(isinstance(x, str) and len(x) <= 20 for x in v)),  # guide steps marked done/skipped
    "setup_hidden": (False, lambda v: isinstance(v, bool)),  # the Get started guide is put away
    "setup_done": ([], lambda v: isinstance(v, list) and len(v) <= 20
                   and all(isinstance(x, str) and len(x) <= 20 for x in v)),  # guide steps that ticked themselves (they stay ticked)
    "paper_size": ("letter", lambda v: v in ("letter", "a4", "a5", "half_letter")),  # for Print on paper
    "journal_mode": (True, lambda v: isinstance(v, bool)),   # daily label for a journal page; keep every read-back photo
    "slug_after": (3, lambda v: type(v) is int and 2 <= v <= 10),  # moves before a task counts as a slug
    "display_name": ("", lambda v: isinstance(v, str) and len(v.strip()) <= 40),
    "text_size": ("normal", lambda v: v in ("normal", "large", "xlarge")),
    "start_tab": ("mylist", lambda v: v in ("mylist", "print", "journal")),
    "new_tasks_to": ("nextbox", lambda v: v in ("nextbox", "linked")),  # where new tasks go
    "sticker_size": ("normal", lambda v: v in ("normal", "big")),
    "show_waiting": (True, lambda v: isinstance(v, bool)),
    "time_24h": (False, lambda v: isinstance(v, bool)),
    "default_filter": ("", lambda v: isinstance(v, str) and len(v) <= 200),
    "auto_apply": (True, lambda v: isinstance(v, bool)),
    "confidence": (0.7, lambda v: isinstance(v, (int, float)) and 0.5 <= v <= 0.95),
    "default_printer": ("", lambda v: isinstance(v, str) and len(v) <= 40),
    "color_rules": ({}, lambda v: _valid_rules(v)),     # reason -> label color, e.g. overdue -> red
    "split_overdue": (False, lambda v: isinstance(v, bool)),  # overdue tasks on their own ticket
}

# What to tell someone whose setting wasn't saved: plain words, never the setting's key.
PREF_HINTS = {
    "max_rows": "Pick a number from 1 to 20.",
    "slug_after": "Pick a number from 2 to 10.",
    "default_filter": "Keep the filter to 200 characters or less.",
    "display_name": "Keep the name to 40 characters or less.",
    "confidence": "Pick a value from 50% to 95%.",
    "paper_size": "Pick a paper size from the list.",
    "text_size": "Pick a text size from the list.",
    "start_tab": "Pick a screen from the list.",
    "new_tasks_to": "Pick My list or your linked app.",
    "default_printer": "Pick a printer from the list.",
    "color_rules": "Pick a label color from the list.",
}


def _valid_rules(v) -> bool:
    from .printer import COLORS
    from .printers import REASONS
    return isinstance(v, dict) and all(k in REASONS and c in (*COLORS, "any") for k, c in v.items())


def default_prefs() -> dict:
    from .printers import DEFAULT_RULES
    prefs = {k: (dict(d) if isinstance(d, dict) else d) for k, (d, _) in PREFS.items()}
    prefs["color_rules"] = dict(DEFAULT_RULES)
    return prefs


def rank(role: str) -> int:
    return ROLES.index(role) if role in ROLES else 0


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def norm_code(code: str) -> str:
    """An invite code as stored (3F9A-07C2-B1E4), however it was typed: lowercase, spaces instead of
    dashes, no dashes, or O/I/L for 0/1 (codes are hex, so those letters never appear)."""
    raw = re.sub(r"[^0-9A-F]", "", code.upper().translate(str.maketrans("OIL", "011")))
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


class AuthError(ValueError):
    pass


class Accounts:
    def __init__(self, root):
        self.db = db.database(root)
        self._failures: dict[str, list[tuple[float, str]]] = {}
        self._failures_lock = threading.Lock()

    # --- rows ----------------------------------------------------------------
    def _row(self, conn, username: str):
        return conn.execute(select(db.users).where(db.users.c.username == username.strip().lower())).mappings().first()

    @staticmethod
    def _public(row) -> dict:
        out = {k: v for k, v in dict(row).items() if k not in {"salt", "hash", "connection"}}
        out["prefs"] = {**default_prefs(), **(row["prefs"] or {})}
        conn = row["connection"]
        out["connection"] = {k: v for k, v in conn.items() if k != "secret"} if conn else None
        out["calendars"] = [{k: v for k, v in c.items() if k != "secret"} for c in (row.get("calendars") or [])]
        return out

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

    @staticmethod
    def _check_email(email: str | None) -> str | None:
        if email is None or email == "":
            return None
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            raise AuthError("That doesn't look like an email address.")
        return email

    # --- households ----------------------------------------------------------
    def ensure_household(self, household_id: str, name: str = "Home") -> str:
        with self.db.begin() as conn:
            if not conn.execute(select(db.households.c.id).where(db.households.c.id == household_id)).first():
                conn.execute(insert(db.households).values(id=household_id, name=name[:80], created=int(time.time()),
                                                          plan="free"))
        return household_id

    def create_household(self, name: str) -> str:
        return self.ensure_household(secrets.token_hex(6), name.strip()[:80] or "Home")

    def households(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(select(db.households, func.count(db.users.c.username).label("members"))
                                .select_from(db.households.outerjoin(db.users, db.users.c.household_id == db.households.c.id))
                                .group_by(db.households.c.id).order_by(db.households.c.name)).mappings().all()
        return [dict(r) for r in rows]

    def household(self, household_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(select(db.households).where(db.households.c.id == household_id)).mappings().first()
        return dict(row) if row else None

    def household_of(self, username: str) -> str | None:
        u = self.get(username)
        return u["household_id"] if u else None

    def delete_household(self, household_id: str) -> list[str]:
        """Remove the household and everyone in it. Returns the usernames removed."""
        with self.db.begin() as conn:
            names = [r[0] for r in conn.execute(select(db.users.c.username).where(db.users.c.household_id == household_id))]
            if names:
                conn.execute(delete(db.sessions).where(db.sessions.c.username.in_(names)))
                conn.execute(delete(db.users).where(db.users.c.household_id == household_id))
            conn.execute(delete(db.invites).where(db.invites.c.household_id == household_id))
            conn.execute(delete(db.households).where(db.households.c.id == household_id))
        return names

    # --- invites ---------------------------------------------------------------
    def create_invite(self, household_id: str, by: str, days: int = INVITE_DAYS, beta: bool = False,
                      note: str = "", own_household: bool = False) -> str:
        """A one-time code. With own_household the person gets a household of their own (and
        manages it); otherwise they join `household_id` as a user."""
        if not 1 <= int(days) <= 90:
            raise AuthError("an invite lasts 1 to 90 days")
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))  # e.g. 3F9A-07C2-B1E4
        now = int(time.time())
        with self.db.begin() as conn:
            conn.execute(insert(db.invites).values(code=code, household_id=household_id, created_by=by,
                                                   created=now, expires=now + int(days) * 86400, used_by=None,
                                                   beta=bool(beta), note=(note or "").strip()[:80] or None,
                                                   own_household=bool(own_household)))
        return code

    def invites(self, household_id: str | None = None) -> list[dict]:
        q = select(db.invites).order_by(db.invites.c.created.desc())
        if household_id is not None:
            # A household's admins see its own join codes, never staff codes that start a new household
            # (those are stored under the staff member's household, and name people from elsewhere).
            q = q.where(db.invites.c.household_id == household_id,
                        or_(db.invites.c.own_household.is_(None), db.invites.c.own_household.is_(False)))
        with self.db.connect() as conn:
            rows = conn.execute(q).mappings().all()
        now = time.time()
        return [{**dict(r), "beta": bool(r["beta"]), "own_household": bool(r["own_household"]),
                 "status": "used" if r["used_by"] else "expired" if r["expires"] < now else "open"} for r in rows]

    def revoke_invite(self, code: str) -> None:
        with self.db.begin() as conn:
            conn.execute(delete(db.invites).where(and_(db.invites.c.code == norm_code(code),
                                                       db.invites.c.used_by.is_(None))))

    def _redeem(self, conn, code: str, username: str) -> dict:
        code = norm_code(code)
        row = conn.execute(select(db.invites).where(db.invites.c.code == code)).mappings().first()
        if not row:
            raise AuthError("We don't recognize that invite code. Check it and try again.")
        if row["used_by"] or row["expires"] < time.time():
            raise AuthError("That invite code was already used or has expired. Ask for a new one.")
        conn.execute(update(db.invites).where(db.invites.c.code == code).values(used_by=username))
        return dict(row)

    # --- users -----------------------------------------------------------------
    def list_users(self, household_id: str | None = None) -> list[str]:
        q = select(db.users.c.username).order_by(db.users.c.username)
        if household_id is not None:
            q = q.where(db.users.c.household_id == household_id)
        with self.db.connect() as conn:
            return [r[0] for r in conn.execute(q)]

    def has_users(self) -> bool:
        with self.db.connect() as conn:
            return bool(conn.execute(select(func.count()).select_from(db.users)).scalar())

    def get(self, username: str) -> dict | None:
        """Public view of a user: never includes the password hash, salt or connection secret."""
        with self.db.connect() as conn:
            row = self._row(conn, username)
        return self._public(row) if row else None

    def all_users(self, household_id: str | None = None) -> list[dict]:
        q = select(db.users).order_by(db.users.c.username)
        if household_id is not None:
            q = q.where(db.users.c.household_id == household_id)
        with self.db.connect() as conn:
            return [self._public(r) for r in conn.execute(q).mappings()]

    def find_login(self, identifier: str) -> str | None:
        """Sign in with a username or an email address."""
        ident = identifier.strip().lower()
        col = db.users.c.email if "@" in ident else db.users.c.username
        with self.db.connect() as conn:
            row = conn.execute(select(db.users.c.username).where(col == ident)).first()
        return row[0] if row else None

    def create(self, username: str, password: str, role: str = "user", beta: bool = False,
               must_change: bool = False, household_id: str | None = None, email: str | None = None) -> None:
        username = self._check_name(username)
        self._check_password(password)
        email = self._check_email(email)
        if role not in ROLES:
            raise AuthError(f"role must be one of {', '.join(ROLES)}")
        household_id = household_id or self.ensure_household(DEFAULT_HOUSEHOLD)
        salt = secrets.token_bytes(16)
        with self.db.begin() as conn:
            if self._row(conn, username):
                raise AuthError(f"user {username} already exists")
            if email and conn.execute(select(db.users.c.username).where(db.users.c.email == email)).first():
                raise AuthError("that email already has an account")
            conn.execute(insert(db.users).values(
                username=username, email=email, household_id=household_id, role=role, disabled=False,
                beta=beta, debug=False, must_change=must_change, salt=salt.hex(),
                hash=_hash_password(password, salt), created=int(time.time()), last_login=None,
                prefs=default_prefs(), connection=None, consent=None, support_until=None))

    def sign_up(self, username: str, password: str, email: str | None = None,
                invite: str | None = None, household_name: str = "") -> dict:
        """Public sign-up: with an invite code you join that household, otherwise you start your own
        (and manage it). The very first account on a fresh server is also staff (superadmin)."""
        username = self._check_name(username)
        self._check_password(password)
        email = self._check_email(email)
        first = not self.has_users()
        beta, made_household = False, False
        if invite:
            with self.db.begin() as conn:
                # The code first, so a bad code never reveals which names or emails have accounts.
                # A name or email error below rolls this back, so the code still works.
                inv = self._redeem(conn, invite, username)
                if self._row(conn, username):
                    raise AuthError(f"user {username} already exists")
                if email and conn.execute(select(db.users.c.username).where(db.users.c.email == email)).first():
                    raise AuthError("another account already uses that email")
            beta = bool(inv.get("beta"))
            if inv.get("own_household"):
                household_id, role, made_household = self.create_household(household_name or f"{username}'s home"), "admin", True
            else:
                household_id, role = inv["household_id"], "user"
        else:
            household_id = self.create_household(household_name or f"{username}'s household")
            role, made_household = "admin", True
        if first:
            role = "superadmin"
        try:
            self.create(username, password, role=role, household_id=household_id, email=email, beta=beta)
        except AuthError:
            if made_household:
                self.delete_household(household_id)
            raise
        return self.get(username)

    # --- the protected admin account and whose tasks Home Assistant prints -----------------
    def _flag(self, key: str) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute(select(db.server_settings.c.value).where(db.server_settings.c.key == key)).first()
        return row[0] if row else None

    def _set_flag(self, key: str, value: str | None) -> None:
        with self.db.begin() as conn:
            conn.execute(delete(db.server_settings).where(db.server_settings.c.key == key))
            if value is not None:
                conn.execute(insert(db.server_settings).values(key=key, value=value))

    def protected(self) -> str | None:
        """The admin account that always stays a superadmin. Only the computer's CLI changes it."""
        return self._flag("_protected_admin")

    def set_protected(self, username: str | None) -> None:
        if username is not None:
            username = self._check_name(username)
            u = self.get(username)
            if not u:
                raise AuthError(f"no user {username}")
            if u["role"] != "superadmin" or u["disabled"]:
                raise AuthError(f"{username} must be an active superadmin first")
        self._set_flag("_protected_admin", username)

    def set_print_owner(self, username: str | None) -> None:
        """Whose tasks Home Assistant and the CLI print (default: the oldest superadmin)."""
        if username is not None:
            username = self._check_name(username)
            if not self.get(username):
                raise AuthError(f"no user {username}")
        self._set_flag("_print_owner", username)

    def set_password(self, username: str, password: str, create: bool = False,
                     must_change: bool = False) -> None:
        if create:
            # First account ever is the superadmin, so a fresh install can't lock itself out.
            role = "user" if self.has_users() else "superadmin"
            return self.create(username, password, role=role)
        username = self._check_name(username)
        self._check_password(password)
        salt = secrets.token_bytes(16)
        with self.db.begin() as conn:
            if not self._row(conn, username):
                raise AuthError(f"no user {username}")
            conn.execute(update(db.users).where(db.users.c.username == username).values(
                salt=salt.hex(), hash=_hash_password(password, salt), must_change=must_change))
        self.revoke_user_sessions(username)  # a password change signs out every device

    def _active_superadmins(self, conn) -> int:
        return conn.execute(select(func.count()).select_from(db.users).where(
            and_(db.users.c.role == "superadmin", db.users.c.disabled.is_(False)))).scalar()

    def update(self, username: str, **fields) -> dict:
        allowed = {"role", "disabled", "beta", "debug", "email", "household_id", "must_change"}
        if set(fields) - allowed:
            raise AuthError(f"can't change {', '.join(set(fields) - allowed)}")
        if "role" in fields and fields["role"] not in ROLES:
            raise AuthError(f"role must be one of {', '.join(ROLES)}")
        if "email" in fields:
            fields["email"] = self._check_email(fields["email"])
        username = username.strip().lower()
        with self.db.begin() as conn:
            row = self._row(conn, username)
            if not row:
                raise AuthError(f"no user {username}")
            if fields.get("email") and conn.execute(select(db.users.c.username).where(and_(
                    db.users.c.email == fields["email"], db.users.c.username != username))).first():
                raise AuthError("another account already uses that email")
            if "household_id" in fields and not conn.execute(select(db.households.c.id).where(
                    db.households.c.id == fields["household_id"])).first():
                raise AuthError("no such household")
            demoting = fields.get("role", "superadmin") != "superadmin" or fields.get("disabled")
            if demoting and username == self.protected():
                raise AuthError(f"{username} is the protected admin account; it always stays a superadmin")
            # A superadmin who's already turned off doesn't count toward the active ones it would leave.
            if row["role"] == "superadmin" and not row["disabled"] and demoting and self._active_superadmins(conn) <= 1:
                raise AuthError("can't demote or disable the last superadmin")
            conn.execute(update(db.users).where(db.users.c.username == username).values(**fields))
        if fields.get("disabled"):
            self.revoke_user_sessions(username)
        return self.get(username)

    def set_prefs(self, username: str, changes: dict) -> dict:
        for k, v in changes.items():
            if k not in PREFS:
                raise AuthError(f"unknown setting {k}")
            if not PREFS[k][1](v):
                raise AuthError(PREF_HINTS.get(k, "That value isn't allowed."))
        with self.db.begin() as conn:
            row = self._row(conn, username)
            prefs = {**default_prefs(), **(row["prefs"] or {}), **changes}
            conn.execute(update(db.users).where(db.users.c.username == username).values(prefs=prefs))
        return prefs

    # --- to-do app connection (secret is sealed by the Vault) ----------------
    def connection(self, username: str) -> dict | None:
        """Includes the sealed secret: server-side use only."""
        with self.db.connect() as conn:
            row = self._row(conn, username)
        return row["connection"] if row else None

    def set_connection(self, username: str, connection: dict | None) -> None:
        with self.db.begin() as conn:
            if not self._row(conn, username):
                raise AuthError(f"no user {username}")
            conn.execute(update(db.users).where(db.users.c.username == username).values(connection=connection))

    def calendars(self, username: str) -> list[dict]:
        """Includes the sealed secrets: server-side use only."""
        with self.db.connect() as conn:
            row = self._row(conn, username)
        return list(row["calendars"] or []) if row else []

    def set_calendars(self, username: str, calendars: list[dict]) -> None:
        with self.db.begin() as conn:
            if not self._row(conn, username):
                raise AuthError(f"no user {username}")
            conn.execute(update(db.users).where(db.users.c.username == username).values(calendars=calendars or None))

    def owner(self, household_id: str | None = None) -> str | None:
        """Self-hosted (no household given): the oldest active superadmin, whose to-do app Home
        Assistant and the CLI use. With a household: its oldest active manager."""
        if not household_id:
            chosen = self._flag("_print_owner")
            u = self.get(chosen) if chosen else None
            if u and not u["disabled"]:
                return chosen
        roles = ("admin", "superadmin") if household_id else ("superadmin",)
        q = (select(db.users.c.username).where(and_(db.users.c.role.in_(roles), db.users.c.disabled.is_(False)))
             .order_by(db.users.c.created, db.users.c.username))
        if household_id:
            q = q.where(db.users.c.household_id == household_id)
        with self.db.connect() as conn:
            row = conn.execute(q).first()
        return row[0] if row else None

    def remove_user(self, username: str) -> None:
        username = username.strip().lower()
        with self.db.begin() as conn:
            row = self._row(conn, username)
            if not row:
                raise AuthError(f"no user {username}")
            if username == self.protected():
                raise AuthError(f"{username} is the protected admin account and can't be removed")
            if row["role"] == "superadmin" and not row["disabled"] and self._active_superadmins(conn) <= 1:
                raise AuthError("can't remove the last superadmin")
            conn.execute(delete(db.users).where(db.users.c.username == username))
        self.revoke_user_sessions(username)

    def removable(self, username: str) -> bool:
        """Whether remove_user would work: not the protected admin, nor the last active superadmin."""
        username = username.strip().lower()
        if username == self.protected():
            return False
        with self.db.connect() as conn:
            row = self._row(conn, username)
            if not row:
                return False
            return not (row["role"] == "superadmin" and not row["disabled"] and self._active_superadmins(conn) <= 1)

    def password_ok(self, username: str, password: str) -> bool:
        """Right password, whether or not the account is turned off."""
        user = None
        with self.db.connect() as conn:
            user = self._row(conn, username)
        # Hash even for unknown users so response time doesn't reveal which names exist.
        salt = bytes.fromhex(user["salt"]) if user else b"\0" * 16
        digest = _hash_password(password, salt)
        return bool(user) and hmac.compare_digest(digest, user["hash"])

    def verify(self, username: str, password: str) -> bool:
        if not self.password_ok(username, password):
            return False
        return not self.get(username)["disabled"]

    def record_consent(self, username: str, version: int | None, terms: int | None = None) -> None:
        """`version`: the data notice they accepted. `terms`: the Terms of Use and Privacy Policy
        version they agreed to. Either can be None to leave it as it was."""
        with self.db.begin() as conn:
            row = self._row(conn, username)
            consent = dict((row["consent"] if row else None) or {})
            now = int(time.time())
            if version is not None:
                consent.update(version=version, at=now)
            if terms is not None:
                consent.update(terms=terms, terms_at=now)
            conn.execute(update(db.users).where(db.users.c.username == username).values(consent=consent))

    def touch_login(self, username: str) -> None:
        with self.db.begin() as conn:
            conn.execute(update(db.users).where(db.users.c.username == username).values(last_login=int(time.time())))

    def mark_reset(self, username: str, by: str) -> None:
        """An admin set a one-time password: remembered so the person is told who did it, and when."""
        with self.db.begin() as conn:
            conn.execute(update(db.users).where(db.users.c.username == username.strip().lower()).values(
                reset_by=by, reset_at=int(time.time())))

    # --- support access (product privacy) --------------------------------------
    def grant_support(self, username: str, hours: int) -> int | None:
        """The person lets Next Box staff see their tickets and activity for a while (0 = revoke)."""
        until = int(time.time()) + hours * 3600 if hours > 0 else None
        with self.db.begin() as conn:
            conn.execute(update(db.users).where(db.users.c.username == username).values(support_until=until))
        return until

    def support_active(self, username: str) -> bool:
        u = self.get(username)
        return bool(u and u.get("support_until") and u["support_until"] > time.time())

    @staticmethod
    def temp_password() -> str:
        return "-".join(secrets.token_hex(2) for _ in range(3))  # e.g. 3f9a-07c2-b1e4

    # --- rate limit -------------------------------------------------------
    # Each bucket (a client address, or a key like "user:..." or "pw:mike") keeps (time, tag) for
    # recent failures. The tag says whose sign-in failed, so signing in to your own account doesn't
    # wipe the record of guesses at someone else's password from the same address.
    def failure_count(self, client: str, window: int = 300) -> int:
        now = time.time()
        with self._failures_lock:
            recent = [f for f in self._failures.get(client, []) if now - f[0] < window]
            if recent:
                self._failures[client] = recent
            else:
                self._failures.pop(client, None)
            return len(recent)

    def too_many_failures(self, client: str, limit: int = 5, window: int = 300) -> bool:
        return self.failure_count(client, window) >= limit

    def record_failure(self, client: str, tag: str = "?") -> None:
        now = time.time()
        with self._failures_lock:
            if len(self._failures) > 10_000:  # forget buckets nobody has touched for a day
                for k in [k for k, v in self._failures.items() if not v or now - v[-1][0] > 86400]:
                    del self._failures[k]
            self._failures.setdefault(client, []).append((now, tag))

    def clear_failures(self, client: str, tag: str | None = None) -> None:
        """Forget a bucket's failures, or only the ones with this tag."""
        with self._failures_lock:
            left = [] if tag is None else [f for f in self._failures.get(client, []) if f[1] != tag]
            if left:
                self._failures[client] = left
            else:
                self._failures.pop(client, None)

    # --- sessions ---------------------------------------------------------
    def create_session(self, username: str, ip: str = "", agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.db.begin() as conn:
            conn.execute(delete(db.sessions).where(db.sessions.c.expires < now))
            conn.execute(insert(db.sessions).values(token_hash=_token_hash(token), username=username.lower(),
                                                    created=now, expires=now + SESSION_DAYS * 86400,
                                                    ip=ip[:64], agent=agent[:200], last_used=now))
        return token

    def session_user(self, token: str | None) -> str | None:
        if not token:
            return None
        h = _token_hash(token)
        now = time.time()
        with self.db.connect() as conn:
            s = conn.execute(select(db.sessions).where(db.sessions.c.token_hash == h)).mappings().first()
            if not s or s["expires"] < now:
                return None
            user = self._row(conn, s["username"])
        if not user or user["disabled"]:
            return None
        changes = {}
        if s["expires"] - now < (SESSION_DAYS - 1) * 86400:
            # Sliding expiry: anyone who uses the app at least monthly stays signed in.
            changes["expires"] = int(now) + SESSION_DAYS * 86400
        if not s.get("last_used") or now - s["last_used"] > SESSION_TOUCH:
            changes["last_used"] = int(now)  # "last used" in Signed-in devices, every 10 minutes at most
        if changes:
            with self.db.begin() as conn:
                conn.execute(update(db.sessions).where(db.sessions.c.token_hash == h).values(**changes))
        if not user["last_seen"] or now - user["last_seen"] > 3600:
            # "Last active": phones stay signed in for weeks, so sign-ins alone undercount. Hourly at most.
            with self.db.begin() as conn:
                conn.execute(update(db.users).where(db.users.c.username == s["username"]).values(
                    last_seen=int(now)))
        return s["username"]

    def sessions_for(self, username: str, current: str | None = None) -> list[dict]:
        cur = _token_hash(current) if current else None
        with self.db.connect() as conn:
            rows = conn.execute(select(db.sessions).where(and_(
                db.sessions.c.username == username, db.sessions.c.expires > int(time.time())))
                .order_by(db.sessions.c.created.desc())).mappings().all()
        out = [{"id": r["token_hash"][:12], "created": r["created"], "last_used": r["last_used"] or r["created"],
                "ip": r["ip"], "agent": r["agent"], "current": r["token_hash"] == cur} for r in rows]
        # This device first, then the most recently used.
        return sorted(out, key=lambda s: (not s["current"], -(s["last_used"] or 0)))

    def end_session_id(self, username: str, sid: str, keep: str | None = None) -> None:
        """Sign out one of your devices by the short id sessions_for shows (never `keep`, this one).
        KeyError when it isn't one of yours (or is already signed out)."""
        if not re.fullmatch(r"[0-9a-f]{12}", sid or ""):
            raise KeyError(sid)
        with self.db.begin() as conn:
            found = [r[0] for r in conn.execute(select(db.sessions.c.token_hash).where(and_(
                db.sessions.c.username == username, db.sessions.c.token_hash.startswith(sid))))]
            if not found:
                raise KeyError(sid)
            if keep and _token_hash(keep) in found:
                raise AuthError("That's this device. Use Sign out instead.")
            conn.execute(delete(db.sessions).where(db.sessions.c.token_hash.in_(found)))

    def end_session(self, token: str | None) -> None:
        if not token:
            return
        with self.db.begin() as conn:
            conn.execute(delete(db.sessions).where(db.sessions.c.token_hash == _token_hash(token)))

    def revoke_user_sessions(self, username: str, keep: str | None = None) -> int:
        q = delete(db.sessions).where(db.sessions.c.username == username)
        if keep:
            q = q.where(db.sessions.c.token_hash != _token_hash(keep))
        with self.db.begin() as conn:
            return conn.execute(q).rowcount
