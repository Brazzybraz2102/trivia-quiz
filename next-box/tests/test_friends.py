"""Friends and notes (SPEC §34): adding friends, the four kinds of note, the swipe feed, printing only on a
tap, and the privacy rules (no admin view, nothing logged, 30 days, export, deletion, db sql masking)."""
import dataclasses
import io
import json
import time
import zipfile
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from nextbox import db, dbtools, friends, jobs, printer, render, server
from nextbox.auth import Accounts
from nextbox.config import PROJECT_DIR
from nextbox.printers import Printers
from nextbox.server import create_app

from conftest import dump, rows

SECRET = "PURPLEWALRUS secret plans for Saturday"  # what a note says: it must never show up anywhere else


def make_site(ctx, **settings):
    """boss (superadmin) and sam at Home; lee (admin) and ana in "Lee's place". Previews only unless a test
    turns dry_run off."""
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    for k, v in settings.items():
        setattr(ctx.settings, k, v)
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("sam", "sam password")
    other = a.create_household("Lee's place")
    a.create("lee", "lee password", role="admin", household_id=other)
    a.create("ana", "ana password", household_id=other)
    a.set_prefs("lee", {"display_name": "Lee Park"})
    app = create_app(ctx)
    clients = {}

    def login(name):
        if name not in clients:
            c = TestClient(app, base_url="http://testserver")
            assert c.post("/auth/login", json={"username": name, "password": f"{name} password"}).status_code == 200
            clients[name] = c
        return clients[name]
    return ctx, a, app, login


@pytest.fixture
def site(ctx):
    return make_site(ctx)


def befriend(login, x, y):
    code = login(y).get("/friends").json()["code"]
    assert login(x).post("/friends/add", json={"code": code}).json()["status"] == "requested"
    assert login(y).post(f"/friends/{x}/accept").status_code == 200


def png_bytes(img, fmt="PNG", **kw) -> bytes:
    out = io.BytesIO()
    img.save(out, fmt, **kw)
    return out.getvalue()


def doodle(size=(300, 200), transparent=True) -> bytes:
    img = Image.new("RGBA" if transparent else "RGB", size, (0, 0, 0, 0) if transparent else "white")
    d = ImageDraw.Draw(img)
    d.line((20, 20, size[0] - 20, size[1] - 20), fill=(0, 0, 0, 255), width=8)
    d.ellipse((60, 40, 140, 120), outline=(0, 0, 0, 255), width=6)
    return png_bytes(img)


def photo_with_gps(size=(400, 300)) -> bytes:
    img = Image.new("RGB", size, (200, 120, 40))
    ImageDraw.Draw(img).rectangle((50, 50, 200, 150), fill=(20, 40, 200))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"                  # Make
    exif[0x0112] = 6                             # Orientation: turn 90° to stand it up
    exif[0x8825] = {1: "N", 2: (40.0, 26.0, 46.0), 3: "W", 4: (79.0, 58.0, 56.0)}  # GPS: where it was taken
    return png_bytes(img, "JPEG", exif=exif, quality=95)


def shades(img) -> set[int]:
    """The gray levels an image uses."""
    return {v for v, n in enumerate(img.convert("L").histogram()) if n}


def send_note(c, to, text=SECRET, color="pink"):
    r = c.post("/notes", json={"to": to if isinstance(to, list) else [to], "kind": "note", "text": text,
                               "color": color})
    assert r.status_code == 200, r.text
    return r.json()


def send_image(c, to, kind, data, caption="", name="p.jpg", mime="image/jpeg"):
    return c.post("/notes/image", data={"to": to, "kind": kind, "caption": caption},
                  files={"image": (name, data, mime)})


# ------------------------------------------------------------------ adding friends
def test_friend_link_qr_and_rotating_it(site, monkeypatch):
    ctx, a, app, login = site
    sam = login("sam")
    page = sam.get("/friends")
    assert page.headers["cache-control"] == "no-store"
    me = page.json()
    assert me["friends"] == me["incoming"] == me["outgoing"] == me["blocked"] == [] and me["receipts"] is True
    assert len(me["code"]) == friends.CODE_LENGTH and me["link"] == f"http://testserver/app/#friend={me['code']}"
    through = sam.get("/friends", headers={"cf-ray": "x", "host": "nextbox.braznarkel.com"}).json()["link"]
    assert through == f"https://nextbox.braznarkel.com/app/#friend={me['code']}"  # came in through Cloudflare
    monkeypatch.setenv("NEXTBOX_PUBLIC_URL", "https://nextbox.braznarkel.com/")
    assert sam.get("/friends").json()["link"] == f"https://nextbox.braznarkel.com/app/#friend={me['code']}"
    qr = sam.get("/friends/code/qr.png")
    assert qr.status_code == 200 and qr.headers["content-type"] == "image/png" and qr.headers["cache-control"] == "no-store"
    assert Image.open(io.BytesIO(qr.content)).size[0] > 200
    lee = login("lee")
    assert lee.get(f"/friends/code/{me['code']}").json() == {"username": "sam", "name": "sam", "status": "none"}
    assert lee.get(f"/friends/code/{me['code'].lower()}").json()["username"] == "sam"  # typed in lower case
    assert sam.get(f"/friends/code/{me['code']}").json()["status"] == "you"
    new = sam.post("/friends/code/rotate").json()
    assert new["code"] != me["code"] and new["link"].endswith(new["code"])
    gone = lee.get(f"/friends/code/{me['code']}")
    assert gone.status_code == 404 and gone.json()["detail"] == friends.BAD_CODE
    assert lee.post("/friends/add", json={"code": me["code"]}).status_code == 404


