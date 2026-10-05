"""To-do app providers: Todoist (mocked HTTP) and CalDAV (a real Radicale server in-process)."""
import datetime as dt
import json
import socket
import threading
import time

import httpx
import pytest

from nextbox.providers import CalDAVTasks, ProviderError, Todoist


# --- Todoist ----------------------------------------------------------------
def todoist(handler, **kw):
    client = httpx.Client(base_url="https://api.todoist.com/api/v1", transport=httpx.MockTransport(handler))
    return Todoist("tok", client=client, backoff=0, **kw)


def test_todoist_rejects_ids_that_would_change_the_url():
    calls = []
    td = todoist(lambda r: calls.append(r) or httpx.Response(200, json={}))
    for bad in ("../projects/1", "1/close", "a b", ""):
        with pytest.raises(ProviderError):
            td.close_task(bad)
    assert calls == []


def test_todoist_retries_rate_limits_with_the_same_request_id():
    seen = []

    def handler(req):
        seen.append(req.headers.get("x-request-id"))
        return httpx.Response(429, headers={"retry-after": "0"}) if len(seen) < 3 else httpx.Response(204)
    todoist(handler).close_task("abc123")
    assert len(seen) == 3 and len(set(seen)) == 1


def test_todoist_gives_up_after_retries_and_explains_bad_tokens():
    with pytest.raises(ProviderError, match="502"):
        todoist(lambda r: httpx.Response(502, text="bad gateway"), retries=1).today()
    with pytest.raises(ProviderError, match="rejected the token"):
        todoist(lambda r: httpx.Response(401)).today()


def test_todoist_check_reports_account_and_projects():
    def handler(req):
        if req.url.path.endswith("/user"):
            return httpx.Response(200, json={"email": "sam@example.com"})
        return httpx.Response(200, json={"results": [{"name": "Inbox"}, {"name": "Home"}]})
    assert todoist(handler).check() == {"account": "sam@example.com", "lists": ["Inbox", "Home"]}


def test_todoist_move_uses_due_date_not_due_string():
    bodies = []
    todoist(lambda r: bodies.append(json.loads(r.content)) or httpx.Response(200, json={})).set_due_date("a1", "2026-10-06")
    assert bodies == [{"due_date": "2026-10-06"}]


# --- CalDAV against a real server ---------------------------------------------
@pytest.fixture(scope="module")
def radicale(tmp_path_factory):
    import radicale.server
    from radicale import config as rconf

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    conf = rconf.load()
    conf.update({"server": {"hosts": f"127.0.0.1:{port}"}, "auth": {"type": "none"},
                 "storage": {"filesystem_folder": str(tmp_path_factory.mktemp("radicale"))},
                 "rights": {"type": "owner_only"}, "logging": {"level": "error"}}, "test", privileged=True)
    shutdown, keep = socket.socketpair()
    threading.Thread(target=radicale.server.serve, args=(conf, shutdown), daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    for _ in range(50):
        try:
            httpx.get(url)
            break
        except httpx.TransportError:
            time.sleep(0.1)
    yield url
    keep.close()


@pytest.fixture
def cal(radicale, request):
    import caldav
    user = request.node.name[:30].replace("[", "_").replace("]", "")
    client = caldav.DAVClient(radicale, username=user, password="pw")
    principal = client.principal()
    chores = principal.make_calendar(name="Chores", supported_calendar_component_set=["VTODO"])
    principal.make_calendar(name="Work", supported_calendar_component_set=["VTODO"])
    return radicale, user, chores


def test_caldav_check_and_today(cal):
    url, user, chores = cal
    today = dt.date.today()
    chores.save_todo(summary="Due today", due=today, priority=1)
    chores.save_todo(summary="Overdue", due=today - dt.timedelta(days=3))
    chores.save_todo(summary="Future", due=today + dt.timedelta(days=5))
    chores.save_todo(summary="No date")
    chores.save_todo(summary="At 9:30", due=dt.datetime.combine(today, dt.time(9, 30)))
    p = CalDAVTasks(url, user, "pw")
    assert p.check() == {"account": user, "lists": ["Chores", "Work"]}
    items = {t["content"]: t for t in p.today()}
    assert set(items) == {"Due today", "Overdue", "At 9:30"}
    assert items["Due today"]["priority"] == 4 and items["Overdue"]["priority"] == 1
    assert items["At 9:30"]["due"]["date"].endswith("09:30:00")
    assert len(p.filter_tasks("all")) == 5
    assert p.filter_tasks("work") == []
    with pytest.raises(ProviderError, match="Your lists: Chores, Work"):
        p.filter_tasks("Groceries")


def test_caldav_complete_move_delete_add(cal):
    url, user, chores = cal
    today = dt.date.today()
    one = chores.save_todo(summary="One-off", due=today)
    daily = chores.save_todo(summary="Water plants", due=today, dtstart=today, rrule={"FREQ": "DAILY"})
    later = chores.save_todo(summary="Move me", due=dt.datetime.combine(today, dt.time(14, 0)))
    gone = chores.save_todo(summary="Delete me", due=today)
    uid = lambda t: str(t.icalendar_component["uid"])  # noqa: E731
    p = CalDAVTasks(url, user, "pw")

    p.close_task(uid(one))
    p.close_task(uid(daily))            # repeating: advances, stays on the list
    p.set_due_date(uid(later), (today + dt.timedelta(days=1)).isoformat())
    p.delete_task(uid(gone))
    added = p.add_task("Buy tape", "62mm")

    left = {t["content"]: t for t in p.filter_tasks("all")}
    assert "One-off" not in left and "Delete me" not in left
    assert left["Water plants"]["due"]["date"] == (today + dt.timedelta(days=1)).isoformat()
    assert left["Water plants"]["due"]["is_recurring"]
    assert left["Move me"]["due"]["date"].startswith((today + dt.timedelta(days=1)).isoformat())
    assert left["Move me"]["due"]["date"].endswith("14:00:00")  # kept its time
    assert added["content"] == "Buy tape" and "Buy tape" in left
    with pytest.raises(ProviderError, match="repeating"):
        p.set_due_date(uid(daily), (today + dt.timedelta(days=3)).isoformat())


def test_caldav_single_list_and_bad_login(cal, radicale):
    url, user, chores = cal
    with pytest.raises(ProviderError, match="No task list called"):
        CalDAVTasks(url, user, "pw", list_name="Nope").check()
    assert CalDAVTasks(url, user, "pw", list_name="Work").check()["account"] == user
    with pytest.raises(ProviderError, match="Couldn't reach"):
        CalDAVTasks("http://127.0.0.1:9/", user, "pw").check()
