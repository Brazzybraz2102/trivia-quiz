"""Shared plumbing for apps reached over the web: retries, plain-English errors, and a guard so
an address someone types can't point Next Box at this computer or the home network."""
from __future__ import annotations

import ipaddress
import os
import socket
import time
from urllib.parse import urlparse

import httpx

from .base import ProviderError

RETRY_STATUS = {429, 500, 502, 503, 504}


def allow_private() -> bool:
    """Off by default: people (testers too) type server addresses into Next Box, so it must not
    fetch from the home network. Set NEXTBOX_ALLOW_PRIVATE_URLS=1 for a CalDAV server on your LAN."""
    return os.environ.get("NEXTBOX_ALLOW_PRIVATE_URLS", "").strip().lower() in ("1", "true", "yes")


def check_public_url(url: str, what: str = "That address") -> str:
    """https only (http allowed for private servers when permitted), and never the local network."""
    return resolve_public(url, what)[0]


def resolve_public(url: str, what: str = "That address") -> tuple[str, str | None]:
    """check_public_url, plus the checked IP address to connect to, so the name can't point
    somewhere else a moment later (DNS rebinding). The IP is None when private servers are allowed."""
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    p = urlparse(url)
    private_ok = allow_private()
    try:
        port = p.port
    except ValueError:
        port = -1
    if p.scheme not in ("https", "http") or not p.hostname or (p.scheme == "http" and not private_ok):
        raise ProviderError(f"{what} must start with https://")
    if port == -1:
        raise ProviderError(f"{what} has a port number that isn't right (the part after the colon). "
                            "Copy the address again.")
    if private_ok:
        return url, None
    try:
        infos = socket.getaddrinfo(p.hostname, port or (443 if p.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError) as exc:
        raise ProviderError(f"{what}: can't find {p.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ProviderError(f"{what} points inside a private network, which Next Box doesn't allow. "
                                "If it's on your home network, ask your Next Box owner.")
    # IPv4 first when there is one: not every home connection reaches IPv6 addresses.
    return url, next((i[4][0] for i in infos if i[0] == socket.AF_INET), infos[0][4][0])


def guard_session(session, what: str = "The server address") -> None:
    """Check every request a requests/niquests session sends (CalDAV): redirects, links the server
    hands back and discovery all go through here, not just the address the person typed."""
    for adapter in set(session.adapters.values()):
        def send(request, *args, _send=adapter.send, **kwargs):
            check_public_url(str(request.url), what)
            return _send(request, *args, **kwargs)
        adapter.send = send


class Api:
    """A small JSON client: retries busy/failed calls, turns errors into sentences."""

    def __init__(self, name: str, base_url: str, headers: dict | None = None,
                 client: httpx.Client | None = None, retries: int = 2, backoff: float = 1.0):
        self.name = name
        self.headers = dict(headers or {})  # sent on every call, even through a client passed in
        self.http = client or httpx.Client(base_url=base_url, timeout=20.0)
        self.retries = retries
        self.backoff = backoff

    def call(self, method: str, path: str, **kwargs):
        for attempt in range(self.retries + 1):
            try:
                resp = self.http.request(method, path, **{**kwargs, "headers": {**self.headers, **kwargs.get("headers", {})}})
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise ProviderError(f"{self.name} didn't answer ({type(exc).__name__}). Try again in a minute.") from exc
                time.sleep(self.backoff * 2 ** attempt)
                continue
            if resp.status_code in RETRY_STATUS and attempt < self.retries:
                wait = resp.headers.get("retry-after", "")
                time.sleep(min(float(wait), 30) if wait.isdigit() else self.backoff * 2 ** attempt)
                continue
            if resp.status_code in (401, 403):
                raise ProviderError(f"{self.name} said no (signed out or no permission). Reconnect it in Settings.")
            if resp.status_code == 404:
                raise ProviderError(f"{self.name} couldn't find that (it may have been deleted).")
            if resp.status_code >= 400:
                raise ProviderError(f"{self.name} error {resp.status_code}: {resp.text[:200]}")
            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
        raise ProviderError(f"{self.name} is busy; try again in a minute")  # pragma: no cover
