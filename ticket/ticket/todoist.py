"""Thin Todoist API v1 client. Todoist is the only source of truth: nothing here caches tasks."""
from __future__ import annotations

import uuid

import httpx

BASE_URL = "https://api.todoist.com/api/v1"


class TodoistError(RuntimeError):
    pass


class Todoist:
    def __init__(self, token: str, base_url: str = BASE_URL, client: httpx.Client | None = None):
        if not token:
            raise TodoistError("TODOIST_TOKEN is not set")
        self._http = client or httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if method != "GET":
            # Lets Todoist de-duplicate a request if the network hiccups mid-call.
            headers["X-Request-Id"] = str(uuid.uuid4())
        resp = self._http.request(method, path, headers=headers, **kwargs)
        if resp.status_code >= 400:
            # Response bodies never contain the token, so they're safe to surface.
            raise TodoistError(f"Todoist {method} {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp

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

    def filter_tasks(self, query: str) -> list[dict]:
        return self._paginate("/tasks/filter", {"query": query})

    def get_task(self, task_id: str) -> dict:
        return self._request("GET", f"/tasks/{task_id}").json()

    def projects(self) -> list[dict]:
        return self._paginate("/projects", {})

    def add_task(self, content: str, due_string: str | None = None, description: str = "") -> dict:
        body: dict = {"content": content}
        if due_string:
            body["due_string"] = due_string
        if description:
            body["description"] = description
        return self._request("POST", "/tasks", json=body).json()

    def close_task(self, task_id: str) -> None:
        self._request("POST", f"/tasks/{task_id}/close")

    def set_due_date(self, task_id: str, date_iso: str) -> None:
        # due_date, never due_string: a due_string would replace a recurring rule.
        self._request("POST", f"/tasks/{task_id}", json={"due_date": date_iso})

    def delete_task(self, task_id: str) -> None:
        self._request("DELETE", f"/tasks/{task_id}")
