"""Sign-in, sign-out, onboarding and Get started: fixes from the A batch of the audit."""
import dataclasses
from datetime import date

import pytest
from fastapi.testclient import TestClient

from nextbox import server
from nextbox.auth import Accounts, norm_code
from nextbox.scan import ReadBack, RowMark
from nextbox.server import create_app

from conftest import rows, tiny_jpeg

EVIL = {"Origin": "http://evil.example"}


@pytest.fixture
def site(ctx):
    """boss (superadmin), mike and sam (users), My list only, a fake camera."""
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)
    a.create("mike", "mike password")
    a.create("sam", "sam password")

    def vision(image, media_type, manifest):
        return ReadBack(label_code=None, rows=[RowMark(row=1, mark="done", confidence=0.95)], notes="")
    app = create_app(ctx, vision=vision)

    def client(ip="testclient"):
        return TestClient(app, base_url="http://testserver", client=(ip, 50000))

    def login(name, pw, ip="testclient"):
        c = client(ip)
        r = c.post("/auth/login", json={"username": name, "password": pw})
        assert r.status_code == 200, r.text
        return c
    return ctx, a, client, login


def attempt(c, name, pw):
    return c.post("/auth/login", json={"username": name, "password": pw})


# ------------------------------------------------------------------ A-04, A-10, A-16: signing in
def test_signing_in_to_your_own_account_does_not_reset_guesses_at_someone_else(site):
    ctx, a, client, login = site
    c = client()
    codes = [attempt(c, "mike", f"guess {i}").status_code for i in range(4)]
    assert attempt(c, "sam", "sam password").status_code == 200  # sam's own sign-in from the same address
    codes += [attempt(c, "mike", f"guess {i}").status_code for i in range(4)]
    # Five wrong guesses are allowed (as before); the sixth is refused, sam's sign-in notwithstanding.
    assert codes == [401] * 5 + [429] * 3
    assert attempt(c, "sam", "sam password").status_code == 429  # the address waits, whoever it is


