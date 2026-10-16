"""Review batch RA: AI safety (crisis words), journal privacy (read-back errors, the shared key, the
reset hold, db sql on PostgreSQL), exports (rate limits, no-store, a PDF that streams) and linked-app
tokens kept out of the log."""
import dataclasses
import io
import json
import os
import re
import zipfile
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import dump, rows, tiny_jpeg
from nextbox import coach as coaching
from nextbox import db, dbtools, paper, scan, upkeep
from nextbox import journal as journal_mod
from nextbox.auth import Accounts
from nextbox.coach import Breakdown, WeekNote
from nextbox.config import Settings
from nextbox.events import Events
from nextbox.feedback import Feedback
from nextbox.journal import Journal
from nextbox.providers import ProviderError
from nextbox.providers.http import Api
from nextbox.providers.todoist import Todoist
from nextbox.scan import ReadBack, RowMark
from nextbox.server import create_app

TODAY = date.today()
KEY = {"X-NextBox-Key": "k"}  # the conftest ctx's server key


class Coach:
    """A fake AI that records what would have been sent."""

    def __init__(self):
        self.sent: list[str] = []
        self.concern = False

    def breakdown(self, message):
        self.sent.append(message)
        return Breakdown(steps=[] if self.concern else ["Open the drawer", "Find the form"], concern=self.concern)

    def week_note(self, message):
        self.sent.append(message)
        return WeekNote(concern=False, went_well="A good week.", slug="", suggestion="Rest on Sunday.")


def site(ctx, coach=None, read=None):
    """boss (superadmin: the owner the shared key acts as) and sam, My list only. `read` is what the
    fake camera sees ({"fail": an exception} makes reading fail)."""
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    ctx.settings.anthropic_api_key = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    seen = read if read is not None else {}

    def vision(image, media_type, manifest):
        if seen.get("fail"):
            raise seen["fail"]
        return ReadBack(label_code=None, rows=[RowMark(**r) for r in seen.get("rows", [])],
                        notes=seen.get("notes", ""), win=seen.get("win", ""))
    app = create_app(ctx, vision=vision, coach=coach)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, app, login, seen


