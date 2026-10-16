"""Each person's tasks and calendars: their built-in list, an optional linked to-do app, and any
number of calendars. Secrets (tokens, passwords, calendar links) are sealed by the Vault."""
from __future__ import annotations

import datetime as dt
import secrets as _secrets
import time
from collections.abc import Callable

from .auth import Accounts
from .calendars import KINDS as CALENDAR_KINDS
from .calendars import CalendarSource, build_calendar, forget_feed, sort_events
from .config import Settings
from .mylist import LocalTasks, Merged
from .providers import READY, ProviderError, TaskProvider, build_provider, display_name, oauth, secret_fields
from .providers.base import NotConnected  # noqa: F401  (re-exported: server.py imports it from here)
from .vault import Vault

NOT_CONNECTED = "Connect your to-do app in Settings first."
MAX_CALENDARS = 8

Builder = Callable[[str, dict, dict], TaskProvider]
CalendarBuilder = Callable[[str, dict, dict], CalendarSource]


def _split(kind_fields: list[dict], fields: dict) -> tuple[dict, dict]:
    missing = [f["label"] for f in kind_fields if not f.get("optional") and not str(fields.get(f["name"], "")).strip()]
    if missing:
        raise ProviderError(f"Fill in: {', '.join(missing)}")
    names = {f["name"] for f in kind_fields}
    hidden = {f["name"] for f in kind_fields if f.get("secret")}
    config = {k: str(v).strip() for k, v in fields.items() if k in names and k not in hidden}
    secret = {k: str(v).strip() for k, v in fields.items() if k in hidden}
    for f in kind_fields:
        if any(not ch.isprintable() for ch in secret.get(f["name"], "")):
            # A line break or control character pasted in the middle. Sent as a header it would
            # fail, and the error would quote the token.
            label = f["label"] if f["label"].split()[0].isupper() else f["label"][:1].lower() + f["label"][1:]
            raise ProviderError(f"That {label} has a hidden character in it. Copy it again.")
    return config, secret