def test_guesses_at_one_account_from_many_addresses_are_slowed(site):
    ctx, a, client, login = site
    codes = [attempt(client(f"10.0.0.{i}"), "mike", "nope nope").status_code for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[10] == 429
    assert "15 minutes" in attempt(client("10.0.1.1"), "mike", "mike password").json()["detail"]
    # Other people aren't affected.
    assert attempt(client("10.0.1.2"), "sam", "sam password").status_code == 200


def test_wrong_password_messages_are_plain_and_warn_before_the_pause(site):
    ctx, a, client, login = site
    c = client()
    details = [attempt(c, "mike", "nope nope").json()["detail"] for _ in range(5)]
    assert details[0] == "That username or password didn't match."
    assert details[1] == details[0]
    assert "2 tries left before a 5-minute pause" in details[2]
    assert "1 try left" in details[3]
    assert "last try" in details[4]
    r = attempt(c, "mike", "mike password")
    assert r.status_code == 429 and r.json()["detail"].startswith("Too many tries. Wait 5 minutes")
    assert "one-time password" in r.json()["detail"]


def test_failed_sign_in_never_logs_what_was_typed_as_the_name(site):
    ctx, a, client, login = site
    c = client()
    attempt(c, "Hunter2-MyRealPassword!", "x")
    attempt(c, "mike", "wrong one")
    logins = [e for e in rows(ctx.settings.data_dir, "events") if e["action"] == "login"]
    assert sorted(e["user"] for e in logins) == ["?", "mike"]
    assert "hunter2" not in str(rows(ctx.settings.data_dir, "events")).lower()


def test_the_lockout_keeps_no_typed_text(site):
    ctx, a, client, login = site
    attempt(client(), "Hunter2-MyRealPassword!", "x")
    assert "hunter2" not in repr(a._failures).lower()


# ------------------------------------------------------------------ A-12: cross-site requests
def test_sign_in_endpoints_refuse_other_sites(site):
    ctx, a, client, login = site
    assert client().post("/auth/login", json={"username": "sam", "password": "sam password"},
                         headers=EVIL).status_code == 403
    sam = login("sam", "sam password")
    assert sam.post("/auth/consent", headers=EVIL).status_code == 403
    assert sam.post("/auth/logout", headers=EVIL).status_code == 403
    assert sam.get("/auth/me").status_code == 200  # still signed in
    assert client().post("/auth/signup", json={"username": "x1", "password": "long enough", "invite": "AAAA-BBBB-CCCC",
                                               "agree": True}, headers=EVIL).status_code == 403
    # This site's own pages (and scripts, which send no Origin) are fine.
    assert sam.post("/auth/consent", headers={"Origin": "http://testserver"}).status_code == 200
    assert sam.post("/auth/logout").status_code == 200
    assert sam.get("/auth/me").status_code == 401


# ------------------------------------------------------------------ A-07, A-08: your password
def test_a_new_password_must_differ_from_the_current_one(site):
    ctx, a, client, login = site
    temp = a.temp_password()
    a.create("tempy", temp, must_change=True)
    t = login("tempy", temp)
    r = t.post("/auth/password", json={"current": temp, "new": temp})
    assert r.status_code == 400 and "different" in r.json()["detail"]
    assert a.get("tempy")["must_change"]  # still has to pick their own
    assert t.post("/auth/password", json={"current": temp, "new": "my own password"}).status_code == 200
    assert not a.get("tempy")["must_change"]
    sam = login("sam", "sam password")
    assert sam.post("/auth/password", json={"current": "sam password", "new": "sam password"}).status_code == 400


def test_wrong_passwords_at_change_password_and_delete_account_lock_out(site):
    ctx, a, client, login = site
    sam = login("sam", "sam password")
    codes = [sam.post("/auth/password", json={"current": f"nope {i}", "new": "whatever123"}).status_code for i in range(3)]
    codes += [sam.post("/auth/delete-account", json={"password": f"nope {i}", "confirm": "sam"}).status_code
              for i in range(3)]
    assert codes == [401, 401, 401, 403, 403, 429]
    r = sam.post("/auth/password", json={"current": "sam password", "new": "whatever123"})
    assert r.status_code == 429 and "Wait 5 minutes" in r.json()["detail"]
    assert a.verify("sam", "sam password") and a.get("sam")  # nothing changed, nothing deleted
    # Someone else's lockout is their own.
    mike = login("mike", "mike password")
    assert mike.post("/auth/password", json={"current": "mike password", "new": "mike password 2"}).status_code == 200


def test_a_right_password_clears_the_count(site):
    ctx, a, client, login = site
    sam = login("sam", "sam password")
    for _ in range(4):
        assert sam.post("/auth/password", json={"current": "nope nope", "new": "whatever123"}).status_code == 401
    assert sam.post("/auth/password", json={"current": "sam password", "new": "sam password 2"}).status_code == 200
    for _ in range(4):
        assert sam.post("/auth/password", json={"current": "nope nope", "new": "whatever123"}).status_code == 401


# ------------------------------------------------------------------ A-09, A-14: joining with a code
def invite(login, **kw):
    return login("boss", "boss password").post("/admin/invites", json=kw).json()["code"]


def test_a_bad_code_never_reveals_which_names_or_emails_exist(site):
    ctx, a, client, login = site
    a.update("mike", email="mike@example.com")
    for body in ({"username": "mike"}, {"username": "nobody0"}, {"username": "newbie", "email": "mike@example.com"}):
        r = client().post("/auth/signup", json={"password": "long enough", "invite": "BAD-CODE", "agree": True, **body})
        assert r.status_code == 400 and r.json()["detail"] == "We don't recognize that invite code. Check it and try again."


def test_a_taken_name_with_a_good_code_keeps_the_code_usable(site):
    ctx, a, client, login = site
    code = invite(login)
    r = client().post("/auth/signup", json={"username": "mike", "password": "long enough", "invite": code, "agree": True})
    assert r.status_code == 400 and "already exists" in r.json()["detail"]
    r = client().post("/auth/signup", json={"username": "kim", "password": "long enough", "invite": code, "agree": True})
    assert r.status_code == 200 and r.json()["username"] == "kim"


def test_every_refused_sign_up_counts_toward_the_lockout(site):
    ctx, a, client, login = site
    code = invite(login)
    c = client("10.9.9.9")
    codes = [c.post("/auth/signup", json={"username": "mike", "password": "long enough", "invite": code,
                                          "agree": True}).status_code for _ in range(6)]
    assert codes == [400] * 5 + [429]
    signups = [e for e in rows(ctx.settings.data_dir, "events") if e["action"] == "signup"]
    assert signups and {e["user"] for e in signups} == {"?"}


@pytest.mark.parametrize("typed", ["{code}", "{lower}", "{bare}", "{spaced}", " {bare_lower} "])
def test_codes_work_however_they_are_typed(site, typed):
    ctx, a, client, login = site
    code = invite(login)
    text = typed.format(code=code, lower=code.lower(), bare=code.replace("-", ""), spaced=code.replace("-", " "),
                        bare_lower=code.replace("-", "").lower())
    r = client().post("/auth/signup", json={"username": "kim", "password": "long enough", "invite": text, "agree": True})
    assert r.status_code == 200, r.text


def test_norm_code():
    assert norm_code("817fe370f205") == "817F-E370-F205"
    assert norm_code(" 817F E370 F205 ") == "817F-E370-F205"
    assert norm_code("8I7F-E37O-F2O5") == "817F-E370-F205"  # I and O read as 1 and 0


def test_used_and_unknown_codes_say_different_things(site):
    ctx, a, client, login = site
    code = invite(login)
    assert client().post("/auth/signup", json={"username": "kim", "password": "long enough", "invite": code,
                                               "agree": True}).status_code == 200
    r = client().post("/auth/signup", json={"username": "lin", "password": "long enough", "invite": code, "agree": True})
    assert r.json()["detail"] == "That invite code was already used or has expired. Ask for a new one."


def test_cancelling_an_invite_typed_without_dashes(site):
    ctx, a, client, login = site
    code = invite(login)
    a.revoke_invite(code.replace("-", "").lower())
    assert code not in {i["code"] for i in a.invites()}


# ------------------------------------------------------------------ A-17: Terms for everyone
def test_people_an_admin_added_agree_to_the_terms_too(site, monkeypatch):
    ctx, a, client, login = site
    sam = login("sam", "sam password")
    me = sam.get("/auth/me").json()
    assert me["consented"] is False and me["terms_ok"] is False and me["notice_changed"] is False
    assert sam.post("/auth/consent").status_code == 200
    me = sam.get("/auth/me").json()
    assert me["consented"] and me["terms_ok"] and me["terms_version"] == server.TERMS_VERSION
    # New Terms: asked again, and told something changed.
    monkeypatch.setattr(server, "TERMS_VERSION", server.TERMS_VERSION + 1)
    me = sam.get("/auth/me").json()
    assert me["consented"] and me["terms_ok"] is False and me["notice_changed"] is True
    sam.post("/auth/consent")
    assert sam.get("/auth/me").json()["terms_ok"] is True


def test_joining_records_the_terms_but_still_shows_the_data_notice(site):
    ctx, a, client, login = site
    code = invite(login)
    kim = client()
    assert kim.post("/auth/signup", json={"username": "kim", "password": "long enough", "invite": code,
                                          "agree": True}).status_code == 200
    me = kim.get("/auth/me").json()
    assert me["terms_ok"] is True and me["consented"] is False and me["notice_changed"] is False


# ------------------------------------------------------------------ A-06, A-18: Get started
def steps(c):
    return {s["key"]: s["done"] for s in c.get("/setup").json()["steps"]}


def test_get_started_teaches_the_daily_ticket_and_snap(site):
    ctx, a, client, login = site
    sam = login("sam", "sam password")
    keys = [s["key"] for s in sam.get("/setup").json()["steps"]]
    assert keys == ["tasks", "home", "app", "calendar", "printer", "preview", "today", "snap", "focus"]
    sam.post("/list/dump", json={"text": "Pay rent today"})
    assert sam.post("/print/today", json={}).status_code == 200
    s = steps(sam)
    assert s["today"] and not s["snap"] and not s["preview"]  # a daily ticket isn't stickers
    assert sam.post("/scan", files={"photo": ("p.jpg", tiny_jpeg(), "image/jpeg")}).status_code == 200
    assert steps(sam)["snap"]


def test_finishing_your_only_task_keeps_the_first_step_ticked(site):
    ctx, a, client, login = site
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "Buy milk"})
    assert steps(sam)["tasks"]
    task = sam.get("/list").json()["tasks"][0]["id"]
    assert sam.post(f"/list/{task}/done").status_code == 200
    assert steps(sam)["tasks"]


