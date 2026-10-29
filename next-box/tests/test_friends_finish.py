"""Friends and notes (SPEC §34): what three independent checks found, fixed. Nobody can find out who has an
account (not from the Blocked list, not from a block), Download my data says no more than Sent does, a photo
is only decoded when it can be sent (and never holds up the server), taking a request back can't get round
the day's limit or undo a quiet no, two people adding each other at once are friends, a request typed before
the account existed reaches them when asked again, a blocked person's reaction doesn't reach you, a note of
blank lines stays a label, old notes go on time, and a heart or a long note fits one die-cut label."""
import asyncio
import io
import json
import threading
import time
import zipfile

import pytest
from PIL import Image

from nextbox import db, friends, render
from nextbox.auth import Accounts
from nextbox.printers import Printers
from nextbox.server import create_app

from conftest import rows
from test_friends import SECRET, befriend, make_site, png_bytes, send_image, send_note, site  # noqa: F401

LONG = ("All the words a friend could fit in one note, typed on a phone, about Saturday and the plans. " * 4)[:280]


# ------------------------------------------------------------------ the Blocked list never tells who has an account
def test_blocking_a_name_you_only_asked_shows_just_that_name(site):
    """lee has an account and a display name ("Lee Park"); nobodyhere has neither. Asked and then blocked by
    username only, both look exactly alike, before and after a restart."""
    ctx, a, app, login = site
    sam = login("sam")
    for name in ("lee", "nobodyhere"):
        assert sam.post("/friends/add", json={"username": name}).json()["message"] == friends.SENT_BY_NAME
    for name in ("lee", "nobodyhere"):
        assert sam.post(f"/friends/{name}/block").status_code == 200
    page = sam.get("/friends").json()
    shown = {b["username"]: b for b in page["blocked"]}
    assert shown["lee"]["name"] == "lee" and shown["nobodyhere"]["name"] == "nobodyhere"
    assert {tuple(sorted(b)) for b in page["blocked"]} == {("at", "name", "username")}
    assert "Lee Park" not in json.dumps(page)
    assert "Lee Park" not in json.dumps(sam.get("/auth/export").json()["friends"])
    create_app(ctx)  # a restart: upkeep's sweep keeps a block on a name with no account too
    assert sorted(b["username"] for b in sam.get("/friends").json()["blocked"]) == ["lee", "nobodyhere"]
    for name in ("lee", "nobodyhere"):
        assert sam.delete(f"/friends/{name}/block").status_code == 200


def test_blocking_someone_you_knew_shows_the_name_you_saw(site):
    """A friend, someone whose request you could see, and someone who sent you a note: their display name."""
    ctx, a, app, login = site
    a.set_prefs("sam", {"display_name": "Sam Smith"})
    a.set_prefs("ana", {"display_name": "Ana Bell"})
    a.set_prefs("boss", {"display_name": "The Boss"})
    lee, ana, boss = login("lee"), login("ana"), login("boss")
    befriend(login, "lee", "sam")                          # a friend
    ana.post("/friends/add", json={"username": "lee"})     # asked lee: lee saw "Ana Bell"
    befriend(login, "boss", "lee")
    send_note(boss, "lee", text="hello")                   # sent lee a note, then stopped being friends
    boss.delete("/friends/lee")
    for name in ("sam", "ana", "boss"):
        assert lee.post(f"/friends/{name}/block").status_code == 200
    shown = {b["username"]: b["name"] for b in lee.get("/friends").json()["blocked"]}
    assert shown == {"sam": "Sam Smith", "ana": "Ana Bell", "boss": "The Boss"}


def test_blocking_someone_whose_request_is_waiting_looks_like_a_quiet_no(site):
    """sam's request is declined, ana's is blocked: on their side the two look the same (still waiting),
    taking them back works the same, and lee sees neither."""
    ctx, a, app, login = site
    sam, lee, ana = login("sam"), login("lee"), login("ana")
    sam.post("/friends/add", json={"username": "lee"})
    ana.post("/friends/add", json={"username": "lee"})
    lee.post("/friends/sam/decline")
    lee.post("/friends/ana/block")
    for asker in (sam, ana):
        assert [o["username"] for o in asker.get("/friends").json()["outgoing"]] == ["lee"]
    assert lee.get("/friends").json()["incoming"] == [] and lee.get("/notes/count").json()["requests"] == 0
    assert sam.delete("/friends/lee").status_code == ana.delete("/friends/lee").status_code == 200
    for asker in (sam, ana):  # asking again: the same answer, and lee still sees nothing
        assert asker.post("/friends/add", json={"username": "lee"}).json()["message"] == friends.SENT_BY_NAME
        assert [o["username"] for o in asker.get("/friends").json()["outgoing"]] == ["lee"]
    assert lee.get("/friends").json()["incoming"] == []
    assert lee.delete("/friends/ana/block").status_code == 200  # unblocked: ana's request doesn't come back
    assert lee.get("/friends").json()["incoming"] == []


