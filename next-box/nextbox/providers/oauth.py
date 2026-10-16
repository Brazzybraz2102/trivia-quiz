"""Apps you connect by signing in: Google (Tasks, Calendar), Microsoft (To Do, Outlook Calendar)
and TickTick.

Each needs a one-time "app registration" by the Next Box owner (client id + secret in .env) and a
public https address for the sign-in to come back to (NEXTBOX_PUBLIC_URL). Until then these show
as "needs setup". Tokens are sealed by the Vault like every other secret, and only the refresh
token is kept: a fresh access token is fetched when needed.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
import re
import secrets
import threading
import time
from urllib.parse import quote, urlencode

import httpx

from .calendar_base import CalendarSource, sort_events
from .base import ProviderError, TaskProvider
from .http import Api
from .ids import pack, unpack

# --- the three sign-in services --------------------------------------------------------------
APPS = {
    "google": {
        "name": "Google",
        "auth": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scopes": {"tasks": "https://www.googleapis.com/auth/tasks",
                   "calendar": "https://www.googleapis.com/auth/calendar.readonly"},
        "base_scopes": "openid email",
        "extra": {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
        "console": "console.cloud.google.com → APIs & Services → Credentials → OAuth client (Web application)",
    },
    "microsoft": {
        "name": "Microsoft",
        "auth": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": {"tasks": "Tasks.ReadWrite", "calendar": "Calendars.Read"},
        "base_scopes": "offline_access User.Read",
        "extra": {"prompt": "select_account"},
        "console": "entra.microsoft.com → App registrations → New registration (any account type)",
    },
    "ticktick": {
        "name": "TickTick",
        "auth": "https://ticktick.com/oauth/authorize",
        "token": "https://ticktick.com/oauth/token",
        "scopes": {"tasks": "tasks:read tasks:write"},
        "base_scopes": "",
        "extra": {},
        "basic_auth": True,
        "console": "developer.ticktick.com → Manage Apps → New App",
    },
}
# Which provider key each (app, purpose) connects.
TASK_KEYS = {("google", "tasks"): "google_tasks", ("microsoft", "tasks"): "microsoft_todo",
             ("ticktick", "tasks"): "ticktick"}
CAL_KEYS = {("google", "calendar"): "google_calendar", ("microsoft", "calendar"): "outlook_calendar"}


def credentials(app: str) -> tuple[str, str]:
    prefix = app.upper()
    return os.environ.get(f"{prefix}_CLIENT_ID", "").strip(), os.environ.get(f"{prefix}_CLIENT_SECRET", "").strip()


def public_url() -> str:
    return os.environ.get("NEXTBOX_PUBLIC_URL", "").strip().rstrip("/")


def ready(app: str) -> bool:
    cid, secret = credentials(app)
    return bool(cid and secret and public_url())


def redirect_uri(app: str) -> str:
    return f"{public_url()}/connect/{app}/callback"


def setup_status() -> dict:
    """For the owner: which sign-ins are switched on, and what each still needs (no secrets)."""
    out = {}
    for app, spec in APPS.items():
        cid, secret = credentials(app)
        missing = [x for x, ok in ((f"{app.upper()}_CLIENT_ID", cid), (f"{app.upper()}_CLIENT_SECRET", secret),
                                   ("NEXTBOX_PUBLIC_URL", public_url())) if not ok]
        out[app] = {"ready": not missing, "missing": missing,
                    "redirect_uri": redirect_uri(app) if public_url() else None, "where": spec["console"]}
    return out


# --- the sign-in round trip ---------------------------------------------------------------------
_pending: dict[str, dict] = {}  # state -> who started it; lives a few minutes, in memory only
_pending_lock = threading.Lock()
ELSEWHERE = "That sign-in was started somewhere else. Start it again from Settings."


def start(app: str, purpose: str, username: str) -> tuple[str, str]:
    """(The address to send the person to, a one-time nonce for this browser's cookie.)
    PKCE + a one-time state tied to who started it. The nonce proves the browser that comes back
    is the one that started, so a consent link forwarded to someone else can't land their account
    on the starter's (see finish)."""
    if app not in APPS or purpose not in APPS[app]["scopes"]:
        raise ProviderError("That sign-in isn't available.")
    if not ready(app):
        raise ProviderError(f"{APPS[app]['name']} sign-in isn't set up on this Next Box yet.")
    spec = APPS[app]
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(16)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    with _pending_lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if v["expires"] < now]:
            del _pending[k]
        _pending[state] = {"user": username, "app": app, "purpose": purpose, "verifier": verifier,
                           "nonce": nonce, "expires": now + 600}
    params = {"client_id": credentials(app)[0], "redirect_uri": redirect_uri(app), "response_type": "code",
              "scope": " ".join(x for x in (spec["base_scopes"], spec["scopes"][purpose]) if x),
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256", **spec["extra"]}
    return f"{spec['auth']}?{urlencode(params)}", nonce


def who_started(state: str) -> str | None:
    with _pending_lock:
        p = _pending.get(state)
    return p["user"] if p and p["expires"] >= time.time() else None


def finish(app: str, state: str, code: str, username: str, client: httpx.Client | None = None, *,
           nonce: str) -> tuple[str, dict]:
    """Swap the code for tokens. Returns (purpose, secrets to seal). The state is single-use and
    names who started the sign-in, and `nonce` (from the starter's browser cookie) must match, so
    the result can only land on that person's account, from the browser they started in."""
    with _pending_lock:
        pending = _pending.pop(state, None)
    if not pending or pending["app"] != app or pending["expires"] < time.time():
        raise ProviderError("That sign-in took too long or was already used. Try connecting again.")
    if not secrets.compare_digest(str(nonce or "").encode(), pending["nonce"].encode()):
        raise ProviderError(ELSEWHERE)
    if pending["user"] != username:
        raise ProviderError("That sign-in was started by a different Next Box account.")
    cid, secret = credentials(app)
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(app),
            "code_verifier": pending["verifier"], "client_id": cid}
    tokens = _token_request(app, data, client)
    return pending["purpose"], _keep(tokens)


