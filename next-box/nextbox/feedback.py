"""Anonymous feedback.

Two tables, on purpose:

- feedback             the public board: what people said, scrubbed, dated by day only.
- feedback_identities  who said it (username, IP, device, any error details they attached).
                       The ONLY place a person is linked to their feedback. Read only to show
                       "yours" to the author and for an audited staff reveal.

Nothing else (events log, debug bundles, server access log) records who sent feedback.
"""
from __future__ import annotations

import re
import secrets
import time
from collections.abc import Iterable
from datetime import date

from sqlalchemy import and_, delete, func, insert, select, update

from . import db

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
    def __init__(self, root):
        self.db = db.database(root)

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
        sender = who or f"ip:{ip}"
        with self.db.begin() as conn:
            if enforce_limit:
                recent = conn.execute(select(func.count()).select_from(db.feedback_identities).where(and_(
                    db.feedback_identities.c.sender == sender,
                    db.feedback_identities.c.ts > time.time() - 3600))).scalar()
                if recent >= PER_HOUR:
                    raise FeedbackError("That's a lot of feedback this hour. Please try again later.")
            fid = secrets.token_hex(4)
            while conn.execute(select(db.feedback.c.id).where(db.feedback.c.id == fid)).first():
                fid = secrets.token_hex(4)
            seq = (conn.execute(select(func.max(db.feedback.c.n))).scalar() or 0) + 1  # order, not time
            day = (today or date.today()).isoformat()  # day only: exact times correlate with activity
            item = {"id": fid, "n": seq, "date": day, "mode": mode, "type": kind,
                    "page": re.sub(r"[^a-z_-]", "", page.lower())[:30], "rating": rating, **body,
                    "status": "new", "reply": "", "reply_date": None}
            conn.execute(insert(db.feedback).values(id=fid, n=seq, date=day, item=item))
            conn.execute(insert(db.feedback_identities).values(
                id=fid, sender=sender[:80], user=who, ts=round(ts or time.time()), ip=ip[:64],
                agent=agent[:200], debug=debug or None))
        return item

    # --- read ----------------------------------------------------------------
    def board(self, viewer: str | None, *, include_hidden: bool = False, kind: str | None = None,
              status: str | None = None, limit: int = 300) -> list[dict]:
        """Anonymous items. `mine` tells the viewer which are theirs; nobody else learns it."""
        with self.db.connect() as conn:
            rows = [r[0] for r in conn.execute(select(db.feedback.c.item).order_by(
                db.feedback.c.date.desc(), db.feedback.c.n.desc()).limit(limit * 2))]
            mine = set()
            if viewer:
                mine = {r[0] for r in conn.execute(select(db.feedback_identities.c.id).where(
                    db.feedback_identities.c.user == viewer))}
        out = []
        for item in rows:
            if item["status"] == "hidden" and not include_hidden and item["id"] not in mine:
                continue
            if kind and item["type"] != kind:
                continue
            if status and item["status"] != status:
                continue
            out.append({**item, "mine": item["id"] in mine})
        return out[:limit]

    def is_author(self, fid: str, viewer: str) -> bool:
        if not viewer:
            return False
        with self.db.connect() as conn:
            return bool(conn.execute(select(db.feedback_identities.c.id).where(and_(
                db.feedback_identities.c.id == fid, db.feedback_identities.c.user == viewer))).first())

    def identity(self, fid: str) -> dict:
        with self.db.connect() as conn:
            row = conn.execute(select(db.feedback_identities).where(db.feedback_identities.c.id == fid)).mappings().first()
        if not row:
            raise KeyError(fid)
        return {k: row[k] for k in ("sender", "user", "ts", "ip", "agent", "debug")}

    # --- change --------------------------------------------------------------
    def respond(self, fid: str, status: str | None = None, reply: str | None = None,
                names: Iterable[str] = (), today: date | None = None) -> dict:
        if status is not None and status not in STATUSES:
            raise FeedbackError(f"status must be one of {', '.join(STATUSES)}")
        with self.db.begin() as conn:
            row = conn.execute(select(db.feedback.c.item).where(db.feedback.c.id == fid)).first()
            if not row:
                raise KeyError(fid)
            item = dict(row[0])
            if status is not None:
                item["status"] = status
            if reply is not None:
                item["reply"] = scrub(reply, names)
                item["reply_date"] = (today or date.today()).isoformat() if reply.strip() else None
            conn.execute(update(db.feedback).where(db.feedback.c.id == fid).values(item=item))
        return item

    def withdraw(self, fid: str) -> None:
        with self.db.begin() as conn:
            if not conn.execute(delete(db.feedback).where(db.feedback.c.id == fid)).rowcount:
                raise KeyError(fid)
            conn.execute(delete(db.feedback_identities).where(db.feedback_identities.c.id == fid))

    def forget_user(self, username: str) -> int:
        """When an account is deleted, its feedback stays but nothing links it to anyone."""
        with self.db.begin() as conn:
            return conn.execute(delete(db.feedback_identities).where(
                db.feedback_identities.c.user == username)).rowcount