# ------------------------------------------------------------------ Download my data says only what Sent says
def test_download_my_data_says_only_what_sent_shows(site):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    assert lee.patch("/settings", json={"friend_receipts": False}).status_code == 200
    [nid] = send_note(sam, "lee")["ids"]
    lee.post(f"/notes/{nid}/seen")
    lee.post(f"/notes/{nid}/keep", json={"keep": True})
    friends.Notes(a.db, "lee").mark_printed(nid)
    assert sam.get("/notes/sent").json()["notes"][0]["status"] == "delivered"
    for data in (sam.get("/auth/export").json(),
                 json.loads(zipfile.ZipFile(io.BytesIO(sam.get("/auth/export?format=zip").content)).read("data.json"))):
        [mine] = data["friends"]["notes"]["sent"]
        assert set(mine) == {"id", "kind", "color", "created", "text", "has_image", "to", "reaction", "status"}
        assert mine["status"] == "delivered" and mine["to"] == "lee" and mine["text"] == SECRET
    [got] = lee.get("/auth/export").json()["friends"]["notes"]["received"]  # lee's own copy: everything
    assert got["seen"] and got["printed"] and got["kept"] is True and got["from"] == "sam"
    lee.patch("/settings", json={"friend_receipts": True})
    [mine] = sam.get("/auth/export").json()["friends"]["notes"]["sent"]
    assert mine["status"] == "printed" and not {"seen", "printed", "kept"} & set(mine)


# ------------------------------------------------------------------ photos: checked first, decoded off the event loop
def test_a_photo_is_decoded_only_when_it_can_be_sent_and_never_on_the_event_loop(site, monkeypatch):
    ctx, a, app, login = site
    where, real = [], friends.clean_photo

    def spy(data):
        try:
            asyncio.get_running_loop()
            where.append("event loop")
        except RuntimeError:
            where.append("worker thread")
        return real(data)
    monkeypatch.setattr(friends, "clean_photo", spy)
    sam = login("sam")
    pic = png_bytes(Image.new("RGB", (800, 600), (200, 100, 50)), "JPEG")
    r = send_image(sam, "lee", "photo", pic)  # no friends yet: refused before the photo is even read
    assert r.status_code == 400 and "isn't on your friends list" in r.json()["detail"] and where == []
    befriend(login, "sam", "lee")
    assert send_image(sam, "lee", "photo", pic).status_code == 200 and where == ["worker thread"]
    monkeypatch.setattr(friends, "NOTES_PER_FRIEND_PER_DAY", 1)  # over the day's limit: refused first too
    r = send_image(sam, "lee", "photo", pic)
    assert r.status_code == 429 and "in the last 24 hours" in r.json()["detail"] and where == ["worker thread"]
    r = sam.post("/notes/preview/image", data={"kind": "photo"}, files={"image": ("p.jpg", pic, "image/jpeg")})
    assert r.status_code == 200 and where == ["worker thread"] * 2


def test_a_big_phone_photo_is_decoded_small_and_still_comes_out_right():
    big = Image.new("RGB", (4032, 3024), (30, 120, 200))
    big.paste((250, 250, 250), (0, 0, 2016, 3024))  # the left half white
    out = Image.open(io.BytesIO(friends.clean_photo(png_bytes(big, "JPEG", quality=90))))
    assert out.size == (1600, 1200)
    assert out.getpixel((200, 600))[0] > 240 and out.getpixel((1400, 600))[2] > 180


