"""Which to-do apps people can link, and how to build a connection.

Four kinds:
  token  paste a token or password (Todoist, CalDAV, Notion, Asana, monday.com)
  oauth  "Sign in with ..." (Google Tasks, Microsoft To Do, TickTick); needs the owner's one-time setup
  later  possible, not built yet (ClickUp, Trello): people can ask for it in Feedback
  none   the app has no way in for other apps (Apple Reminders, Things, Any.do, Google Keep)
"""
from __future__ import annotations

from . import oauth
from .asana import Asana
from .base import ProviderError, TaskProvider
from .caldav_tasks import CalDAVTasks
from .monday import Monday
from .notion import Notion
from .todoist import Todoist

# Paste-a-token apps. `fields` drive the connect form; secret fields are encrypted at rest.
_TOKEN = [
    {"key": "todoist", "name": "Todoist", "status": "ready",
     "fields": [{"name": "token", "label": "API token", "secret": True,
                 "help": "Todoist → Settings → Integrations → Developer → API token"}],
     "filter_help": "Any Todoist filter: #Groceries, p1, @errands, tomorrow"},
    {"key": "caldav", "name": "CalDAV task list", "status": "ready",
     "also": "Nextcloud Tasks, Fastmail, Synology, Zoho, Radicale, DAVx⁵, older iCloud Reminders lists",
     "fields": [{"name": "url", "label": "Server address",
                 "help": "Nextcloud: https://<your-cloud>/remote.php/dav · Fastmail: https://caldav.fastmail.com/ "
                         "· iCloud: https://caldav.icloud.com/"},
                {"name": "username", "label": "Username"},
                {"name": "password", "label": "App password", "secret": True,
                 "help": "Make an app-specific password in your account's security settings"},
                {"name": "list", "label": "Only this list", "optional": True,
                 "help": "Leave blank to use every task list"}],
     "filter_help": "A list name, or \"all\""},
    {"key": "notion", "name": "Notion", "status": "ready",
     "also": "a Notion database you use as a to-do list",
     "fields": [{"name": "token", "label": "Integration token", "secret": True,
                 "help": "notion.so/my-integrations → New integration (Internal) → copy the secret. Then open your "
                         "to-do database → ⋯ → Connections → add the integration."},
                {"name": "database", "label": "Database link",
                 "help": "In the database: Share → Copy link. It needs a Checkbox or Status column for done."}],
     "filter_help": "\"all\", #tag, or words in the task"},
    {"key": "asana", "name": "Asana", "status": "ready",
     "also": "tasks assigned to you",
     "fields": [{"name": "token", "label": "Personal access token", "secret": True,
                 "help": "app.asana.com/0/my-apps → Create new token"},
                {"name": "workspace", "label": "Workspace", "optional": True,
                 "help": "Leave blank to use your first workspace"}],
     "filter_help": "\"all\", #tag, or words in the task"},
    {"key": "monday", "name": "monday.com", "status": "ready",
     "also": "items assigned to you on boards with a person, a date and a status column",
     "fields": [{"name": "token", "label": "Personal API token", "secret": True,
                 "help": "monday.com → your picture → Developers → My access tokens → Show, then copy it"},
                {"name": "board", "label": "Board", "optional": True,
                 "help": "Leave blank to use your boards that have a person, a date and a status column. Or paste "
                         "one board's link from your browser."}],
     "filter_help": "\"all\", #tag, or words in the item"},
]

_OAUTH = [
    {"key": "google_tasks", "name": "Google Tasks", "app": "google", "also": "Gmail and Google Calendar's task lists"},
    {"key": "microsoft_todo", "name": "Microsoft To Do", "app": "microsoft", "also": "Outlook tasks"},
    {"key": "ticktick", "name": "TickTick", "app": "ticktick"},
]

# Possible, not built yet. Settings lists them under "Other apps" with a way to ask for them.
_LATER = [
    {"key": "clickup", "name": "ClickUp", "status": "later",
     "reason": "Not yet. Ask for it in Feedback, so we know to build it next."},
    {"key": "trello", "name": "Trello", "status": "later",
     "reason": "Not yet. Ask for it in Feedback, so we know to build it next."},
]

_NONE = [
    {"key": "apple_reminders", "name": "Apple Reminders", "status": "unsupported",
     "reason": "Apple doesn't let other apps reach current Reminders lists. Use My list instead, or lists that "
               "were never upgraded through CalDAV (https://caldav.icloud.com/)."},
    {"key": "things", "name": "Things", "status": "unsupported",
     "reason": "Things has no way for other apps to connect. Use My list instead."},
    {"key": "anydo", "name": "Any.do", "status": "unsupported",
     "reason": "Any.do has no public way for other apps to connect. Use My list instead."},
    {"key": "google_keep", "name": "Google Keep", "status": "unsupported",
     "reason": "Google Keep has no public way for other apps to connect. Google Tasks works."},
]
READY = {p["key"]: p for p in _TOKEN}


def providers() -> list[dict]:
    """What the Settings page shows. Sign-in apps say "needs setup" until the owner switches them on."""
    signin = []
    for p in _OAUTH:
        on = oauth.ready(p["app"])
        signin.append({**p, "status": "oauth" if on else "needs_setup",
                       **({} if on else {"reason": f"Needs a one-time {oauth.APPS[p['app']]['name']} sign-in setup "
                                                   "by the Next Box owner."})})
    return _TOKEN + signin + _LATER + _NONE


def secret_fields(key: str) -> set[str]:
    return {f["name"] for f in READY[key]["fields"] if f.get("secret")}


def build_provider(key: str, config: dict, secrets: dict) -> TaskProvider:
    if key == "todoist":
        return Todoist(secrets.get("token", ""))
    if key == "caldav":
        return CalDAVTasks(config.get("url", ""), config.get("username", ""),
                           secrets.get("password", ""), config.get("list", ""))
    if key == "notion":
        return Notion(secrets.get("token", ""), config.get("database", ""))
    if key == "asana":
        return Asana(secrets.get("token", ""), config.get("workspace", ""))
    if key == "monday":
        return Monday(secrets.get("token", ""), config.get("board", ""))
    if key in oauth.TASKS:
        return oauth.TASKS[key](secrets)
    raise ProviderError(f"{key} isn't available yet")


def display_name(key: str) -> str:
    for p in _TOKEN + _OAUTH + _LATER + _NONE:
        if p["key"] == key:
            return p["name"]
    return key


# Older code reads a constant list.
PROVIDERS = _TOKEN + _NONE

__all__ = ["PROVIDERS", "READY", "ProviderError", "TaskProvider", "build_provider", "secret_fields",
           "providers", "display_name", "Todoist", "CalDAVTasks", "Notion", "Asana", "Monday"]