def _token_request(app: str, data: dict, client: httpx.Client | None = None) -> dict:
    cid, secret = credentials(app)
    http = client or httpx.Client(timeout=20.0)
    auth = None
    if APPS[app].get("basic_auth"):
        auth = (cid, secret)
    else:
        data = {**data, "client_secret": secret}
    try:
        r = http.post(APPS[app]["token"], data=data, auth=auth, headers={"Accept": "application/json"})
    except httpx.TransportError as exc:
        raise ProviderError(f"{APPS[app]['name']} didn't answer ({type(exc).__name__}). Try again in a minute.") from exc
    body = r.json() if r.content else {}
    if r.status_code >= 400 or "access_token" not in body:
        why = body.get("error_description") or body.get("error") or r.status_code
        raise ProviderError(f"{APPS[app]['name']} sign-in didn't work ({why}). Connect again.")
    return body


def _keep(tokens: dict) -> dict:
    return {"access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token", ""),
            "expires_at": int(time.time()) + int(tokens.get("expires_in") or 3600)}


class Session:
    """Hands out a valid access token, refreshing it (and saving any new refresh token) as needed."""

    def __init__(self, app: str, secrets: dict, client: httpx.Client | None = None):
        self.app = app
        self.tokens = {k: v for k, v in secrets.items() if not k.startswith("_")}
        self.save = secrets.get("_save")
        self.client = client

    def token(self) -> str:
        if self.tokens.get("access_token") and self.tokens.get("expires_at", 0) > time.time() + 60:
            return self.tokens["access_token"]
        if not self.tokens.get("refresh_token"):
            raise ProviderError(f"{APPS[self.app]['name']} sign-in expired. Connect it again in Settings.")
        fresh = _token_request(self.app, {"grant_type": "refresh_token", "refresh_token": self.tokens["refresh_token"],
                                          "client_id": credentials(self.app)[0]}, self.client)
        self.tokens = {**_keep(fresh), "refresh_token": fresh.get("refresh_token") or self.tokens["refresh_token"]}
        if callable(self.save):
            self.save(dict(self.tokens))
        return self.tokens["access_token"]

    def api(self, name: str, base: str, client: httpx.Client | None = None, extra_headers: dict | None = None) -> Api:
        return Api(name, base, headers={"Authorization": f"Bearer {self.token()}", **(extra_headers or {})},
                   client=client)