def test_steps_only_tick_for_the_right_reasons(site):
    ctx, a, client, login = site
    sam = login("sam", "sam password")
    assert sam.post("/print/text", json={"text": "Hello"}).status_code == 200  # a note
    s = steps(sam)
    assert not s["tasks"] and not s["preview"]
    sam.post("/list/dump", json={"text": "Pay bill today"})
    sam.post("/print/stickers", json={})
    s = steps(sam)
    assert s["tasks"] and s["preview"]


def test_ticks_stay_ticked(site):
    ctx, a, client, login = site
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "Pay bill today"})
    sam.post("/print/stickers", json={})
    assert steps(sam)["preview"]
    ctx.store.delete_for_user("sam")  # the tickets age out
    assert steps(sam)["preview"]
    assert "preview" in a.get("sam")["prefs"]["setup_done"]


def test_people_who_had_finished_are_not_sent_back(site):
    ctx, a, client, login = site
    sam = login("sam", "sam password")
    sam.post("/list/dump", json={"text": "Pay bill today"})
    sam.post("/print/stickers", json={})
    for k in ("home", "app", "calendar", "printer", "focus"):
        sam.post(f"/setup/{k}/done")
    body = sam.get("/setup").json()
    assert body["all_done"], body  # the new steps count as done for them
    mike = login("mike", "mike password")
    mike.post("/setup/guide/hide")
    assert steps(mike)["today"] and steps(mike)["snap"]
    # A brand-new person still has them to do.
    boss = login("boss", "boss password")
    s = steps(boss)
    assert not s["today"] and not s["snap"]