def test_a_request_has_to_be_accepted_and_friends_can_be_in_any_household(site):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    code = lee.get("/friends").json()["code"]
    r = sam.post("/friends/add", json={"code": code}).json()
    assert r["status"] == "requested" and r["message"] == friends.SENT_BY_CODE
    assert r["friends"]["outgoing"] == [{"username": "lee", "name": "lee", "at": r["friends"]["outgoing"][0]["at"]}]
    assert sam.post("/notes", json={"to": ["lee"], "kind": "heart"}).status_code == 400  # not friends yet
    incoming = lee.get("/friends").json()["incoming"]
    assert [p["username"] for p in incoming] == ["sam"] and lee.get("/notes/count").json() == {"unseen": 0, "requests": 1}
    assert lee.get(f"/friends/code/{sam.get('/friends').json()['code']}").json()["status"] == "asked_you"
    assert sam.post("/friends/lee/accept").status_code == 404  # only the one who was asked can accept
    after = lee.post("/friends/sam/accept").json()
    assert [p["username"] for p in after["friends"]] == ["sam"] and after["incoming"] == []
    mine = sam.get("/friends").json()
    assert mine["friends"][0]["username"] == "lee" and mine["friends"][0]["name"] == "Lee Park"  # their display name
    assert mine["outgoing"] == []
    assert sam.post("/friends/add", json={"code": code}).json()["message"] == friends.ALREADY
    assert lee.get("/notes/count").json()["requests"] == 0


def test_if_both_ask_they_are_friends_straight_away(site):
    ctx, a, app, login = site
    sam, ana = login("sam"), login("ana")
    assert sam.post("/friends/add", json={"username": "ana"}).json()["status"] == "requested"
    r = ana.post("/friends/add", json={"username": "sam"}).json()
    assert r["status"] == "friends" and r["message"] == friends.NOW_FRIENDS
    assert [p["username"] for p in sam.get("/friends").json()["friends"]] == ["ana"]
    assert sam.post("/friends/add", json={"username": "SAM"}).json()["detail"].startswith("That's you")
    assert sam.post("/friends/add", json={"username": "no spaces!"}).json()["detail"] == friends.NOT_A_NAME
    assert sam.post("/friends/add", json={}).status_code == 400


def test_adding_by_username_never_tells_who_has_an_account(site):
    """The same reply for a real name, a name nobody has, someone who blocked you, and a repeat; the
    asker's list shows only what they typed; and nobody else ever sees a request meant for nobody."""
    ctx, a, app, login = site
    sam, ana = login("sam"), login("ana")
    # ana blocks sam (from his request), so his next one is never shown to her
    sam.post("/friends/add", json={"username": "ana"})
    assert ana.post("/friends/sam/block").status_code == 200
    replies = {}
    for name in ("lee", "nobody", "ana", "lee"):
        r = sam.post("/friends/add", json={"username": name})
        assert r.status_code == 200
        body = r.json()
        replies[name] = (body["status"], body["message"])
    assert set(replies.values()) == {("requested", friends.SENT_BY_NAME)}
    out = sam.get("/friends").json()["outgoing"]
    assert {o["username"] for o in out} == {"lee", "nobody", "ana"}
    assert all(o["name"] == o["username"] and set(o) == {"username", "name", "at"} for o in out)  # "Lee Park" isn't shown
    assert ana.get("/friends").json()["incoming"] == [] and ana.get("/notes/count").json()["requests"] == 0
    assert [p["username"] for p in login("lee").get("/friends").json()["incoming"]] == ["sam"]
    # Someone who later takes the name "nobody" never sees sam's request, and asking sam isn't instant friendship.
    time.sleep(1.1)  # (joined after the request, in whole seconds)
    a.create("nobody", "nobody password")
    nobody = login("nobody")
    assert nobody.get("/friends").json()["incoming"] == []
    assert nobody.post("/friends/add", json={"username": "sam"}).json()["status"] == "requested"
    assert [p["username"] for p in sam.get("/friends").json()["incoming"]] == ["nobody"]


def test_saying_no_is_quiet_and_requests_can_be_taken_back(site):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    sam.post("/friends/add", json={"username": "lee"})
    assert lee.post("/friends/sam/decline").json()["incoming"] == []
    assert [o["username"] for o in sam.get("/friends").json()["outgoing"]] == ["lee"]  # still waiting, as far as sam knows
    assert sam.post("/friends/add", json={"username": "lee"}).json()["message"] == friends.SENT_BY_NAME
    assert lee.get("/friends").json()["incoming"] == []
    assert lee.post("/friends/sam/accept").json()["detail"] == friends.NO_REQUEST
    assert lee.post("/friends/add", json={"username": "sam"}).json()["status"] == "friends"  # lee changed their mind
    assert sam.delete("/friends/lee").json()["friends"] == []  # remove a friend
    assert lee.get("/friends").json()["friends"] == []
    sam.post("/friends/add", json={"username": "lee"})
    assert sam.delete("/friends/lee").json()["outgoing"] == []  # take a request back
    assert lee.get("/friends").json()["incoming"] == []
    assert sam.delete("/friends/lee").status_code == 404


def test_blocking_stops_requests_and_notes(site):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    send_note(sam, "lee")
    assert len(lee.get("/notes/feed").json()["notes"]) == 1
    assert lee.post("/friends/ana/block").json()["detail"] == friends.CANT_BLOCK  # no reason to: never in touch
    page = lee.post("/friends/sam/block").json()
    assert page["friends"] == [] and [b["username"] for b in page["blocked"]] == ["sam"]
    assert lee.get("/notes/feed").json()["notes"] == [] and lee.get("/notes/count").json()["unseen"] == 0
    r = sam.post("/notes", json={"to": ["lee"], "kind": "heart"})
    assert r.status_code == 400 and "isn't on your friends list" in r.json()["detail"]
    assert sam.post("/friends/add", json={"username": "lee"}).json()["message"] == friends.SENT_BY_NAME
    assert lee.get("/friends").json()["incoming"] == []
    assert "Unblock them first" in lee.post("/friends/add", json={"username": "sam"}).json()["detail"]
    assert lee.delete("/friends/sam/block").json()["blocked"] == []
    assert lee.get("/friends").json()["friends"] == []  # unblocking doesn't make them friends again
    assert lee.delete("/friends/sam/block").status_code == 404
    assert lee.post("/friends/add", json={"username": "sam"}).json()["status"] == "friends"  # both asked now


