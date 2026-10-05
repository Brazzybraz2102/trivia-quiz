"""Todoist API v1. Each person connects with their own API token."""
from __future__ import annotations

import re
import time
import uuid

import httpx

from .base import ProviderError, TaskProvider

BASE_URL = "https://api.todoist.com/api/v1"
TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RETRY_STATUS = {429, 500, 502, 503, 504}

# Kept for older imports.
TodoistError = ProviderError


class Todoist(TaskProvider):
    key = "todoist"

    def __init__(self, token: str, base_url: str = BASE_URL, client: httpx.Client | None = None,
                 retries: int = 3, backoff: float = 1.0):
        if not token:
            raise ProviderError("No Todoist token. Connect Todoist in Settings.")
        self._http = client or httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
        self._retries = retries
        self._backoff = backoff

    @staticmethod
    def _id(task_id: str) -> str:
        # IDs go into the URL path, so anything else could reach a different endpoint.
        task_id = str(task_id)
        if not TASK_ID.match(task_id):
            raise ProviderError(f"not a Todoist task id: {task_id[:40]!r}")
        return task_id

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if method != "GET":
            # Same id on every retry, so Todoist applies a write at most once.
            headers["X-Request-Id"] = str(uuid.uuid4())
        for attempt in range(self._retries + 1):
            try:
                resp = self._http.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self._retries:
                    raise ProviderError(f"Todoist didn't answer: {exc}") from exc
                time.sleep(self._backoff * 2 ** attempt)
                continue
            if resp.status_code in RETRY_STATUS and attempt < self._retries:
                wait = resp.headers.get("retry-after", "")
                time.sleep(min(float(wait), 30) if wait.isdigit() else self._backoff * 2 ** attempt)
                continue
            if resp.status_code in (401, 403):
                raise ProviderError("Todoist rejected the token. Reconnect Todoist in Settings.")
            if resp.status_code >= 400:
                # Response bodies never contain the token, so they're safe to surface.
                raise ProviderError(f"Todoist {method} {path} -> {resp.status_code}: {resp.text[:200]}")
            return resp
        raise AssertionError("unreachable")

    def _paginate(self, path: str, params: dict) -> list[dict]:
        items: list[dict] = []
        cursor = None
        while True:
            p = dict(params, limit=200)
            if cursor:
                p["cursor"] = cursor
            data = self._request("GET", path, params=p).json()
            items.extend(data.get("results", []))
            cursor = data.get("next_cursor")
            if not cursor:
                return items

    def check(self) -> dict:
        user = self._request("GET", "/user").json()
        return {"account": user.get("email") or user.get("full_name") or "Todoist",
                "lists": [p["name"] for p in self.projects()]}

    def today(self) -> list[dict]:
        return self.filter_tasks("today | overdue")

    def filter_tasks(self, query: str) -> list[dict]:
        return self._paginate("/tasks/filter", {"query": query})

    def get_task(self, task_id: str) -> dict:
        return self._request("GET", f"/tasks/{self._id(task_id)}").json()

    def projects(self) -> list[dict]:
        return self._paginate("/projects", {})

    def add_task(self, content: str, description: str = "", due_string: str | None = None) -> dict:
        body: dict = {"content": content}
        if due_string:
            body["due_string"] = due_string
        if description:
            body["description"] = description
        return self._request("POST", "/tasks", json=body).json()

    def close_task(self, task_id: str) -> None:
        self._request("POST", f"/tasks/{self._id(task_id)}/close")

    def set_due_date(self, task_id: str, date_iso: str) -> None:
        # due_date, never due_string: a due_string would replace a recurring rule.
        self._request("POST", f"/tasks/{self._id(task_id)}", json={"due_date": date_iso})

    def delete_task(self, task_id: str) -> None:
        self._request("DELETE", f"/tasks/{self._id(task_id)}")