# ------------------------------------------------------------------ requests: taken back, at the same moment, before the account
def test_taking_a_request_back_still_counts_and_a_quiet_no_stands(site):
    ctx, a, app, login = site
    sam, lee, ana = login("sam"), login("lee"), login("ana")
    sam.post("/friends/add", json={"username": "lee"})
    lee.post("/friends/sam/decline")
    assert sam.delete("/friends/lee").json()["outgoing"] == []  # gone from sam's list
    assert sam.post("/friends/add", json={"username": "lee"}).json()["message"] == friends.SENT_BY_NAME
    assert [o["username"] for o in sam.get("/friends").json()["outgoing"]] == ["lee"]
    assert lee.get("/friends").json()["incoming"] == []  # lee's no stands
    assert lee.post("/friends/add", json={"username": "sam"}).json()["status"] == "friends"  # until lee asks
    # A request taken back is no request: the other one asking is a new request, not instant friends.
    ana.post("/friends/add", json={"username": "sam"})
    ana.delete("/friends/sam")
    assert sam.get("/friends").json()["incoming"] == [] and sam.get("/notes/count").json()["requests"] == 0
    assert friends.Friends(a.db, "sam").status_with("ana") == "none"
    assert sam.post("/friends/add", json={"username": "ana"}).json()["status"] == "requested"
    assert [p["username"] for p in ana.get("/friends").json()["incoming"]] == ["sam"]
    # The day's limit in the database counts what was asked, not what's still standing.
    boss = friends.Friends(a.db, "boss")
    for n in range(friends.REQUESTS_PER_DAY):
        boss.request(username=f"person{n}")
        boss.remove(f"person{n}")
    with pytest.raises(friends.FriendError) as no:
        boss.request(username="someone")
    assert no.value.status == 429 and no.value.message == friends.TOO_MANY_REQUESTS
    assert boss.lists()["outgoing"] == []
    assert friends.prune(a.db, now=int(time.time()) + 31 * 86400)["requests"] >= friends.REQUESTS_PER_DAY
    assert not [r for r in rows(ctx.settings.data_dir, "friendships") if r["status"] == "withdrawn"]


