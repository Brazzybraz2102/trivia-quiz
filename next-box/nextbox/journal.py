"""The journal, wins and slugs.

The daily routine: paste today's label on the left page of a paper journal, work the list, write
notes on the right page, then take one photo of both pages. Read-back applies the marks, reads the
handwritten notes, and keeps the photo here, so a lost journal is never a lost record.

- **Journal pages** (`journal` table): one per photo or typed note, grouped by day. Private to the
  person who wrote them; admins can't read them. Snapping the same ticket again updates its page.
- **Wins** (`wins` table): what you finished each day, one row per task, filed under the day it was
  done (the ticket's day for a photo read-back). Kept like the journal, until the account goes.
- **Slugs**: tasks that keep sliding to another day (`task_slides` counts the moves) or have waited
  a long time. They're shown gently, with a way to make them smaller.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
import unicodedata
from datetime import date

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.engine import Engine

from . import db

MAX_NOTES = 20_000
MAX_WIN = 500
SLUG_MOVES = 3       # moved this many times...
SLUG_WAITING = 7     # ...or waiting this many days
MAX_SLUGS = 10
SEARCH_LIMIT = 50    # results shown for a search
SEARCH_SCAN = 5000   # newest pages a search looks through
SNIPPET = 160        # characters of a page shown around a match
MAX_LINES = 200      # lines of a page offered as tasks


def merge_notes(old: str, new: str) -> str:
    """Notes read from another photo of the same page: keep what's there, add what's new."""
    old, new = (old or "").strip(), (new or "").strip()

    def squash(t: str) -> str:
        return " ".join(t.split())
    if not new or squash(new) in squash(old):
        return old
    if not old or squash(old) in squash(new):
        return new
    return f"{old}\n\n{new}"


class Journal:
    """One person's journal pages."""

    def __init__(self, engine: Engine, owner: str, household: str = "home"):
        self.db, self.owner, self.household = engine, owner, household

    def _mine(self, page_id: str):
        return and_(db.journal.c.id == page_id, db.journal.c.owner == self.owner)

    @staticmethod
    def _page(row) -> dict:
        return {"id": row["id"], "day": row["day"], "created": row["created"], "updated": row["updated"],
                "notes": row["notes"], "has_photo": bool(row["has_photo"]), "scan_id": row["scan_id"],
                "label_id": row["label_id"], "win": row["win"] or ""}

    def _cols(self):
        c = db.journal.c
        return [c.id, c.day, c.created, c.updated, c.notes, c.scan_id, c.label_id, c.win,
                (c.photo.isnot(None)).label("has_photo")]

    @staticmethod
    def _day(day: str) -> str:
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("day looks like 2026-10-16")
        try:
            date.fromisoformat(day)
        except ValueError:
            raise ValueError("day looks like 2026-10-16") from None
        return day

    def add(self, day: str, notes: str = "", photo: bytes | None = None, scan_id: str | None = None,
            label_id: str | None = None, win: str = "") -> dict:
        day = self._day(day)
        notes = (notes or "").strip()
        if len(notes) > MAX_NOTES:
            raise ValueError(f"notes can be up to {MAX_NOTES} characters")
        if not notes and photo is None:
            raise ValueError("write something first")
        page_id, now = secrets.token_hex(6), int(time.time())
        with self.db.begin() as conn:
            conn.execute(insert(db.journal).values(
                id=page_id, owner=self.owner, household_id=self.household, day=day, created=now,
                updated=now, notes=notes, photo=photo, scan_id=scan_id, label_id=label_id,
                win=(win or "").strip()[:MAX_WIN] or None,
                photo_sha=hashlib.sha256(photo).hexdigest() if photo is not None else None))
        return self.get(page_id)

    def save_snap(self, day: str, notes: str = "", photo: bytes | None = None, scan_id: str | None = None,
                  label_id: str | None = None, win: str = "", page_id: str | None = None) -> dict:
        """Keep a read-back photo as a page. Snapping the same ticket again on its day, or the very
        same photo again, updates that page (newer photo, notes merged) instead of adding another.
        `page_id` without a photo moves that page to this ticket and day (the ticket was picked
        afterwards, or the photo was read again after reading it failed)."""
        day = self._day(day)
        if photo is None and page_id:
            photo = self.photo(page_id)
        if photo is None:
            return self.add(day, notes, None, scan_id, label_id, win)
        sha = hashlib.sha256(photo).hexdigest()
        c = db.journal.c
        cols = (c.id, c.notes, c.win)
        with self.db.connect() as conn:
            by_page = conn.execute(select(*cols).where(self._mine(page_id))).mappings().all() if page_id else []
            by_label = conn.execute(select(*cols).where(c.owner == self.owner, c.day == day, c.label_id == label_id)
                                    .order_by(c.created)).mappings().all() if label_id else []
            by_photo = conn.execute(select(*cols).where(c.owner == self.owner, c.photo_sha == sha)
                                    .order_by(c.created)).mappings().all()
        pages = list({p["id"]: dict(p) for p in [*by_label, *by_page, *by_photo]}.values())
        if not pages:
            return self.add(day, notes, photo, scan_id, label_id, win)
        keep = pages[0]  # the ticket's page if it has one; the others (the same photo) fold into it
        merged = keep["notes"] or ""
        for p in pages[1:]:
            merged = merge_notes(merged, p["notes"])
        merged = merge_notes(merged, notes)[:MAX_NOTES]
        new_win = (win or "").strip()[:MAX_WIN] or next((p["win"] for p in pages if p["win"]), None)
        values = {"day": day, "photo": photo, "photo_sha": sha, "notes": merged, "win": new_win or None,
                  "updated": int(time.time())}
        if scan_id:
            values["scan_id"] = scan_id
        if label_id:
            values["label_id"] = label_id
        with self.db.begin() as conn:
            conn.execute(update(db.journal).where(self._mine(keep["id"])).values(**values))
            for p in pages[1:]:
                conn.execute(delete(db.journal).where(self._mine(p["id"])))
        return self.get(keep["id"])

    def get(self, page_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(select(*self._cols()).where(self._mine(page_id))).mappings().first()
        return self._page(row) if row else None

    def photo(self, page_id: str) -> bytes | None:
        with self.db.connect() as conn:
            return conn.execute(select(db.journal.c.photo).where(self._mine(page_id))).scalar()

    def update(self, page_id: str, notes: str | None = None, day: str | None = None) -> dict:
        """Change a page's notes, or move it to another day (`day`), or both. Left out: unchanged."""
        page = self.get(page_id)
        if page is None:
            raise KeyError(page_id)
        values: dict = {}
        if notes is not None:
            notes = notes.strip()
            if len(notes) > MAX_NOTES:
                raise ValueError(f"notes can be up to {MAX_NOTES} characters")
            if not notes and not page["has_photo"]:
                raise ValueError("This page would be empty. Delete it instead?")
            values["notes"] = notes
        if day is not None:
            values["day"] = self._day(day)
        if not values:
            raise ValueError("Nothing to change.")
        with self.db.begin() as conn:
            done = conn.execute(update(db.journal).where(self._mine(page_id)).values(
                **values, updated=int(time.time()))).rowcount
        if not done:
            raise KeyError(page_id)
        return self.get(page_id)

    def delete(self, page_id: str) -> None:
        with self.db.begin() as conn:
            if not conn.execute(delete(db.journal).where(self._mine(page_id))).rowcount:
                raise KeyError(page_id)

    def pages(self, since: str, until: str) -> list[dict]:
        """Pages from `since` to `until` (inclusive), newest day first, without the photos."""
        c = db.journal.c
        q = (select(*self._cols()).where(and_(c.owner == self.owner, c.day >= since, c.day <= until))
             .order_by(c.day.desc(), c.created))
        with self.db.connect() as conn:
            return [self._page(r) for r in conn.execute(q).mappings()]

    def count(self) -> int:
        with self.db.connect() as conn:
            return conn.execute(select(func.count()).select_from(db.journal).where(
                db.journal.c.owner == self.owner)).scalar() or 0

    def span(self) -> tuple[str | None, str | None]:
        """The first and last day with a page."""
        c = db.journal.c
        with self.db.connect() as conn:
            first, last = conn.execute(select(func.min(c.day), func.max(c.day)).where(c.owner == self.owner)).first()
        return first, last

    def stats(self, since: str, until: str) -> dict:
        """How much a download of these days holds: pages, photos and their size in bytes."""
        c = db.journal.c
        with self.db.connect() as conn:
            pages, photos, size, text = conn.execute(
                select(func.count(), func.count(c.photo), func.coalesce(func.sum(func.length(c.photo)), 0),
                       func.coalesce(func.sum(func.length(c.notes)), 0))
                .where(c.owner == self.owner, c.day >= since, c.day <= until)).first()
        return {"pages": pages or 0, "photos": photos or 0, "bytes": int(size or 0) + int(text or 0)}

    def search(self, query: str, limit: int = SEARCH_LIMIT) -> tuple[list[dict], int]:
        """Pages whose notes (or win) hold every word of `query`, newest first, each with a snippet
        around the first match as parts: [{"t": text, "m": is_a_match}]. Case and accents don't
        matter ("cafe" finds "Café"). Returns (pages, how many matched in all)."""
        words = list(dict.fromkeys(_fold(query)[0].split()))[:8]
        if not words:
            return [], 0
        c = db.journal.c
        q = (select(c.id, c.day, c.created, c.notes, c.win, (c.photo.isnot(None)).label("has_photo"))
             .where(c.owner == self.owner).order_by(c.day.desc(), c.created.desc()).limit(SEARCH_SCAN))
        found, total = [], 0
        with self.db.connect() as conn:
            for r in conn.execute(q).mappings():
                text = (r["notes"] or "") + (f"\n\nYour win: {r['win']}" if r["win"] else "")
                quick = _fold_fast(text)
                if not all(w in quick for w in words):
                    continue
                total += 1
                if len(found) < limit:  # where each match is, only for the pages shown
                    folded, where = _fold(text)
                    spans = [(where[m.start()], where[m.end() - 1] + 1)
                             for w in words for m in re.finditer(re.escape(w), folded)]
                    found.append({"id": r["id"], "day": r["day"], "created": r["created"],
                                  "has_photo": bool(r["has_photo"]), "matches": len(spans),
                                  "parts": _snippet(text, spans)})
        return found, total

    def newest_day_before(self, day: str) -> str | None:
        """The latest day with a page before `day`, so "Older days" can jump over empty weeks."""
        c = db.journal.c
        with self.db.connect() as conn:
            return conn.execute(select(func.max(c.day)).where(c.owner == self.owner, c.day < day)).scalar()

    def delete_for_user(self) -> int:
        with self.db.begin() as conn:
            return conn.execute(delete(db.journal).where(db.journal.c.owner == self.owner)).rowcount


class Wins:
    """What one person finished, by day. My list tasks keep their name; a linked app's don't."""

    def __init__(self, engine: Engine, owner: str, household: str = "home"):
        self.db, self.owner, self.household = engine, owner, household

    def add(self, task_id: str | None, day: str, source: str, content: str | None = None,
            scan_id: str | None = None, once_a_day: bool = False) -> bool:
        """Record a finished task. `once_a_day`: not again if this task already counted that day
        (a linked task ticked in the app and also ✓-marked on paper)."""
        w = db.wins.c
        with self.db.begin() as conn:
            if once_a_day and task_id and conn.execute(select(w.id).where(
                    w.owner == self.owner, w.task_id == str(task_id)[:200], w.day == day).limit(1)).first():
                return False
            conn.execute(insert(db.wins).values(
                owner=self.owner, household_id=self.household, day=day, ts=int(time.time()),
                task_id=str(task_id)[:200] if task_id else None, source=source,
                content=(content or "")[:500] or None, scan_id=scan_id))
        return True

    def remove_last(self, task_id: str, scan_id: str | None = None) -> bool:
        """Undo: forget the newest win for this task (from this read-back, when given)."""
        w = db.wins.c
        conds = [w.owner == self.owner, w.task_id == str(task_id)[:200]]
        if scan_id:
            conds.append(w.scan_id == scan_id)
        with self.db.begin() as conn:
            newest = conn.execute(select(w.id).where(*conds).order_by(w.ts.desc(), w.id.desc()).limit(1)).scalar()
            if newest is None:
                return False
            conn.execute(delete(db.wins).where(w.id == newest))
        return True

    def between(self, first: str, last: str) -> list[dict]:
        w = db.wins.c
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                select(w.day, w.task_id, w.source, w.content, w.scan_id)
                .where(w.owner == self.owner, w.day >= first, w.day <= last).order_by(w.ts, w.id)).mappings()]

    def newest_day_before(self, day: str) -> str | None:
        w = db.wins.c
        with self.db.connect() as conn:
            return conn.execute(select(func.max(w.day)).where(w.owner == self.owner, w.day < day)).scalar()

    def span(self) -> tuple[str | None, str | None]:
        w = db.wins.c
        with self.db.connect() as conn:
            first, last = conn.execute(select(func.min(w.day), func.max(w.day)).where(w.owner == self.owner)).first()
        return first, last

    def delete_for_user(self) -> int:
        with self.db.begin() as conn:
            return conn.execute(delete(db.wins).where(db.wins.c.owner == self.owner)).rowcount


