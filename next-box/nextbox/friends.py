"""Friends and notes (SPEC §34): add friends, send them a note, a doodle, a photo or a heart, and swipe
through what they sent you. A note prints only when you tap Print and confirm.

API CONTRACT (server.py). Every endpoint needs a signed-in person (cookie or bearer token; the shared
X-NextBox-Key gets 403). Errors are {"detail": "<a plain sentence to show as it is>"} with 400 (fix what
you sent), 403, 404 (not there, or not yours), 409, 413 (too big), 415 (not a picture), 429 (a limit; wait).
Personal answers are sent Cache-Control: no-store. Times are Unix seconds. A person is shown as
{"username": "lee", "name": "Lee"} (`name`: their display name, else the username).

Friends
- GET  /friends -> {"friends": [{username, name, since}], "incoming": [{username, name, at}],
       "outgoing": [{username, name, at}], "blocked": [{username, name, at}], "code": "K7QW2MZP4X",
       "link": "https://<public url>/app/#friend=K7QW2MZP4X", "receipts": true}
       `outgoing` shows only the username that was typed (name == username), whether or not that account
       exists, so the list can't be used to find out who has an account; `blocked` too, unless you could
       already see their name (a friend, a request you saw, a note they sent you). Requests fade after 30 days.
- POST /friends/code/rotate -> {"code", "link"}: a new friend link; the old one stops working.
- GET  /friends/code/qr.png -> image/png: a QR code of your link (to show on screen).
- GET  /friends/code/{code} -> {"username", "name", "status"}: who a friend link belongs to, before adding.
       status: "you" | "friends" | "requested" (you asked) | "asked_you" (they asked you) | "none".
       404 "That friend link doesn't work. Ask them for a new one." 60 lookups an hour.
- POST /friends/add {"username": "lee"} or {"code": "K7QW2MZP4X"} -> {"status", "message", "friends": <GET /friends>}
       status "requested": {"message": "If that name is right, they'll see your request."} (by username: the
       SAME answer whether or not the name exists, is blocked, or already asked) or "Request sent. They'll
       see it in Friends." (by code). status "friends": "You're friends now." (they had asked you too) or
       "You're already friends." 400 "That's you." / "You blocked <name>. Unblock them first." /
       "That isn't a username: ...". 429 after 20 in 24 hours (taken-back ones count). Asking again after the
       account was made reaches it (a request typed before it existed never does); after a no, it doesn't.
- POST /friends/{username}/accept -> <GET /friends>.  404 "That request isn't there anymore."
- POST /friends/{username}/decline -> <GET /friends>. (They aren't told: their request just fades.)
- DELETE /friends/{username} -> <GET /friends>: remove a friend, or take back a request you sent (it stays,
       "withdrawn", shown to nobody, until it fades).
- POST /friends/{username}/block -> <GET /friends>: no more requests or notes from them; ends the friendship,
       and their notes leave your feed. Only someone who is a friend, asked to be one, or sent you a note. A
       request they sent you stays on their side, unanswered (as after Decline): they aren't told.
- DELETE /friends/{username}/block -> <GET /friends>: unblock (they aren't friends again until one asks).

Notes  (kinds: "note" text ≤ 280 and ≤ 12 lines + a color from NOTE_COLORS; "draw" a doodle; "photo" +
        caption ≤ 140; "heart" one tap. Blank lines in a row are kept to one.)
- POST /notes {"to": ["lee", "ana"], "kind": "note"|"heart", "text": "...", "color": "yellow"}
       -> {"sent": 2, "ids": [...], "message": "Sent to Lee and Ana."}  To friends only, 20 at most.
- POST /notes/image (multipart) to=lee&to=ana (or "lee,ana"), kind=draw|photo, caption=..., image=<file>
       -> the same. A photo (JPEG, PNG, WebP, GIF, HEIC with the heic extra) up to 15 MB is re-encoded here:
       no EXIF or location, 1600 px at most. A doodle (PNG/JPEG/WebP) up to 3 MB becomes black ink on white.
       413 too big, 415 not a picture (or HEIC without the extra: scan.HEIC_HELP). Recipients and limits are
       checked before the picture is read; it's decoded in a worker thread, two at a time (503 when busy).
- POST /notes/preview {"kind", "text", "color"} and POST /notes/preview/image (multipart kind, caption,
       image) -> image/png: "how it prints", the black-and-white label, from you, today. Nothing is kept.
       300 previews an hour, 120 of them photos.
- GET  /notes/feed?limit=30&cursor= -> {"notes": [FEED], "next": "<cursor>"|null, "unseen": N}
       Unseen notes first, then the rest, each part newest first. Pass `next` back as `cursor` for more;
       a note marked seen meanwhile may come round again: skip ids you already have.
       FEED = {id, from: {username, name}, kind, text, color, image: "/notes/<id>/image"|null,
               label: "/notes/<id>/label.png", created, seen, printed, kept, reaction,
               expires (when it goes, unless kept; null when kept), can_reply (still friends)}
- GET  /notes/count -> {"unseen": N, "requests": M}  (the tab's badge: unseen notes, friend requests)
- GET  /notes/sent?limit=30&cursor= -> {"notes": [SENT], "next"}
       SENT = {id, to: {username, name}, kind, text, color, image, created, reaction,
               status: "delivered" | "seen" | "printed"}  ("delivered" only, when they turned receipts off;
               and with reaction null for someone you blocked). Never when it was seen or printed, or kept.
- GET  /notes/{id}/image -> the doodle (PNG) or photo (JPEG): its sender or recipient only.
- POST /notes/{id}/seen -> {"id", "seen"}         (recipient; the app sends it after ~1 s at 60% visible)
- POST /notes/{id}/react {"reaction": "❤️"|null} -> FEED item  (REACTIONS; null takes it back)
- POST /notes/{id}/keep {"keep": true} -> FEED item  (kept notes aren't deleted after 30 days)
- DELETE /notes/{id}?confirm=true -> {"ok": true}  "Delete for me" (from your feed, or from your Sent list)
- GET  /notes/{id}/label.png[?download=1] -> image/png: how it comes out of YOUR printer (die-cut labels
       and all); download=1 is Save label image (attachment). Recipient or sender.
- GET  /notes/{id}/paper.pdf?paper=letter|a4|a5|half_letter -> Print on paper (any printer).
- POST /notes/{id}/print {"confirm": true, "dry_run": false} -> {status: "printed"|"dry_run"|"failed",
       dry_run, note, printer, prints_here}. Recipient only, and only with confirm: true, which the app
       sends after "Send this to the printer?" (400 "Tap Print, then say yes to send it to the printer.").
       Your own household's printer, the same dry-run rules and pause as every ticket; no printer here:
       a preview, with jobs.PRINT_AWAY or the no-printer note, so the app offers Print on paper / Save
       label image instead. Never saved as a ticket.
- PATCH /settings {"friend_receipts": false}: "Tell friends when I've seen or printed their notes".

Limits, in any 24 hours: 20 friend requests; 30 notes to one friend and 100 in all; 20 friends per send.
Privacy: notes live only in friend_notes (masked in `nextbox db sql`); no admin, superadmin, usage, debug
bundle or event shows them or who your friends are (events carry counts only). Unsaved notes are deleted
30 days after they arrive (startup upkeep, and reading the feed, the count or Sent; no timer), and no read
shows one past that. Download my data has your friends and your own notes: received ones in full, sent
ones as SENT shows them. Deleting your account deletes them all.
"""
from __future__ import annotations