def _check_id(value: str) -> str:
    if not value or len(value) > 512 or any(c in value for c in "\r\n"):
        raise ProviderError("That task id is damaged.")
    return quote(value, safe="")


def _days_through(day: dt.date) -> str:
    return day.isoformat()


# --- Google Tasks -----------------------------------------------------------------------------
class GoogleTasks(TaskProvider):
    key = "google_tasks"
    BASE = "https://tasks.googleapis.com/tasks/v1"

    def __init__(self, secrets: dict, client: httpx.Client | None = None, today=dt.date.today):
        self.session = Session("google", secrets, client)
        self.client = client
        self._api = None
        self._today = today

    @property
    def api(self) -> Api:
        if self._api is None:
            self._api = self.session.api("Google Tasks", self.BASE, self.client)
        return self._api

    def _lists(self) -> list[dict]:
        return (self.api.call("GET", "/users/@me/lists", params={"maxResults": 100}) or {}).get("items", [])

    def _tasks(self, list_id: str) -> list[dict]:
        out, token = [], None
        for _ in range(20):
            params = {"showCompleted": "false", "showHidden": "false", "maxResults": 100}
            if token:
                params["pageToken"] = token
            page = self.api.call("GET", f"/lists/{_check_id(list_id)}/tasks", params=params) or {}
            out += [self._shape(list_id, t) for t in page.get("items", []) if t.get("status") != "completed"]
            token = page.get("nextPageToken")
            if not token:
                break
        return out

    def _shape(self, list_id: str, t: dict) -> dict:
        due = t.get("due")  # Google keeps only the date, written as midnight UTC
        return {"id": pack("gt", list_id, t["id"]), "content": t.get("title") or "(untitled)",
                "description": t.get("notes", ""), "priority": 1,
                "due": {"date": due[:10], "is_recurring": False} if due else None, "labels": []}

    def check(self) -> dict:
        return {"account": "Google account", "lists": [lst.get("title", "Tasks") for lst in self._lists()]}

    def _all(self) -> list[dict]:
        out = []
        for lst in self._lists()[:20]:
            out += self._tasks(lst["id"])
        return out

    def today(self) -> list[dict]:
        today = self._today().isoformat()
        return [t for t in self._all() if t["due"] and t["due"]["date"] <= today]

    def filter_tasks(self, query: str) -> list[dict]:
        q = query.strip().lower()
        if q in ("", "all"):
            return self._all()
        if q in ("today | overdue", "today", "overdue"):
            return self.today()
        lists = [lst for lst in self._lists() if lst.get("title", "").lower() == q]
        if not lists:
            raise ProviderError(f"No Google Tasks list called {query!r}")
        return self._tasks(lists[0]["id"])

    def get_task(self, task_id: str) -> dict:
        lst, tid = unpack("gt", task_id)
        return self._shape(lst, self.api.call("GET", f"/lists/{_check_id(lst)}/tasks/{_check_id(tid)}"))

    def add_task(self, content: str, description: str = "") -> dict:
        t = self.api.call("POST", "/lists/@default/tasks", json={"title": content, "notes": description})
        return self._shape("@default", t)

    def close_task(self, task_id: str) -> None:
        lst, tid = unpack("gt", task_id)
        self.api.call("PATCH", f"/lists/{_check_id(lst)}/tasks/{_check_id(tid)}", json={"status": "completed"})

    def set_due_date(self, task_id: str, date_iso: str) -> None:
        lst, tid = unpack("gt", task_id)
        self.api.call("PATCH", f"/lists/{_check_id(lst)}/tasks/{_check_id(tid)}",
                      json={"due": f"{date_iso[:10]}T00:00:00.000Z"})

    def delete_task(self, task_id: str) -> None:
        lst, tid = unpack("gt", task_id)
        self.api.call("DELETE", f"/lists/{_check_id(lst)}/tasks/{_check_id(tid)}")


