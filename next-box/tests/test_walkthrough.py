"""Fixes from the final walkthrough of 0.14.2 (W1: a new person on a phone, W2: the owner and admins).

Server-side fixes are tested through the API, the CLI and dbtools. The browser-only ones were checked in a
real browser; the last tests pin the pieces that make them work, so a later change doesn't quietly undo one."""
import dataclasses
import json
import threading
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nextbox import auth, dbtools, server
from nextbox.auth import PREFS, Accounts
from nextbox.providers.base import ProviderError
from nextbox.providers.http import check_public_url
from nextbox.scan import ReadBack, RowMark
from nextbox.server import create_app

from conftest import rows, tiny_jpeg

WEB = Path(server.WEB_DIR)


def site(ctx, read=None):
    """boss (superadmin) and sam, My list only. `read` is what the fake camera sees."""
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    seen = read if read is not None else {}

    def vision(image, media_type, manifest):
        return ReadBack(label_code=None, rows=[RowMark(**r) for r in seen.get("rows", [])], notes=seen.get("notes", ""))
    app = create_app(ctx, vision=vision)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, app, login, seen


# ------------------------------------------------------------------ W2-01: Download my data asks first
def test_download_my_data_can_be_checked_without_counting(ctx):
    ctx, a, app, login, _ = site(ctx)
    sam = login("sam", "sam password")
    for _ in range(8):  # asking isn't a download
        assert sam.get("/auth/export/check").json() == {"ok": True}
    for _ in range(5):
        assert sam.get("/auth/export", params={"format": "zip"}).status_code == 200
    r = sam.get("/auth/export/check")
    assert r.status_code == 429 and "Try again" in r.json()["detail"]
    a.mark_reset("sam", "boss")
    r = sam.get("/auth/export/check")
    assert r.status_code == 403 and "paused" in r.json()["detail"]


# ------------------------------------------------------------------ W2-03, W2-06: db sql
def test_db_sql_shows_people_with_no_linked_app(tmp_path):
    Accounts(tmp_path).create("sam", "sam password")
    cols, got, _ = dbtools.run_sql(tmp_path, "SELECT username, connection FROM users")
    assert got[0][0] == "sam" and got[0][1] in (None, "null")
    dbtools.run_sql(tmp_path, "SELECT * FROM users")  # used to stop with 'NoneType' object has no attribute 'pop'


def test_db_sql_read_only_makes_no_files(tmp_path):
    Accounts(tmp_path).create("sam", "sam password")
    out = tmp_path / "elsewhere"
    out.mkdir()
    for sql in (f"ATTACH DATABASE '{out / 'x.db'}' AS x", f"  -- note\nVACUUM INTO '{out / 'z.db'}'",
                "detach x", f"/* hi */ attach '{out / 'y.db'}' as y"):
        with pytest.raises(PermissionError, match="read-only"):
            dbtools.run_sql(tmp_path, sql)
    assert not list(out.iterdir())
    assert dbtools.run_sql(tmp_path, "SELECT COUNT(*) FROM users")[1] == [[1]]


def test_every_table_is_described():
    from nextbox import db
    assert set(db.metadata.tables) <= set(dbtools.TABLE_NOTES)


# ------------------------------------------------------------------ W2-04: tag names stay private
def test_superadmin_sees_how_many_tags_not_their_names(ctx):
    ctx, a, app, login, _ = site(ctx)
    login("sam", "sam password").post("/list/dump", json={"text": "Session notes #therapy"})
    d = login("boss", "boss password").get("/super/users/sam").json()
    assert d["tag_count"] >= 1 and "tags" not in d and "therapy" not in json.dumps(d)


# ------------------------------------------------------------------ W2-05: a deleted account leaves no name on its invite
def test_deleting_an_account_takes_its_name_off_the_invite_it_used(ctx):
    ctx, a, app, login, _ = site(ctx)
    boss = login("boss", "boss password")
    code = boss.post("/admin/invites", json={}).json()["code"]
    zed = TestClient(app, base_url="http://testserver")
    assert zed.post("/auth/signup", json={"username": "zed", "password": "zed password", "invite": code,
                                          "agree": True}).status_code == 200
    assert zed.post("/auth/delete-account", json={"password": "zed password", "confirm": "zed"}).status_code == 200
    used = [i for i in boss.get("/admin/invites").json() if i["code"] == code][0]
    assert used["status"] == "used" and "zed" not in json.dumps(used)  # still used: the code can't be used again
    assert TestClient(app, base_url="http://testserver").post("/auth/signup", json={
        "username": "zed2", "password": "zed password", "invite": code, "agree": True}).status_code == 400