# ------------------------------------------------------------------ sending and the feed
def test_the_four_kinds_of_note(site):
    ctx, a, app, login = site
    sam, lee, ana = login("sam"), login("lee"), login("ana")
    befriend(login, "sam", "lee")
    befriend(login, "sam", "ana")
    r = send_note(sam, ["lee", "ana"])
    assert r["sent"] == 2 and r["message"] == "Sent to Lee Park and ana."
    assert sam.post("/notes", json={"to": ["lee"], "kind": "heart"}).json()["sent"] == 1
    assert send_image(sam, "lee", "draw", doodle(), caption="a cat", name="d.png", mime="image/png").json()["sent"] == 1
    assert send_image(sam, "lee,ana", "photo", photo_with_gps(), caption="the view").json()["sent"] == 2
    feed = lee.get("/notes/feed").json()
    assert feed["unseen"] == 4 and lee.get("/notes/count").json()["unseen"] == 4
    kinds = [n["kind"] for n in feed["notes"]]
    assert sorted(kinds) == ["draw", "heart", "note", "photo"]
    note = next(n for n in feed["notes"] if n["kind"] == "note")
    assert note["text"] == SECRET and note["color"] == "pink" and note["image"] is None and note["can_reply"]
    assert note["from"] == {"username": "sam", "name": "sam"} and note["expires"] == note["created"] + 30 * 86400
    pic = next(n for n in feed["notes"] if n["kind"] == "photo")
    assert pic["text"] == "the view" and pic["image"] == f"/notes/{pic['id']}/image"
    got = lee.get(pic["image"])
    assert got.headers["content-type"] == "image/jpeg" and got.headers["cache-control"] == "no-store"
    drawn = next(n for n in feed["notes"] if n["kind"] == "draw")
    assert lee.get(drawn["image"]).headers["content-type"] == "image/png"
    # Only the sender and the recipient can open it.
    for outsider in ("ana", "boss"):
        assert login(outsider).get(pic["image"]).status_code == 404
    assert sam.get(pic["image"]).status_code == 200
    # Things that can't be sent, in plain words
    for body, says in (({"to": ["lee"], "kind": "note", "text": "x" * 281}, "Keep a note to 280 characters"),
                       ({"to": ["lee"], "kind": "note", "text": "  "}, "Write something first."),
                       ({"to": ["lee"], "kind": "note", "text": "hi", "color": "plaid"}, "Pick one of the note colors."),
                       ({"to": ["lee"], "kind": "gif"}, "Pick a note, a drawing, a photo or a heart."),
                       ({"to": [], "kind": "heart"}, "Pick a friend to send it to."),
                       ({"to": ["boss"], "kind": "heart"}, "boss isn't on your friends list"),
                       ({"to": ["lee"], "kind": "photo"}, "POST /notes/image")):
        r = sam.post("/notes", json=body)
        assert r.status_code == 400 and says in r.json()["detail"], (body, r.json())
    r = send_image(sam, "lee", "photo", photo_with_gps(), caption="c" * 141)
    assert r.status_code == 400 and "140 characters" in r.json()["detail"]
    r = sam.post("/notes/image", data={"to": "lee", "kind": "photo"})  # no file
    assert r.status_code == 400 and r.json()["detail"] == "Pick a photo first."
    r = sam.post("/notes/image", data={"kind": "draw"}, files={"image": ("d.png", doodle(), "image/png")})
    assert r.status_code == 400 and r.json()["detail"] == "Pick a friend to send it to."


def test_photos_are_cleaned_exif_and_location_gone(site):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    raw = photo_with_gps()
    assert Image.open(io.BytesIO(raw)).getexif().get_ifd(0x8825)  # the original says where it was taken
    send_image(sam, "lee", "photo", raw)
    [row] = rows(ctx.settings.data_dir, "friend_notes")
    kept = Image.open(io.BytesIO(row["image"]))
    assert kept.format == "JPEG" and kept.size == (300, 400)  # stood upright by its Orientation
    assert not kept.getexif() and "exif" not in kept.info and "icc_profile" not in kept.info
    assert b"PhoneMaker" not in row["image"] and b"GPS" not in row["image"]
    big = png_bytes(Image.effect_noise((4000, 3000), 60).convert("RGB"), "JPEG", quality=95)
    assert len(big) > friends.PHOTO_BYTES
    cleaned = Image.open(io.BytesIO(friends.clean_photo(big)))
    assert max(cleaned.size) <= friends.PHOTO_EDGE and len(friends.clean_photo(big)) <= friends.PHOTO_BYTES
    for bad, mime in ((b"%PDF-1.4 not a picture", "application/pdf"), (b"GIF89a\x00\x00", "image/gif")):
        r = send_image(sam, "lee", "photo", bad, mime=mime)
        assert r.status_code == 415 and "Try a JPEG or PNG" in r.json()["detail"]
    tiff = png_bytes(Image.new("RGB", (20, 20)), "TIFF")
    assert send_image(sam, "lee", "photo", tiff, mime="image/tiff").status_code == 415  # not a type it takes
    r = send_image(sam, "lee", "photo", b"\0" * (friends.PHOTO_UPLOAD + 1))
    assert r.status_code == 413 and r.json()["detail"] == "That photo is too big. Pick one under 15 MB."
    heic = b"\0\0\0\x18ftypheic" + b"\0" * 64
    r = send_image(sam, "lee", "photo", heic, mime="image/heic")
    assert r.status_code == 415 and ("HEIC" in r.json()["detail"] or "Try a JPEG" in r.json()["detail"])
    assert len(rows(ctx.settings.data_dir, "friend_notes")) == 1  # nothing else got through