# --- Microsoft To Do ----------------------------------------------------------------------------
class MicrosoftTodo(TaskProvider):
    key = "microsoft_todo"
    BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self, secrets: dict, client: httpx.Client | None = None, today=dt.date.today):
        self.session = Session("microsoft", secrets, client)
        self.client = client
        self._api = None
        self._today = today

    @property
    def api(self) -> Api:
        if self._api is None:
            self._api = self.session.api("Microsoft To Do", self.BASE, self.client)
        return self._api

    def _lists(self) -> list[dict]:
        return (self.api.call("GET", "/me/todo/lists") or {}).get("value", [])

    def _tasks(self, list_id: str) -> list[dict]:
        out = []
        page = self.api.call("GET", f"/me/todo/lists/{_check_id(list_id)}/tasks",
                             params={"$filter": "status ne 'completed'", "$top": 100}) or {}
        for _ in range(20):
            out += [self._shape(list_id, t) for t in page.get("value", [])]
            nxt = page.get("@odata.nextLink")
            if not nxt or not nxt.startswith(self.BASE + "/"):
                break
            page = self.api.call("GET", nxt[len(self.BASE):]) or {}
        return out

    def _shape(self, list_id: str, t: dict) -> dict:
        due = (t.get("dueDateTime") or {}).get("dateTime")
        imp = t.get("importance", "normal")
        return {"id": pack("ms", list_id, t["id"]), "content": t.get("title") or "(untitled)",
                "description": (t.get("body") or {}).get("content", "") if (t.get("body") or {}).get("contentType") == "text" else "",
                "priority": 4 if imp == "high" else 1,
                "due": {"date": due[:10], "is_recurring": bool(t.get("recurrence"))} if due else None,
                "labels": list(t.get("categories") or [])}

    def check(self) -> dict:
        me = self.api.call("GET", "/me", params={"$select": "mail,userPrincipalName"}) or {}
        return {"account": me.get("mail") or me.get("userPrincipalName") or "Microsoft account",
                "lists": [lst.get("displayName", "Tasks") for lst in self._lists()]}

    def _all(self) -> list[dict]:
        out = []
        for lst in self._lists()[:20]:
            out += self._tasks(lst["id"])
        return out

    def today(self) -> list[dict]:
        today = self._today().isoformat()
        return [t for t in self._all() if t["due"] and t["due"]["date"] <= today]

    def filter_tasks(self, query: str) -> list[dict]:
        q = query.strip().lower()
        if q in ("", "all"):
            return self._all()
        if q in ("today | overdue", "today", "overdue"):
            return self.today()
        lists = [lst for lst in self._lists() if lst.get("displayName", "").lower() == q]
        if not lists:
            raise ProviderError(f"No Microsoft To Do list called {query!r}")
        return self._tasks(lists[0]["id"])

    def _path(self, task_id: str) -> str:
        lst, tid = unpack("ms", task_id)
        return f"/me/todo/lists/{_check_id(lst)}/tasks/{_check_id(tid)}"

    def get_task(self, task_id: str) -> dict:
        lst, _ = unpack("ms", task_id)
        return self._shape(lst, self.api.call("GET", self._path(task_id)))

    def add_task(self, content: str, description: str = "") -> dict:
        lists = self._lists()
        default = next((lst for lst in lists if lst.get("wellknownListName") == "defaultList"), lists[0] if lists else None)
        if not default:
            raise ProviderError("Microsoft To Do has no lists yet.")
        body = {"title": content}
        if description:
            body["body"] = {"content": description, "contentType": "text"}
        t = self.api.call("POST", f"/me/todo/lists/{_check_id(default['id'])}/tasks", json=body)
        return self._shape(default["id"], t)

    def close_task(self, task_id: str) -> None:
        self.api.call("PATCH", self._path(task_id), json={"status": "completed"})

    def set_due_date(self, task_id: str, date_iso: str) -> None:
        self.api.call("PATCH", self._path(task_id),
                      json={"dueDateTime": {"dateTime": f"{date_iso[:10]}T00:00:00", "timeZone": "UTC"}})

    def delete_task(self, task_id: str) -> None:
        self.api.call("DELETE", self._path(task_id))


# --- TickTick -----------------------------------------------------------------------------------
def _tt_priority(p) -> int:
    return {5: 4, 3: 3, 1: 2}.get(int(p or 0), 1)


