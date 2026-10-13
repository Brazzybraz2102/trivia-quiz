"""Asana: tasks assigned to you. Each person connects with a personal access token."""
from __future__ import annotations

import datetime as dt
import re

import httpx

from .base import ProviderError, TaskProvider
from .http import Api

BASE = "https://app.asana.com/api/1.0"
GID = re.compile(r"^\d{1,32}$")
FIELDS = "name,notes,due_on,due_at,completed,tags.name"


class Asana(TaskProvider):
    key = "asana"

    def __init__(self, token: str, workspace: str = "", client: httpx.Client | None = None, today=dt.date.today):
        if not token:
            raise ProviderError("Paste your Asana personal access token.")
        self.api = Api("Asana", BASE, {"Authorization": f"Bearer {token}", "Accept": "application/json"}, client=client)
        self.workspace_name = workspace.strip()
        self._me = None
        self._today = today

    def _data(self, method: str, path: str, **kwargs):
        body = self.api.call(method, path, **kwargs)
        return (body or {}).get("data") if isinstance(body, dict) else body

    def me(self) -> dict:
        if self._me is None:
            self._me = self._data("GET", "/users/me", params={"opt_fields": "email,name,workspaces.name"}) or {}
        return self._me

    def workspace(self) -> str:
        spaces = self.me().get("workspaces") or []
        if not spaces:
            raise ProviderError("Your Asana account has no workspace.")
        if self.workspace_name:
            match = [w for w in spaces if w.get("name", "").lower() == self.workspace_name.lower()]
            if not match:
                raise ProviderError(f"No Asana workspace called {self.workspace_name!r}")
            return match[0]["gid"]
        return spaces[0]["gid"]

    @staticmethod
    def _gid(task_id: str) -> str:
        if not GID.match(str(task_id)):
            raise ProviderError("That isn't an Asana task.")
        return task_id

    def _shape(self, t: dict) -> dict:
        due = None
        if t.get("due_at"):
            due = dt.datetime.fromisoformat(t["due_at"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
        elif t.get("due_on"):
            due = t["due_on"]
        return {"id": t["gid"], "content": t.get("name") or "(untitled)", "description": t.get("notes", ""),
                "priority": 1, "due": {"date": due, "is_recurring": False} if due else None,
                "labels": [x.get("name", "") for x in t.get("tags") or [] if x.get("name")]}

    def _open(self) -> list[dict]:
        out, offset = [], None
        for _ in range(20):
            params = {"assignee": "me", "workspace": self.workspace(), "completed_since": "now",
                      "opt_fields": FIELDS, "limit": 100}
            if offset:
                params["offset"] = offset
            body = self.api.call("GET", "/tasks", params=params) or {}
            out += [self._shape(t) for t in body.get("data", []) if not t.get("completed")]
            offset = (body.get("next_page") or {}).get("offset")
            if not offset:
                break
        return out

    def check(self) -> dict:
        me = self.me()
        return {"account": me.get("email") or me.get("name") or "Asana",
                "lists": [w.get("name", "") for w in me.get("workspaces") or []]}

    def today(self) -> list[dict]:
        today = self._today().isoformat()
        return [t for t in self._open() if t["due"] and t["due"]["date"][:10] <= today]

    def filter_tasks(self, query: str) -> list[dict]:
        q = query.strip()
        if q.lower() in ("today | overdue", "today", "overdue"):
            return self.today()
        tasks = self._open()
        if q.lower() in ("", "all"):
            return tasks
        if q[:1] in "#@":
            return [t for t in tasks if q[1:].lower() in [x.lower() for x in t["labels"]]]
        return [t for t in tasks if q.lower() in t["content"].lower()]

    def get_task(self, task_id: str) -> dict:
        return self._shape(self._data("GET", f"/tasks/{self._gid(task_id)}", params={"opt_fields": FIELDS}))

    def add_task(self, content: str, description: str = "") -> dict:
        return self._shape(self._data("POST", "/tasks", json={"data": {
            "name": content, "notes": description, "workspace": self.workspace(), "assignee": "me"}}))

    def close_task(self, task_id: str) -> None:
        self._data("PUT", f"/tasks/{self._gid(task_id)}", json={"data": {"completed": True}})

    def set_due_date(self, task_id: str, date_iso: str) -> None:
        self._data("PUT", f"/tasks/{self._gid(task_id)}", json={"data": {"due_on": date_iso[:10]}})

    def delete_task(self, task_id: str) -> None:
        self._data("DELETE", f"/tasks/{self._gid(task_id)}")