def test_two_people_adding_each_other_at_the_same_moment_are_friends(tmp_path, monkeypatch):
    """Both read "no request yet", then both write one: the second sees the first and they're friends."""
    engine = db.database(tmp_path)
    accounts = Accounts(tmp_path)
    real, done = friends._exists, threading.local()
    gate = threading.Barrier(2, timeout=10)

    def both_have_looked(conn, name):  # (just before writing the request) wait until the other has looked too
        if not getattr(done, "waited", False):
            done.waited = True
            gate.wait()
        return real(conn, name)
    monkeypatch.setattr(friends, "_exists", both_have_looked)
    for i in range(3):
        x, y = f"xan{i}", f"yul{i}"
        accounts.create(x, "a long password")
        accounts.create(y, "a long password")
        said, errors = {}, []
        gate.reset()

        def ask(me, other):
            done.waited = False
            try:
                said[me] = friends.Friends(engine, me).request(username=other)["status"]
            except Exception as exc:  # noqa: BLE001 - the test says what went wrong
                errors.append(repr(exc))
        threads = [threading.Thread(target=ask, args=(x, y)), threading.Thread(target=ask, args=(y, x))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert errors == [] and sorted(said.values()) == ["friends", "requested"], (said, errors)
        assert friends.Friends(engine, x).friends() == [y] and friends.Friends(engine, y).friends() == [x]


def test_a_request_typed_before_their_account_existed_reaches_them_when_asked_again(site):
    ctx, a, app, login = site
    sam = login("sam")
    sam.post("/friends/add", json={"username": "zoe"})   # Mike sends an invite and types the name at once
    sam.post("/friends/add", json={"username": "ziggy"})
    sam.delete("/friends/ziggy")                          # ...or took it back meanwhile
    time.sleep(1.1)  # (whole seconds)
    a.create("zoe", "zoe password")
    a.create("ziggy", "ziggy password")
    zoe, ziggy = login("zoe"), login("ziggy")
    assert zoe.get("/friends").json()["incoming"] == []  # the first one wasn't asked of her
    for name, who in (("zoe", zoe), ("ziggy", ziggy)):
        assert sam.post("/friends/add", json={"username": name}).json()["message"] == friends.SENT_BY_NAME
        assert [p["username"] for p in who.get("/friends").json()["incoming"]] == ["sam"]
        assert who.get("/notes/count").json()["requests"] == 1
    zoe.post("/friends/sam/decline")  # a quiet no still stands when asked again
    sam.post("/friends/add", json={"username": "zoe"})
    assert zoe.get("/friends").json()["incoming"] == []
    assert ziggy.post("/friends/sam/accept").status_code == 200


# ------------------------------------------------------------------ Sent: someone you blocked
def test_someone_you_blocked_cannot_signal_through_reactions(site):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    nid = sam.post("/notes", json={"to": ["lee"], "kind": "heart"}).json()["ids"][0]
    lee.post(f"/notes/{nid}/seen")
    assert sam.get("/notes/sent").json()["notes"][0]["status"] == "seen"
    assert sam.post("/friends/lee/block").status_code == 200
    assert lee.post(f"/notes/{nid}/react", json={"reaction": "🔥"}).status_code == 200  # nothing tells lee
    [it] = sam.get("/notes/sent").json()["notes"]
    assert it["reaction"] is None and it["status"] == "delivered"
    [mine] = sam.get("/auth/export").json()["friends"]["notes"]["sent"]
    assert mine["reaction"] is None and mine["status"] == "delivered"


# ------------------------------------------------------------------ a note stays a label
def test_a_note_of_blank_lines_stays_a_label(site):
    ctx, a, app, login = site
    check = friends.Notes(None, "x").check
    assert check("note", "a" + "\n" * 278 + "b", "yellow", None)[0] == "a\n\nb"
    assert check("photo", "on\r\n\r\n\r\n\r\nthe beach", None, b"?")[0] == "on\n\nthe beach"
    with pytest.raises(friends.FriendError) as no:
        check("note", "\n".join("abcdefghijklm"), "yellow", None)
    assert no.value.message == "Keep a note to 12 lines (this one has 13)."
    twelve = check("note", "\n".join(["Twenty letters here!"] * 12), "yellow", None)[0]
    assert render.render_friend_note("lee", "Oct 29", "note", twelve, "yellow").height < 800
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    r = sam.post("/notes", json={"to": ["lee"], "kind": "note", "text": "\n".join("abcdefghijklm")})
    assert r.status_code == 400 and "12 lines" in r.json()["detail"]
    [nid] = send_note(sam, "lee", text="a" + "\n" * 270 + "b")["ids"]
    label = Image.open(io.BytesIO(lee.get(f"/notes/{nid}/label.png").content))
    assert label.height < 600


def test_a_heart_and_a_long_note_fit_one_die_cut_label(site):
    for kind, text in (("heart", ""), ("note", LONG), ("note", "hi")):
        img = render.render_friend_note("Lee Park", "Oct 29", kind, text, "yellow", width=696, max_height=271)
        assert img.height <= 271, kind
    assert render.render_friend_note("Lee Park", "Oct 29", "heart", width=696).height > 400  # a roll: big
    ctx, a, app, login = site
    Printers(ctx.settings.data_dir, ctx.settings).add({"name": "SMALL", "driver": "brother_ql",
                                                       "address": "192.168.1.62", "label": "62x29", "width_px": 696})
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    heart = lee.post("/notes", json={"to": ["sam"], "kind": "heart"}).json()["ids"][0]
    [long_note] = send_note(lee, "sam", text=LONG)["ids"]
    for nid in (heart, long_note):
        label = Image.open(io.BytesIO(sam.get(f"/notes/{nid}/label.png").content))
        assert label.size == (696, 271), nid  # one label, not two with a cut line across the heart


# ------------------------------------------------------------------ old notes go on time
def test_old_notes_go_even_if_nobody_opens_the_feed(site, monkeypatch):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    [old] = send_note(sam, "lee", text="old")["ids"]
    month = int(time.time()) - 31 * 86400
    with db.database(ctx.settings.data_dir).begin() as conn:
        conn.execute(db.friend_notes.update().where(db.friend_notes.c.id == old).values(created=month))
    real = friends.prune
    monkeypatch.setattr(friends, "prune", lambda *args, **kw: {"notes": 0, "requests": 0})  # not run yet
    assert lee.get(f"/notes/{old}/label.png").status_code == 404
    assert lee.get("/notes/count").json()["unseen"] == 0 and sam.get("/notes/sent").json()["notes"] == []
    assert sam.get("/auth/export").json()["friends"]["notes"]["sent"] == []
    monkeypatch.setattr(friends, "prune", real)
    friends._pruned.clear()
    lee.get("/notes/count")  # the badge's minute-by-minute count deletes it
    assert rows(ctx.settings.data_dir, "friend_notes") == []


def test_the_limits_say_the_last_24_hours():
    assert friends.TOO_MANY_REQUESTS == "That's a lot of friend requests in the last 24 hours. Try again later."
    assert "tomorrow" not in friends.__doc__ and "today" not in friends.TOO_MANY_REQUESTS
