"""Feedback: guided or open, on every page, public but anonymous, identities in one file."""
import json
import stat

from fastapi.testclient import TestClient

from nextbox.auth import Accounts
from nextbox.events import Events
from nextbox.feedback import Feedback, scrub
from nextbox.server import create_app

H = {"X-NextBox-Key": "k"}


def setup(ctx):
    a = Accounts(ctx.settings.data_dir)
    a.set_password("mike", "super secret", create=True)
    a.create("helper", "helper pass", role="admin")
    a.create("sam", "sam password")
    a.create("jo", "jo password")
    app = create_app(ctx)

    def login(name, pw):
        c = TestClient(app, base_url="http://testserver", headers={"user-agent": f"Phone-of-{name}"})
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return app, login


GUIDED = {"mode": "guided", "type": "bug", "page": "scan", "rating": 2,
          "trying": "Read back my Tuesday ticket",
          "happened": "It missed row 2. Email me at sam.smith@example.com or call 555-123-4567",
          "expected": "Mark it done like jo said it would", "include_debug": True}


def test_scrub_removes_identifying_details():
    out = scrub("I'm sam, mail sam@x.io, see https://nc.home/s/abc, 192.168.1.20, +1 (555) 123-4567, "
                "ping @jojo, token ghp_abcdefghijklmnopqrstuvwxyz12", names=["sam", "jo"])
    for leak in ("sam", "x.io", "nc.home", "192.168", "555", "jojo", "ghp_"):
        assert leak not in out, leak
    assert "[email]" in out and "[link]" in out and "[phone]" in out and "[someone]" in out
    assert scrub("Jordan and sammy are fine", names=["jo", "sam"]) == "Jordan and sammy are fine"


def test_guided_and_open_feedback_are_public_and_anonymous(ctx):
    app, login = setup(ctx)
    sam, jo = login("sam", "sam password"), login("jo", "jo password")
    posted = sam.post("/feedback", json=GUIDED).json()
    assert posted["mine"] and posted["date"].count("-") == 2 and "T" not in posted["date"]
    jo.post("/feedback", json={"mode": "open", "message": "Love the tickets!", "type": "praise", "page": "print"})

    board = jo.get("/feedback").json()
    assert len(board) == 2
    text = json.dumps(board)
    for leak in ("sam", "example.com", "555-123", "Phone-of", "127.0.0.1", "testclient"):
        assert leak not in text.lower(), leak
    assert "[someone] said" in text  # jo's name scrubbed from sam's text
    mine = {i["type"]: i["mine"] for i in board}
    assert mine == {"bug": False, "praise": True}  # jo only learns which one is hers


def test_identity_lives_in_one_private_file_only(ctx):
    app, login = setup(ctx)
    sam = login("sam", "sam password")
    fid = sam.post("/feedback", json=GUIDED).json()["id"]
    data = ctx.settings.data_dir
    ident = json.loads((data / "feedback_identities.json").read_text())[fid]
    assert ident["user"] == "sam" and ident["agent"] == "Phone-of-sam" and ident["ip"]
    assert ident["debug"] is not None
    assert oct(stat.S_IMODE((data / "feedback_identities.json").stat().st_mode)) == "0o600"
    # No other file links sam to this feedback or holds its text.
    for p in data.rglob("*"):
        if p.is_file() and p.name not in {"feedback_identities.json", "feedback.json"}:
            body = p.read_text(errors="ignore")
            assert f'"{fid}"' not in body and "missed row 2" not in body, p.name
    assert "sam" not in (data / "feedback.json").read_text()


def test_feedback_before_sign_in_and_rate_limit(ctx):
    app, login = setup(ctx)
    anon = TestClient(app, base_url="http://testserver")
    r = anon.post("/feedback", json={"mode": "open", "message": "Can't remember my password", "page": "login"})
    assert r.status_code == 200
    assert anon.get("/feedback").status_code == 401  # reading the board needs an account
    codes = [anon.post("/feedback", json={"mode": "open", "message": f"again {i}"}).status_code for i in range(10)]
    assert codes.count(400) >= 1
    assert anon.post("/feedback", json={"mode": "open", "message": "x"},
                     headers={"Origin": "http://evil.example"}).status_code == 403
    assert login("sam", "sam password").post("/feedback", json={"mode": "guided"}).status_code == 400


def test_withdraw_respond_hide_and_reveal(ctx):
    app, login = setup(ctx)
    sam, jo = login("sam", "sam password"), login("jo", "jo password")
    helper, mike = login("helper", "helper pass"), login("mike", "super secret")
    fid = sam.post("/feedback", json=GUIDED).json()["id"]
    other = sam.post("/feedback", json={"mode": "open", "message": "my neighbour Pat Lee at 12 Elm St"}).json()["id"]

    assert jo.delete(f"/feedback/{fid}").status_code == 403
    r = helper.patch(f"/admin/feedback/{fid}", json={"status": "planned", "reply": "Thanks sam, fixing it"})
    assert r.json()["status"] == "planned" and "sam" not in r.json()["reply"]
    assert jo.get("/feedback").json()[0]["reply"].startswith("Thanks [someone]")
    helper.patch(f"/admin/feedback/{other}", json={"status": "hidden"})  # personal details slipped through
    assert other not in [i["id"] for i in jo.get("/feedback").json()]
    assert other in [i["id"] for i in sam.get("/feedback").json()]       # author still sees it
    assert other in [i["id"] for i in helper.get("/admin/feedback").json()]
    assert jo.patch(f"/admin/feedback/{fid}", json={"status": "fixed"}).status_code == 403

    assert helper.get(f"/super/feedback/{fid}/identity").status_code == 403
    who = mike.get(f"/super/feedback/{fid}/identity").json()
    assert who["user"] == "sam"
    audit = Events(ctx.settings.data_dir).query(kind="audit")
    reveal = [e for e in audit if e["action"] == "reveal_feedback_sender"][0]
    assert reveal["detail"] == {"feedback": fid} and "sam" not in json.dumps(reveal)

    assert sam.delete(f"/feedback/{other}").json()["ok"]
    assert other not in json.loads((ctx.settings.data_dir / "feedback_identities.json").read_text())


def test_deleting_an_account_unlinks_its_feedback(ctx):
    app, login = setup(ctx)
    sam, mike = login("sam", "sam password"), login("mike", "super secret")
    fid = sam.post("/feedback", json=GUIDED).json()["id"]
    assert mike.delete("/admin/users/sam").status_code == 200
    assert fid in [i["id"] for i in mike.get("/feedback").json()]
    assert mike.get(f"/super/feedback/{fid}/identity").status_code == 404


def test_old_feedback_moves_out_of_the_events_log(ctx):
    a = Accounts(ctx.settings.data_dir)
    a.set_password("mike", "super secret", create=True)
    ev = Events(ctx.settings.data_dir)
    for i in range(12):
        ev.log("sam", "feedback", kind="feedback", detail={"message": f"old note {i} from sam", "page": "print"})
    ev.log("sam", "print_today", detail={})
    create_app(ctx)
    log = (ctx.settings.data_dir / "events.jsonl").read_text()
    assert "old note" not in log and "print_today" in log
    board = Feedback(ctx.settings.data_dir).board(None)
    assert len(board) == 12 and all("sam" not in i["message"] for i in board)
