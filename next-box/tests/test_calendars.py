"""Calendars: subscription links, CalDAV, Google and Outlook, on My list and the daily ticket."""
import dataclasses
import datetime as dt

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import TODAY
from nextbox import calendars, jobs
from nextbox.auth import Accounts
from nextbox.calendars import IcsFeed
from nextbox.providers import ProviderError
from nextbox.providers.http import check_public_url
from nextbox.server import create_app

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:Family
BEGIN:VEVENT
UID:standup
DTSTART:20260901T090000
DTEND:20260901T091500
RRULE:FREQ=WEEKLY;BYDAY=MO,WE
SUMMARY:Team standup
END:VEVENT
BEGIN:VEVENT
UID:bday
DTSTART;VALUE=DATE:20260928
DTEND;VALUE=DATE:20260929
SUMMARY:Mom's birthday
END:VEVENT
BEGIN:VEVENT
UID:dentist
DTSTART:20260928T143000
DTEND:20260928T153000
SUMMARY:Dentist
LOCATION:Main St
END:VEVENT
BEGIN:VEVENT
UID:cancelled
DTSTART:20260928T170000
STATUS:CANCELLED
SUMMARY:Cancelled thing
END:VEVENT
BEGIN:VEVENT
UID:other-day
DTSTART:20260929T100000
SUMMARY:Tomorrow only
END:VEVENT
END:VCALENDAR
"""


def feed(handler, url="https://cal.example.com/family.ics"):
    calendars._cache.clear()
    return IcsFeed(url, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_ics_feed_expands_repeats_and_skips_cancelled():
    f = feed(lambda r: httpx.Response(200, content=ICS))
    assert f.check() == {"account": "Family", "calendars": ["Family"]}
    events = f.events(TODAY)  # Monday Sep 28 2026
    assert [(e["all_day"], e["start"], e["title"]) for e in events] == [
        (True, None, "Mom's birthday"), (False, "09:00", "Team standup"), (False, "14:30", "Dentist")]
    assert events[2]["end"] == "15:30" and events[2]["location"] == "Main St"
    assert [e["title"] for e in f.events(TODAY + dt.timedelta(days=1))] == ["Tomorrow only"]


def test_ics_feed_refuses_bad_links():
    assert "isn't a calendar" in str(pytest.raises(ProviderError, feed(lambda r: httpx.Response(200, text="<html>")).check).value)
    assert "fresh one" in str(pytest.raises(ProviderError, feed(lambda r: httpx.Response(404)).check).value)
    big = feed(lambda r: httpx.Response(200, content=b"BEGIN:VCALENDAR\n" + b"x" * 6_000_000))
    with pytest.raises(ProviderError, match="too big"):
        big.check()


def test_private_network_guard(monkeypatch):
    monkeypatch.delenv("NEXTBOX_ALLOW_PRIVATE_URLS", raising=False)
    for bad in ("https://127.0.0.1/x.ics", "https://10.0.0.5/x.ics", "https://192.168.1.1/", "https://[::1]/",
                "https://169.254.169.254/latest", "http://93.184.216.34/x.ics", "file:///etc/passwd"):
        with pytest.raises(ProviderError):
            check_public_url(bad)
    assert check_public_url("webcal://93.184.216.34/x.ics") == "https://93.184.216.34/x.ics"
    # A redirect to the home network is refused too.
    f = feed(lambda r: httpx.Response(302, headers={"location": "https://192.168.1.1/secret.ics"}),
             url="https://93.184.216.34/x.ics")
    with pytest.raises(ProviderError, match="private network"):
        f.check()


@pytest.fixture
def site(ctx):
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    ctx.settings.todoist_token = ""
    Accounts(ctx.settings.data_dir).set_password("sam", "sam password", create=True)

    def build(kind, config, secrets):
        assert kind == "ics" and secrets["url"].startswith("https://")
        return feed(lambda r: httpx.Response(200, content=ICS))
    app = create_app(ctx, calendar_builder=build)
    c = TestClient(app, base_url="http://testserver")
    assert c.post("/auth/login", json={"username": "sam", "password": "sam password"}).status_code == 200
    return ctx, c


def test_add_list_remove_calendars(site):
    ctx, c = site
    kinds = {k["key"]: k for k in c.get("/calendars").json()["kinds"]}
    assert kinds["ics"]["status"] == "ready" and kinds["google_calendar"]["status"] == "needs_setup"
    assert c.post("/calendars", json={"kind": "ics", "fields": {}}).status_code == 400
    added = c.post("/calendars", json={"kind": "ics", "fields": {"url": "https://cal.example.com/f.ics", "name": "Home"}}).json()
    assert added["name"] == "Home" and "secret" not in added and "url" not in str(added)
    from conftest import dump
    assert "cal.example.com" not in dump(ctx.settings.data_dir)  # the private link is sealed
    today = c.get("/calendars/today").json()
    assert [e["title"] for e in today["events"]] == ["Mom's birthday", "Team standup", "Dentist"] and not today["problems"]
    # It prints at the top of the daily ticket.
    r = c.post("/print/today", json={}).json()
    assert r["status"] == "dry_run" and r["manifest"] == []  # events aren't tasks
    assert c.delete(f"/calendars/{added['id']}").status_code == 200
    assert c.get("/calendars").json()["calendars"] == []


def test_daily_ticket_lists_events_and_survives_a_broken_calendar(ctx):
    def events(day):
        return ([{"title": "Standup", "all_day": False, "start": "09:00", "end": None, "calendar": "", "location": ""}],
                ["Work: The calendar didn't answer"])
    c = dataclasses.replace(ctx, events=events)
    r = jobs.print_today(c)
    assert "Work" in r["note"]
    assert jobs._schedule(c)[0] == [("9:00a", "Standup")]
    c24 = dataclasses.replace(c, prefs={**c.prefs, "time_24h": True})
    assert jobs._schedule(c24)[0] == [("09:00", "Standup")]
    off = dataclasses.replace(c, prefs={**c.prefs, "show_calendar": False})
    assert jobs._schedule(off) == ([], "")

    def broken(day):
        raise RuntimeError("boom")
    assert jobs.print_today(dataclasses.replace(ctx, events=broken))["status"] == "dry_run"


def test_signin_through_the_server(ctx, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    from nextbox.providers import oauth
    ctx = dataclasses.replace(ctx, tasks_factory=None)
    ctx.settings.todoist_token = ""
    Accounts(ctx.settings.data_dir).set_password("sam", "sam password", create=True)
    added = []

    def build(kind, config, secrets):
        added.append((kind, secrets.get("refresh_token")))
        return feed(lambda r: httpx.Response(200, content=ICS))
    app = create_app(ctx, calendar_builder=build)
    c = TestClient(app, base_url="http://testserver", follow_redirects=False)
    c.post("/auth/login", json={"username": "sam", "password": "sam password"})
    assert c.get("/connect/google/calendar/start").status_code == 400  # not set up yet
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("NEXTBOX_PUBLIC_URL", "https://nextbox.example.com")
    r = c.get("/connect/google/calendar/start")
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    monkeypatch.setattr(oauth, "_token_request", lambda app, data, client=None: {
        "access_token": "A", "refresh_token": "R", "expires_in": 3600})
    # Another browser (say, a consent link forwarded to someone else) can't finish it...
    elsewhere = TestClient(app, base_url="http://testserver", follow_redirects=False).get(
        f"/connect/google/callback?state={state}&code=abc")
    assert "somewhere%20else" in elsewhere.headers["location"] and added == []
    # ...the browser that started it can (its nb_oauth cookie matches), once.
    r = c.get("/connect/google/calendar/start")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    back = c.get(f"/connect/google/callback?state={state}&code=abc")
    assert back.status_code == 303 and "Connected%20Google" in back.headers["location"]
    assert added == [("google_calendar", "R")]
    assert c.get("/calendars").json()["calendars"][0]["provider"] == "google_calendar"
    # A replayed or made-up state does nothing.
    again = TestClient(app, base_url="http://testserver", follow_redirects=False).get(
        f"/connect/google/callback?state={state}&code=abc")
    assert "expired" in again.headers["location"] and len(added) == 1
    assert "cancelled" in c.get("/connect/google/callback?error=access_denied").headers["location"]
