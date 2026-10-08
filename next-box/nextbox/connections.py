"""Each person's tasks: their built-in list, plus an optional linked app (connect, check, disconnect)."""
from __future__ import annotations

import time
from collections.abc import Callable

from .auth import Accounts
from .config import Settings
from .mylist import LocalTasks, Merged
from .providers import READY, ProviderError, TaskProvider, build_provider, secret_fields
from .vault import Vault

NOT_CONNECTED = "Connect your to-do app in Settings first."

Builder = Callable[[str, dict, dict], TaskProvider]


class NotConnected(ProviderError):
    pass


class Connections:
    def __init__(self, settings: Settings, accounts: Accounts, vault: Vault,
                 builder: Builder = build_provider):
        self.settings = settings
        self.accounts = accounts
        self.vault = vault
        self.builder = builder

    def provider_for(self, username: str) -> TaskProvider:
        """Everyone has the built-in list; a linked app adds its tasks next to it."""
        local = LocalTasks(self.accounts.db, username, self.accounts.household_of(username) or "home")
        try:
            linked = self.linked_for(username)
        except NotConnected:
            linked = None
        prefs = (self.accounts.get(username) or {}).get("prefs") or {}
        return Merged(local, linked, new_tasks_to=prefs.get("new_tasks_to", "nextbox"))

    def linked_for(self, username: str) -> TaskProvider:
        conn = self.accounts.connection(username)
        if conn:
            secrets = self.vault.open(conn["secret"]) if conn.get("secret") else {}
            return self.builder(conn["provider"], conn.get("config", {}), secrets)
        # Before per-person connections, .env held one Todoist token: it still serves the owner.
        if self.settings.todoist_token and username == self.accounts.owner():
            return self.builder("todoist", {}, {"token": self.settings.todoist_token})
        raise NotConnected(NOT_CONNECTED)

    def status(self, username: str) -> dict:
        user = self.accounts.get(username) or {}
        conn = user.get("connection")
        if conn:
            return {"connected": True, **conn, "name": READY.get(conn["provider"], {}).get("name", conn["provider"])}
        if self.settings.todoist_token and username == self.accounts.owner():
            return {"connected": True, "provider": "todoist", "name": "Todoist", "legacy": True,
                    "account": "token in .env", "config": {}}
        return {"connected": False}

    def connect(self, username: str, provider: str, fields: dict) -> dict:
        if provider not in READY:
            raise ProviderError(f"{provider} can't be connected yet")
        spec = READY[provider]["fields"]
        missing = [f["label"] for f in spec if not f.get("optional") and not str(fields.get(f["name"], "")).strip()]
        if missing:
            raise ProviderError(f"Fill in: {', '.join(missing)}")
        names = {f["name"] for f in spec}
        secret_names = secret_fields(provider)
        config = {k: str(v).strip() for k, v in fields.items() if k in names and k not in secret_names}
        secrets = {k: str(v).strip() for k, v in fields.items() if k in secret_names}
        info = self.builder(provider, config, secrets).check()  # refuse to save a connection that doesn't work
        self.accounts.set_connection(username, {
            "provider": provider, "config": config, "secret": self.vault.seal(secrets),
            "account": str(info.get("account", ""))[:120], "lists": list(info.get("lists", []))[:50],
            "connected_at": int(time.time()),
        })
        return self.status(username)

    def disconnect(self, username: str) -> None:
        self.accounts.set_connection(username, None)