import io
import re
import secrets
import threading
import time

from sqlalchemy import and_, case, delete, func, insert, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from . import db
from .render import NOTE_COLORS

KINDS = ("note", "draw", "photo", "heart")
MAX_TEXT = 280                # a note's words
MAX_LINES = 12                # and its lines (blank lines in a row count as one): a label stays a label
MAX_CAPTION = 140             # a photo's or doodle's caption
REACTIONS = ("❤️", "😂", "😮", "😢", "👍", "🔥")
MAX_TO = 20                   # friends per send
KEEP_DAYS = 30                # unsaved notes go this long after they arrive
REQUEST_DAYS = 30             # a request nobody answered fades after this
REQUESTS_PER_DAY = 20
NOTES_PER_FRIEND_PER_DAY = 30
NOTES_PER_DAY = 100
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I
CODE_LENGTH = 10
PHOTO_UPLOAD = 15 * 1024 * 1024   # what a phone sends
PHOTO_EDGE = 1600                 # kept at most this big
PHOTO_BYTES = 450_000             # and about this many bytes
DOODLE_UPLOAD = 3 * 1024 * 1024
DOODLE_EDGE = 1200
DOODLE_PIXELS = 4_000_000
PHOTO_FORMATS = {"JPEG", "MPO", "PNG", "WEBP", "GIF", "HEIF", "HEIC", "AVIF"}
DOODLE_FORMATS = {"PNG", "JPEG", "WEBP"}
DAY = 86400

SENT_BY_NAME = "If that name is right, they'll see your request."
SENT_BY_CODE = "Request sent. They'll see it in Friends."
NOW_FRIENDS = "You're friends now."
ALREADY = "You're already friends."
NO_REQUEST = "That request isn't there anymore."
BAD_CODE = "That friend link doesn't work. Ask them for a new one."
NOT_A_NAME = "That isn't a username: usernames are 2 to 32 letters, numbers, dots, dashes or underscores."
TOO_MANY_REQUESTS = "That's a lot of friend requests in the last 24 hours. Try again later."
NOT_FOUND = "That note isn't here anymore."
CANT_BLOCK = "You can block people who are your friends, asked to be, or sent you a note."
_NAME = re.compile(r"^[a-z0-9_.-]{2,32}$")