class Connections:
    def __init__(self, settings: Settings, accounts: Accounts, vault: Vault,
                 builder: Builder = build_provider, calendar_builder: CalendarBuilder = build_calendar):
        self.settings = settings
        self.accounts = accounts
        self.vault = vault
        self.builder = builder
        self.calendar_builder = calendar_builder

    # --- to-do app ---------------------------------------------------------------------------
    def provider_for(self, username: str) -> TaskProvider:
        """Everyone has the built-in list; a linked app adds its tasks next to it."""
        local = LocalTasks(self.accounts.db, username, self.accounts.household_of(username) or "home")
        try:
            linked = self.linked_for(username)
        except NotConnected:
            linked = None
        prefs = (self.accounts.get(username) or {}).get("prefs") or {}
        return Merged(local, linked, new_tasks_to=prefs.get("new_tasks_to", "nextbox"))

    def _open(self, sealed: str | None, username: str, resave: Callable[[dict], None]) -> dict:
        secrets = self.vault.open(sealed) if sealed else {}
        secrets["_save"] = resave  # sign-in apps hand back refreshed tokens through this
        return secrets

    def linked_for(self, username: str) -> TaskProvider:
        conn = self.accounts.connection(username)
        if conn:
            def resave(tokens: dict, username=username) -> None:
                current = self.accounts.connection(username)
                if current and current.get("provider") == conn["provider"]:
                    self.accounts.set_connection(username, {**current, "secret": self.vault.seal(tokens)})
            return self.builder(conn["provider"], conn.get("config", {}),
                                self._open(conn.get("secret"), username, resave))
        # Before per-person connections, .env held one Todoist token: it still serves the owner.
        if self.settings.todoist_token and username == self.accounts.owner():
            return self.builder("todoist", {}, {"token": self.settings.todoist_token})
        raise NotConnected(NOT_CONNECTED)

    def status(self, username: str) -> dict:
        user = self.accounts.get(username) or {}
        conn = user.get("connection")
        if conn:
            return {"connected": True, **conn, "name": display_name(conn["provider"])}
        if self.settings.todoist_token and username == self.accounts.owner():
            return {"connected": True, "provider": "todoist", "name": "Todoist", "legacy": True,
                    "account": "token in .env", "config": {}}
        return {"connected": False}

    def _save_connection(self, username: str, provider: str, config: dict, secrets: dict) -> dict:
        info = self.builder(provider, config, dict(secrets)).check()  # never save one that doesn't work
        self.accounts.set_connection(username, {
            "provider": provider, "config": config, "secret": self.vault.seal(secrets),
            "account": str(info.get("account", ""))[:120], "lists": list(info.get("lists", []))[:50],
            "connected_at": int(time.time()),
        })
        return self.status(username)

    def connect(self, username: str, provider: str, fields: dict) -> dict:
        if provider not in READY:
            raise ProviderError(f"{provider} can't be connected with a token" +
                                ("; use its Sign in button" if provider in oauth.TASKS else ""))
        config, secrets = _split(READY[provider]["fields"], fields)
        assert set(secrets) <= secret_fields(provider)
        return self._save_connection(username, provider, config, secrets)

    def disconnect(self, username: str) -> None:
        self.accounts.set_connection(username, None)

    def check(self, username: str) -> dict:
        """The status, plus whether the linked app still answers with the saved sign-in (ok/error)."""
        status = self.status(username)
        if not status.get("connected"):
            return {**status, "ok": True, "error": ""}
        name = status.get("name") or "your app"
        try:
            self.linked_for(username).check()
        except ProviderError as exc:  # the app said no: its own words say what to do
            return {**status, "ok": False, "error": str(exc)[:300]}
        except ValueError:  # the Vault can't open it (secret.key changed or the data dir was moved)
            return {**status, "ok": False, "error": f"Reconnect {name}: the saved sign-in can't be read any more."}
        except Exception:  # noqa: BLE001 - a server we can't reach at all
            return {**status, "ok": False, "error": f"{name} didn't answer. Try again in a bit, or reconnect it."}
        return {**status, "ok": True, "error": ""}

    # --- sign in with Google / Microsoft / TickTick --------------------------------------------
    def finish_signin(self, username: str, app: str, state: str, code: str, client=None, *, nonce: str) -> dict:
        purpose, tokens = oauth.finish(app, state, code, username, client=client, nonce=nonce)
        if purpose == "tasks":
            return {"kind": "tasks", **self._save_connection(username, oauth.TASK_KEYS[(app, purpose)], {}, tokens)}
        return {"kind": "calendar", **self._add_calendar(username, oauth.CAL_KEYS[(app, purpose)], {}, tokens)}

    # --- calendars -----------------------------------------------------------------------------
    def calendar_kinds(self) -> list[dict]:
        out = [dict(k) for k in CALENDAR_KINDS]
        for (app, _), key in oauth.CAL_KEYS.items():
            name = "Google Calendar" if app == "google" else "Outlook / Microsoft 365 Calendar"
            on = oauth.ready(app)
            out.append({"key": key, "name": name, "app": app, "status": "oauth" if on else "needs_setup",
                        **({} if on else {"reason": f"Needs a one-time {oauth.APPS[app]['name']} sign-in setup by "
                                                    "the Next Box owner. A calendar link (.ics) works today."})})
        return out

    def calendars(self, username: str) -> list[dict]:
        return [{k: v for k, v in c.items() if k != "secret"} for c in self.accounts.calendars(username)]

    def _same_calendar(self, kind: str, config: dict, secrets: dict) -> Callable[[dict], bool]:
        """Matches a saved calendar that's this one again: the same .ics link, or the same CalDAV
        server, username and "only this calendar"."""
        def link(u: str) -> str:
            u = (u or "").strip()
            u = "https://" + u[len("webcal://"):] if u.lower().startswith("webcal://") else u
            return u.rstrip("/").lower()

        def caldav(cfg: dict) -> tuple:
            return (link(cfg.get("url", "")), cfg.get("username", "").strip().lower(), cfg.get("only", "").strip().lower())

        def same(entry: dict) -> bool:
            if entry.get("provider") != kind:
                return False
            if kind == "caldav":
                return caldav(entry.get("config") or {}) == caldav(config)
            if kind == "ics" and entry.get("secret"):
                try:
                    return link(self.vault.open(entry["secret"]).get("url", "")) == link(secrets.get("url", ""))
                except ValueError:  # can't be read any more: not a match
                    return False
            return False
        return same

    def _add_calendar(self, username: str, kind: str, config: dict, secrets: dict) -> dict:
        existing = self.accounts.calendars(username)
        if len(existing) >= MAX_CALENDARS:
            raise ProviderError(f"{MAX_CALENDARS} calendars is the limit")
        if any(map(self._same_calendar(kind, config, secrets), existing)):
            raise ProviderError("You already added this calendar.")
        info = self.calendar_builder(kind, config, dict(secrets)).check()
        entry = {"id": _secrets.token_hex(4), "provider": kind, "config": config, "secret": self.vault.seal(secrets),
                 "account": str(info.get("account", ""))[:120], "calendars": list(info.get("calendars", []))[:20],
                 "name": config.get("name") or str(info.get("account", "") or kind)[:60],
                 "connected_at": int(time.time())}
        self.accounts.set_calendars(username, existing + [entry])
        return {k: v for k, v in entry.items() if k != "secret"}

    def add_calendar(self, username: str, kind: str, fields: dict) -> dict:
        spec = next((k for k in CALENDAR_KINDS if k["key"] == kind), None)
        if not spec:
            raise ProviderError(f"{kind} calendars connect with a Sign in button" if kind in oauth.CALENDARS
                                else f"unknown calendar kind {kind}")
        config, secrets = _split(spec["fields"], fields)
        return self._add_calendar(username, kind, config, secrets)

    def remove_calendar(self, username: str, cal_id: str) -> None:
        mine = self.accounts.calendars(username)
        keep = [c for c in mine if c.get("id") != cal_id]
        self.accounts.set_calendars(username, keep)
        for gone in (c for c in mine if c.get("id") == cal_id and c.get("provider") == "ics"):
            try:  # don't keep a removed calendar's feed in memory
                forget_feed(self.vault.open(gone["secret"]).get("url", "") if gone.get("secret") else "")
            except Exception:  # noqa: BLE001 - a damaged secret just means there's nothing to forget
                pass

    def rename_calendar(self, username: str, cal_id: str, name: str) -> dict:
        name = (name or "").strip()
        if not 1 <= len(name) <= 60:
            raise ProviderError("A calendar name is 1 to 60 characters.")
        cals = self.accounts.calendars(username)
        mine = next((c for c in cals if c.get("id") == cal_id), None)
        if not mine:
            raise KeyError(cal_id)
        mine["name"] = name
        self.accounts.set_calendars(username, cals)
        return {k: v for k, v in mine.items() if k != "secret"}

    def events_for(self, username: str, day: dt.date,
                   failing: dict[str, str] | None = None) -> tuple[list[dict], list[str]]:
        """Today's events from every calendar. A calendar that fails is reported, not fatal.
        `failing`, when given, also gets each failing calendar's problem by its id."""
        events, problems = [], []
        for entry in self.accounts.calendars(username):
            def resave(tokens: dict, cid=entry["id"]) -> None:
                cals = self.accounts.calendars(username)
                for c in cals:
                    if c.get("id") == cid:
                        c["secret"] = self.vault.seal(tokens)
                self.accounts.set_calendars(username, cals)
            try:
                source = self.calendar_builder(entry["provider"], entry.get("config", {}),
                                               self._open(entry.get("secret"), username, resave))
                for e in source.events(day):
                    events.append({**e, "calendar": e.get("calendar") or entry.get("name", "")})
            except ProviderError as exc:
                problems.append(f"{entry.get('name', 'A calendar')}: {exc}")
                if failing is not None:
                    failing[entry["id"]] = str(exc)
            except Exception as exc:  # a broken feed must not stop the daily ticket
                problems.append(f"{entry.get('name', 'A calendar')} didn't work: {str(exc)[:120]}")
                if failing is not None:
                    failing[entry["id"]] = "It didn't answer. Check the link or sign-in."
        return sort_events(events), problems
