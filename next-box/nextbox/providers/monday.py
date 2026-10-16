"""monday.com: the items assigned to you, on boards with a person, a date and a status column.

Each person connects with their personal API token (GraphQL at https://api.monday.com/v2). Next Box
finds the columns itself on each board: the person column it's assigned by, a date column for the
due day, and the status column whose "Done" label finishes an item. Done sets that label, moving a
task changes the date (keeping its time), and delete archives the item (it can be brought back from
the board's archive). Items are read each time and never stored (SPEC §1.1).
"""
from __future__ import annotations

import datetime as dt
import json
import re

import httpx

from .base import ProviderError, TaskNotFound, TaskProvider
from .http import Api

BASE = "https://api.monday.com"
API_VERSION = "2026-01"
ITEM_ID = re.compile(r"^\d{1,20}$")
MAX_BOARDS = 10         # boards read when no single board is chosen (the most recently used first)
PAGE = 100
MAX_PAGES = 5           # per board: 500 of your items
PERSON = ("person", "people", "owner", "assignee", "assigned to", "assigned", "who")
DUE = ("due", "due date", "date", "deadline", "when")
DONE_WORDS = ("done", "complete", "completed", "finished")
PRIORITY = {"critical": 4, "urgent": 4, "high": 3, "medium": 2, "low": 1}
COLUMN_FIELDS = "id title type settings_str"
VALUE_FIELDS = "id type text value"

ME = "query { me { id name email account { name } } }"
BOARDS = ("query ($ids: [ID!], $limit: Int) { boards(ids: $ids, limit: $limit, state: active, order_by: used_at) "
          "{ id name type columns { %s } } }" % COLUMN_FIELDS)
NEXT = ("query ($cursor: String!, $limit: Int!, $cols: [String!]) { next_items_page(cursor: $cursor, limit: $limit) "
        "{ cursor items { id name state column_values(ids: $cols) { %s } } } }" % VALUE_FIELDS)
ITEM = ("query ($ids: [ID!]) { items(ids: $ids) { id name state board { id } column_values { %s } } }"
        % VALUE_FIELDS)
CHANGE = ("mutation ($board: ID!, $item: ID!, $col: String!, $value: JSON!) "
          "{ change_column_value(board_id: $board, item_id: $item, column_id: $col, value: $value) { id } }")
ARCHIVE = "mutation ($item: ID!) { archive_item(item_id: $item) { id } }"
CREATE = ("mutation ($board: ID!, $name: String!, $values: JSON) "
          "{ create_item(board_id: $board, item_name: $name, column_values: $values) "
          "{ id name state column_values { %s } } }" % VALUE_FIELDS)


def board_id(text: str) -> str:
    """A board's link (…monday.com/boards/1234567890…) or its number."""
    text = (text or "").strip()
    m = re.search(r"/boards/(\d{1,20})", text) or re.fullmatch(r"(\d{1,20})", text)
    if not m:
        raise ProviderError("Paste the board's link from your browser (it has /boards/ and a number in it), "
                            "or leave Board blank.")
    return m[1]


def _json(value) -> dict:
    try:
        out = json.loads(value) if isinstance(value, str) and value else {}
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


