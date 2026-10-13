"""Some apps' task ids need their list id too, and may contain characters (/ + =) that can't go in
a web address. Pack both into one URL-safe id: "<prefix>.<base64url(list \\n task)>"."""
from __future__ import annotations

import base64

from .base import ProviderError


def pack(prefix: str, list_id: str, task_id: str) -> str:
    raw = f"{list_id}\n{task_id}".encode()
    return f"{prefix}.{base64.urlsafe_b64encode(raw).decode().rstrip('=')}"


def unpack(prefix: str, packed: str) -> tuple[str, str]:
    head, _, body = str(packed).partition(".")
    if head != prefix or not body or len(body) > 800:
        raise ProviderError("That task isn't from this app.")
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
        list_id, task_id = raw.split("\n", 1)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderError("That task id is damaged.") from exc
    return list_id, task_id
