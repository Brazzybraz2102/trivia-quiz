"""Which to-do apps people can connect, and how to build a connection."""
from __future__ import annotations

from .base import ProviderError, TaskProvider
from .caldav_tasks import CalDAVTasks
from .todoist import Todoist

# Shown on the Settings page. `fields` drive the connect form; secret fields are encrypted at rest.
PROVIDERS = [
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
                {"name": "list", "label": "Only this list (optional)", "optional": True,
                 "help": "Leave blank to use every task list"}],
     "filter_help": "A list name, or \"all\""},
    {"key": "google_tasks", "name": "Google Tasks", "status": "needs_setup",
     "reason": "Needs a one-time Google sign-in setup by the Next Box owner."},
    {"key": "microsoft_todo", "name": "Microsoft To Do", "status": "needs_setup",
     "reason": "Needs a one-time Microsoft sign-in setup by the Next Box owner."},
    {"key": "ticktick", "name": "TickTick", "status": "needs_setup",
     "reason": "Needs a one-time TickTick developer app setup by the Next Box owner."},
    {"key": "apple_reminders", "name": "Apple Reminders", "status": "unsupported",
     "reason": "Apple doesn't let other apps reach current Reminders lists. Lists that were never "
               "upgraded still work through CalDAV (https://caldav.icloud.com/)."},
    {"key": "things", "name": "Things", "status": "unsupported", "reason": "Things has no way for other apps to connect."},
    {"key": "anydo", "name": "Any.do", "status": "unsupported", "reason": "Any.do has no public way for other apps to connect."},
    {"key": "google_keep", "name": "Google Keep", "status": "unsupported", "reason": "Google Keep has no public way for other apps to connect."},
]
READY = {p["key"]: p for p in PROVIDERS if p["status"] == "ready"}


def secret_fields(key: str) -> set[str]:
    return {f["name"] for f in READY[key]["fields"] if f.get("secret")}


def build_provider(key: str, config: dict, secrets: dict) -> TaskProvider:
    if key == "todoist":
        return Todoist(secrets.get("token", ""))
    if key == "caldav":
        return CalDAVTasks(config.get("url", ""), config.get("username", ""),
                           secrets.get("password", ""), config.get("list", ""))
    raise ProviderError(f"{key} isn't available yet")


__all__ = ["PROVIDERS", "READY", "ProviderError", "TaskProvider", "build_provider", "secret_fields",
           "Todoist", "CalDAVTasks"]
