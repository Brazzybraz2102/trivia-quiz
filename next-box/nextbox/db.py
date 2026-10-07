"""The database. SQLite in the data dir by default; PostgreSQL when DATABASE_URL is set.

Every store (accounts, tickets, events, feedback, printers, settings) takes either a data-dir path
or a URL and gets the shared engine for it from `database()`.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from sqlalchemy import (JSON, BigInteger, Boolean, Column, Float, Index, Integer, LargeBinary, MetaData,
                        String, Table, Text, create_engine, event)
from sqlalchemy.engine import Engine

metadata = MetaData()

households = Table(
    "households", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(80), nullable=False),
    Column("created", BigInteger, nullable=False),
    Column("plan", String(20), nullable=False, default="free"),
)

users = Table(
    "users", metadata,
    Column("username", String(32), primary_key=True),
    Column("email", String(254), unique=True, nullable=True),
    Column("household_id", String(32), nullable=False, index=True),
    Column("role", String(16), nullable=False, default="user"),       # user | admin (household owner) | superadmin (staff)
    Column("disabled", Boolean, nullable=False, default=False),
    Column("beta", Boolean, nullable=False, default=False),
    Column("debug", Boolean, nullable=False, default=False),
    Column("must_change", Boolean, nullable=False, default=False),
    Column("salt", String(64), nullable=False),
    Column("hash", String(128), nullable=False),
    Column("created", BigInteger, nullable=False),
    Column("last_login", BigInteger, nullable=True),
    Column("prefs", JSON, nullable=False, default=dict),
    Column("connection", JSON, nullable=True),      # to-do app; the secret inside is sealed by the Vault
    Column("consent", JSON, nullable=True),
    Column("support_until", BigInteger, nullable=True),  # time-limited support access granted by the person
)

sessions = Table(
    "sessions", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("username", String(32), nullable=False, index=True),
    Column("created", BigInteger, nullable=False),
    Column("expires", BigInteger, nullable=False),
    Column("ip", String(64), nullable=False, default=""),
    Column("agent", String(200), nullable=False, default=""),
)

invites = Table(
    "invites", metadata,
    Column("code", String(32), primary_key=True),
    Column("household_id", String(32), nullable=False, index=True),
    Column("created_by", String(32), nullable=False),
    Column("created", BigInteger, nullable=False),
    Column("expires", BigInteger, nullable=False),
    Column("used_by", String(32), nullable=True),
)

tickets = Table(
    "tickets", metadata,
    Column("id", String(16), primary_key=True),
    Column("household_id", String(32), nullable=False),
    Column("by", String(32), nullable=False),
    Column("created_at", String(19), nullable=False),
    Column("record", JSON, nullable=False),
    Column("png", LargeBinary, nullable=True),
    Index("ix_tickets_by_time", "by", "created_at"),
    Index("ix_tickets_household_time", "household_id", "created_at"),
)

scans = Table(
    "scans", metadata,
    Column("id", String(16), primary_key=True),
    Column("household_id", String(32), nullable=False),
    Column("by", String(32), nullable=False),
    Column("created_at", String(19), nullable=False),
    Column("record", JSON, nullable=False),
    Index("ix_scans_by_time", "by", "created_at"),
)

auto_guard = Table(
    "auto_guard", metadata,
    Column("key", String(80), primary_key=True),   # "<scope>:real" or "<scope>:dry"
    Column("day", String(10), nullable=False),
)

events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", Float, nullable=False, index=True),
    Column("user", String(64), nullable=False, index=True),
    Column("household_id", String(32), nullable=True),
    Column("kind", String(16), nullable=False, index=True),
    Column("action", String(64), nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("detail", JSON, nullable=True),
    Column("error", Text, nullable=True),
    Column("trace", Text, nullable=True),
)

server_settings = Table(
    "server_settings", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", JSON, nullable=True),
)

printers = Table(
    "printers", metadata,
    Column("id", String(16), primary_key=True),
    Column("household_id", String(32), nullable=False, index=True),
    Column("position", Integer, nullable=False, default=0),
    Column("config", JSON, nullable=False),
)

# Feedback: the board is anonymous. feedback_identities is the ONLY place that links a person to a post.
feedback = Table(
    "feedback", metadata,
    Column("id", String(16), primary_key=True),
    Column("n", Integer, nullable=False, index=True),
    Column("date", String(10), nullable=False),
    Column("item", JSON, nullable=False),
)

feedback_identities = Table(
    "feedback_identities", metadata,
    Column("id", String(16), primary_key=True),
    Column("sender", String(80), nullable=False, index=True),
    Column("user", String(32), nullable=True, index=True),
    Column("ts", BigInteger, nullable=False),
    Column("ip", String(64), nullable=False, default=""),
    Column("agent", String(200), nullable=False, default=""),
    Column("debug", JSON, nullable=True),
)

_engines: dict[str, Engine] = {}
_lock = threading.Lock()


def url_for(root_or_url) -> str:
    if isinstance(root_or_url, str) and "://" in root_or_url:
        return root_or_url
    env = os.environ.get("DATABASE_URL")
    if env:
        return env
    root = Path(root_or_url)
    root.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{root / 'nextbox.db'}"


def database(root_or_url) -> Engine:
    """The shared engine for a data dir (SQLite) or a URL. Creates tables on first use."""
    url = url_for(root_or_url)
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]  # Fly/Heroku style
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    with _lock:
        engine = _engines.get(url)
        if engine is None:
            if url.startswith("sqlite"):
                engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False})

                @event.listens_for(engine, "connect")
                def _pragmas(conn, _):
                    cur = conn.cursor()
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA busy_timeout=30000")
                    cur.close()
                path = Path(url.removeprefix("sqlite:///"))
            else:
                engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)
                path = None
            metadata.create_all(engine)
            if path is not None and path.exists():
                os.chmod(path, 0o600)
            _engines[url] = engine
        return engine


def reset_engines() -> None:
    """Tests: forget cached engines (each test gets a fresh temp data dir)."""
    with _lock:
        for e in _engines.values():
            e.dispose()
        _engines.clear()