class Monday(TaskProvider):
    key = "monday"

    def __init__(self, token: str, board: str = "", client: httpx.Client | None = None, today=dt.date.today):
        if not token.strip():
            raise ProviderError("Paste your monday.com personal API token.")
        self.api = Api("monday.com", BASE, {"Authorization": token.strip(), "API-Version": API_VERSION,
                                             "Content-Type": "application/json"}, client=client)
        self.board = board_id(board) if board.strip() else ""
        self._today = today
        self._me: dict | None = None
        self._boards: dict[str, dict] = {}  # board id -> its columns, as found

    # GraphQL
    def gql(self, query: str, variables: dict | None = None) -> dict:
        body = self.api.call("POST", "/v2", json={"query": query, "variables": variables or {}})
        if not isinstance(body, dict):
            raise ProviderError("monday.com sent an answer Next Box couldn't read. Try again in a minute.")
        errors = body.get("errors") or ([{"message": body["error_message"], "extensions": {"code": body.get("error_code")}}]
                                         if body.get("error_message") else [])
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
            code = str((first.get("extensions") or {}).get("code") or "")
            message = str(first.get("message") or "")
            if "complexity" in (code + message).lower() or "rate" in code.lower():
                raise ProviderError("monday.com is busy; try again in a minute.")
            if code.lower() in ("unauthorized", "usernotauthorized", "user_unauthorized") or "not authenticated" in message.lower():
                raise ProviderError("monday.com said no (signed out or no permission). Reconnect it in Settings.")
            raise ProviderError(f"monday.com said: {message[:200] or 'something went wrong'}")
        return body.get("data") or {}

    def me(self) -> dict:
        if self._me is None:
            self._me = self.gql(ME).get("me") or {}
            if not self._me.get("id"):
                raise ProviderError("monday.com didn't say who this token belongs to. Check the token.")
        return self._me

    # boards and their columns
    @staticmethod
    def _columns(board: dict) -> dict | None:
        """The board's person, date and status columns (and priority and tags when there are some),
        or None when it doesn't have the three Next Box needs."""
        cols = board.get("columns") or []

        def pick(types: tuple, preferred: tuple, skip=()) -> dict | None:
            found = [c for c in cols if c.get("type") in types and c.get("title", "").strip().lower() not in skip]
            return next((c for c in found if c.get("title", "").strip().lower() in preferred), found[0] if found else None)
        person = pick(("people", "multiple-person"), PERSON)
        date = pick(("date",), DUE)
        status = pick(("status", "color"), ("status",), skip=("priority",))
        if not (person and date and status):
            return None
        settings = _json(status.get("settings_str"))
        labels = {str(k): str(v) for k, v in (settings.get("labels") or {}).items()}
        done = [str(i) for i in settings.get("done_colors") or [] if str(i) in labels]
        done += [k for k, v in labels.items() if v.strip().lower() in DONE_WORDS and k not in done]
        if not done:
            return None
        priority = next((c for c in cols if c.get("type") in ("status", "color")
                         and c.get("title", "").strip().lower() == "priority"), None)
        tags = next((c for c in cols if c.get("type") == "tags"), None)
        return {"id": str(board["id"]), "name": board.get("name") or "monday.com board", "person": person["id"],
                "date": date["id"], "status": status["id"], "done": done, "done_label": labels[done[0]],
                "priority": priority["id"] if priority else None, "tags": tags["id"] if tags else None}

    def boards(self) -> list[dict]:
        """The boards Next Box uses: the chosen one, or your most recently used boards that have a
        person, a date and a status column."""
        if self._boards:
            return list(self._boards.values())
        variables = {"ids": [self.board], "limit": 1} if self.board else {"limit": 50}
        found = self.gql(BOARDS, variables).get("boards") or []
        for b in found:
            if b.get("type") not in (None, "board"):  # sub-item boards and docs
                continue
            cols = self._columns(b)
            if cols:
                self._boards[cols["id"]] = cols
            if len(self._boards) >= MAX_BOARDS:
                break
        if not self._boards:
            if self.board:
                raise ProviderError("That monday.com board needs a person column, a date column and a status "
                                    "column with a Done label. Add them, or leave Board blank.")
            raise ProviderError("None of your monday.com boards has a person column, a date column and a status "
                                "column with a Done label. Add them to the board you use for your to-dos.")
        return list(self._boards.values())

    def _board(self, bid: str) -> dict:
        if bid not in self._boards:
            found = self.gql(BOARDS, {"ids": [bid], "limit": 1}).get("boards") or []
            cols = self._columns(found[0]) if found else None
            if not cols:
                raise ProviderError("That item's monday.com board needs a person, a date and a status column.")
            self._boards[bid] = cols
        return self._boards[bid]

    # items
    def _shape(self, item: dict, board: dict) -> dict:
        values = {v.get("id"): v for v in item.get("column_values") or []}
        raw = _json((values.get(board["date"]) or {}).get("value"))
        due = None
        if raw.get("date"):
            due = raw["date"][:10]
            if raw.get("time"):  # monday.com keeps the time in UTC
                try:
                    when = dt.datetime.fromisoformat(f"{raw['date'][:10]}T{raw['time'][:8]}+00:00")
                    due = when.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    pass
        prio = (values.get(board["priority"]) or {}).get("text") if board.get("priority") else ""
        tags = (values.get(board["tags"]) or {}).get("text") if board.get("tags") else ""
        return {"id": str(item["id"]), "content": item.get("name") or "(untitled)", "description": "",
                "priority": PRIORITY.get(str(prio or "").strip().lower(), 1),
                "due": {"date": due, "is_recurring": False} if due else None,
                "labels": [t.strip() for t in str(tags or "").split(",") if t.strip()]}

    def _is_done(self, item: dict, board: dict) -> bool:
        status = next((v for v in item.get("column_values") or [] if v.get("id") == board["status"]), {})
        index = _json(status.get("value")).get("index")
        return (index is not None and str(index) in board["done"]) or \
            str(status.get("text") or "").strip().lower() == board["done_label"].strip().lower()

    def _mine(self, item: dict, board: dict) -> bool:
        """Assigned to you: the filter asks monday.com for that, and this checks what came back."""
        people = next((v for v in item.get("column_values") or [] if v.get("id") == board["person"]), {})
        who = _json(people.get("value")).get("personsAndTeams")
        if who is None:
            return True  # the column's value wasn't sent: trust the filter
        me = str(self.me()["id"])
        return any(str(p.get("id")) == me and p.get("kind", "person") == "person" for p in who)

    def _open(self) -> list[dict]:
        me = str(self.me()["id"])
        out: list[dict] = []
        for board in self.boards():
            cols = [c for c in (board["person"], board["date"], board["status"], board.get("priority"),
                                board.get("tags")) if c]
            rules = [{"column_id": board["person"], "compare_value": [f"person-{me}"], "operator": "any_of"}]
            query = ("query ($board: [ID!], $limit: Int!, $rules: [ItemsQueryRule!], $cols: [String!]) "
                     "{ boards(ids: $board) { items_page(limit: $limit, query_params: {rules: $rules}) "
                     "{ cursor items { id name state column_values(ids: $cols) { %s } } } } }" % VALUE_FIELDS)
            found = (self.gql(query, {"board": [board["id"]], "limit": PAGE, "rules": rules, "cols": cols})
                     .get("boards") or [{}])[0].get("items_page") or {}
            for _ in range(MAX_PAGES):
                for item in found.get("items") or []:
                    if item.get("state") in (None, "active") and self._mine(item, board) \
                            and not self._is_done(item, board):
                        out.append(self._shape(item, board))
                cursor = found.get("cursor")
                if not cursor:
                    break
                found = self.gql(NEXT, {"cursor": cursor, "limit": PAGE, "cols": cols}).get("next_items_page") or {}
        return out

    def _item(self, task_id: str) -> tuple[dict, dict]:
        if not ITEM_ID.match(str(task_id)):
            raise ProviderError("That isn't a monday.com item.")
        items = self.gql(ITEM, {"ids": [str(task_id)]}).get("items") or []
        if not items or items[0].get("state") == "deleted":
            raise TaskNotFound("That item isn't on monday.com any more.")
        item = items[0]
        return item, self._board(str((item.get("board") or {}).get("id") or ""))

    def _change(self, board: dict, item_id: str, column: str, value: dict) -> None:
        self.gql(CHANGE, {"board": board["id"], "item": str(item_id), "col": column, "value": json.dumps(value)})

    # TaskProvider
    def check(self) -> dict:
        me = self.me()
        return {"account": me.get("email") or me.get("name") or "monday.com",
                "lists": [b["name"] for b in self.boards()]}

    def today(self) -> list[dict]:
        today = self._today().isoformat()
        return [t for t in self._open() if t["due"] and t["due"]["date"][:10] <= today]

    def filter_tasks(self, query: str) -> list[dict]:
        q = query.strip()
        if q.lower() in ("today | overdue", "overdue | today", "today", "overdue"):
            return self.today()
        tasks = self._open()
        if q.lower() in ("", "all"):
            return tasks
        if q[:1] in "#@" and len(q) > 1:
            return [t for t in tasks if q[1:].lower() in [x.lower() for x in t["labels"]]]
        return [t for t in tasks if q.lower() in t["content"].lower()]

    def get_task(self, task_id: str) -> dict:
        item, board = self._item(task_id)
        return self._shape(item, board)

    def add_task(self, content: str, description: str = "") -> dict:
        board = self.boards()[0]
        values = {board["person"]: {"personsAndTeams": [{"id": int(self.me()["id"]), "kind": "person"}]}}
        made = self.gql(CREATE, {"board": board["id"], "name": content[:255], "values": json.dumps(values)})
        return self._shape(made.get("create_item") or {"id": "", "name": content}, board)

    def close_task(self, task_id: str) -> None:
        item, board = self._item(task_id)
        self._change(board, item["id"], board["status"], {"index": int(board["done"][0])})

    def set_due_date(self, task_id: str, date_iso: str) -> None:
        item, board = self._item(task_id)
        current = _json(next((v.get("value") for v in item.get("column_values") or []
                              if v.get("id") == board["date"]), None))
        value = {"date": date_iso[:10]}
        if current.get("time"):
            value["time"] = current["time"]  # keep its time of day
        self._change(board, item["id"], board["date"], value)

    def delete_task(self, task_id: str) -> None:
        """Archives the item, so it can be brought back from the board's archive on monday.com. Next
        Box only calls this after the person confirmed (DELETE /list/{id}?confirm=true)."""
        item, _ = self._item(task_id)
        self.gql(ARCHIVE, {"item": str(item["id"])})