class FriendError(Exception):
    """Something to tell the person, with the HTTP status that goes with it."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def _now() -> int:
    return int(time.time())


def _pair(x: str, y: str) -> tuple[str, str]:
    return (x, y) if x < y else (y, x)


def _pair_is(x: str, y: str):
    a, b = _pair(x, y)
    return and_(db.friendships.c.a == a, db.friendships.c.b == b)


def clean_name(name) -> str:
    return str(name or "").strip().lstrip("@").strip().lower()


def clean_code(code) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())[:32]


def names(conn, usernames) -> dict[str, str]:
    """{username: what to call them}: their display name, else the username."""
    wanted = sorted({u for u in usernames if u})
    out = {u: u for u in wanted}
    if wanted:
        for name, prefs in conn.execute(select(db.users.c.username, db.users.c.prefs)
                                        .where(db.users.c.username.in_(wanted))):
            shown = str((prefs or {}).get("display_name") or "").strip()
            out[name] = shown or name
    return out


def person(username: str, shown: dict[str, str]) -> dict:
    return {"username": username, "name": shown.get(username, username)}


def _exists(conn, username: str) -> bool:
    return conn.execute(select(db.users.c.username).where(db.users.c.username == username)).first() is not None


def _blocked(conn, blocker: str, blocked: str) -> bool:
    return conn.execute(select(db.blocks.c.blocker).where(
        db.blocks.c.blocker == blocker, db.blocks.c.blocked == blocked)).first() is not None


# --- friends -------------------------------------------------------------------------------------
class Friends:
    """One person's friends, requests, blocks and friend link."""

    def __init__(self, engine: Engine, username: str):
        self.db, self.me = engine, username

    # the friend link
    def _saved_code(self) -> str | None:
        with self.db.connect() as conn:
            return conn.execute(select(db.friend_codes.c.code).where(db.friend_codes.c.username == self.me)).scalar()

    def code(self) -> str:
        """Your friend link's code, made the first time it's asked for."""
        found = self._saved_code()
        if found:
            return found
        try:
            with self.db.begin() as conn:
                conn.execute(insert(db.friend_codes).values(
                    username=self.me, code="".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH)),
                    created=_now()))
        except IntegrityError:
            pass  # two screens asked at once: both get the one that was saved
        return self._saved_code() or self.rotate_code()

    def rotate_code(self) -> str:
        """A new friend link: the old one stops working."""
        for _ in range(20):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            try:
                with self.db.begin() as conn:
                    if conn.execute(update(db.friend_codes).where(db.friend_codes.c.username == self.me)
                                    .values(code=code, created=_now())).rowcount == 0:
                        conn.execute(insert(db.friend_codes).values(username=self.me, code=code, created=_now()))
                return code
            except IntegrityError:
                continue  # the same code twice (or two taps at once): try another
        raise RuntimeError("couldn't make a friend link")

    def owner_of(self, code: str) -> str | None:
        code = clean_code(code)
        if len(code) < 6:
            return None
        with self.db.connect() as conn:
            return conn.execute(select(db.friend_codes.c.username).where(db.friend_codes.c.code == code)).scalar()

    def status_with(self, other: str) -> str:
        if other == self.me:
            return "you"
        with self.db.connect() as conn:
            row = conn.execute(select(db.friendships).where(_pair_is(self.me, other))).mappings().first()
        if not row or row["status"] == "withdrawn":
            return "none"
        if row["status"] == "accepted":
            return "friends"
        if row["requested_by"] == self.me:
            return "requested"
        return "none" if row["hidden"] else "asked_you"

    def lookup(self, code: str) -> dict:
        """Who a friend link belongs to, before adding them."""
        owner = self.owner_of(code)
        if not owner:
            raise FriendError(BAD_CODE, 404)
        with self.db.connect() as conn:
            shown = names(conn, [owner])
        return {**person(owner, shown), "status": self.status_with(owner)}

    # asking, answering
    def requests_today(self) -> int:
        """Requests asked in the last 24 hours, including ones taken back since (they stay, "withdrawn")."""
        with self.db.connect() as conn:
            return conn.execute(select(func.count()).select_from(db.friendships).where(
                db.friendships.c.requested_by == self.me, db.friendships.c.created > _now() - DAY)).scalar() or 0

    def request(self, username: str | None = None, code: str | None = None) -> dict:
        """Ask to be friends, by an exact username or a friend link's code. By username, the answer is the
        same whether or not that account exists (or blocked you, or already asked): nobody can use this to
        find out who has an account. If they had asked you too, you're friends straight away."""
        if code:
            target = self.owner_of(code)
            if not target:
                raise FriendError(BAD_CODE, 404)
            sent = SENT_BY_CODE
        else:
            target = clean_name(username)
            if not _NAME.match(target):
                raise FriendError(NOT_A_NAME)
            sent = SENT_BY_NAME
        if target == self.me:
            raise FriendError("That's you. Share your friend link with someone else.")
        if self.requests_today() >= REQUESTS_PER_DAY:
            raise FriendError(TOO_MANY_REQUESTS, 429)
        for tries_left in (1, 0):
            try:
                return self._ask(target) or {"status": "requested", "message": sent}
            except IntegrityError:
                # Someone wrote the pair at the same moment: two taps at once, or they asked you just now.
                # Once more, reading what they wrote (both asked: friends straight away).
                if not tries_left:
                    break
        return {"status": "requested", "message": sent}

    def _before_their_account(self, conn, target: str, row) -> bool:
        """This hidden request of yours was asked before `target` had an account (so they never saw it, and
        never said no to it), they have one now, and they haven't blocked you."""
        made = conn.execute(select(db.users.c.created).where(db.users.c.username == target)).scalar()
        return made is not None and made > row["created"] and not _blocked(conn, target, self.me)

    def _ask(self, target: str) -> dict | None:
        """One try at asking, in one transaction: an answer when you're friends now (or already), else None."""
        now = _now()
        a, b = _pair(self.me, target)
        with self.db.begin() as conn:
            if _blocked(conn, self.me, target):
                raise FriendError(f"You blocked {target}. Unblock them first, in Friends → Blocked.")
            row = conn.execute(select(db.friendships).where(_pair_is(a, b)).with_for_update()).mappings().first()
            joined = conn.execute(select(db.users.c.created).where(db.users.c.username == self.me)).scalar() or 0
            if row is not None and row["requested_by"] != self.me and (
                    row["status"] == "withdrawn" or (row["hidden"] and row["created"] < joined)):
                # They took it back, or asked for this name before this account existed (meant for someone
                # else, never shown): it's as if they hadn't asked.
                conn.execute(delete(db.friendships).where(_pair_is(a, b)))
                row = None
            if row is not None and row["status"] == "accepted":
                return {"status": "friends", "message": ALREADY}
            if row is not None and row["requested_by"] != self.me and _exists(conn, target):
                # They asked first (even one you'd said no to): both asked, so you're friends now.
                conn.execute(update(db.friendships).where(_pair_is(a, b)).values(
                    status="accepted", accepted=now, hidden=False))
                return {"status": "friends", "message": NOW_FRIENDS}
            if row is None:
                # Never shown to them when there's no such account or they blocked you.
                hidden = not _exists(conn, target) or _blocked(conn, target, self.me)
                conn.execute(insert(db.friendships).values(a=a, b=b, status="pending", requested_by=self.me,
                                                           created=now, accepted=None, hidden=hidden))
            elif row["requested_by"] == self.me:
                # Asking again. A quiet no (or a block) stands; but a request typed before their account
                # existed reaches them now, as a new one (they never saw the first).
                fresh = bool(row["hidden"]) and self._before_their_account(conn, target, row)
                if row["status"] == "withdrawn":
                    hidden = (bool(row["hidden"]) and not fresh) or not _exists(conn, target) \
                        or _blocked(conn, target, self.me)
                    conn.execute(update(db.friendships).where(_pair_is(a, b)).values(
                        status="pending", created=now, hidden=hidden))
                elif fresh:
                    conn.execute(update(db.friendships).where(_pair_is(a, b)).values(created=now, hidden=False))
        return None

    def _incoming(self, conn, other: str):
        row = conn.execute(select(db.friendships).where(_pair_is(self.me, other))).mappings().first()
        if not row or row["status"] != "pending" or row["requested_by"] != other or row["hidden"]:
            raise FriendError(NO_REQUEST, 404)
        return row

    def accept(self, other: str) -> None:
        other = clean_name(other)
        with self.db.begin() as conn:
            self._incoming(conn, other)
            conn.execute(update(db.friendships).where(_pair_is(self.me, other)).values(
                status="accepted", accepted=_now()))

    def decline(self, other: str) -> None:
        """Say no. They aren't told: on their side the request stays until it fades, as if unanswered."""
        other = clean_name(other)
        with self.db.begin() as conn:
            self._incoming(conn, other)
            conn.execute(update(db.friendships).where(_pair_is(self.me, other)).values(hidden=True))

    def remove(self, other: str) -> None:
        """Stop being friends, or take back a request you sent. A request taken back stays, shown to nobody
        ("withdrawn"), until it fades: so it still counts towards the day's requests, and asking again
        doesn't undo a quiet no."""
        other = clean_name(other)
        f = db.friendships.c
        with self.db.begin() as conn:
            done = conn.execute(delete(db.friendships).where(_pair_is(self.me, other), f.status == "accepted")).rowcount
            if not done:
                done = conn.execute(update(db.friendships).where(
                    _pair_is(self.me, other), f.status == "pending", f.requested_by == self.me).values(
                    status="withdrawn")).rowcount
        if not done:
            raise FriendError("They aren't on your friends list.", 404)

    def _knows(self, conn, other: str) -> bool | None:
        """Can you block them, and could you already see their name? None: you've never been in touch (you
        can't block them). False: only a request you sent (their name was never shown to you, so the Blocked
        list mustn't show it either: it would tell you the account exists). True: a friend, someone whose
        request you could see, or someone who sent you a note."""
        f, n = db.friendships.c, db.friend_notes.c
        row = conn.execute(select(f.status, f.requested_by, f.hidden, f.created).where(_pair_is(self.me, other))).first()
        if row and row.status == "accepted":
            return True
        if row and row.status == "pending" and row.requested_by == other:
            # Hidden too: a request you saw and said no to, or blocked (and unblocked since). Not one asked for
            # this name before your account existed: that was never shown to you.
            joined = conn.execute(select(db.users.c.created).where(db.users.c.username == self.me)).scalar() or 0
            if not row.hidden or row.created >= joined:
                return True
        if conn.execute(select(n.id).where(n.sender == other, n.recipient == self.me).limit(1)).first() is not None:
            return True
        if row and row.status == "pending" and row.requested_by == self.me:
            return False
        return None

    def block(self, other: str) -> None:
        """No more requests or notes from them. Ends the friendship, and their notes leave your feed. A
        request they sent you stays on their side, unanswered, as a quiet no does (a request that vanished
        would tell them they're blocked)."""
        other = clean_name(other)
        if other == self.me:
            raise FriendError("That's you.")
        f, n = db.friendships.c, db.friend_notes.c
        with self.db.begin() as conn:
            if not _blocked(conn, self.me, other):
                knows = self._knows(conn, other)
                if knows is None:
                    raise FriendError(CANT_BLOCK, 404)
                shown = names(conn, [other])[other] if knows else None
                conn.execute(insert(db.blocks).values(blocker=self.me, blocked=other, created=_now(),
                                                      shown=shown if shown != other else None))
            theirs_asked = and_(f.status == "pending", f.requested_by == other)
            mine_asked = and_(f.status == "pending", f.requested_by == self.me)
            conn.execute(update(db.friendships).where(_pair_is(self.me, other), theirs_asked).values(hidden=True))
            # Your own request is taken back, as with Take back: it still counts towards the day's requests.
            conn.execute(update(db.friendships).where(_pair_is(self.me, other), mine_asked).values(status="withdrawn"))
            conn.execute(delete(db.friendships).where(_pair_is(self.me, other), f.status == "accepted"))
            theirs = and_(n.sender == other, n.recipient == self.me)
            conn.execute(delete(db.friend_notes).where(theirs, n.gone_sender.is_(True)))
            conn.execute(update(db.friend_notes).where(theirs).values(gone_recipient=True, kept=False))

    def unblock(self, other: str) -> None:
        other = clean_name(other)
        with self.db.begin() as conn:
            if not conn.execute(delete(db.blocks).where(db.blocks.c.blocker == self.me,
                                                        db.blocks.c.blocked == other)).rowcount:
                raise FriendError("They aren't blocked.", 404)

    # who
    def friends(self) -> list[str]:
        f = db.friendships.c
        with self.db.connect() as conn:
            rows = conn.execute(select(f.a, f.b).where(f.status == "accepted", or_(f.a == self.me, f.b == self.me))).all()
        return sorted(b if a == self.me else a for a, b in rows)

    def is_friend(self, other: str) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(select(db.friendships.c.status).where(_pair_is(self.me, other))).scalar()
            return row == "accepted" and not _blocked(conn, self.me, other) and not _blocked(conn, other, self.me)

    def lists(self) -> dict:
        f = db.friendships.c
        with self.db.connect() as conn:
            rows = conn.execute(select(db.friendships).where(or_(f.a == self.me, f.b == self.me),
                                                              f.status != "withdrawn")
                                .order_by(f.created.desc())).mappings().all()
            blocked = conn.execute(select(db.blocks.c.blocked, db.blocks.c.created, db.blocks.c.shown).where(
                db.blocks.c.blocker == self.me).order_by(db.blocks.c.created.desc())).all()
            others = [r["b"] if r["a"] == self.me else r["a"] for r in rows
                      if r["status"] == "accepted" or (r["requested_by"] != self.me and not r["hidden"])]
            shown = names(conn, others)
        out: dict[str, list] = {"friends": [], "incoming": [], "outgoing": [], "blocked": []}
        for r in rows:
            other = r["b"] if r["a"] == self.me else r["a"]
            if r["status"] == "accepted":
                out["friends"].append({**person(other, shown), "since": r["accepted"] or r["created"]})
            elif r["requested_by"] == self.me:  # only the name typed: the same whether or not it's an account
                out["outgoing"].append({"username": other, "name": other, "at": r["created"]})
            elif not r["hidden"]:
                out["incoming"].append({**person(other, shown), "at": r["created"]})
        out["friends"].sort(key=lambda p: p["name"].lower())
        # Only a name you could already see (Friends.block keeps it); else the username, like `outgoing`.
        out["blocked"] = [{"username": b, "name": name or b, "at": at} for b, at, name in blocked]
        return out

    def incoming_count(self) -> int:
        f = db.friendships.c
        with self.db.connect() as conn:
            return conn.execute(select(func.count()).select_from(db.friendships).where(
                f.status == "pending", f.hidden.is_(False), f.requested_by != self.me,
                or_(f.a == self.me, f.b == self.me))).scalar() or 0