class TickTick(TaskProvider):
    key = "ticktick"
    BASE = "https://api.ticktick.com/open/v1"

    def __init__(self, secrets: dict, client: httpx.Client | None = None, today=dt.date.today):
        self.session = Session("ticktick", secrets, client)
        self.client = client
        self._api = None
        self._today = today

    @property
    def api(self) -> Api:
        if self._api is None:
            self._api = self.session.api("TickTick", self.BASE, self.client)
        return self._api

    def _projects(self) -> list[dict]:
        return [{"id": "inbox", "name": "Inbox"}] + list(self.api.call("GET", "/project") or [])

    def _tasks(self, project_id: str) -> list[dict]:
        try:
            data = self.api.call("GET", f"/project/{_check_id(project_id)}/data") or {}
        except ProviderError:
            if project_id == "inbox":
                return []  # some accounts don't expose the inbox this way
            raise
        return [self._shape(t) for t in data.get("tasks", []) if int(t.get("status", 0)) == 0]

    def _shape(self, t: dict) -> dict:
        due = t.get("dueDate")
        when = None
        if due:
            try:
                d = dt.datetime.strptime(due[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
                if due[19:].strip() and due[19:].strip() not in ("Z", ".000Z"):
                    d = dt.datetime.strptime(due.replace(".000", ""), "%Y-%m-%dT%H:%M:%S%z")
                local = d.astimezone()
                when = local.date().isoformat() if t.get("isAllDay") else local.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                when = due[:10]
        return {"id": pack("tt", t.get("projectId", "inbox"), t["id"]), "content": t.get("title") or "(untitled)",
                "description": t.get("content", ""), "priority": _tt_priority(t.get("priority")),
                "due": {"date": when, "is_recurring": bool(t.get("repeatFlag"))} if when else None,
                "labels": list(t.get("tags") or [])}

    def check(self) -> dict:
        return {"account": "TickTick account", "lists": [p.get("name", "List") for p in self._projects()]}

    def _all(self) -> list[dict]:
        out = []
        for p in self._projects()[:30]:
            out += self._tasks(p["id"])
        return out

    def today(self) -> list[dict]:
        today = self._today().isoformat()
        return [t for t in self._all() if t["due"] and t["due"]["date"][:10] <= today]

    def filter_tasks(self, query: str) -> list[dict]:
        q = query.strip().lower()
        if q in ("", "all"):
            return self._all()
        if q in ("today | overdue", "today", "overdue"):
            return self.today()
        projects = [p for p in self._projects() if p.get("name", "").lower() == q]
        if not projects:
            raise ProviderError(f"No TickTick list called {query!r}")
        return self._tasks(projects[0]["id"])

    def get_task(self, task_id: str) -> dict:
        proj, tid = unpack("tt", task_id)
        return self._shape(self.api.call("GET", f"/project/{_check_id(proj)}/task/{_check_id(tid)}"))

    def add_task(self, content: str, description: str = "") -> dict:
        return self._shape(self.api.call("POST", "/task", json={"title": content, "content": description}))

    def close_task(self, task_id: str) -> None:
        proj, tid = unpack("tt", task_id)
        self.api.call("POST", f"/project/{_check_id(proj)}/task/{_check_id(tid)}/complete")

    def set_due_date(self, task_id: str, date_iso: str) -> None:
        proj, tid = unpack("tt", task_id)
        self.api.call("POST", f"/task/{_check_id(tid)}", json={
            "id": tid, "projectId": proj, "dueDate": f"{date_iso[:10]}T00:00:00.000+0000", "isAllDay": True})

    def delete_task(self, task_id: str) -> None:
        proj, tid = unpack("tt", task_id)
        self.api.call("DELETE", f"/project/{_check_id(proj)}/task/{_check_id(tid)}")


# --- calendars ------------------------------------------------------------------------------------
def _when(value: dict | None) -> dt.datetime | dt.date | None:
    """Google {"date"} / {"dateTime"} and Microsoft {"dateTime", "timeZone": "UTC"} -> local."""
    if not value:
        return None
    if value.get("date"):
        return dt.date.fromisoformat(value["date"][:10])
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?$",
                 value.get("dateTime", "").strip())
    if not m:
        return None
    zone = (m[2] or "").replace("Z", "+00:00")
    d = dt.datetime.fromisoformat(m[1] + zone)
    if d.tzinfo is None and value.get("timeZone", "UTC").upper() == "UTC":
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def _event(title: str, start, end, day: dt.date, calendar: str, location: str = "", all_day: bool | None = None) -> dict | None:
    if start is None:
        return None
    is_all_day = all_day if all_day is not None else not isinstance(start, dt.datetime)
    if not is_all_day and isinstance(start, dt.datetime) and start.date() != day:
        is_all_day = True
    return {"title": (title or "(no title)")[:200], "all_day": is_all_day,
            "start": None if is_all_day else start.strftime("%H:%M"),
            "end": end.strftime("%H:%M") if (not is_all_day and isinstance(end, dt.datetime) and end.date() == day) else None,
            "calendar": calendar, "location": (location or "")[:120]}


def _day_bounds(day: dt.date) -> tuple[str, str]:
    start = dt.datetime.combine(day, dt.time()).astimezone()
    end = start + dt.timedelta(days=1)
    return start.isoformat(), end.isoformat()


class GoogleCalendar(CalendarSource):
    key = "google_calendar"
    BASE = "https://www.googleapis.com/calendar/v3"

    def __init__(self, secrets: dict, client: httpx.Client | None = None):
        self.session = Session("google", secrets, client)
        self.client = client
        self._api = None

    @property
    def api(self) -> Api:
        if self._api is None:
            self._api = self.session.api("Google Calendar", self.BASE, self.client)
        return self._api

    def _calendars(self) -> list[dict]:
        items = (self.api.call("GET", "/users/me/calendarList", params={"maxResults": 50}) or {}).get("items", [])
        return [c for c in items if c.get("selected", True) and not c.get("deleted")][:15]

    def check(self) -> dict:
        cals = self._calendars()
        primary = next((c for c in cals if c.get("primary")), {})
        return {"account": primary.get("id", "Google account"), "calendars": [c.get("summary", "") for c in cals]}

    def events(self, day: dt.date) -> list[dict]:
        lo, hi = _day_bounds(day)
        out = []
        for cal in self._calendars():
            page = self.api.call("GET", f"/calendars/{_check_id(cal['id'])}/events", params={
                "timeMin": lo, "timeMax": hi, "singleEvents": "true", "orderBy": "startTime", "maxResults": 100}) or {}
            for e in page.get("items", []):
                if e.get("status") == "cancelled":
                    continue
                ev = _event(e.get("summary"), _when(e.get("start")), _when(e.get("end")), day,
                            cal.get("summary", ""), e.get("location", ""))
                if ev:
                    out.append(ev)
        return sort_events(out)


class OutlookCalendar(CalendarSource):
    key = "outlook_calendar"
    BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self, secrets: dict, client: httpx.Client | None = None):
        self.session = Session("microsoft", secrets, client)
        self.client = client
        self._api = None

    @property
    def api(self) -> Api:
        if self._api is None:
            self._api = self.session.api("Outlook Calendar", self.BASE, self.client,
                                         extra_headers={"Prefer": 'outlook.timezone="UTC"'})
        return self._api

    def check(self) -> dict:
        me = self.api.call("GET", "/me", params={"$select": "mail,userPrincipalName"}) or {}
        cals = (self.api.call("GET", "/me/calendars", params={"$select": "name"}) or {}).get("value", [])
        return {"account": me.get("mail") or me.get("userPrincipalName") or "Microsoft account",
                "calendars": [c.get("name", "") for c in cals]}

    def events(self, day: dt.date) -> list[dict]:
        lo, hi = _day_bounds(day)
        page = self.api.call("GET", "/me/calendarView", params={
            "startDateTime": lo, "endDateTime": hi, "$top": 100,
            "$select": "subject,start,end,isAllDay,isCancelled,location"}) or {}
        out = []
        for e in page.get("value", []):
            if e.get("isCancelled"):
                continue
            start, end = _when(e.get("start")), _when(e.get("end"))
            if e.get("isAllDay") and isinstance(start, dt.datetime):
                start = start.date()
            ev = _event(e.get("subject"), start, end, day, "Outlook", (e.get("location") or {}).get("displayName", ""),
                        all_day=bool(e.get("isAllDay")))
            if ev:
                out.append(ev)
        return sort_events(out)


TASKS = {"google_tasks": GoogleTasks, "microsoft_todo": MicrosoftTodo, "ticktick": TickTick}
CALENDARS = {"google_calendar": GoogleCalendar, "outlook_calendar": OutlookCalendar}
APP_OF = {"google_tasks": "google", "microsoft_todo": "microsoft", "ticktick": "ticktick",
          "google_calendar": "google", "outlook_calendar": "microsoft"}