class Slides:
    """How often each of a person's tasks was moved to a later day. -1 means "not a slug": the
    person said so, and it stays off the slugs list until it's moved again."""

    def __init__(self, engine: Engine, owner: str):
        self.db, self.owner = engine, owner

    def _key(self, task_id: str):
        return and_(db.task_slides.c.owner == self.owner, db.task_slides.c.task_id == str(task_id)[:200])

    def bump(self, task_id: str) -> int:
        now = int(time.time())
        with self.db.begin() as conn:
            have = conn.execute(select(db.task_slides.c.moves).where(self._key(task_id))).scalar()
            if have is None:
                conn.execute(insert(db.task_slides).values(owner=self.owner, task_id=str(task_id)[:200],
                                                           moves=1, last_moved=now))
                return 1
            moves = max(have, 0) + 1
            conn.execute(update(db.task_slides).where(self._key(task_id)).values(moves=moves, last_moved=now))
            return moves

    def unbump(self, task_id: str) -> None:
        """A move that was undone doesn't count."""
        with self.db.begin() as conn:
            have = conn.execute(select(db.task_slides.c.moves).where(self._key(task_id))).scalar()
            if have is None or have < 0:
                return
            if have <= 1:
                conn.execute(delete(db.task_slides).where(self._key(task_id)))
            else:
                conn.execute(update(db.task_slides).where(self._key(task_id)).values(moves=have - 1))

    def dismiss(self, task_id: str) -> None:
        """"Not a slug": off the list until it's moved again."""
        with self.db.begin() as conn:
            conn.execute(delete(db.task_slides).where(self._key(task_id)))
            conn.execute(insert(db.task_slides).values(owner=self.owner, task_id=str(task_id)[:200],
                                                       moves=-1, last_moved=int(time.time())))

    def clear(self, task_id: str) -> None:
        with self.db.begin() as conn:
            conn.execute(delete(db.task_slides).where(self._key(task_id)))

    def set(self, task_id: str, moves: int | None) -> None:
        """Put a count back as it was (Undo after Done or a new step cleared it). None: no count."""
        with self.db.begin() as conn:
            conn.execute(delete(db.task_slides).where(self._key(task_id)))
            if moves:
                conn.execute(insert(db.task_slides).values(owner=self.owner, task_id=str(task_id)[:200],
                                                           moves=int(moves), last_moved=int(time.time())))

    def counts(self) -> dict[str, int]:
        with self.db.connect() as conn:
            return {r[0]: r[1] for r in conn.execute(select(db.task_slides.c.task_id, db.task_slides.c.moves)
                                                     .where(db.task_slides.c.owner == self.owner))}

    def delete_for_user(self) -> int:
        with self.db.begin() as conn:
            return conn.execute(delete(db.task_slides).where(db.task_slides.c.owner == self.owner)).rowcount


