"""Notion, Asana, Google Tasks, Microsoft To Do and TickTick, against recorded-style fake servers,
plus the "Sign in with ..." round trip."""
import datetime as dt
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from nextbox.providers import ProviderError, build_provider, oauth, providers
from nextbox.providers.asana import Asana
from nextbox.providers.ids import pack, unpack
from nextbox.providers.notion import Notion, database_id

TODAY = dt.date(2026, 10, 13)


class Fake:
    """Routes (method, path) to canned JSON and records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, request):
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith("application/json") else None
        self.calls.append((request.method, request.url.path, body, dict(request.url.params)))
        for (method, path), reply in self.routes.items():
            if method == request.method and request.url.path.endswith(path):
                return httpx.Response(200, json=reply(request) if callable(reply) else reply)
        return httpx.Response(204)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self), base_url="https://api.test")


def test_packed_ids_round_trip_and_reject_junk():
    pid = pack("ms", "AAMk+/list==", "AAMk/task+id=")
    assert "/" not in pid and "+" not in pid and unpack("ms", pid) == ("AAMk+/list==", "AAMk/task+id=")
    for bad in ("gt.abc", "ms.!!!", "nb:5", "ms." + "A" * 900):
        with pytest.raises(ProviderError):
            unpack("ms", bad)


# --- Notion -----------------------------------------------------------------------------------
NOTION_DB = {"title": [{"plain_text": "To do"}], "properties": {
    "Name": {"type": "title"}, "Done": {"type": "checkbox"}, "Due": {"type": "date"},
    "Tags": {"type": "multi_select"}, "Created": {"type": "date"}}}


def notion_page(pid, title, due=None, tags=()):
    return {"id": pid, "properties": {"Name": {"title": [{"plain_text": title}]},
                                      "Due": {"date": {"start": due} if due else None},
                                      "Tags": {"multi_select": [{"name": t} for t in tags]}}}


def test_notion_finds_columns_and_round_trips():
    pid = "12345678-1234-1234-1234-123456789abc"
    fake = Fake({("GET", "/databases/" + "a" * 32): NOTION_DB,
                 ("POST", "/query"): {"results": [notion_page(pid, "Pay bill", "2026-10-12", ["urgent"]),
                                                  notion_page("22345678-1234-1234-1234-123456789abc", "Later", "2026-11-01")],
                                      "has_more": False},
                 ("POST", "/pages"): notion_page("32345678-1234-1234-1234-123456789abc", "New one")})
    n = Notion("secret_x", f"https://www.notion.so/me/To-do-{'a' * 32}?v=1", client=fake.client(), today=lambda: TODAY)
    assert n.check() == {"account": "To do", "lists": ["To do"]}
    s = n.schema()
    assert (s["title"], s["done"], s["date"], s["tags"]) == ("Name", "Done", "Due", "Tags")
    tasks = n.filter_tasks("all")
    assert tasks[0]["content"] == "Pay bill" and tasks[0]["labels"] == ["urgent"] and tasks[0]["due"]["date"] == "2026-10-12"
    assert [t["content"] for t in n.filter_tasks("#urgent")] == ["Pay bill"]
    n.today()
    query = [c for c in fake.calls if c[1].endswith("/query")][-1][2]
    assert query["filter"]["and"][1] == {"property": "Due", "date": {"on_or_before": "2026-10-13"}}
    n.close_task(pid)
    n.set_due_date(pid, "2026-10-20")
    n.delete_task(pid)
    patches = [c[2] for c in fake.calls if c[0] == "PATCH"]
    assert patches == [{"properties": {"Done": {"checkbox": True}}},
                       {"properties": {"Due": {"date": {"start": "2026-10-20"}}}}, {"archived": True}]
    assert n.add_task("New one", "notes")["content"] == "New one"
    with pytest.raises(ProviderError):
        n.close_task("../../users")
    with pytest.raises(ProviderError):
        database_id("not a link")


def test_notion_status_column_and_missing_done_column():
    status_db = {"title": [], "properties": {"Task": {"type": "title"}, "Status": {"type": "status", "status": {
        "options": [{"id": "1", "name": "Not started"}, {"id": "2", "name": "Finished"}],
        "groups": [{"name": "To-do", "option_ids": ["1"]}, {"name": "Complete", "option_ids": ["2"]}]}}}}
    fake = Fake({("GET", "/databases/" + "b" * 32): status_db, ("POST", "/query"): {"results": [], "has_more": False}})
    n = Notion("t", "b" * 32, client=fake.client())
    n.close_task("12345678-1234-1234-1234-123456789abc")
    assert fake.calls[-1][2] == {"properties": {"Status": {"status": {"name": "Finished"}}}}
    n.filter_tasks("all")
    assert [c for c in fake.calls if c[1].endswith("/query")][0][2]["filter"] == {
        "property": "Status", "status": {"does_not_equal": "Finished"}}
    bare = Fake({("GET", "/databases/" + "c" * 32): {"properties": {"Name": {"type": "title"}}}})
    with pytest.raises(ProviderError, match="Checkbox"):
        Notion("t", "c" * 32, client=bare.client()).check()


# --- Asana ------------------------------------------------------------------------------------
def test_asana_round_trip():
    fake = Fake({("GET", "/users/me"): {"data": {"email": "sam@example.com", "workspaces": [
                     {"gid": "11", "name": "Home"}, {"gid": "22", "name": "Work"}]}},
                 ("GET", "/tasks"): {"data": [
                     {"gid": "501", "name": "Send report", "due_on": "2026-10-13", "tags": [{"name": "work"}]},
                     {"gid": "502", "name": "Plan party", "due_at": "2026-10-20T15:00:00.000Z"},
                     {"gid": "503", "name": "Done one", "completed": True}], "next_page": None},
                 ("POST", "/tasks"): {"data": {"gid": "600", "name": "New"}}})
    a = Asana("pat", "work", client=fake.client(), today=lambda: TODAY)
    assert a.check()["account"] == "sam@example.com"
    assert [t["content"] for t in a.today()] == ["Send report"]
    params = [c for c in fake.calls if c[1].endswith("/tasks") and c[0] == "GET"][0][3]
    assert params["workspace"] == "22" and params["assignee"] == "me" and params["completed_since"] == "now"
    assert [t["content"] for t in a.filter_tasks("#work")] == ["Send report"]
    a.close_task("501")
    a.set_due_date("501", "2026-10-15")
    a.delete_task("501")
    assert [c[2] for c in fake.calls if c[0] in ("PUT", "DELETE")] == [
        {"data": {"completed": True}}, {"data": {"due_on": "2026-10-15"}}, None]
    assert a.add_task("New")["id"] == "600"
    with pytest.raises(ProviderError):
        a.close_task("../users")
    with pytest.raises(ProviderError, match="No Asana workspace"):
        Asana("pat", "Nope", client=fake.client()).workspace()


# --- sign-in apps -------------------------------------------------------------------------------
@pytest.fixture
def signin_on(monkeypatch):
    for app in ("GOOGLE", "MICROSOFT", "TICKTICK"):
        monkeypatch.setenv(f"{app}_CLIENT_ID", f"{app.lower()}-id")
        monkeypatch.setenv(f"{app}_CLIENT_SECRET", f"{app.lower()}-secret")
    monkeypatch.setenv("NEXTBOX_PUBLIC_URL", "https://nextbox.example.com/")


FRESH = {"access_token": "at", "refresh_token": "rt", "expires_at": 4102444800}  # far future


def test_needs_setup_until_the_owner_switches_it_on(monkeypatch, signin_on):
    on = {p["key"]: p["status"] for p in providers()}
    assert on["google_tasks"] == on["microsoft_todo"] == on["ticktick"] == "oauth"
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET")
    p = {x["key"]: x for x in providers()}
    assert p["google_tasks"]["status"] == "needs_setup" and "owner" in p["google_tasks"]["reason"]
    status = oauth.setup_status()
    assert status["google"]["missing"] == ["GOOGLE_CLIENT_SECRET"] and status["microsoft"]["ready"]
    assert status["ticktick"]["redirect_uri"] == "https://nextbox.example.com/connect/ticktick/callback"
    assert "secret" not in json.dumps(status).replace("CLIENT_SECRET", "")
    with pytest.raises(ProviderError, match="isn't set up"):
        oauth.start("google", "tasks", "sam")


def test_signin_round_trip_with_pkce_and_one_time_state(signin_on):
    url = oauth.start("google", "calendar", "sam")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://accounts.google.com/") and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "https://nextbox.example.com/connect/google/callback"
    assert "calendar.readonly" in q["scope"] and q["access_type"] == "offline"
    assert oauth.who_started(q["state"]) == "sam"
    seen = []

    def token(request):
        seen.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "A", "refresh_token": "R", "expires_in": 3600})
    client = httpx.Client(transport=httpx.MockTransport(token))
    with pytest.raises(ProviderError, match="different"):
        oauth.finish("google", q["state"], "code", "kim", client=client)  # wrong person: state is now spent
    url = oauth.start("google", "calendar", "sam")
    state = parse_qs(urlparse(url).query)["state"][0]
    purpose, tokens = oauth.finish("google", state, "the-code", "sam", client=client)
    assert purpose == "calendar" and tokens["refresh_token"] == "R"
    assert seen[-1]["code_verifier"] and seen[-1]["client_secret"] == ["google-secret"]
    with pytest.raises(ProviderError, match="already used"):
        oauth.finish("google", state, "the-code", "sam", client=client)


def test_expired_tokens_refresh_and_are_saved(signin_on):
    saved = []
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer new"
        return httpx.Response(200, json={"value": []})
    from nextbox.providers.oauth import MicrosoftTodo
    ms = MicrosoftTodo({"access_token": "old", "refresh_token": "rt", "expires_at": 0, "_save": saved.append},
                       client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://graph.test"))
    assert ms.filter_tasks("all") == []
    assert saved and saved[0]["refresh_token"] == "rotated" and saved[0]["access_token"] == "new"
    gone = MicrosoftTodo({"access_token": "old", "refresh_token": "", "expires_at": 0},
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError, match="expired"):
        gone.today()


def test_google_tasks_round_trip(signin_on):
    fake = Fake({("GET", "/users/@me/lists"): {"items": [{"id": "L1", "title": "My Tasks"}]},
                 ("GET", "/lists/L1/tasks"): {"items": [
                     {"id": "T1", "title": "Renew passport", "due": "2026-10-12T00:00:00.000Z", "notes": "photo"},
                     {"id": "T2", "title": "Someday"}, {"id": "T3", "title": "Old", "status": "completed"}]},
                 ("POST", "/lists/@default/tasks"): {"id": "T9", "title": "New"}})
    g = build_provider("google_tasks", {}, dict(FRESH))
    g.client = fake.client()
    g._today = lambda: TODAY
    assert g.check()["lists"] == ["My Tasks"]
    today = g.today()
    assert [t["content"] for t in today] == ["Renew passport"] and today[0]["due"]["date"] == "2026-10-12"
    tid = today[0]["id"]
    g.close_task(tid)
    g.set_due_date(tid, "2026-10-14")
    g.delete_task(tid)
    writes = [(c[0], c[1], c[2]) for c in fake.calls if c[0] in ("PATCH", "DELETE")]
    assert writes == [("PATCH", "/lists/L1/tasks/T1", {"status": "completed"}),
                      ("PATCH", "/lists/L1/tasks/T1", {"due": "2026-10-14T00:00:00.000Z"}),
                      ("DELETE", "/lists/L1/tasks/T1", None)]
    assert g.add_task("New")["content"] == "New"
    with pytest.raises(ProviderError):
        g.filter_tasks("No such list")


def test_microsoft_todo_round_trip(signin_on):
    fake = Fake({("GET", "/me/todo/lists"): {"value": [{"id": "AQ+/x==", "displayName": "Tasks", "wellknownListName": "defaultList"}]},
                 ("GET", "/tasks"): {"value": [
                     {"id": "TK/1=", "title": "Call bank", "importance": "high", "categories": ["Red category"],
                      "dueDateTime": {"dateTime": "2026-10-13T00:00:00.0000000", "timeZone": "UTC"},
                      "recurrence": {"pattern": {}}}]},
                 ("POST", "/tasks"): {"id": "TK/2=", "title": "New"},
                 ("GET", "/me"): {"mail": "sam@outlook.com"}})
    m = build_provider("microsoft_todo", {}, dict(FRESH))
    m.client = fake.client()
    m._today = lambda: TODAY
    assert m.check()["account"] == "sam@outlook.com"
    t = m.today()[0]
    assert (t["content"], t["priority"], t["labels"], t["due"]) == (
        "Call bank", 4, ["Red category"], {"date": "2026-10-13", "is_recurring": True})
    m.close_task(t["id"])
    m.set_due_date(t["id"], "2026-10-16")
    paths = [c[1] for c in fake.calls if c[0] == "PATCH"]
    assert paths[0].endswith("/me/todo/lists/AQ%2B%2Fx%3D%3D/tasks/TK%2F1%3D") or "AQ" in paths[0]
    assert [c[2] for c in fake.calls if c[0] == "PATCH"] == [
        {"status": "completed"}, {"dueDateTime": {"dateTime": "2026-10-16T00:00:00", "timeZone": "UTC"}}]
    assert m.add_task("New")["content"] == "New"


def test_ticktick_round_trip(signin_on):
    fake = Fake({("GET", "/project"): [{"id": "P1", "name": "Home"}],
                 ("GET", "/project/inbox/data"): {"tasks": [{"id": "I1", "projectId": "inbox123", "title": "Inbox thing", "status": 0}]},
                 ("GET", "/project/P1/data"): {"tasks": [
                     {"id": "X1", "projectId": "P1", "title": "Fix sink", "priority": 5, "status": 0, "isAllDay": True,
                      "dueDate": "2026-10-12T04:00:00.000+0000", "tags": ["home"]},
                     {"id": "X2", "projectId": "P1", "title": "Done", "status": 2}]}})
    t = build_provider("ticktick", {}, dict(FRESH))
    t.client = fake.client()
    t._today = lambda: TODAY
    tasks = t.filter_tasks("all")
    assert [x["content"] for x in tasks] == ["Inbox thing", "Fix sink"]
    sink = tasks[1]
    assert sink["priority"] == 4 and sink["labels"] == ["home"] and sink["due"]["date"] == "2026-10-12"
    t.close_task(sink["id"])
    t.set_due_date(sink["id"], "2026-10-15")
    t.delete_task(sink["id"])
    writes = [(c[0], c[1]) for c in fake.calls if c[0] != "GET"]
    assert writes == [("POST", "/project/P1/task/X1/complete"), ("POST", "/task/X1"), ("DELETE", "/project/P1/task/X1")]


def test_google_and_outlook_calendars(signin_on):
    from nextbox.providers.oauth import GoogleCalendar, OutlookCalendar
    gfake = Fake({("GET", "/users/me/calendarList"): {"items": [
                      {"id": "sam@gmail.com", "summary": "Sam", "primary": True},
                      {"id": "x#holiday@group.v.calendar.google.com", "summary": "Holidays", "selected": False}]},
                  ("GET", "/events"): {"items": [
                      {"summary": "Gym", "start": {"dateTime": "2026-10-13T07:00:00Z"}, "end": {"dateTime": "2026-10-13T08:00:00Z"}},
                      {"summary": "Trip", "start": {"date": "2026-10-13"}, "end": {"date": "2026-10-14"}},
                      {"summary": "Gone", "status": "cancelled", "start": {"dateTime": "2026-10-13T09:00:00Z"}}]}})
    g = GoogleCalendar(dict(FRESH), client=gfake.client())
    events = g.events(TODAY)
    assert [(e["all_day"], e["title"]) for e in events] == [(True, "Trip"), (False, "Gym")]
    assert sum(1 for c in gfake.calls if c[1].endswith("/events")) == 1  # unselected calendars skipped
    ofake = Fake({("GET", "/me/calendarView"): {"value": [
        {"subject": "1:1", "start": {"dateTime": "2026-10-13T15:00:00.0000000", "timeZone": "UTC"},
         "end": {"dateTime": "2026-10-13T15:30:00.0000000", "timeZone": "UTC"}, "location": {"displayName": "Room 4"}},
        {"subject": "Off", "isAllDay": True, "start": {"dateTime": "2026-10-13T00:00:00.0000000", "timeZone": "UTC"},
         "end": {"dateTime": "2026-10-14T00:00:00.0000000", "timeZone": "UTC"}},
        {"subject": "Nope", "isCancelled": True, "start": {"dateTime": "2026-10-13T10:00:00", "timeZone": "UTC"}}]}})
    o = OutlookCalendar(dict(FRESH), client=ofake.client())
    out = o.events(TODAY)
    assert [(e["all_day"], e["title"], e["location"]) for e in out] == [(True, "Off", ""), (False, "1:1", "Room 4")]