def test_the_snap_step_can_be_skipped(site):
    ctx, a, client, login = site
    sam = login("sam", "sam password")
    assert sam.post("/setup/snap/done").status_code == 200
    s = {x["key"]: (x["done"], x["skipped"]) for x in sam.get("/setup").json()["steps"]}
    assert s["snap"] == (True, True)


# ------------------------------------------------------------------ A-11: coming back from "Sign in with ..."
def test_sign_in_errors_are_not_put_in_the_address(site, monkeypatch):
    from nextbox.connections import Connections
    from nextbox.providers import ProviderError, oauth
    ctx, a, client, login = site
    monkeypatch.setattr(oauth, "who_started", lambda state: "sam")

    def broken(self, *args, **kw):
        raise ProviderError("Google said: invalid_grant <script>")
    monkeypatch.setattr(Connections, "finish_signin", broken)
    r = TestClient(client().app, base_url="http://testserver", follow_redirects=False).get(
        "/connect/google/callback?state=s&code=c")
    assert r.status_code == 303
    assert r.headers["location"] == "/app/#settings&signin=That%20sign-in%20didn%27t%20work.%20Try%20again."
    failed = [e for e in rows(ctx.settings.data_dir, "events") if e["action"] == "signin"]
    assert failed and "invalid_grant" in failed[0]["error"]  # kept for whoever fixes it


# ------------------------------------------------------------------ A-19: the private-address hint
def test_private_address_error_says_what_to_do(monkeypatch):
    from nextbox.providers import ProviderError
    from nextbox.providers.http import check_public_url
    monkeypatch.delenv("NEXTBOX_ALLOW_PRIVATE_URLS", raising=False)
    with pytest.raises(ProviderError, match="ask your Next Box owner"):
        check_public_url("https://127.0.0.1/dav", "Server address")


# ------------------------------------------------------------------ the page itself
def test_the_sign_in_page_says_what_next_box_is_and_has_no_app_tabs(site):
    ctx, a, client, login = site
    page = client().get("/app/").text
    login_part = page.split('<div id="login"')[1].split('<div id="mustChange"')[0]
    assert "<h1>Next Box</h1>" in login_part and "print it, tick it with a pen" in login_part
    shell = page.split('<div id="appShell"')[1].split('<dialog id="noticeDialog"')[0]
    assert 'class="bottombar"' in shell and 'id="moreDialog"' in shell  # only inside the signed-in app
    assert 'id="noticeNo"' in page and 'id="mcSignOut"' in page
    for field in ("mcCur", "mcNew", "pwCur", "pwNew", "joinPass"):
        assert f'data-show="{field}"' in page