def snap(c, headers=None):
    return c.post("/scan", headers=headers or {}, files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")})


def events(ctx, action=None):
    return [e for e in rows(ctx.settings.data_dir, "events") if action is None or e["action"] == action]


def check_pdf(data: bytes) -> int:
    """A well-formed PDF: every cross-reference entry points at its object. Returns the page count."""
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    start = int(re.search(rb"startxref\n(\d+)\n%%EOF", data)[1])
    assert data[start:start + 4] == b"xref"
    head = re.match(rb"xref\n0 (\d+)\n", data[start:])
    size, table = int(head[1]), data[start + head.end():]
    for n in range(1, size):
        offset = int(table[n * 20:n * 20 + 10])
        assert data[offset:].startswith(f"{n} 0 obj".encode()), n
    return int(re.search(rb"/Type /Pages /Kids \[[^\]]*\] /Count (\d+)", data)[1])


def quoting_error() -> ValidationError:
    """What the Anthropic SDK raises for a read-back cut off mid-reply: pydantic quotes the reply."""
    try:
        ReadBack.model_validate_json('{"label_code": null, "rows": [], "notes": "upped my sertraline because of the '
                                     'panic attacks')
    except ValidationError as exc:
        assert "panic attacks" in str(exc)
        return exc
    raise AssertionError("expected a ValidationError")


# ------------------------------------------------------------------ R-A01: crisis words, wherever they are
@pytest.mark.parametrize("text", [
    "I want to hurt myself", "thinking about an overdose", "kms", "I'm going to kms tonight", "Kms.",
    "I just want to end it", "I want to end it now", "I cant go on", "I can’t go on like this", "I cannot go on.",
    "I dont want to be here anymore", "I do not want to wake up", "I don't want to live", "unalive myself",
    "there's no point living", "writing a suicide note", "harming myself again", "I can't go on\nsee you",
    "Call the helpline because I want to die today", "stop the self-harm urges ZZC2"])
def test_crisis_words_catch_the_common_ways_people_say_it(text):
    assert coaching.crisis(text)


@pytest.mark.parametrize("text", [
    "Kill the weeds", "ran 5 kms", "ran 10kms today", "Ran 21 kms this week", "We can't go on Friday",
    "I cant go on the trip", "Book the overdue car service", "Want to end it with a bang at the party",
    "Cut the hedge", "Harm reduction training", "Hurt my knee, see the doctor", "Do not want to forget milk"])
def test_crisis_words_leave_everyday_tasks_alone(text):
    assert not coaching.crisis(text)


def test_a_crisis_task_name_in_the_wins_or_slugs_gets_care_and_nothing_is_sent(ctx):
    fake = Coach()
    ctx, a, app, login, _ = site(ctx, fake)
    sam = login("sam", "sam password")
    t = sam.post("/list", json={"content": "Call the helpline because I want to die today"}).json()
    sam.post(f"/list/{t['id']}/done")
    sam.post("/journal", json={"notes": "A calm day."})
    r = sam.post("/journal/look-back", json={"include_notes": False}).json()  # the notes aren't even ticked
    assert r["concern"] is True and r["support"] == coaching.SUPPORT and "note" not in r
    assert fake.sent == [] and events(ctx, "ai_look_back") == []
    assert "helpline" not in json.dumps(events(ctx), default=str)


def test_a_crisis_slug_gets_care_too(ctx):
    fake = Coach()
    ctx, a, app, login, _ = site(ctx, fake)
    sam = login("sam", "sam password")
    t = sam.post("/list", json={"content": "Tell someone I don't want to be here anymore",
                                "due_date": TODAY.isoformat()}).json()
    for i in (1, 2, 3):
        sam.patch(f"/list/{t['id']}", json={"due_date": (TODAY + timedelta(days=i)).isoformat()})
    sam.patch(f"/list/{t['id']}", json={"due_date": TODAY.isoformat()})
    assert len(sam.get("/journal/slugs").json()["slugs"]) == 1
    r = sam.post("/journal/look-back", json={}).json()
    assert r["concern"] is True and fake.sent == []
    # An everyday week still gets its note.
    sam.delete(f"/list/{t['id']}", params={"confirm": "true"})
    done = sam.post("/list", json={"content": "Kill the weeds"}).json()
    sam.post(f"/list/{done['id']}/done")
    assert sam.post("/journal/look-back", json={}).json()["note"]["went_well"] == "A good week."


def test_break_it_down_checks_the_steps_and_the_hint_too(ctx):
    fake = Coach()
    ctx, a, app, login, _ = site(ctx, fake)
    sam = login("sam", "sam password")
    t = sam.post("/list", json={"content": "Tidy the desk",
                                "steps": [{"text": "stop the self-harm urges ZZC2", "done": False}]}).json()
    r = sam.post(f"/list/{t['id']}/breakdown").json()
    assert r["steps"] == [] and r["support"] == coaching.SUPPORT
    plain = sam.post("/list", json={"content": "Sort the post"}).json()
    r = sam.post(f"/list/{plain['id']}/breakdown", json={"hint": "honestly I just want to end it"}).json()
    assert r["steps"] == [] and r["support"] == coaching.SUPPORT
    assert fake.sent == [] and events(ctx, "ai_breakdown") == []
    assert "ZZC2" not in json.dumps(events(ctx), default=str)


def test_when_the_model_is_worried_about_a_task_only_the_kind_nudge_comes_back(ctx):
    fake = Coach()
    ctx, a, app, login, _ = site(ctx, fake)
    sam = login("sam", "sam password")
    t = sam.post("/list", json={"content": "Get away from him"}).json()
    fake.concern = True
    r = sam.post(f"/list/{t['id']}/breakdown").json()
    assert r == {"task": r["task"], "steps": [], "support": coaching.SUPPORT}
    ev, = events(ctx, "ai_breakdown")
    assert set(ev["detail"]) == {"ms"}  # that it ran: not what was asked or that it worried
    fake.concern = False
    assert sam.post(f"/list/{t['id']}/breakdown").json()["steps"] == ["Open the drawer", "Find the form"]
    # The model is asked for the flag, and told to leave the steps empty with it.
    assert "concern" in Breakdown.model_json_schema()["properties"]
    assert "set concern to true and leave the steps empty" in coaching.BREAKDOWN_SYSTEM


# ------------------------------------------------------------------ R-A02: read-back errors never quote notes
def test_a_failed_read_back_never_keeps_the_notes_its_error_quoted(ctx):
    ctx, a, app, login, seen = site(ctx, read={"fail": quoting_error()})
    sam = login("sam", "sam password")
    rec = snap(sam).json()
    assert rec["status"] == "failed" and rec["vision_error"] == "ValidationError"
    again = sam.post(f"/scan/{rec['id']}/retry").json()  # Try again fails the same way
    assert again["status"] == "failed" and again["vision_error"] == "ValidationError"
    boss = login("boss", "boss password")
    for path in ("/super/events", "/super/users/sam/bundle", "/admin/users/sam/activity", "/super/users/sam/tickets"):
        r = boss.get(path)
        assert r.status_code == 200, path
        assert "panic attacks" not in r.text, path
    assert "panic attacks" not in dump(ctx.settings.data_dir)
    assert any(e["error"] == "ValidationError" for e in events(ctx, "scan"))


def test_why_keeps_only_the_type_and_status():
    class Overloaded(Exception):
        status_code = 529
    assert scan._why(Overloaded("notes: my private words")) == "Overloaded (status 529)"
    assert scan._why(RuntimeError("my private words")) == "RuntimeError"
    assert scan._why(scan.ReadBackFailed("vision reply was cut short or declined")) == (
        "ReadBackFailed: vision reply was cut short or declined")


def test_claude_vision_turns_a_cut_short_reply_into_its_own_words(monkeypatch):
    import anthropic
    err = quoting_error()

    class Messages:
        def parse(self, **kwargs):
            raise err

    class Client:
        def __init__(self, **kwargs):
            self.messages = Messages()
    monkeypatch.setattr(anthropic, "Anthropic", Client)
    read = scan.claude_vision(Settings(anthropic_api_key="sk-test-not-real"))
    with pytest.raises(scan.ReadBackFailed) as caught:
        read(tiny_jpeg(), "image/jpeg", [])
    assert "panic attacks" not in str(caught.value) and caught.value.__suppress_context__


def test_upkeep_cuts_old_read_back_errors_to_their_type(ctx):
    root = ctx.settings.data_dir
    engine = db.database(root)
    quoted = str(quoting_error())
    ctx.store.save_scan({"id": "261016-AAAA", "by": "sam", "created_at": "2026-10-16T09:00:00", "status": "failed",
                         "vision_error": f"ValidationError: {quoted}"[:300]})
    ctx.store.save_scan({"id": "261016-BBBB", "by": "sam", "created_at": "2026-10-16T09:01:00", "status": "failed",
                         "vision_error": "APIStatusError (status 529)"})
    Events(root).log("sam", "scan", ok=False, kind="error", error=f"ValidationError: {quoted}"[:300])
    Feedback(root).submit(mode="open", kind="bug", page="scan", rating=None, fields={}, message="it broke",
                          who="sam", ip="", agent="", names=[],
                          debug={"recent_errors": [{"ts": 1, "action": "scan", "error": f"ValidationError: {quoted}"},
                                                   {"ts": 2, "action": "print", "error": "printer offline"}]})
    assert "panic attacks" in dump(root)
    upkeep.scrub_vision_errors(engine)
    assert "panic attacks" not in dump(root)
    assert ctx.store.get_scan("261016-AAAA")["vision_error"] == "ValidationError"
    assert ctx.store.get_scan("261016-BBBB")["vision_error"] == "APIStatusError (status 529)"  # left alone
    assert [e["error"] for e in events(ctx, "scan")] == ["ValidationError"]
    debug, = [r["debug"] for r in rows(root, "feedback_identities")]
    assert [e["error"] for e in debug["recent_errors"]] == ["ValidationError", "printer offline"]


# ------------------------------------------------------------------ R-A03: the shared key never reads the journal
def test_the_shared_key_can_snap_but_never_sees_the_journal(ctx):
    ctx, a, app, login, seen = site(ctx, read={"notes": "BOSSPRIVATE my divorce lawyer said", "win": "BOSSWIN"})
    key = TestClient(app, base_url="http://testserver")
    r = snap(key, KEY)
    assert r.status_code == 200
    rec = r.json()
    assert "notes" not in rec and "win" not in rec and "journal_id" not in rec and "BOSSPRIVATE" not in r.text
    assert key.get("/journal", headers=KEY).status_code == 403
    listed = key.get("/scans", headers=KEY).json()
    assert listed and all("journal_id" not in s for s in listed)
    assert all("journal_id" not in s for s in key.get("/scans", params={"pending": "true"}, headers=KEY).json())
    assert "journal_id" not in key.get(f"/scan/{rec['id']}", headers=KEY).json()
    # The person's taps are the person's: the key can't relabel, retry, confirm or undo.
    for path, body in (("label", {"label_id": "261016-ABCD"}), ("retry", None),
                       ("confirm", {"decisions": {}}), ("undo", {"row": 1})):
        r = key.post(f"/scan/{rec['id']}/{path}", json=body, headers=KEY)
        assert r.status_code == 403 and "BOSSPRIVATE" not in r.text, path
    # The owner, signed in, sees the page the photo made.
    boss = login("boss", "boss password")
    page, = [p for d in boss.get("/journal").json()["days"] for p in d["pages"]]
    assert page["notes"] == "BOSSPRIVATE my divorce lawyer said"
    mine = snap(boss).json()
    assert mine["notes"] == "BOSSPRIVATE my divorce lawyer said" and mine["win"] == "BOSSWIN" and mine["journal_id"]


def test_retry_with_the_key_is_refused_but_the_person_can(ctx):
    ctx, a, app, login, seen = site(ctx, read={"fail": RuntimeError("down")})
    key = TestClient(app, base_url="http://testserver")
    rec = snap(key, KEY).json()
    assert rec["status"] == "failed"
    seen.clear()
    seen.update(notes="BOSSPRIVATE second try")
    assert key.post(f"/scan/{rec['id']}/retry", headers=KEY).status_code == 403
    boss = login("boss", "boss password")
    again = boss.post(f"/scan/{rec['id']}/retry")
    assert again.status_code == 200, again.text
    again = again.json()
    assert again.get("status") != "failed" and again["notes"] == "BOSSPRIVATE second try"


# ------------------------------------------------------------------ R-A04: the reset hold covers every download
def test_during_a_reset_hold_only_print_my_journal_page_works(ctx):
    ctx, a, app, login, _ = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/journal", json={"notes": "A page to print."})
    a.mark_reset("sam", "boss")
    day = TODAY.isoformat()
    r = sam.get("/journal/export", params={"format": "zip", "day": day})
    assert r.status_code == 403 and "paused" in r.json()["detail"]
    assert "private" not in r.json()["detail"]  # it doesn't promise what the hold doesn't do
    assert "downloading all your data" in r.json()["detail"]
    assert sam.get("/journal/export", params={"format": "zip"}).status_code == 403
    assert sam.get("/auth/export").status_code == 403
    one = sam.get("/journal/export", params={"format": "pdf", "day": day})
    assert one.status_code == 200 and one.headers["content-disposition"].startswith("inline")


# ------------------------------------------------------------------ R-A05: Download my data: no-store and 5 an hour
def test_download_my_data_is_never_cached_and_the_zip_is_rate_limited(ctx):
    ctx, a, app, login, _ = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/journal", json={"notes": "Mine."})
    r = sam.get("/auth/export")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and "Cookie" in r.headers["vary"]
    for _ in range(5):
        r = sam.get("/auth/export", params={"format": "zip"})
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        assert "data.json" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    r = sam.get("/auth/export", params={"format": "zip"})
    assert r.status_code == 429 and "Try again" in r.json()["detail"]
    assert sam.get("/auth/export").status_code == 200  # the small JSON copy still works
    boss = login("boss", "boss password")
    assert boss.get("/auth/export", params={"format": "zip"}).status_code == 200  # per person


# ------------------------------------------------------------------ R-A06: bounded journal work
def _days(n, photo=True):
    return [{"day": (date(2026, 10, 1) + timedelta(days=i)).isoformat(),
             "pages": [{"id": f"p{i}", "notes": "Walked to the park.", "has_photo": photo, "created": 0, "win": ""}],
             "wins": {"count": 0, "items": [], "linked": 0}} for i in range(n)]


def test_the_journal_pdf_starts_going_out_before_any_sheet_is_drawn():
    drawn: list[str] = []

    def photo(page_id):
        drawn.append(page_id)
        return tiny_jpeg((60, 80))
    chunks = paper.journal_pdf_chunks(_days(3), "a5", photo)
    first = next(chunks)
    assert first.startswith(b"%PDF-1.4") and drawn == []  # the reply starts at once
    second = next(chunks)
    assert drawn == ["p0"] and b"/Subtype /Image" in second  # then a sheet at a time
    data = first + second + b"".join(chunks)
    assert check_pdf(data) == 3 and drawn == ["p0", "p1", "p2"]
    whole = io.BytesIO()
    assert paper.journal_pdf(_days(3), "a5", whole, lambda _: tiny_jpeg((60, 80))) == 3
    assert whole.getvalue() == data  # the same file as building it all first


def test_the_server_streams_the_journal_pdf(ctx):
    ctx, a, app, login, _ = site(ctx)
    sam = login("sam", "sam password")
    sam.post("/journal", json={"notes": "Typed today."})
    sam.post("/journal", json={"notes": "Typed earlier.", "day": (TODAY - timedelta(days=3)).isoformat()})
    r = sam.get("/journal/export", params={"format": "pdf"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "content-length" not in r.headers and r.headers["cache-control"] == "no-store"
    assert r.headers["content-disposition"].startswith("attachment")
    assert check_pdf(r.content) == 2


def test_a_runaway_day_stops_at_the_sheet_limit():
    huge = {"day": "2026-10-15", "wins": {"count": 0, "items": [], "linked": 0},
            "pages": [{"id": "a", "notes": "a\n" * 10_000, "has_photo": False, "created": 0, "win": ""}]}
    sheets = paper.journal_day_sheets(huge, "a5", lambda _: None)
    assert len(sheets) == paper.MAX_DAY_SHEETS
    normal = paper.journal_day_sheets({**huge, "pages": [{**huge["pages"][0], "notes": "a\n" * 30}]}, "a5",
                                      lambda _: None)
    assert len(normal) < paper.MAX_DAY_SHEETS


def test_a_photo_that_cant_be_drawn_doesnt_break_the_pdf():
    days = _days(2)
    data = b"".join(paper.journal_pdf_chunks(days, "letter", lambda pid: b"not a jpeg" if pid == "p0" else tiny_jpeg()))
    assert check_pdf(data) == 2


def test_search_matches_with_a_fast_fold_and_spells_out_only_the_pages_shown(ctx, monkeypatch):
    for text in ("Café crème", "Straße", "ﬁle", "İstanbul", "naïve", "한국어", "x́y", "Ⅻ ①", "ǅ", "plain ascii"):
        assert journal_mod._fold_fast(text) == journal_mod._fold(text)[0], text
    j = Journal(db.database(ctx.settings.data_dir), "sam")
    for i in range(60):
        j.add(TODAY.isoformat(), f"Coffee at the CAFÉ number {i}")
    j.add(TODAY.isoformat(), "Nothing here")
    calls = []
    real = journal_mod._fold

    def counting(text):
        calls.append(text)
        return real(text)
    monkeypatch.setattr(journal_mod, "_fold", counting)
    found, total = j.search("cafe")
    assert total == 60 and len(found) == journal_mod.SEARCH_LIMIT
    assert len(calls) == 1 + journal_mod.SEARCH_LIMIT  # the query, then only the pages shown
    assert all(any(p["m"] and p["t"].casefold() == "café" for p in f["parts"]) for f in found)


def test_search_and_new_typed_pages_have_a_limit(ctx):
    ctx, a, app, login, _ = site(ctx)
    sam = login("sam", "sam password")
    for _ in range(60):
        assert sam.get("/journal/search", params={"q": "park"}).status_code == 200
    r = sam.get("/journal/search", params={"q": "park"})
    assert r.status_code == 429 and "Try again" in r.json()["detail"]
    for i in range(300):
        assert sam.post("/journal", json={"notes": f"note {i}"}).status_code == 200
    r = sam.post("/journal", json={"notes": "one too many"})
    assert r.status_code == 429 and "Try again" in r.json()["detail"]


# ------------------------------------------------------------------ R-A07: db sql masking on PostgreSQL
def _plan(node_type, output, children=(), **fields):
    node = {"Node Type": node_type, "Output": list(output), **fields}
    if children:
        node["Plans"] = list(children)
    return node


def _scan(rel, alias, output, link="Outer", **fields):
    return _plan("Seq Scan", output, (), **{"Parent Relationship": link, "Relation Name": rel, "Schema": "public",
                                             "Alias": alias, **fields})


def _root(node):
    return {k: v for k, v in node.items() if k != "Parent Relationship"}


ALL = ["id", "owner", "day", "notes", "photo", "win"]


@pytest.mark.parametrize("plan, passed", [
    (_root(_scan("journal", "journal", ALL)), [(3, "notes"), (4, "photo"), (5, "win")]),  # SELECT *
    (_plan("Aggregate", ["count(*)"], [_scan("journal", "journal", ALL)]), []),             # count(*)
    (_plan("Hash Join", ["j.notes"], [_scan("journal", "j", ["j.notes", "j.owner"]),
                                      _plan("Hash", ["u.username"], [_scan("users", "u", ["u.username"])],
                                            **{"Parent Relationship": "Inner"})],
           **{"Hash Cond": "((j.owner)::text = (u.username)::text)"}), [(0, "notes")]),
    (_root(_scan("journal", "J", ["notes"])), [(0, "notes")]),
    (_root(_scan("journal", "journal", ["'my notes'::text"])), []),                          # only a literal
    (_root(_scan("journal", "journal", ["id"], Filter="((photo_sha)::text IS NULL)")), []),
])
def test_pg_plans_that_show_protected_columns_only_hidden_pass(plan, passed):
    assert dbtools._pg_plan_check(plan) == passed


@pytest.mark.parametrize("plan", [
    _root(_scan("journal", "journal", ["(notes || ''::text)"])),                     # a concatenation
    _root(_scan("journal", "j", ["j.id"], Filter="(j.notes ~~ 'P%'::text)")),        # WHERE notes LIKE
    _root(_scan("journal", "j'k", ['"j\'k".id'], Filter='("j\'k".notes = \'abc\'::text)')),  # a quote in a name
    _plan("Sort", ["id"], [_scan("journal", "journal", ["id", "notes"])], **{"Sort Key": ["journal.notes"]}),
    _root(_scan("journal", "j", ["row_to_json(j.*)"])),                               # the whole row
    _root(_scan("users", "users", ["length((hash)::text)"])),
    _plan("Subquery Scan", ["s.n"], [_plan("Limit", ["journal.notes"], [_scan("journal", "journal", ["journal.notes"])],
                                           **{"Parent Relationship": "Subquery"})], Alias="s"),
    _plan("Result", ["$0"], [_plan("Limit", ["journal.notes"], [_scan("journal", "journal", ["journal.notes"])],
                                   **{"Parent Relationship": "InitPlan", "Subplan Name": "InitPlan 1"})]),
    _plan("Append", [], [_scan("journal", "journal", ["journal.notes"], link="Member")]),
    _root(_plan("Result", ["query_to_xml('select notes from journal'::text, true, false, ''::text)"])),
    _plan("Function Scan", ["word"], **{"Function Call": "ts_stat('select to_tsvector(notes) from journal'::text)"}),
    _plan("Nested Loop", ["NULL::anyarray"], [_scan("pg_statistic", "s", ["s.stavalues1"], Schema="pg_catalog")]),
])
def test_pg_plans_that_would_read_a_protected_column_are_refused(plan):
    with pytest.raises(PermissionError, match="can't read journal text or password"):
        dbtools._pg_plan_check(plan)


def test_db_sql_never_shows_journal_text_or_hashes_whatever_the_query_calls_them(tmp_path):
    """On SQLite the values read as NULL (the authorizer); on PostgreSQL the query is refused."""
    a = Accounts(tmp_path)
    a.create("sam", "sam password")
    Journal(db.database(tmp_path), "sam").add(TODAY.isoformat(), "PGSECRETNOTE my diary", win="PGSECRETWIN")
    postgres = db.database(tmp_path).dialect.name != "sqlite"
    for sql in ("SELECT notes AS n FROM journal", "SELECT notes || '' AS x FROM journal",
                "SELECT win AS w FROM journal", "SELECT hash AS h FROM users",
                "SELECT n FROM (SELECT notes AS n FROM journal LIMIT 5) s",
                "SELECT id FROM journal WHERE notes LIKE 'PGSECRET%'"):
        try:
            cols, found, _ = dbtools.run_sql(tmp_path, sql)
        except PermissionError:
            assert postgres, sql
            continue
        assert not postgres, sql
        assert "PGSECRET" not in str(found) and "scrypt" not in str(found), sql
    cols, found, _ = dbtools.run_sql(tmp_path, "SELECT * FROM journal")
    shown = dict(zip(cols, found[0], strict=True))
    assert shown["notes"] == shown["win"] == "•••• (hidden)" and shown["owner"] == "sam"
    assert dbtools.run_sql(tmp_path, "SELECT owner, count(*) FROM journal GROUP BY owner")[1] == [["sam", 1]]


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="PostgreSQL only")
def test_db_sql_on_postgresql_runs_one_query_and_no_sql_hidden_in_strings(tmp_path):
    Accounts(tmp_path).create("sam", "sam password")
    Journal(db.database(tmp_path), "sam").add(TODAY.isoformat(), "PGSECRETNOTE my diary")
    for sql in ("SELECT 1; SELECT notes AS n FROM journal", "COPY journal TO STDOUT",
                "SELECT query_to_xml('select notes from journal', true, false, '')",
                "SELECT * FROM ts_stat('select to_tsvector(notes) from journal')",
                "SELECT most_common_vals::text FROM pg_stats WHERE tablename = 'journal'",
                "EXPLAIN ANALYZE SELECT id FROM journal WHERE notes LIKE 'PG%'"):
        with pytest.raises(PermissionError):
            dbtools.run_sql(tmp_path, sql)
    assert dbtools.run_sql(tmp_path, "SELECT notes FROM journal")[1] == [["•••• (hidden)"]]
    plan = dbtools.run_sql(tmp_path, "EXPLAIN SELECT * FROM journal")
    assert plan[0] == ["QUERY PLAN"] and "PGSECRET" not in str(plan[1])
    with pytest.raises(PermissionError, match="one query at a time.*syntax error"):
        dbtools.run_sql(tmp_path, "SELEC * FROM users")


# ------------------------------------------------------------------ R-A08: tokens stay out of the log
def test_a_transport_error_never_quotes_the_token():
    token = "eyJhbGciOiJIUzI1NiJ9.SECRETPART1\nSECRETPART2.sig"

    def refuse(request):
        raise httpx.LocalProtocolError(f"Illegal header value b'{token}'")
    api = Api("monday.com", "https://api.monday.com", {"Authorization": "x"},
              client=httpx.Client(base_url="https://api.monday.com", transport=httpx.MockTransport(refuse)),
              retries=0)
    with pytest.raises(ProviderError) as caught:
        api.call("POST", "/v2", json={})
    assert "SECRETPART" not in str(caught.value) and "LocalProtocolError" in str(caught.value)
    todo = Todoist("x", client=httpx.Client(base_url="https://api.todoist.com", transport=httpx.MockTransport(refuse)),
                   retries=0)
    with pytest.raises(ProviderError) as caught:
        todo.check()
    assert "SECRETPART" not in str(caught.value)


def test_a_pasted_token_with_a_hidden_character_is_refused_and_never_logged(ctx):
    ctx, a, app, login, _ = site(ctx)
    sam = login("sam", "sam password")
    r = sam.put("/connection", json={"provider": "todoist", "fields": {"token": "abc123SECRET\nTAIL456"}})
    assert r.status_code == 400 and r.json()["detail"] == "That API token has a hidden character in it. Copy it again."
    r = sam.post("/calendars", json={"kind": "caldav", "fields": {"url": "https://cal.example.com/", "username": "sam",
                                                                  "password": "pw\u0000SECRET2"}})
    assert r.status_code == 400 and "app password has a hidden character" in r.json()["detail"]
    stored = dump(ctx.settings.data_dir)
    assert "SECRET" not in stored and "TAIL456" not in stored
    assert sam.get("/connection").json()["connected"] is False