def waiting_days(task: dict, today: date) -> int:
    due = (task.get("due") or {}).get("date")
    return max(0, (today - date.fromisoformat(due[:10])).days) if due else 0


def slugs(tasks: list[dict], moves: dict[str, int], today: date, min_moves: int = SLUG_MOVES) -> list[dict]:
    """Tasks that keep sliding: moved `min_moves`+ times, or waiting SLUG_WAITING+ days. Most stuck first."""
    out = []
    for t in tasks:
        n, waited = moves.get(str(t["id"]), 0), waiting_days(t, today)
        if n < 0:
            continue  # they said it isn't one
        if n >= min_moves or waited >= SLUG_WAITING:
            out.append({"id": str(t["id"]), "content": t["content"], "moves": n, "waiting_days": waited,
                        "has_steps": bool(t.get("steps"))})
    out.sort(key=lambda s: (-s["moves"], -s["waiting_days"]))
    return out[:MAX_SLUGS]


# --- search ---------------------------------------------------------------------------------------
def _fold_fast(text: str) -> str:
    """The same text as _fold gives, without the positions: the whole string at once, so a search
    can test thousands of pages quickly."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    return folded if folded.isascii() else "".join(c for c in folded if not unicodedata.combining(c))


def _fold(text: str) -> tuple[str, list[int]]:
    """`text` in lower case without accents, and where each folded character came from in `text`."""
    out: list[str] = []
    where: list[int] = []
    for i, ch in enumerate(text):
        f = "".join(c for c in unicodedata.normalize("NFKD", ch.casefold()) if not unicodedata.combining(c))
        out.append(f)
        where.extend([i] * len(f))
    return "".join(out), where


def _snippet(text: str, spans: list[tuple[int, int]]) -> list[dict]:
    """A short piece of `text` around the first match, as parts with the matches marked."""
    merged: list[list[int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    first = merged[0][0]
    a = max(0, first - SNIPPET // 3)
    while a > 0 and not text[a - 1].isspace() and first - a < SNIPPET // 2:  # start at a word
        a -= 1
    b = min(len(text), a + SNIPPET)
    while b < len(text) and not text[b].isspace() and b - a < SNIPPET + 30:  # end at one too
        b += 1
    parts: list[dict] = []
    pos = a
    for s, e in merged:
        s, e = max(s, a), min(e, b)
        if s >= e:
            continue
        if s > pos:
            parts.append({"t": text[pos:s], "m": False})
        parts.append({"t": text[s:e], "m": True})
        pos = e
    if pos < b:
        parts.append({"t": text[pos:b], "m": False})
    for p in parts:
        p["t"] = re.sub(r"\s+", " ", p["t"])
    if parts and a > 0:
        parts[0]["t"] = "…" + parts[0]["t"].lstrip()
    if parts and b < len(text):
        parts[-1]["t"] = parts[-1]["t"].rstrip() + "…"
    return parts


# --- notes -> tasks -------------------------------------------------------------------------------
# What a handwritten (or typed) line starts with. Checkboxes and bullets look like to-dos; a ticked
# box or a check mark is already done; a number is a list, but often not of things to do.
_PREFIX = re.compile(
    r"^\s*(?:(?P<done>\[[xX✓✔]\]|[☑☒✅✓✔])"
    r"|(?P<box>\[\s?\]|\(\s?\)|[□☐▢❏❑○])"
    r"|(?P<todo>to[\s-]?do\s*[:\-–—]|todo(?=\s))"
    r"|(?P<bullet>[-*+](?=\s)|[•–—·▪◦‣])"
    r"|(?P<num>\d{1,2}[.)](?=\s)))\s*", re.IGNORECASE)
_HEADER = re.compile(r"^\s*(?:my\s+)?(?:to[\s-]?dos?|tasks?|things\s+to\s+do)"
                     r"(?:\s+(?:list|today|tomorrow|this\s+week))?\s*:?\s*$", re.IGNORECASE)
_DONE_AFTER = re.compile(r"\s*[✓✔☑✅]\s*$")
_LISTY = {"box", "todo", "bullet", "list"}


def note_lines(text: str, today: date, on_list: set[str] | None = None) -> list[dict]:
    """A journal page's lines, each as a possible task for "Add lines to my list". Lines that look
    like to-dos (a drawn box, a bullet, "todo:", or a line under a "To do:" heading) are suggested;
    lines already ticked, already on the list, or with a word that couldn't be read are not.
    `task` is what Brain dump would make of the line (dates, times, #tags), and `text` is the line
    to send it, without the bullet."""
    from .mylist import parse_line
    on_list = on_list or set()
    out: list[dict] = []
    under_heading = False
    for number, raw in enumerate((text or "").splitlines()[:MAX_LINES * 2], start=1):
        if not raw.strip():
            under_heading = False  # a blank line ends a "To do:" list
            continue
        if _HEADER.match(raw):
            under_heading = True
            continue
        kinds, rest = set(), raw
        for _ in range(3):  # "- [ ] call mom": a bullet and a box
            m = _PREFIX.match(rest)
            if not m:
                break
            kinds.add(m.lastgroup)
            rest = rest[m.end():]
        if _DONE_AFTER.search(rest):  # "call mom ✓": ticked at the end of the line
            kinds.add("done")
            rest = _DONE_AFTER.sub("", rest)
        rest = rest.strip()
        if not re.search(r"\w", rest):
            continue
        if under_heading:
            kinds.add("list")
        task = parse_line(rest, today)
        if not task:
            continue
        done = "done" in kinds
        unclear = "[unclear]" in rest.lower()
        listed = task["content"].casefold() in on_list
        kind = next((k for k in ("done", "box", "todo", "bullet", "list", "num") if k in kinds), "")
        out.append({"line": number, "text": rest, "kind": kind, "done": done, "unclear": unclear, "on_list": listed,
                    "suggest": bool(kinds & _LISTY) and not (done or unclear or listed or task.get("problem"))
                               and len(rest) <= 200,
                    "task": task})
        if len(out) >= MAX_LINES:
            break
    return out


# --- download my journal --------------------------------------------------------------------------
README = """Your Next Box journal

One folder for each day (year-month-day), oldest first:
  notes.txt      what you wrote that day, read from your photos or typed
  photo-1.jpg    the photos of your journal pages
  wins.txt       what you finished that day

Only you can download your journal. Keep this somewhere safe, or share it with someone you trust.
"""


def day_notes_text(day: dict, photo_names: dict[str, str]) -> str:
    """notes.txt for one day: its win, then each page's notes with when it was snapped or written."""
    from .paper import clock, day_title
    out = [day_title(day["day"]), ""]
    out += [f"Your win: {p['win']}\n" for p in day["pages"] if p.get("win")]
    for p in day["pages"]:
        name = photo_names.get(p["id"])
        out.append(f"Snapped at {clock(p['created'])} ({name})" if name else f"Written at {clock(p['created'])}")
        out.append((p.get("notes") or "").strip() or "(no notes on this page)")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def day_wins_text(day: dict) -> str:
    from .paper import day_title
    wins = day["wins"]
    counted: dict[str, int] = {}
    for item in wins["items"]:
        counted[item] = counted.get(item, 0) + 1
    lines = [f"Wins on {day_title(day['day'])}: {wins['count']}", ""]
    lines += [f"✓ {item}" + (f" ×{n}" if n > 1 else "") for item, n in counted.items()]
    if wins["linked"]:
        lines.append(f"{'+' if counted else ''}{wins['linked']} {'more ' if counted else ''}in your linked app")
    if wins.get("other") and (counted or wins["linked"]):  # unnamed My list wins (older versions kept no name)
        lines.append(f"+{wins['other']} more")
    return "\n".join(lines) + "\n"


def journal_zip(days: list[dict], photo, out, root: str = "next-box-journal") -> None:
    """The journal as a .zip written to `out`: a folder for each day with notes.txt, its photos and
    wins.txt. `photo(page_id)` returns a page's JPEG; photos are fetched one at a time."""
    import zipfile
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{root}/README.txt", README)
        for day in days:
            folder = f"{root}/{day['day']}"
            names: dict[str, str] = {}
            for p in day["pages"]:
                image = photo(p["id"]) if p.get("has_photo") else None
                if image:
                    names[p["id"]] = f"photo-{len(names) + 1}.jpg"
                    z.writestr(f"{folder}/{names[p['id']]}", image, compress_type=zipfile.ZIP_STORED)
            if day["pages"]:
                z.writestr(f"{folder}/notes.txt", day_notes_text(day, names))
            if day["wins"]["count"]:
                z.writestr(f"{folder}/wins.txt", day_wins_text(day))