# ------------------------------------------------------------------ W2-07: the README's open-the-PNG line works
def test_cli_json_says_where_the_label_image_is(tmp_path, monkeypatch, capsys):
    from nextbox import cli
    Accounts(tmp_path).create("sam", "sam password")
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("NEXTBOX_DRY_RUN", "1")
    monkeypatch.setenv("TODOIST_TOKEN", "")
    assert cli.main(["--user", "sam", "--dry-run", "--json", "today"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["png"].endswith(".png") and Path(out["png"]).exists()


# ------------------------------------------------------------------ W2-11: a bad port says so
@pytest.mark.parametrize("url", ["https://cal.example.com:99999/x.ics", "https://dav.example.com:0x50/"])
def test_a_bad_port_gets_its_own_message(url):
    with pytest.raises(ProviderError, match="port number"):
        check_public_url(url)


# ------------------------------------------------------------------ W2-14: device ids aren't pieces of the hash
def test_device_ids_are_not_part_of_the_stored_hash(ctx):
    ctx, a, app, login, _ = site(ctx)
    sam, other = login("sam", "sam password"), login("sam", "sam password")
    devices = sam.get("/auth/sessions").json()
    hashes = [r["token_hash"] for r in rows(ctx.settings.data_dir, "sessions")]
    assert len(devices) == 2 and not any(h.startswith(d["id"]) for d in devices for h in hashes)
    them = next(d for d in devices if not d["current"])
    assert sam.delete(f"/auth/sessions/{them['id']}").status_code == 200
    assert other.get("/auth/me").status_code == 401 and sam.get("/auth/me").status_code == 200


# ------------------------------------------------------------------ W1-02: settings changed a moment apart are all kept
def test_settings_saved_at_the_same_moment_are_all_kept(tmp_path):
    a = Accounts(tmp_path)
    a.create("sam", "sam password")
    flips = {k: not d for k, (d, ok) in PREFS.items() if isinstance(d, bool) and ok(not d)}
    assert len(flips) >= 5
    start = threading.Barrier(len(flips))

    def save(k, v):
        start.wait()
        a.set_prefs("sam", {k: v})
    threads = [threading.Thread(target=save, args=kv) for kv in flips.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    prefs = a.get("sam")["prefs"]
    assert {k: prefs[k] for k in flips} == flips
    assert isinstance(auth._PREFS_LOCK, type(threading.Lock()))


# ------------------------------------------------------------------ W1-06: typos while joining stop counting once you've joined
def test_joining_clears_the_typos_made_while_joining(ctx):
    ctx, a, app, login, _ = site(ctx)
    code = login("boss", "boss password").post("/admin/invites", json={}).json()["code"]
    c = TestClient(app, base_url="http://testserver")
    join = {"password": "kim password", "agree": True}
    assert c.post("/auth/signup", json={**join, "username": "kim", "invite": "WRONG-CODE"}).status_code == 400
    for _ in range(2):
        assert c.post("/auth/signup", json={**join, "username": "sam", "invite": code}).status_code == 400
    assert c.post("/auth/signup", json={**join, "username": "kim", "invite": code}).status_code == 200
    c.post("/auth/logout")
    for _ in range(4):  # a fresh count: four mistakes are still allowed
        r = c.post("/auth/login", json={"username": "kim", "password": "not it"})
        assert r.status_code != 429, r.json()
    assert c.post("/auth/login", json={"username": "kim", "password": "kim password"}).status_code == 200


# ------------------------------------------------------------------ W1-08: Snap with no ticket yet says one thing
def test_snap_with_no_ticket_yet_doesnt_ask_to_pick_one(ctx):
    from nextbox import scan
    ctx, a, app, login, seen = site(ctx, read={"rows": [{"row": 1, "mark": "done", "confidence": 0.95}]})
    sam = login("sam", "sam password")
    rec = sam.post("/scan", files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")}).json()
    assert rec["candidates"] == [] and rec["errors"] == [scan.NO_TICKETS]


# ------------------------------------------------------------------ W1-09: messages people see are sentences
def test_messages_people_see_are_sentences(ctx):
    ctx, a, app, login, _ = site(ctx)
    sam = login("sam", "sam password")
    r = sam.post("/auth/delete-account", json={"password": "nope", "confirm": "sam"})
    assert r.json()["detail"] == "That password didn't match."
    r = sam.post("/auth/delete-account", json={"password": "sam password", "confirm": "sm"})
    assert r.json()["detail"] == "Type your username to confirm."
    r = sam.post("/list", json={"content": "   "})
    assert r.status_code == 400 and r.json()["detail"][0].isupper()
    with pytest.raises(auth.AuthError, match="^The username sam already exists"):
        a.create("sam", "another password")


# ------------------------------------------------------------------ browser-only fixes, pinned
@pytest.fixture(scope="module")
def js():
    return (WEB / "app.js").read_text()


def test_browser_fixes_are_in_place(js):
    html = (WEB / "index.html").read_text()
    # W1-01: a4/a5 have digits in them; the paper size is set as a parameter, not by a pattern.
    assert 'u.searchParams.set("paper", e.target.value)' in js and "paper=[a-z_]+" not in js
    # W1-02: one settings save at a time.
    assert "prefQueue = prefQueue.then(() => savePref(el, k, v, typed))" in js
    # W1-03: the journal line shows when only the paper size changed.
    assert '"journal_mode" in c || "paper_size" in c' in js
    # W1-04: times typed on the times question survive ← Back.
    assert "quiz.draft = { plan_at:" in js and "quiz.draft?.plan_at" in js
    # W1-05: focus stays somewhere useful after removing a journal page, a slug or a device.
    assert js.count("focusFirst(") >= 4
    # W1-07: hint chips don't break in the middle.
    assert ".hint code {" in html and "white-space:nowrap; }" in html.split(".hint code {", 1)[1].split("\n", 1)[0]
    # W2-01: Download my data asks first.
    assert 'api("/auth/export/check")' in js and html.count("data-export-data") == 1
    # W2-02: no Break it down on Just one thing without an AI key.
    assert "!me?.ai_ready || !t || asking" in js
    # W2-08: feedback moderation is a superadmin's, so its card is too.
    assert '<div class="card" data-min="superadmin" hidden><h2>Feedback</h2>' in html
    # W2-10: About · Privacy · Terms once on the sign-in page (the footer).
    assert html.count('<a href="privacy.html">Privacy</a>') == 1