# --- pictures ------------------------------------------------------------------------------------
def _open(data: bytes, formats: set[str], what: str, max_pixels: int, edge: int | None = None):
    from PIL import Image

    from .scan import HEIC_HELP, MAX_PIXELS, heic_supported
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    heic = heic_supported()
    try:
        img = Image.open(io.BytesIO(data))  # the header only: nothing is decoded yet
        fmt = (img.format or "").upper()
        if fmt not in formats:
            raise FriendError(f"That file isn't a {what} Next Box can use. Try a JPEG or PNG.", 415)
        if img.width * img.height > max_pixels:
            raise FriendError(f"That {what} is far too large.", 413)
        if edge and fmt in ("JPEG", "MPO") and max(img.size) > edge:
            # A big JPEG is decoded at 1/2, 1/4 or 1/8 of its size, still at least `edge` on its long side:
            # a phone's 12 MP photo decodes about four times faster, and it's shrunk to `edge` anyway.
            s = edge / max(img.size)
            img.draft(None, (max(1, round(img.width * s)), max(1, round(img.height * s))))
        img.load()
        return img
    except FriendError:
        raise
    except Image.DecompressionBombError:
        raise FriendError(f"That {what} is far too large.", 413) from None
    except Exception:
        if not heic and data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1", b"ftyphevc"):
            raise FriendError(HEIC_HELP, 415) from None
        raise FriendError(f"That file isn't a {what} Next Box can read. Try a JPEG or PNG.", 415) from None


