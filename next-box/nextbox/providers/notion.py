"""A Notion database used as a to-do list. Each person makes an "internal integration" token and
shares the database with it. Next Box finds the columns itself: the title, a Checkbox or Status
column for done, a Date column and a Multi-select column for tags."""
from __future__ import annotations

import datetime as dt
import re

import httpx

from .base import ProviderError, TaskProvider
from .http import Api

BASE = "https://api.notion.com/v1"
DB_ID = re.compile(r"([0-9a-f]{32})")
PAGE_ID = re.compile(r"^[0-9a-f-]{32,36}$")
PREFERRED_DATE = ("due", "due date", "date", "when", "deadline")
PREFERRED_TAGS = ("tags", "labels", "tag", "category", "categories")


def database_id(text: str) -> str:
    m = DB_ID.search(text.replace("-", "").lower().split("?")[0])
    if not m:
        raise ProviderError("Paste the database's link (Share → Copy link) or its 32-character id.")
    return m[1]


class Notion(TaskProvider):
    key = "notion"

    def __init__(self, token: str, database: str, client: httpx.Client | None = None, today=dt.date.today):
        if not token:
            raise ProviderError("Paste your Notion integration token.")
        self.db = database_id(database or "")
        self.api = Api("Notion", BASE, {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
                       client=client)
        self._today = today
        self._schema = None

    # columns
    def schema(self) -> dict:
        if self._schema is not None:
            return self._schema
        info = self.api.call("GET", f"/databases/{self.db}") or {}
        props = info.get("properties", {})
        by_type: dict[str, list[str]] = {}
        for name, p in props.items():
            by_type.setdefault(p.get("type"), []).append(name)

        def pick(kind: str, preferred: tuple = ()) -> str | None:
            names = by_type.get(kind, [])
            return next((n for n in names if n.lower() in preferred), names[0] if names else None)
        done, done_value = pick("checkbox", ("done", "complete", "completed")), None
        if not done and by_type.get("status"):
            done = pick("status", ("status",))
            groups = props[done]["status"].get("groups", [])
            options = {o["id"]: o["name"] for o in props[done]["status"].get("options", [])}
            complete = next((g for g in groups if g.get("name", "").lower() == "complete"), None)
            done_value = options.get((complete or {}).get("option_ids", [None])[0]) if complete else None
            if not done_value:
                raise ProviderError(f"Notion's {done!r} column has no 'Complete' option.")
        if not done:
            raise ProviderError("Add a Checkbox column (e.g. Done) or a Status column to the Notion database.")
        title = (by_type.get("title") or [None])[0]
        name = "".join(t.get("plain_text", "") for t in info.get("title", [])) or "Notion database"
        self._schema = {"title": title, "done": done, "done_kind": props[done]["type"], "done_value": done_value,
                        "date": pick("date", PREFERRED_DATE), "tags": pick("multi_select", PREFERRED_TAGS),
                        "name": name}
        return self._schema

    def _open_filter(self) -> dict:
        s = self.schema()
        if s["done_kind"] == "checkbox":
            return {"property": s["done"], "checkbox": {"equals": False}}
        return {"property": s["done"], "status": {"does_not_equal": s["done_value"]}}

    def _shape(self, page: dict) -> dict:
        s = self.schema()
        props = page.get("properties", {})
        title = "".join(t.get("plain_text", "") for t in (props.get(s["title"], {}).get("title") or []))
        due = (props.get(s["date"], {}).get("date") or {}).get("start") if s["date"] else None
        tags = [o["name"] for o in (props.get(s["tags"], {}).get("multi_select") or [])] if s["tags"] else []
        if due and "T" in due:
            try:
                due = dt.datetime.fromisoformat(due.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                due = due[:10]
        return {"id": page["id"], "content": title or "(untitled)", "description": "", "priority": 1,
                "due": {"date": due, "is_recurring": False} if due else None, "labels": tags}

    def _query(self, extra: dict | None = None) -> list[dict]:
        flt = self._open_filter()
        if extra:
            flt = {"and": [flt, extra]}
        out, cursor = [], None
        for _ in range(20):
            body = {"filter": flt, "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            page = self.api.call("POST", f"/databases/{self.db}/query", json=body) or {}
            out += [self._shape(p) for p in page.get("results", []) if not p.get("archived")]
            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
        return out

    @staticmethod
    def _page(task_id: str) -> str:
        if not PAGE_ID.match(str(task_id)):
            raise ProviderError("That isn't a Notion task.")
        return task_id

    # TaskProvider
    def check(self) -> dict:
        s = self.schema()
        return {"account": s["name"], "lists": [s["name"]]}

    def today(self) -> list[dict]:
        s = self.schema()
        if not s["date"]:
            return []
        return self._query({"property": s["date"], "date": {"on_or_before": self._today().isoformat()}})

    def filter_tasks(self, query: str) -> list[dict]:
        q = query.strip()
        if q.lower() in ("today | overdue", "today", "overdue"):
            return self.today()
        tasks = self._query()
        if q.lower() in ("", "all"):
            return tasks
        if q[:1] in "#@":
            return [t for t in tasks if q[1:].lower() in [x.lower() for x in t["labels"]]]
        return [t for t in tasks if q.lower() in t["content"].lower()]

    def get_task(self, task_id: str) -> dict:
        return self._shape(self.api.call("GET", f"/pages/{self._page(task_id)}"))

    def add_task(self, content: str, description: str = "") -> dict:
        s = self.schema()
        body = {"parent": {"database_id": self.db},
                "properties": {s["title"]: {"title": [{"text": {"content": content[:2000]}}]}}}
        if description:
            body["children"] = [{"object": "block", "type": "paragraph", "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": description[:2000]}}]}}]
        return self._shape(self.api.call("POST", "/pages", json=body))

    def close_task(self, task_id: str) -> None:
        s = self.schema()
        value = {"checkbox": True} if s["done_kind"] == "checkbox" else {"status": {"name": s["done_value"]}}
        self.api.call("PATCH", f"/pages/{self._page(task_id)}", json={"properties": {s["done"]: value}})

    def set_due_date(self, task_id: str, date_iso: str) -> None:
        s = self.schema()
        if not s["date"]:
            raise ProviderError("The Notion database has no Date column to move this to.")
        self.api.call("PATCH", f"/pages/{self._page(task_id)}",
                      json={"properties": {s["date"]: {"date": {"start": date_iso[:10]}}}})

    def delete_task(self, task_id: str) -> None:
        self.api.call("PATCH", f"/pages/{self._page(task_id)}", json={"archived": True})  # Notion's trash
