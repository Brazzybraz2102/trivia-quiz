"""Anonymous feedback.

Two files, on purpose:

- feedback.json            the public board: what people said, scrubbed, dated by day only.
- feedback_identities.json who said it (username, IP, device, any error details they attached).
                           The ONLY place a person is linked to their feedback. chmod 600,
                           read only to show "yours" to the author and for a superadmin reveal.

Nothing else (events log, debug bundles, server access log) records who sent feedback.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import time
from collections.abc import Iterable, Iterator
from datetime import date
from pathlib import Path

TYPES = ("bug", "confusing", "idea", "praise", "other")
STATUSES = ("new", "seen", "planned", "fixed", "wontfix", "hidden")
GUIDED_FIELDS = ("trying", "happened", "expected")
MAX_TEXT = 2000
PER_HOUR = 10  # per person (or per IP before sign-in)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{6,}\d)(?!\w)")
_HANDLE = re.compile(r"(?<!\w)@\w{2,}")
_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def scrub(text: str, names: Iterable[str] = ()) -> str:
    """Remove things that identify someone: contact details, links, addresses, account names."""
    text = (text or "").strip()[:MAX_TEXT]
    text = _EMAIL.sub("[email]", text)
    text = _URL.sub("[link]", text)
    text = _IP.sub("[address]", text)
    text = _PHONE.sub("[phone]", text)
    text = _HANDLE.sub("[someone]", text)
    text = _TOKEN.sub("[removed]", text)
    for name in sorted(set(names), key=len, reverse=True):
        if len(name) >= 2:
            text = re.sub(rf"(?<![\w.]){re.escape(name)}(?![\w])", "[someone]", text, flags=re.I)
    return text


class FeedbackError(ValueError):
    pass


class Feedback:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_path = self.root / "feedback.json"
        self.identity_path = self.root / "feedback_identities.json"

    # --- storage -------------------------------------------------------------
    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        with open(self.root / ".feedback.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _load(self, path: Path) -> dict:
        return json.loads(path.read_text()) if path.exists() else {}

    def _save(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    # --- submit --------------------------------------------------------------
    def submit(self, *, mode: str, kind: str, page: str, rating: int | None, fields: dict,
               message: str, who: str | None, ip: str, agent: str, names: Iterable[str],
               debug: dict | None = None, today: date | None = None,
               enforce_limit: bool = True, ts: float | None = None) -> dict:
        if mode not in ("guided", "open"):
            raise FeedbackError("mode must be guided or open")
        kind = kind if kind in TYPES else "other"
        if rating is not None and not (isinstance(rating, int) and 1 <= rating <= 5):
            raise FeedbackError("rating is 1–5")
        names = list(names) + ([who] if who else [])  # always scrub the sender's own name
        if mode == "guided":
            body = {k: scrub(fields.get(k, ""), names) for k in GUIDED_FIELDS}
            if not any(body.values()):
                raise FeedbackError("Fill in at least one box.")
        else:
            body = {"message": scrub(message, names)}
            if not body["message"]:
                raise FeedbackError("Write something first.")
        with self._lock():
            identities = self._load(self.identity_path)
            sender = who or f"ip:{ip}"
            hour_ago = time.time() - 3600
            if enforce_limit and sum(
                    1 for i in identities.values() if i["sender"] == sender and i["ts"] > hour_ago) >= PER_HOUR:
                raise FeedbackError("That's a lot of feedback this hour. Please try again later.")
            board = self._load(self.public_path)
            fid = secrets.token_hex(4)
            while fid in board:
                fid = secrets.token_hex(4)
            item = {"id": fid, "date": (today or date.today()).isoformat(),  # day only: times correlate
                    "mode": mode, "type": kind, "page": re.sub(r"[^a-z_-]", "", page.lower())[:30],
                    "rating": rating, **body, "status": "new", "reply": "", "reply_date": None}
            board[fid] = item
            identities[fid] = {"sender": sender, "user": who, "ts": round(ts or time.time()), "ip": ip,
                               "agent": agent[:200], "debug": debug or None}
            self._save(self.public_path, board)
            self._save(self.identity_path, identities)
        return item

    # --- read ----------------------------------------------------------------
    def board(self, viewer: str | None, *, include_hidden: bool = False, kind: str | None = None,
              status: str | None = None) -> list[dict]:
        """Anonymous items. `mine` tells the viewer which are theirs; nobody else learns it."""
        board = self._load(self.public_path)
        mine = {fid for fid, i in self._load(self.identity_path).items() if viewer and i["user"] == viewer}
        out = []
        for item in board.values():
            if item["status"] == "hidden" and not include_hidden and item["id"] not in mine:
                continue
            if kind and item["type"] != kind:
                continue
            if status and item["status"] != status:
                continue
            out.append({**item, "mine": item["id"] in mine})
        out.sort(key=lambda i: (i["date"], i["id"]), reverse=True)
        return out

    def is_author(self, fid: str, viewer: str) -> bool:
        ident = self._load(self.identity_path).get(fid)
        return bool(ident and viewer and ident["user"] == viewer)

    def identity(self, fid: str) -> dict:
        ident = self._load(self.identity_path).get(fid)
        if not ident:
            raise KeyError(fid)
        return ident

    # --- change --------------------------------------------------------------
    def respond(self, fid: str, status: str | None = None, reply: str | None = None,
                names: Iterable[str] = (), today: date | None = None) -> dict:
        if status is not None and status not in STATUSES:
            raise FeedbackError(f"status must be one of {', '.join(STATUSES)}")
        with self._lock():
            board = self._load(self.public_path)
            if fid not in board:
                raise KeyError(fid)
            if status is not None:
                board[fid]["status"] = status
            if reply is not None:
                board[fid]["reply"] = scrub(reply, names)
                board[fid]["reply_date"] = (today or date.today()).isoformat() if reply.strip() else None
            self._save(self.public_path, board)
            return board[fid]

    def withdraw(self, fid: str) -> None:
        with self._lock():
            board, identities = self._load(self.public_path), self._load(self.identity_path)
            if fid not in board:
                raise KeyError(fid)
            board.pop(fid)
            identities.pop(fid, None)
            self._save(self.public_path, board)
            self._save(self.identity_path, identities)

    def forget_user(self, username: str) -> int:
        """When an account is deleted, its feedback stays but nothing links it to anyone."""
        with self._lock():
            identities = self._load(self.identity_path)
            gone = [fid for fid, i in identities.items() if i["user"] == username]
            for fid in gone:
                identities.pop(fid)
            if gone:
                self._save(self.identity_path, identities)
            return len(gone)
