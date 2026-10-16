"""Batch G fixes: Settings, profile and the account menu."""
import dataclasses
import io
import json
import time
import zipfile

from fastapi.testclient import TestClient
from sqlalchemy import update

from conftest import FakeTodoist, rows, tiny_jpeg
from nextbox import db
from nextbox.auth import Accounts
from nextbox.calendars import KINDS
from nextbox.journal import Journal
from nextbox.printers import REASONS
from nextbox.providers import ProviderError
from nextbox.server import create_app


class FakeCal:
    """A calendar that adds fine; one whose link has "broken" in it fails when read."""
    def __init__(self, where: str):
        self.where = where

    def check(self):
        return {"account": "Family", "calendars": ["Family"]}

    def events(self, day):
        if "broken" in self.where:
            raise ProviderError("The calendar link doesn't work any more. Copy a fresh one and add it again.")
        return [{"title": "Standup", "all_day": False, "start": "09:00", "end": None, "calendar": "", "location": ""}]


class Apps:
    """A to-do app per token; the token "bad" is refused when checked."""
    def __call__(self, provider, config, secrets):
        if secrets.get("token") == "bad":
            class Bad(FakeTodoist):
                def check(self):
                    raise ProviderError("Todoist rejected the token. Reconnect Todoist in Settings.")
            return Bad()
        return FakeTodoist([])


def site(ctx):
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.set_password("boss", "boss password", create=True)  # the only superadmin
    a.create("sam", "sam password")

    def calendar(kind, config, secrets):
        return FakeCal(secrets.get("url") or config.get("url", ""))
    app = create_app(ctx, builder=Apps(), calendar_builder=calendar)

    def login(name, pw, agent="testclient"):
        c = TestClient(app, base_url="http://testserver", headers={"user-agent": agent})
        assert c.post("/auth/login", json={"username": name, "password": pw}).status_code == 200
        return c
    return ctx, a, login