def test_doodles_become_black_ink_and_are_checked_too(site):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    assert send_image(sam, "lee", "draw", doodle(), mime="image/png").status_code == 200  # drawn on a see-through canvas
    [row] = rows(ctx.settings.data_dir, "friend_notes")
    kept = Image.open(io.BytesIO(row["image"]))
    assert kept.format == "PNG" and kept.mode == "1" and kept.size == (300, 200)
    gray = kept.convert("L")
    assert gray.getpixel((5, 5)) == 255 and gray.getpixel((150, 100)) == 0  # white paper, black ink
    r = send_image(sam, "lee", "draw", png_bytes(Image.new("RGBA", (100, 100), (0, 0, 0, 0))), mime="image/png")
    assert r.status_code == 400 and r.json()["detail"] == "Draw something first."
    r = send_image(sam, "lee", "draw", b"\0" * (friends.DOODLE_UPLOAD + 1), mime="image/png")
    assert r.status_code == 413 and r.json()["detail"] == "That drawing is too big to send."
    r = send_image(sam, "lee", "draw", png_bytes(Image.new("RGB", (40, 40)), "GIF"), mime="image/gif")
    assert r.status_code == 415
    r = send_image(sam, "lee", "draw", png_bytes(Image.new("1", (3000, 3000), 1)), mime="image/png")
    assert r.status_code == 413 and "far too large" in r.json()["detail"]
    r = send_image(sam, "lee", "note", doodle(), mime="image/png")
    assert r.status_code == 400


def test_feed_unseen_first_then_newest_with_a_cursor(site, monkeypatch):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    clock = [1_800_000_000]
    monkeypatch.setattr(friends, "_now", lambda: clock[0])
    monkeypatch.setattr(friends, "prune", lambda *a, **k: {})  # the clock above is made up
    ids = []
    for n in range(5):
        clock[0] += 60
        ids += send_note(sam, "lee", text=f"note {n}")["ids"]
    assert lee.post(f"/notes/{ids[3]}/seen").json() == {"id": ids[3], "seen": clock[0]}
    assert lee.post(f"/notes/{ids[1]}/seen").status_code == 200
    assert sam.post(f"/notes/{ids[1]}/seen").status_code == 404  # the sender can't
    feed = lee.get("/notes/feed").json()
    assert [n["id"] for n in feed["notes"]] == [ids[4], ids[2], ids[0], ids[3], ids[1]] and feed["unseen"] == 3
    got, cursor = [], None
    while True:
        page = lee.get("/notes/feed", params={"limit": 2, **({"cursor": cursor} if cursor else {})}).json()
        got += [n["id"] for n in page["notes"]]
        cursor = page["next"]
        if not cursor:
            break
    assert got == [ids[4], ids[2], ids[0], ids[3], ids[1]]
    assert lee.get("/notes/feed", params={"cursor": "junk"}).status_code == 400
    assert lee.get("/notes/count").json() == {"unseen": 3, "requests": 0}
    sent = sam.get("/notes/sent", params={"limit": 3}).json()
    assert [n["id"] for n in sent["notes"]] == [ids[4], ids[3], ids[2]] and sent["next"]
    more = sam.get("/notes/sent", params={"cursor": sent["next"]}).json()
    assert [n["id"] for n in more["notes"]] == [ids[1], ids[0]] and more["next"] is None


def test_react_keep_and_delete_for_me(site):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    [nid] = send_note(sam, "lee")["ids"]
    item = lee.post(f"/notes/{nid}/react", json={"reaction": "❤️"}).json()
    assert item["reaction"] == "❤️" and item["seen"]
    assert lee.post(f"/notes/{nid}/react", json={"reaction": "🍕"}).json()["detail"] == "Pick one of the reactions."
    assert sam.get("/notes/sent").json()["notes"][0]["reaction"] == "❤️"
    assert lee.post(f"/notes/{nid}/react", json={"reaction": None}).json()["reaction"] is None
    kept = lee.post(f"/notes/{nid}/keep", json={"keep": True}).json()
    assert kept["kept"] is True and kept["expires"] is None
    assert sam.post(f"/notes/{nid}/keep", json={"keep": True}).status_code == 404
    assert lee.delete(f"/notes/{nid}").status_code == 400  # needs confirm
    assert lee.delete(f"/notes/{nid}?confirm=true").json() == {"ok": True}
    assert lee.get("/notes/feed").json()["notes"] == [] and lee.get(f"/notes/{nid}/label.png").status_code == 404
    assert [n["id"] for n in sam.get("/notes/sent").json()["notes"]] == [nid]  # still in sam's Sent list
    assert sam.delete(f"/notes/{nid}?confirm=true").json() == {"ok": True}
    assert rows(ctx.settings.data_dir, "friend_notes") == []  # gone for both: gone for good
    assert lee.delete(f"/notes/{nid}?confirm=true").status_code == 404


def test_sent_says_delivered_seen_printed_unless_they_turned_it_off(site):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    [nid] = send_note(sam, "lee")["ids"]

    def status():
        return sam.get("/notes/sent").json()["notes"][0]["status"]
    assert status() == "delivered"
    lee.post(f"/notes/{nid}/seen")
    assert status() == "seen"
    friends.Notes(a.db, "lee").mark_printed(nid)
    assert status() == "printed"
    assert lee.patch("/settings", json={"friend_receipts": False}).json()["friend_receipts"] is False
    assert status() == "delivered" and lee.get("/friends").json()["receipts"] is False
    assert lee.patch("/settings", json={"friend_receipts": "no"}).status_code == 400