def _flatten(img):
    """On white, without its transparency (a doodle drawn on a see-through canvas has black on nothing)."""
    from PIL import Image
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        out = Image.new("RGB", rgba.size, "white")
        out.paste(rgba, mask=rgba.getchannel("A"))
        return out
    return img.convert("RGB")


def clean_photo(data: bytes) -> bytes:
    """A photo as Next Box keeps it: turned upright, at most PHOTO_EDGE on its long side, re-encoded as a
    plain JPEG of about PHOTO_BYTES, with nothing of the original file kept (no EXIF, so no location, no
    camera details, no ICC profile, no XMP). Anything that isn't a picture is refused."""
    from PIL import Image, ImageOps
    if len(data) > PHOTO_UPLOAD:
        raise FriendError("That photo is too big. Pick one under 15 MB.", 413)
    if not data:
        raise FriendError("Pick a photo first.")
    img = _open(data, PHOTO_FORMATS, "photo", 50_000_000, edge=PHOTO_EDGE)
    img = _flatten(ImageOps.exif_transpose(img))
    img.thumbnail((PHOTO_EDGE, PHOTO_EDGE))
    fresh = Image.new("RGB", img.size, "white")
    fresh.paste(img)  # a new image: none of the original's metadata comes along
    quality = 82
    while True:  # smaller until it's about PHOTO_BYTES: a lower quality first, then fewer pixels
        out = io.BytesIO()
        fresh.save(out, "JPEG", quality=quality, optimize=True)
        if out.tell() <= PHOTO_BYTES or max(fresh.size) <= 320:
            return out.getvalue()
        if quality > 62:
            quality -= 10
        else:
            fresh = fresh.resize((max(1, fresh.width * 4 // 5), max(1, fresh.height * 4 // 5)), Image.LANCZOS)


def clean_doodle(data: bytes) -> bytes:
    """A finger doodle as Next Box keeps it: black ink on white (it prints on a thermal label), at most
    DOODLE_EDGE on its long side, as a 1-bit PNG. Anything that isn't a picture is refused."""
    from PIL import Image
    if len(data) > DOODLE_UPLOAD:
        raise FriendError("That drawing is too big to send.", 413)
    if not data:
        raise FriendError("Draw something first.")
    img = _flatten(_open(data, DOODLE_FORMATS, "drawing", DOODLE_PIXELS)).convert("L")
    img.thumbnail((DOODLE_EDGE, DOODLE_EDGE))
    ink = img.point(lambda p: 0 if p < 160 else 255)
    if ink.getextrema()[0] == 255:
        raise FriendError("Draw something first.")
    fresh = Image.new("1", ink.size, 1)
    fresh.paste(ink.convert("1", dither=Image.Dither.NONE))
    out = io.BytesIO()
    fresh.save(out, "PNG", optimize=True)
    return out.getvalue()


def picture(data: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(data))


def image_type(data: bytes | None) -> str:
    return "image/png" if (data or b"")[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


# --- notes ---------------------------------------------------------------------------------------
def _cursor(row) -> str:
    return f"{0 if row['seen'] is None else 1}.{row['created']}.{row['id']}"


def _after(cursor: str | None, feed: bool):
    """Rows after a cursor (feed: unseen first, then the rest, each newest first; sent: newest first)."""
    if not cursor:
        return None
    m = re.fullmatch(r"([01])\.(\d{1,12})\.([0-9a-f]{1,16})", cursor or "")
    if not m:
        raise FriendError("That list position isn't right. Load the list again.")
    group, created, nid = int(m[1]), int(m[2]), m[3]
    n = db.friend_notes.c
    later = or_(n.created < created, and_(n.created == created, n.id < nid))
    if not feed:
        return later
    seen = n.seen.is_not(None)
    if group == 0:  # further down the unseen ones, then every seen one
        return or_(and_(n.seen.is_(None), later), seen)
    return and_(seen, later)


class Notes:
    """One person's notes: the ones friends sent them (their feed) and the ones they sent (Sent)."""

    def __init__(self, engine: Engine, username: str):
        self.db, self.me = engine, username

    # sending
    def check(self, kind: str, text: str | None, color: str | None, image: bytes | None) -> tuple[str, str | None]:
        """What would be kept: (words, color). Raises FriendError for anything that can't be sent."""
        if kind not in KINDS:
            raise FriendError("Pick a note, a drawing, a photo or a heart.")
        words = "\n".join(line.rstrip() for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n")
                          .split("\n")).strip()
        words = re.sub(r"\n{3,}", "\n\n", words)  # blank lines in a row: one (a label isn't a roll of paper)
        if kind == "note":
            if not words:
                raise FriendError("Write something first.")
            if len(words) > MAX_TEXT:
                raise FriendError(f"Keep a note to {MAX_TEXT} characters (this one has {len(words)}).")
            lines = words.count("\n") + 1
            if lines > MAX_LINES:
                raise FriendError(f"Keep a note to {MAX_LINES} lines (this one has {lines}).")
            color = color or "yellow"
            if color not in NOTE_COLORS:
                raise FriendError("Pick one of the note colors.")
            return words, color
        if kind == "heart":
            return "", None
        if len(words) > MAX_CAPTION:
            raise FriendError(f"Keep the caption to {MAX_CAPTION} characters (this one has {len(words)}).")
        if not image:
            raise FriendError("Draw something first." if kind == "draw" else "Pick a photo first.")
        return words, None

    def ready(self, to: list[str]) -> tuple[list[str], dict[str, str]]:
        """Who a note can go to: (usernames, {username: name}). Raises FriendError unless every one is a
        friend (and neither has blocked the other) and the limits allow it. Cheap, so a photo is checked
        against it before it's read or decoded."""
        people = list(dict.fromkeys(clean_name(t) for t in to or [] if clean_name(t)))
        if not people:
            raise FriendError("Pick a friend to send it to.")
        if len(people) > MAX_TO:
            raise FriendError(f"Send to {MAX_TO} friends at a time.")
        mine = Friends(self.db, self.me)
        for other in people:
            if not mine.is_friend(other):
                raise FriendError(f"You can send notes only to friends, and {other} isn't on your friends list.")
        n, now = db.friend_notes.c, _now()
        with self.db.connect() as conn:
            today = dict(conn.execute(select(n.recipient, func.count()).where(
                n.sender == self.me, n.created > now - DAY).group_by(n.recipient)).all())
            shown = names(conn, people)
        if sum(today.values()) + len(people) > NOTES_PER_DAY:
            raise FriendError(f"That's {NOTES_PER_DAY} notes in the last 24 hours, the most for a day. "
                              "Try again later.", 429)
        for other in people:
            if today.get(other, 0) >= NOTES_PER_FRIEND_PER_DAY:
                raise FriendError(f"That's {NOTES_PER_FRIEND_PER_DAY} notes to {shown[other]} in the last 24 hours. "
                                  "Try again later.", 429)
        return people, shown

    def send(self, to: list[str], kind: str, text: str | None = None, color: str | None = None,
             image: bytes | None = None) -> dict:
        """Send one note to each friend in `to`. `image` is already cleaned (clean_photo / clean_doodle)."""
        words, color = self.check(kind, text, color, image)
        people, shown = self.ready(to)
        n, now = db.friend_notes.c, _now()
        ids = []
        with self.db.begin() as conn:
            for other in people:
                nid = secrets.token_hex(6)
                conn.execute(insert(db.friend_notes).values(
                    id=nid, sender=self.me, recipient=other, kind=kind, body=words or None, color=color,
                    image=image, created=now, seen=None, printed=None, kept=False, reaction=None,
                    gone_sender=False, gone_recipient=False))
                ids.append(nid)
        who = [shown[p] for p in people]
        listed = who[0] if len(who) == 1 else ", ".join(who[:-1]) + " and " + who[-1]
        return {"sent": len(ids), "ids": ids, "message": f"Sent to {listed}."}

    # reading
    def _row(self, conn, note_id: str):
        if not re.fullmatch(r"[0-9a-f]{12}", note_id or ""):
            return None
        n = db.friend_notes.c
        return conn.execute(select(db.friend_notes).where(n.id == note_id, _alive(), or_(
            and_(n.recipient == self.me, n.gone_recipient.is_(False)),
            and_(n.sender == self.me, n.gone_sender.is_(False), _sender_sees())))).mappings().first()

    def get(self, note_id: str) -> dict:
        """The note (with its image bytes) when it's yours to see: sent to you, or sent by you."""
        with self.db.connect() as conn:
            row = self._row(conn, note_id)
        if row is None:
            raise FriendError(NOT_FOUND, 404)
        return dict(row)

    def received(self, note_id: str) -> dict:
        row = self.get(note_id)
        if row["recipient"] != self.me or row["gone_recipient"]:
            raise FriendError(NOT_FOUND, 404)
        return row

    def _feed_item(self, r, shown: dict, friends: set[str]) -> dict:
        return {"id": r["id"], "from": person(r["sender"], shown), "kind": r["kind"], "text": r["body"] or "",
                "color": r["color"], "image": f"/notes/{r['id']}/image" if r["has_image"] else None,
                "label": f"/notes/{r['id']}/label.png", "created": r["created"], "seen": r["seen"],
                "printed": r["printed"], "kept": bool(r["kept"]), "reaction": r["reaction"],
                "expires": None if r["kept"] else r["created"] + KEEP_DAYS * DAY, "can_reply": r["sender"] in friends}

    def _cols(self):
        n = db.friend_notes.c
        return [n.id, n.sender, n.recipient, n.kind, n.body, n.color, n.created, n.seen, n.printed, n.kept,
                n.reaction, (n.image.is_not(None)).label("has_image")]

    def feed(self, limit: int = 30, cursor: str | None = None) -> dict:
        """Notes friends sent you: unseen first, then the rest, each newest first. Old unsaved notes are
        deleted first (prune)."""
        prune(self.db)
        limit = max(1, min(int(limit), 100))
        n = db.friend_notes.c
        mine = and_(n.recipient == self.me, n.gone_recipient.is_(False), _alive(),
                    n.sender.not_in(select(db.blocks.c.blocked).where(db.blocks.c.blocker == self.me)))
        q = select(*self._cols()).where(mine)
        after = _after(cursor, feed=True)
        if after is not None:
            q = q.where(after)
        q = q.order_by(case((n.seen.is_(None), 0), else_=1), n.created.desc(), n.id.desc()).limit(limit + 1)
        with self.db.connect() as conn:
            rows = conn.execute(q).mappings().all()
            unseen = conn.execute(select(func.count()).select_from(db.friend_notes).where(
                mine, n.seen.is_(None))).scalar() or 0
            shown = names(conn, [r["sender"] for r in rows])
        more = len(rows) > limit
        rows = rows[:limit]
        friends = set(Friends(self.db, self.me).friends())
        return {"notes": [self._feed_item(r, shown, friends) for r in rows],
                "next": _cursor(rows[-1]) if more and rows else None, "unseen": unseen}

    def item(self, note_id: str) -> dict:
        """One note in the feed's shape (after a reaction or Keep)."""
        n = db.friend_notes.c
        with self.db.connect() as conn:
            r = conn.execute(select(*self._cols()).where(
                n.id == note_id, n.recipient == self.me, n.gone_recipient.is_(False), _alive())).mappings().first()
            if r is None:
                raise FriendError(NOT_FOUND, 404)
            shown = names(conn, [r["sender"]])
        return self._feed_item(r, shown, set(Friends(self.db, self.me).friends()))

    def _sent_items(self, conn, rows) -> list[dict]:
        """What you sent, as Sent shows it: how far each got (delivered, seen or printed) and their reaction.
        Someone who turned off "Tell friends when I've seen or printed their notes" shows as delivered, and
        so does anyone you blocked, with no reaction (whatever they do now doesn't reach you, and they
        aren't told they're blocked). Never when they saw or printed it, or whether they kept it."""
        people = {r["recipient"] for r in rows}
        shown = names(conn, people)
        quiet = {u for u, prefs in conn.execute(select(db.users.c.username, db.users.c.prefs).where(
            db.users.c.username.in_(people))) if (prefs or {}).get("friend_receipts") is False}
        blocked = set(conn.execute(select(db.blocks.c.blocked).where(
            db.blocks.c.blocker == self.me, db.blocks.c.blocked.in_(people))).scalars())
        out = []
        for r in rows:
            status, reaction = "delivered", r["reaction"]
            if r["recipient"] in blocked:
                reaction = None
            elif r["recipient"] not in quiet:
                status = "printed" if r["printed"] else "seen" if r["seen"] else "delivered"
            out.append({"id": r["id"], "to": person(r["recipient"], shown), "kind": r["kind"], "text": r["body"] or "",
                        "color": r["color"], "image": f"/notes/{r['id']}/image" if r["has_image"] else None,
                        "created": r["created"], "reaction": reaction, "status": status})
        return out

    def sent(self, limit: int = 30, cursor: str | None = None) -> dict:
        """What you sent, newest first, with how far each got (_sent_items)."""
        prune(self.db)
        limit = max(1, min(int(limit), 100))
        n = db.friend_notes.c
        q = select(*self._cols()).where(n.sender == self.me, n.gone_sender.is_(False), _sender_sees())
        after = _after(cursor, feed=False)
        if after is not None:
            q = q.where(after)
        q = q.order_by(n.created.desc(), n.id.desc()).limit(limit + 1)
        with self.db.connect() as conn:
            rows = conn.execute(q).mappings().all()
            more = len(rows) > limit
            rows = rows[:limit]
            out = self._sent_items(conn, rows)
        return {"notes": out, "next": f"1.{rows[-1]['created']}.{rows[-1]['id']}" if more and rows else None}

    def counts(self) -> dict:
        prune(self.db)  # the badge asks every minute: old notes go on time even if nobody opens Friends
        n = db.friend_notes.c
        with self.db.connect() as conn:
            unseen = conn.execute(select(func.count()).select_from(db.friend_notes).where(
                n.recipient == self.me, n.gone_recipient.is_(False), n.seen.is_(None), _alive(),
                n.sender.not_in(select(db.blocks.c.blocked).where(db.blocks.c.blocker == self.me)))).scalar() or 0
        return {"unseen": unseen, "requests": Friends(self.db, self.me).incoming_count()}

    # what the recipient does
    def _set(self, note_id: str, **values) -> None:
        n = db.friend_notes.c
        with self.db.begin() as conn:
            if not conn.execute(update(db.friend_notes).where(
                    n.id == note_id, n.recipient == self.me, n.gone_recipient.is_(False)).values(**values)).rowcount:
                raise FriendError(NOT_FOUND, 404)

    def mark_seen(self, note_id: str) -> dict:
        row = self.received(note_id)
        seen = row["seen"] or _now()
        if not row["seen"]:
            self._set(note_id, seen=seen)
        return {"id": note_id, "seen": seen}

    def react(self, note_id: str, reaction: str | None) -> dict:
        if reaction not in (None, "", *REACTIONS):
            raise FriendError("Pick one of the reactions.")
        row = self.received(note_id)
        self._set(note_id, reaction=reaction or None, seen=row["seen"] or _now())
        return self.item(note_id)

    def keep(self, note_id: str, keep: bool) -> dict:
        self.received(note_id)
        self._set(note_id, kept=bool(keep))
        return self.item(note_id)

    def mark_printed(self, note_id: str) -> None:
        row = self.received(note_id)
        now = _now()
        self._set(note_id, printed=now, seen=row["seen"] or now)

    def delete(self, note_id: str) -> None:
        """Delete for me: out of your feed (you received it) or your Sent list (you sent it). The note is
        gone for good once both have, or when it's 30 days old and not kept."""
        row = self.get(note_id)
        n = db.friend_notes.c
        with self.db.begin() as conn:
            if row["recipient"] == self.me and not row["gone_recipient"]:
                conn.execute(update(db.friend_notes).where(n.id == note_id).values(gone_recipient=True, kept=False))
            if row["sender"] == self.me and not row["gone_sender"]:
                conn.execute(update(db.friend_notes).where(n.id == note_id).values(gone_sender=True))
            conn.execute(delete(db.friend_notes).where(n.id == note_id, n.gone_sender.is_(True),
                                                       n.gone_recipient.is_(True)))

    def export(self) -> dict:
        """Your own sent and received notes, for Download my data (images: has_image; the .zip has them).
        A note you got has everything about it; one you sent says only what Sent shows (_sent_items)."""
        n = db.friend_notes.c
        with self.db.connect() as conn:
            got = conn.execute(select(*self._cols()).where(
                n.recipient == self.me, n.gone_recipient.is_(False), _alive()).order_by(n.created)).mappings().all()
            sent = conn.execute(select(*self._cols()).where(
                n.sender == self.me, n.gone_sender.is_(False), _sender_sees()).order_by(n.created)).mappings().all()
            sent_items = self._sent_items(conn, sent)
        keys = ("id", "kind", "color", "created", "seen", "printed", "kept", "reaction")
        received = [{**{k: r[k] for k in keys}, "text": r["body"] or "", "has_image": bool(r["has_image"]),
                     "from": r["sender"]} for r in got]
        mine = [{"id": i["id"], "kind": i["kind"], "color": i["color"], "created": i["created"], "text": i["text"],
                 "has_image": i["image"] is not None, "to": i["to"]["username"], "reaction": i["reaction"],
                 "status": i["status"]} for i in sent_items]
        return {"received": received, "sent": mine}

    def image(self, note_id: str) -> bytes:
        row = self.get(note_id)
        if not row["image"]:
            raise FriendError(NOT_FOUND, 404)
        return row["image"]


# --- upkeep --------------------------------------------------------------------------------------
def _alive(now: int | None = None):
    """Notes that are still here: kept, or not yet 30 days old (in case prune hasn't run yet)."""
    n = db.friend_notes.c
    return or_(n.kept.is_(True), n.created >= (now if now is not None else _now()) - KEEP_DAYS * DAY)


def _sender_sees(now: int | None = None):
    """The sender's view of a note ends after 30 days whatever the recipient did, so Sent (and its export)
    never shows that a note was kept."""
    return db.friend_notes.c.created >= (now if now is not None else _now()) - KEEP_DAYS * DAY


_pruned: dict[int, float] = {}
_prune_lock = threading.Lock()


def prune(engine: Engine, now: int | None = None, force: bool = False) -> dict:
    """Delete notes nobody kept 30 days after they arrived, and requests nobody answered (or taken back) in
    30 days. At startup (upkeep), and when a feed, the badge's count or Sent is read (at most once a minute):
    there's no timer."""
    with _prune_lock:
        if not force and now is None and time.time() - _pruned.get(id(engine), 0) < 60:
            return {"notes": 0, "requests": 0}
        _pruned[id(engine)] = time.time()
    now = now if now is not None else _now()
    n, f = db.friend_notes.c, db.friendships.c
    with engine.begin() as conn:
        notes = conn.execute(delete(db.friend_notes).where(
            n.kept.is_(False), n.created < now - KEEP_DAYS * DAY)).rowcount
        requests = conn.execute(delete(db.friendships).where(
            f.status.in_(("pending", "withdrawn")), f.created < now - REQUEST_DAYS * DAY)).rowcount
    return {"notes": notes, "requests": requests}


def forget_user(engine: Engine, username: str) -> None:
    """An account is deleted: its friend link, friendships, requests, blocks and every note it sent or got."""
    f, b, n = db.friendships.c, db.blocks.c, db.friend_notes.c
    with engine.begin() as conn:
        conn.execute(delete(db.friend_codes).where(db.friend_codes.c.username == username))
        conn.execute(delete(db.friendships).where(or_(f.a == username, f.b == username)))
        conn.execute(delete(db.blocks).where(or_(b.blocker == username, b.blocked == username)))
        conn.execute(delete(db.friend_notes).where(or_(n.sender == username, n.recipient == username)))


def sweep(engine: Engine) -> int:
    """Friends' rows that name an account that's gone (deleted before this version, or a household removed).
    A request to a name that never had an account stays until it fades, like any other, and a block on one
    stays too (forget_user removes blocks on an account that's deleted): a block that vanished would tell
    the blocker that name has no account."""
    f, b, n = db.friendships.c, db.blocks.c, db.friend_notes.c
    people = select(db.users.c.username)
    with engine.begin() as conn:
        gone = conn.execute(delete(db.friend_codes).where(db.friend_codes.c.username.not_in(people))).rowcount
        gone += conn.execute(delete(db.friendships).where(or_(
            f.requested_by.not_in(people),
            and_(f.status == "accepted", or_(f.a.not_in(people), f.b.not_in(people)))))).rowcount
        gone += conn.execute(delete(db.blocks).where(b.blocker.not_in(people))).rowcount
        gone += conn.execute(delete(db.friend_notes).where(or_(n.sender.not_in(people),
                                                               n.recipient.not_in(people)))).rowcount
    return gone


def export(engine: Engine, username: str) -> dict:
    """Download my data: your friends, requests, blocks, friend link and notes (images in the .zip)."""
    mine = Friends(engine, username)
    with engine.connect() as conn:
        code = conn.execute(select(db.friend_codes.c.code).where(db.friend_codes.c.username == username)).scalar()
    return {**mine.lists(), "friend_code": code, "notes": Notes(engine, username).export()}