# --- G-01: the data download has the journal photos and ticket images --------------------------
def test_download_my_data_as_zip_has_photos_and_tickets(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    page = Journal(a.db, "sam").add("2026-09-27", notes="A good day", photo=tiny_jpeg())
    Journal(a.db, "sam").add("2026-09-28", notes="Typed only")
    ticket = sam.post("/print/text", json={"text": "hello"}).json()["id"]
    r = sam.get("/auth/export?format=zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert 'filename="next-box-sam.zip"' in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert "data.json" in names and f"journal/2026-09-27-{page['id']}.jpg" in names
    assert f"tickets/{ticket}.png" in names
    assert len([n for n in names if n.startswith("journal/")]) == 1  # the typed page has no photo
    assert z.read(f"journal/2026-09-27-{page['id']}.jpg") == tiny_jpeg()
    data = json.loads(z.read("data.json"))
    assert data["account"]["username"] == "sam" and len(data["journal"]) == 2
    # The device you downloaded it from is marked as this one.
    assert [d["current"] for d in data["devices"]] == [True]
    text = z.read("data.json").decode()
    assert "salt" not in text and "secret" not in text and "token_hash" not in text
    # The plain .json download still works, and marks this device too.
    assert sam.get("/auth/export").json()["devices"][0]["current"] is True


def test_zip_download_is_only_your_own(ctx):
    ctx, a, login = site(ctx)
    Journal(a.db, "sam").add("2026-09-27", notes="Private", photo=tiny_jpeg())
    boss = login("boss", "boss password")
    z = zipfile.ZipFile(io.BytesIO(boss.get("/auth/export?format=zip").content))
    assert [n for n in z.namelist() if n.startswith("journal/")] == []
    assert "Private" not in z.read("data.json").decode()


# --- G-02 / G-03: adding a tag never recolors one, and any typed name works ---------------------
def test_add_tag_refuses_an_existing_name_and_keeps_its_color(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    sam.get("/tags")  # the starter tags, urgent is red
    r = sam.post("/tags", json={"name": "Urgent", "color": "yellow"})
    assert r.status_code == 409 and r.json()["detail"] == "You already have #urgent (red). Change its color in the list above."
    assert {t["name"]: t["color"] for t in sam.get("/tags").json()["tags"]}["urgent"] == "red"
    # The saved name is returned, so the page can say when it differs from what was typed.
    assert sam.post("/tags", json={"name": "#Groceries", "color": "green"}).json() == {"name": "groceries", "color": "green"}
    assert sam.post("/tags", json={"name": "a/b", "color": "blue"}).json()["name"] == "ab"  # no 404 from a slash
    empty = sam.post("/tags", json={"name": "  ", "color": "blue"})
    assert empty.status_code == 400 and "needs a name" in empty.json()["detail"]
    assert sam.post("/tags", json={"name": "bills", "color": "gold"}).status_code == 400
    # Recoloring an existing tag still goes through PUT.
    assert sam.put("/tags/urgent", json={"color": "orange"}).json() == {"name": "urgent", "color": "orange"}


# --- G-04: setting errors say what to do --------------------------------------------------------
def test_settings_errors_are_plain_words(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    cases = {"max_rows": (0, "Pick a number from 1 to 20."), "slug_after": (11, "Pick a number from 2 to 10."),
             "default_filter": ("x" * 250, "Keep the filter to 200 characters or less."),
             "display_name": ("y" * 41, "Keep the name to 40 characters or less."),
             "always_dry_run": ("yes", "That value isn't allowed.")}
    for key, (value, hint) in cases.items():
        r = sam.patch("/settings", json={key: value})
        assert r.status_code == 400 and r.json()["detail"] == hint, key
        assert key not in r.json()["detail"]
    assert sam.patch("/settings", json={"max_rows": None}).json()["detail"] == "Pick a number from 1 to 20."
    assert sam.patch("/settings", json={"max_rows": 7}).json()["max_rows"] == 7


# --- G-05: the profile saves the name and email together, or neither ----------------------------
def test_profile_saves_name_and_email_together(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    r = sam.patch("/auth/profile", json={"display_name": "Bad Email Name", "email": "not-an-email"})
    assert r.status_code == 400
    assert a.get("sam")["prefs"]["display_name"] == "" and a.get("sam")["email"] is None  # nothing half-saved
    a.update("boss", email="taken@example.com")
    assert sam.patch("/auth/profile", json={"display_name": "Sammy", "email": "taken@example.com"}).status_code == 400
    assert a.get("sam")["prefs"]["display_name"] == ""
    assert sam.patch("/auth/profile", json={"display_name": "z" * 41, "email": "sam@example.com"}).status_code == 400
    assert a.get("sam")["email"] is None
    ok = sam.patch("/auth/profile", json={"display_name": "  Sammy ", "email": "Sam@Example.com"}).json()
    assert ok["email"] == "sam@example.com" and ok["prefs"]["display_name"] == "Sammy"
    # Only the name: the email stays.
    assert sam.patch("/auth/profile", json={"display_name": "Sam"}).json()["email"] == "sam@example.com"
    assert a.get("sam")["prefs"]["display_name"] == "Sam"
    # An empty email clears it, as before.
    assert sam.patch("/auth/profile", json={"email": ""}).json()["email"] is None


# --- G-06: Settings can tell when the linked app stopped working --------------------------------
def test_connection_check_reports_a_broken_sign_in(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    assert sam.get("/connection?check=1").json() == {"connected": False, "ok": True, "error": ""}
    sam.put("/connection", json={"provider": "todoist", "fields": {"token": "fine"}})
    good = sam.get("/connection?check=1").json()
    assert good["connected"] and good["ok"] and good["error"] == ""
    assert "ok" not in sam.get("/connection").json()  # a plain status doesn't knock on the app
    # The saved sign-in can't be opened any more (the secret key changed).
    conn = a.connection("sam")
    a.set_connection("sam", {**conn, "secret": "not-a-sealed-secret"})
    bad = sam.get("/connection?check=1").json()
    assert bad["connected"] and bad["ok"] is False
    assert bad["error"] == "Reconnect Todoist: the saved sign-in can't be read any more."
    assert "secret" not in bad and "not-a-sealed-secret" not in json.dumps(bad)
    # The app itself refuses the token: its own words.
    a.set_connection("sam", None)
    sam.put("/connection", json={"provider": "todoist", "fields": {"token": "fine"}})
    from nextbox.vault import Vault
    a.set_connection("sam", {**a.connection("sam"), "secret": Vault(ctx.settings.data_dir).seal({"token": "bad"})})
    assert sam.get("/connection?check=1").json()["error"] == "Todoist rejected the token. Reconnect Todoist in Settings."


# --- G-07: calendars can't be added twice, can be renamed, and say which one is broken -----------
def test_calendars_duplicates_rename_and_failing_status(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    first = sam.post("/calendars", json={"kind": "ics", "fields": {"url": "https://cal.example.com/f.ics"}}).json()
    again = sam.post("/calendars", json={"kind": "ics", "fields": {"url": "webcal://cal.example.com/f.ics/", "name": "Again"}})
    assert again.status_code == 400 and again.json()["detail"] == "You already added this calendar."
    dav = {"url": "https://dav.example.com/", "username": "Sam@Example.com", "password": "pw", "name": "Family"}
    added = sam.post("/calendars", json={"kind": "caldav", "fields": dav}).json()
    assert added["name"] == "Family"  # CalDAV has a Name now, not the Apple ID
    twice = sam.post("/calendars", json={"kind": "caldav", "fields": {**dav, "username": "sam@example.com", "password": "other"}})
    assert twice.status_code == 400 and twice.json()["detail"] == "You already added this calendar."
    # The same account, but only one of its calendars, is a different calendar.
    assert sam.post("/calendars", json={"kind": "caldav", "fields": {**dav, "only": "Work"}}).status_code == 200
    # Rename.
    assert sam.patch(f"/calendars/{first['id']}", json={"name": " Home "}).json()["name"] == "Home"
    assert [c["name"] for c in sam.get("/calendars").json()["calendars"]][0] == "Home"
    assert sam.patch(f"/calendars/{first['id']}", json={"name": " "}).status_code == 400
    assert sam.patch("/calendars/nope", json={"name": "x"}).status_code == 404
    # A broken one is reported by its id, so Settings can mark that row.
    broken = sam.post("/calendars", json={"kind": "ics", "fields": {"url": "https://cal.example.com/broken.ics"}}).json()
    today = sam.get("/calendars/today").json()
    assert list(today["failing"]) == [broken["id"]] and "fresh one" in today["failing"][broken["id"]]
    assert len(today["problems"]) == 1 and today["events"]
    # The link stays sealed.
    assert "cal.example.com" not in json.dumps(rows(ctx.settings.data_dir, "users"))


def test_caldav_kind_offers_a_name():
    caldav = next(k for k in KINDS if k["key"] == "caldav")
    assert {"name": "name", "label": "Name", "optional": True, "help": "e.g. Family"} in caldav["fields"]


# --- G-10: signed-in devices show when they were last used, and one can be signed out -----------
def test_devices_last_used_order_and_sign_out_one(ctx):
    ctx, a, login = site(ctx)
    phone = login("sam", "sam password", agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) Safari/604.1")
    laptop = login("sam", "sam password", agent="Mozilla/5.0 (X11; Linux x86_64) Chrome/130.0 Safari/537.36")
    boss = login("boss", "boss password")
    stored = rows(ctx.settings.data_dir, "sessions")
    assert all(s["last_used"] for s in stored)
    # The phone was last used long ago; the laptop is asking.
    old = int(time.time()) - 86400
    with db.database(ctx.settings.data_dir).begin() as conn:
        conn.execute(update(db.sessions).values(last_used=old))
    listed = laptop.get("/auth/sessions").json()
    assert listed[0]["current"] and "Linux" in listed[0]["agent"]
    assert listed[0]["last_used"] > old  # using it moved "last used" on
    assert listed[1]["last_used"] == old and "iPhone" in listed[1]["agent"]
    # Throttled: a second request within 10 minutes doesn't write again.
    before = {s["token_hash"]: s["last_used"] for s in rows(ctx.settings.data_dir, "sessions")}
    laptop.get("/auth/sessions")
    assert {s["token_hash"]: s["last_used"] for s in rows(ctx.settings.data_dir, "sessions")} == before
    # Sign out just the phone.
    phone_id = listed[1]["id"]
    assert laptop.delete(f"/auth/sessions/{listed[0]['id']}").status_code == 400  # this device: use Sign out
    assert boss.delete(f"/auth/sessions/{phone_id}").status_code == 404           # not boss's device
    assert laptop.delete("/auth/sessions/zz").status_code == 404
    assert laptop.delete(f"/auth/sessions/{phone_id}").status_code == 200
    assert phone.get("/auth/me").status_code == 401
    assert laptop.get("/auth/me").status_code == 200 and len(laptop.get("/auth/sessions").json()) == 1
    assert laptop.delete(f"/auth/sessions/{phone_id}").status_code == 404


def test_old_sessions_without_last_used_still_list(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    with db.database(ctx.settings.data_dir).begin() as conn:
        conn.execute(update(db.sessions).values(last_used=None))
    s = a.sessions_for("sam")[0]
    assert s["last_used"] == s["created"]
    assert sam.get("/auth/me").status_code == 200
    assert rows(ctx.settings.data_dir, "sessions")[0]["last_used"]  # set again on use


# --- G-11: the account menu knows when Delete can't work ----------------------------------------
def test_me_says_whether_the_account_can_be_deleted(ctx):
    ctx, a, login = site(ctx)
    assert login("sam", "sam password").get("/auth/me").json()["can_delete"] is True
    boss = login("boss", "boss password")
    assert boss.get("/auth/me").json()["can_delete"] is False  # the only superadmin
    a.create("kim", "kim password", role="superadmin")
    assert boss.get("/auth/me").json()["can_delete"] is True
    a.set_protected("boss")
    assert boss.get("/auth/me").json()["can_delete"] is False  # the protected admin
    assert login("kim", "kim password").get("/auth/me").json()["can_delete"] is True


# --- G-14: label colour reasons in plain words ---------------------------------------------------
def test_urgent_reason_has_no_todoist_codes():
    assert REASONS["urgent"] == "Urgent tasks"
    assert not any("p1" in v or "p2" in v for v in REASONS.values())


# --- G-15: disconnecting sends new tasks back to My list ----------------------------------------
def test_disconnect_points_new_tasks_back_to_my_list(ctx):
    ctx, a, login = site(ctx)
    sam = login("sam", "sam password")
    sam.put("/connection", json={"provider": "todoist", "fields": {"token": "fine"}})
    assert sam.patch("/settings", json={"new_tasks_to": "linked"}).json()["new_tasks_to"] == "linked"
    assert sam.delete("/connection").json() == {"connected": False}
    assert sam.get("/settings").json()["new_tasks_to"] == "nextbox"
    assert a.get("sam")["prefs"]["new_tasks_to"] == "nextbox"