def test_a_note_preview_shows_how_it_prints(site):
    ctx, a, app, login = site
    sam = login("sam")
    r = sam.post("/notes/preview", json={"kind": "note", "text": "See you at 5!", "color": "yellow"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.headers["cache-control"] == "no-store"
    img = Image.open(io.BytesIO(r.content))
    assert img.width == render.WIDTH and shades(img) <= {0, 255}  # black and white only
    assert sam.post("/notes/preview", json={"kind": "note", "text": ""}).status_code == 200  # while typing
    assert sam.post("/notes/preview", json={"kind": "heart"}).status_code == 200
    assert sam.post("/notes/preview", json={"kind": "note", "text": "x" * 300}).status_code == 400
    r = sam.post("/notes/preview/image", data={"kind": "photo", "caption": "hi"},
                 files={"image": ("p.jpg", photo_with_gps(), "image/jpeg")})
    assert r.status_code == 200 and shades(Image.open(io.BytesIO(r.content))) <= {0, 255}
    r = sam.post("/notes/preview/image", data={"kind": "draw"}, files={"image": ("d.png", doodle(), "image/png")})
    assert r.status_code == 200
    assert rows(ctx.settings.data_dir, "friend_notes") == []  # nothing kept


def test_the_label_layout_for_each_kind():
    for kind, text in (("note", "Hello there, see you at five!"), ("heart", ""), ("draw", "a cat"), ("photo", "the view")):
        pic = Image.open(io.BytesIO(doodle())) if kind == "draw" else Image.new("RGB", (400, 300), (90, 140, 200))
        img = render.render_friend_note("Sam", "Oct 29", kind, text, "yellow" if kind == "note" else None,
                                        pic if kind in ("draw", "photo") else None)
        assert img.width == render.WIDTH and 150 < img.height < 1400, kind
    narrow = render.render_friend_note("Sam", "Oct 29", "note", "hi", None, None, width=306)
    assert narrow.width == 306
    # A die-cut label: the photo shrinks so everything fits on one label.
    w, h = printer.brother_label("62x100").dots_printable
    fitted = render.render_friend_note("Sam", "Oct 29", "photo", "caption", None, Image.new("RGB", (300, 900)),
                                       width=w, max_height=h)
    assert fitted.height <= h
    photo = render.fit_picture(Image.new("RGB", (200, 100), (128, 128, 128)), 100, 100, photo=True)
    assert photo.size == (100, 50) and shades(photo) == {0, 255}  # gray became dots


def test_emoji_the_label_cannot_draw_are_left_off(monkeypatch):
    real = render._font_for
    monkeypatch.setattr(render, "_font_for", lambda ch, font: None if ord(ch) > 0x2FFF else real(ch, font))
    monkeypatch.setattr(render, "can_draw", lambda ch: ord(ch) <= 0x2FFF)
    assert render.printable("Party 🎉 tonight‍!") == "Party tonight!"
    assert render.printable("Café ♥") == "Café ♥"
    assert render.printable("🎉🎉") == ""
    img = render.render_friend_note("Sam 🎉", "Oct 29", "note", "🎉🎉")  # the words say so instead
    assert img.height > 100


# ------------------------------------------------------------------ printing: only on a tap, through the usual path
def test_a_note_prints_only_when_its_recipient_taps_print_and_confirms(ctx, monkeypatch):
    ctx, a, app, login = make_site(ctx, dry_run=False)
    sent = []
    monkeypatch.setattr(printer, "_send_raster", lambda img, cfg: sent.append((img.size, cfg.name)))
    Printers(ctx.settings.data_dir, ctx.settings).add({"name": "Kitchen QL", "driver": "brother_ql",
                                                       "address": "192.168.1.60"})
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    [nid] = send_note(login("lee"), "sam")["ids"]  # lee (another household) sends sam (Home, a printer) a note
    assert sent == []  # sending never prints
    sam.get("/notes/feed"), sam.post(f"/notes/{nid}/seen"), sam.get(f"/notes/{nid}/label.png")
    assert sent == []  # nor does looking at it
    r = sam.post(f"/notes/{nid}/print", json={})
    assert r.status_code == 400 and r.json()["detail"] == "Tap Print, then say yes to send it to the printer."
    assert lee.post(f"/notes/{nid}/print", json={"confirm": True}).status_code == 404  # only the recipient
    assert login("boss").post(f"/notes/{nid}/print", json={"confirm": True}).status_code == 404
    out = sam.post(f"/notes/{nid}/print", json={"confirm": True}).json()
    assert out["status"] == "printed" and out["dry_run"] is False and out["printer"]["name"] == "Kitchen QL"
    assert out["prints_here"] is True and SECRET not in json.dumps(out)
    assert sent == [((696, sent[0][0][1]), "Kitchen QL")]
    assert sam.get("/notes/feed").json()["notes"][0]["printed"]
    assert lee.get("/notes/sent").json()["notes"][0]["status"] == "printed"
    # Not a ticket: nothing about it where the superadmin looks at tickets
    assert rows(ctx.settings.data_dir, "tickets") == [] and sam.get("/printed").json() == []
    ev = [e for e in rows(ctx.settings.data_dir, "events") if e["action"] == "note_print"]
    assert [e["detail"] for e in ev] == [{"status": "printed"}]
    # Asked for a preview, or the admin paused printing: nothing is sent
    assert sam.post(f"/notes/{nid}/print", json={"confirm": True, "dry_run": True}).json()["status"] == "dry_run"
    assert login("boss").patch("/super/settings", json={"printing_paused": True}).status_code == 200
    paused = sam.post(f"/notes/{nid}/print", json={"confirm": True}).json()
    assert paused["status"] == "dry_run" and "paused" in paused["note"] and len(sent) == 1
    login("boss").patch("/super/settings", json={"printing_paused": False})
    # A printer that doesn't answer: said plainly, with Print on paper offered instead
    monkeypatch.setattr(printer, "_send_raster", lambda img, cfg: (_ for _ in ()).throw(OSError("no route")))
    failed = sam.post(f"/notes/{nid}/print", json={"confirm": True}).json()
    assert failed["status"] == "failed" and "Print on paper" in failed["note"] and "no route" not in json.dumps(failed)


def test_dry_run_and_the_conftest_printer_guard(site):
    """The test server is dry-run (NEXTBOX_DRY_RUN): a confirmed Print is a preview; the conftest guard
    would fail this test if anything reached printer._send_raster."""
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    [nid] = send_note(sam, "lee")["ids"]
    out = lee.post(f"/notes/{nid}/print", json={"confirm": True}).json()
    assert out["status"] == "dry_run" and out["dry_run"] is True
    assert lee.get("/notes/feed").json()["notes"][0]["printed"] is None  # a preview isn't a print


def test_no_printer_here_offers_paper_and_the_label_image(ctx):
    ctx, a, app, login = make_site(ctx, dry_run=False)
    sam, ana = login("sam"), login("ana")
    befriend(login, "sam", "ana")
    [nid] = send_note(sam, "ana")["ids"]
    out = ana.post(f"/notes/{nid}/print", json={"confirm": True}).json()  # Lee's place has no printer
    assert out["status"] == "dry_run" and out["note"] == jobs.PRINT_AWAY and out["prints_here"] is False
    assert out["printer"] is None
    label = ana.get(f"/notes/{nid}/label.png?download=1")
    assert label.headers["content-disposition"] == f'attachment; filename="next-box-note-{nid}.png"'
    assert label.headers["cache-control"] == "no-store" and Image.open(io.BytesIO(label.content)).width == 696
    assert ana.get(f"/notes/{nid}/label.png").headers["content-disposition"].startswith("inline")
    pdf = ana.get(f"/notes/{nid}/paper.pdf?paper=a4")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and pdf.headers["cache-control"] == "no-store"
    assert ana.get(f"/notes/{nid}/paper.pdf?paper=legal").status_code == 400
    assert login("lee").get(f"/notes/{nid}/paper.pdf").status_code == 404  # not lee's note
    [home] = send_note(ana, "sam")["ids"]
    note = login("sam").post(f"/notes/{home}/print", json={"confirm": True}).json()  # Home, no printer added yet
    assert note["status"] == "dry_run" and note["note"] == jobs.NO_PRINTER


def test_a_die_cut_label_gets_the_whole_note_fitted(ctx, monkeypatch):
    ctx, a, app, login = make_site(ctx, dry_run=False)
    sent = []
    monkeypatch.setattr(printer, "_send_raster", lambda img, cfg: sent.append(img))
    Printers(ctx.settings.data_dir, ctx.settings).add({"name": "OFFICE", "driver": "brother_ql",
                                                       "address": "192.168.1.61", "label": "62x100",
                                                       "width_px": 696})
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    tall = png_bytes(Image.new("RGB", (300, 1200), (90, 90, 90)), "JPEG")
    send_image(lee, "sam", "photo", tall, caption="tall")
    nid = sam.get("/notes/feed").json()["notes"][0]["id"]
    w, h = printer.brother_label("62x100").dots_printable
    label = Image.open(io.BytesIO(sam.get(f"/notes/{nid}/label.png").content))
    assert label.size == (w, h)  # one label, the photo shrunk to fit
    assert sam.post(f"/notes/{nid}/print", json={"confirm": True}).json()["status"] == "printed"
    assert sent[0].width == w and sent[0].height <= h and sent[0].mode == "1"


# ------------------------------------------------------------------ privacy
def _fill(path: str, who: str, ids: dict) -> str:
    out = path
    for name, value in (("{username}", who), ("{fid}", ids.get("fid", "x")), ("{hid}", ids.get("hid", "home")),
                        ("{code}", "ZZZZ"), ("{label_id}", "x"), ("{pid}", "x"), ("{scan_id}", "x")):
        out = out.replace(name, value)
    return out


def test_no_admin_or_superadmin_screen_shows_notes_or_friends(site):
    """Walk every admin, super, usage, debug and ticket endpoint as the superadmin and as a household
    admin: no note's words, caption, picture, reaction, or who is friends with whom."""
    ctx, a, app, login = site
    sam, lee, ana = login("sam"), login("lee"), login("ana")
    befriend(login, "sam", "ana")
    send_note(sam, "ana")
    send_image(sam, "ana", "photo", photo_with_gps(), caption="CAPTIONSECRET")
    nid = ana.get("/notes/feed").json()["notes"][0]["id"]
    ana.post(f"/notes/{nid}/react", json={"reaction": "🔥"})
    ana.post(f"/notes/{nid}/print", json={"confirm": True})
    a.update("sam", debug=True)  # debug mode records more about each action: still nothing of a note
    send_note(sam, "ana", text="DEBUGSECRET words")
    secrets_ = ("PURPLEWALRUS", "CAPTIONSECRET", "DEBUGSECRET", "🔥")
    staff = {"boss": login("boss"), "lee": lee}
    checked = 0
    for route in app.routes:
        path, methods = getattr(route, "path", ""), getattr(route, "methods", set())
        if "GET" not in methods or not path.startswith(("/admin", "/super", "/printed", "/scans", "/status")):
            continue
        if path in ("/super/update-check",):
            continue
        for who in ("sam", "ana", "boss"):
            for caller in staff.values():
                r = caller.get(_fill(path, who, {}))
                text = r.text
                for s in secrets_:
                    assert s not in text, (path, who, s)
                assert "friend_notes" not in text and "/notes/" not in text, path
                checked += 1
    assert checked > 60
    bundle = staff["boss"].get("/super/users/sam/bundle").text + staff["boss"].get("/super/users/ana/bundle").text
    for s in secrets_:
        assert s not in bundle
    # Events (activity, audit, errors) hold counts only: no words, no friend's name, no note id
    for e in rows(ctx.settings.data_dir, "events"):
        blob = json.dumps(e, default=str)
        if e["action"].startswith(("friend", "note")):
            assert "ana" not in blob or e["user"] == "ana", e
            assert nid not in blob and e.get("target") is None, e
        for s in secrets_:
            assert s not in blob
    # The words are only in friend_notes: nowhere else in the whole database
    everything_else = dump(ctx.settings.data_dir, skip=("friend_notes",))
    for s in secrets_:
        assert s not in everything_else, s


def test_an_unexpected_error_logs_only_its_type(site, monkeypatch):
    ctx, a, app, login = site
    sam = login("sam")
    befriend(login, "sam", "lee")

    def boom(*a, **k):
        raise RuntimeError(f"database said: {SECRET}")
    monkeypatch.setattr(friends.Notes, "send", boom)
    r = sam.post("/notes", json={"to": ["lee"], "kind": "note", "text": SECRET})
    assert r.status_code == 500 and SECRET not in r.text
    [err] = [e for e in rows(ctx.settings.data_dir, "events") if e["kind"] == "error"]
    assert err["error"] == "RuntimeError" and "PURPLEWALRUS" not in (err["trace"] or "") and err["action"] == "note_send"


def test_the_shared_key_cannot_reach_friends_or_notes(site):
    ctx, a, app, login = site
    key = TestClient(app, base_url="http://testserver", headers={"X-NextBox-Key": "k"})
    for method, path in (("GET", "/friends"), ("GET", "/notes/feed"), ("GET", "/notes/count"),
                         ("POST", "/friends/add"), ("POST", "/notes")):
        r = key.request(method, path, json={})
        assert r.status_code == 403, path
    anon = TestClient(app, base_url="http://testserver")
    assert anon.get("/friends").status_code == 401 and anon.get("/notes/feed").status_code == 401


def test_db_sql_hides_what_notes_say(site):
    ctx, a, app, login = site
    sam = login("sam")
    befriend(login, "sam", "lee")
    [nid] = send_note(sam, "lee")["ids"]
    login("lee").post(f"/notes/{nid}/react", json={"reaction": "😂"})
    data = ctx.settings.data_dir
    cols, found, _ = dbtools.run_sql(data, "SELECT * FROM friend_notes")
    shown = dict(zip(cols, found[0], strict=True))
    assert shown["body"] == shown["reaction"] == "•••• (hidden)" and shown["sender"] == "sam"
    postgres = db.database(data).dialect.name != "sqlite"
    for sql in ("SELECT body AS b FROM friend_notes", "SELECT body || '' AS x FROM friend_notes",
                "SELECT reaction AS r FROM friend_notes", "SELECT id FROM friend_notes WHERE body LIKE 'PURPLE%'",
                "SELECT length(image) AS n FROM friend_notes"):
        try:
            _, got, _ = dbtools.run_sql(data, sql)
        except PermissionError:
            assert postgres, sql
            continue
        assert not postgres, sql
        assert "PURPLE" not in str(got) and "😂" not in str(got), sql
    assert "friend_notes" in dbtools.TABLE_NOTES and set(db.metadata.tables) <= set(dbtools.TABLE_NOTES)


def test_unsaved_notes_go_after_30_days_at_startup_and_when_the_feed_is_listed(site):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    old, kept, fresh = (send_note(sam, "lee", text=t)["ids"][0] for t in ("old", "kept", "fresh"))
    lee.post(f"/notes/{kept}/keep", json={"keep": True})
    month = int(time.time()) - 31 * 86400
    with db.database(ctx.settings.data_dir).begin() as conn:
        conn.execute(db.friend_notes.update().where(db.friend_notes.c.id.in_([old, kept])).values(created=month))
    friends._pruned.clear()
    assert {n["id"] for n in lee.get("/notes/feed").json()["notes"]} == {kept, fresh}
    # ... and at startup, with requests nobody answered for 30 days
    with db.database(ctx.settings.data_dir).begin() as conn:
        conn.execute(db.friend_notes.update().where(db.friend_notes.c.id == fresh).values(created=month))
    ana = login("ana")
    ana.post("/friends/add", json={"username": "sam"})
    with db.database(ctx.settings.data_dir).begin() as conn:
        conn.execute(db.friendships.update().where(db.friendships.c.requested_by == "ana").values(created=month))
    create_app(ctx)  # a restart
    assert {r["id"] for r in rows(ctx.settings.data_dir, "friend_notes")} == {kept}
    assert sam.get("/friends").json()["incoming"] == [] and len(sam.get("/friends").json()["friends"]) == 1


def test_download_my_data_has_my_friends_and_my_own_notes(site):
    ctx, a, app, login = site
    sam, lee, ana = login("sam"), login("lee"), login("ana")
    befriend(login, "sam", "lee")
    befriend(login, "lee", "ana")
    send_note(sam, "lee", text="from sam")
    send_image(lee, "sam", "draw", doodle(), mime="image/png")
    send_note(lee, "ana", text="NOT SAMS lee to ana")
    data = sam.get("/auth/export").json()["friends"]
    assert [f["username"] for f in data["friends"]] == ["lee"] and data["friend_code"]
    assert [n["text"] for n in data["notes"]["sent"]] == ["from sam"] and data["notes"]["sent"][0]["to"] == "lee"
    [got] = data["notes"]["received"]
    assert got["kind"] == "draw" and got["from"] == "lee" and got["has_image"]
    assert "NOT SAMS" not in json.dumps(data)
    z = zipfile.ZipFile(io.BytesIO(sam.get("/auth/export?format=zip").content))
    assert f"notes/{got['id']}.png" in z.namelist()
    assert json.loads(z.read("data.json"))["friends"]["notes"]["received"][0]["id"] == got["id"]


def test_deleting_an_account_deletes_its_notes_and_friendships(site):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    befriend(login, "lee", "ana")
    send_note(sam, "lee")
    send_note(lee, "sam", text="to sam")
    send_note(lee, "ana", text="stays")
    login("ana").post("/friends/add", json={"username": "sam"})
    sam.post("/friends/add", json={"username": "boss"})
    assert sam.post("/auth/delete-account", json={"password": "sam password", "confirm": "sam"}).json() == {"ok": True}
    stored = dump(ctx.settings.data_dir, skip=("events", "users", "sessions", "invites", "households"))
    for table in ("friend_codes", "friendships", "blocks", "friend_notes"):
        assert "sam" not in json.dumps(rows(ctx.settings.data_dir, table)), table
    assert "PURPLEWALRUS" not in stored
    assert [n["text"] for n in lee.get("/notes/sent").json()["notes"]] == ["stays"]
    assert [f["username"] for f in lee.get("/friends").json()["friends"]] == ["ana"]
    # An admin deleting someone does the same, and a household deleted when empty leaves nothing behind
    login("boss").delete("/admin/users/ana")
    assert all("ana" not in json.dumps(rows(ctx.settings.data_dir, t))
               for t in ("friend_codes", "friendships", "blocks", "friend_notes"))
    with db.database(ctx.settings.data_dir).begin() as conn:  # left by an older version's deletion
        conn.execute(db.friend_notes.insert().values(id="0123456789ab", sender="ghost", recipient="lee", kind="heart",
                                                     created=int(time.time()), kept=False, gone_sender=False,
                                                     gone_recipient=False))
    assert friends.sweep(db.database(ctx.settings.data_dir)) == 1


def test_rate_limits_are_plain_sentences(site, monkeypatch):
    ctx, a, app, login = site
    sam, lee = login("sam"), login("lee")
    befriend(login, "sam", "lee")
    for _ in range(friends.NOTES_PER_FRIEND_PER_DAY):
        lee.post("/notes", json={"to": ["sam"], "kind": "heart"})
    r = lee.post("/notes", json={"to": ["sam"], "kind": "heart"})
    assert r.status_code == 429 and r.json()["detail"] == "That's 30 notes to sam in the last 24 hours. Try again later."
    monkeypatch.setattr(friends, "NOTES_PER_DAY", 3)
    r = sam.post("/notes", json={"to": ["lee"], "kind": "heart"})
    for _ in range(2):
        assert sam.post("/notes", json={"to": ["lee"], "kind": "heart"}).status_code == 200
    r = sam.post("/notes", json={"to": ["lee"], "kind": "heart"})
    assert r.status_code == 429 and "the most for a day" in r.json()["detail"]
    ana = login("ana")
    names = [f"person{n}" for n in range(friends.REQUESTS_PER_DAY)]
    for name in names:
        assert ana.post("/friends/add", json={"username": name}).status_code == 200
    r = ana.post("/friends/add", json={"username": "lee"})
    assert r.status_code == 429 and r.json()["detail"] == friends.TOO_MANY_REQUESTS
    assert friends.REQUESTS_PER_DAY == 20 and friends.NOTES_PER_FRIEND_PER_DAY == 30


def test_upkeep_and_deletion_helpers(tmp_path):
    engine = db.database(tmp_path)
    a = Accounts(tmp_path)
    a.create("sam", "sam password")
    a.create("lee", "lee password")
    friends.Friends(engine, "sam").request(username="lee")
    friends.Friends(engine, "lee").accept("sam")
    friends.Notes(engine, "sam").send(["lee"], "note", "hello", "blue")
    assert friends.prune(engine, now=int(time.time()) + 31 * 86400) == {"notes": 1, "requests": 0}
    friends.forget_user(engine, "lee")
    assert friends.Friends(engine, "sam").friends() == []


# ------------------------------------------------------------------ the docs say so
def test_spec_readme_and_claude_md_cover_friends():
    spec = (PROJECT_DIR / "SPEC.md").read_text()
    section = " ".join(spec.split("## 34. Friends and notes")[1].split())  # as one line, however it wraps
    for words in ("If that name is right, they'll see your request.", "scroll-snap", "confirm", "30 days",
                  "friend_notes", "Delivered", "no-store", "render.can_draw"):
        assert words in section, words
    readme = (PROJECT_DIR / "README.md").read_text()
    assert "## Friends and notes" in readme
    guide = (PROJECT_DIR / "CLAUDE.md").read_text()
    assert "friend_notes" in guide and "only when its recipient taps Print" in guide
    privacy = (PROJECT_DIR / "web" / "privacy.html").read_text()
    assert "Notes from friends" in privacy and "30 days" in privacy
    assert server.TERMS_VERSION == 6 and "#   6: Friends" in Path(server.__file__).read_text()
    assert "the notes you send your friends: no admin screen shows them or who your friends are" in server.DATA_NOTICE
    contract = friends.__doc__
    for path in ("GET  /friends", "POST /friends/add", "POST /notes", "GET  /notes/feed", "POST /notes/{id}/print",
                 "GET  /notes/{id}/label.png", "GET  /notes/count"):
        assert path in contract, path
