"""Calendars: today's events, shown on My list and printed at the top of the daily ticket.

Read-only. Events are fetched when needed and never stored. Sources:
  ics      a calendar's subscription link (Google, Outlook, iCloud, Proton, Yahoo, Fastmail...)
  caldav   a CalDAV account (iCloud with an app-specific password, Fastmail, Nextcloud...)
  google / microsoft   signed in with the person's account (see providers/oauth.py)
"""
from __future__ import annotations

import datetime as dt
import threading
import time

import httpx

from .providers.base import ProviderError
from .providers.calendar_base import CalendarSource, event_from_component, sort_events
from .providers.http import check_public_url

MAX_FEED_BYTES = 5_000_000
FEED_CACHE_SECONDS = 300


# --- a subscription link (.ics) -----------------------------------------------------------------
_cache: dict[str, tuple[float, bytes]] = {}
_cache_lock = threading.Lock()


class IcsFeed(CalendarSource):
    key = "ics"

    def __init__(self, url: str, name: str = "", client: httpx.Client | None = None):
        if not url:
            raise ProviderError("Paste the calendar's subscription link (it ends in .ics).")
        self.url = check_public_url(url, "The calendar link")
        self.name = name or "Calendar"
        self.http = client or httpx.Client(timeout=15.0, follow_redirects=False)

    def _fetch(self) -> bytes:
        with _cache_lock:
            hit = _cache.get(self.url)
            if hit and time.time() - hit[0] < FEED_CACHE_SECONDS:
                return hit[1]
        url = self.url
        for _ in range(4):  # follow a few redirects, checking each hop
            try:
                with self.http.stream("GET", url, headers={"Accept": "text/calendar"}) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        url = check_public_url(httpx.URL(url).join(r.headers["location"]).__str__(), "The calendar link")
                        continue
                    if r.status_code in (401, 403, 404):
                        raise ProviderError("The calendar link doesn't work any more. Copy a fresh one and add it again.")
                    if r.status_code >= 400:
                        raise ProviderError(f"The calendar server said {r.status_code}.")
                    data = b""
                    for chunk in r.iter_bytes():
                        data += chunk
                        if len(data) > MAX_FEED_BYTES:
                            raise ProviderError("That calendar is too big to read (over 5 MB).")
                    break
            except httpx.TransportError as exc:
                raise ProviderError(f"The calendar didn't answer: {exc}") from exc
        else:
            raise ProviderError("The calendar link redirects too many times.")
        if b"BEGIN:VCALENDAR" not in data[:2000]:
            raise ProviderError("That link isn't a calendar. Look for the one ending in .ics (the 'secret' or 'ICS' address).")
        with _cache_lock:
            _cache[self.url] = (time.time(), data)
        return data

    def _calendar(self):
        import icalendar
        try:
            return icalendar.Calendar.from_ical(self._fetch())
        except ValueError as exc:
            raise ProviderError(f"Couldn't read that calendar: {exc}") from exc

    def check(self) -> dict:
        cal = self._calendar()
        name = str(cal.get("x-wr-calname") or self.name)
        return {"account": name, "calendars": [name]}

    def events(self, day: dt.date) -> list[dict]:
        import recurring_ical_events
        cal = self._calendar()
        name = str(cal.get("x-wr-calname") or self.name)
        out = []
        for c in recurring_ical_events.of(cal).at(day):
            e = event_from_component(c, day, name)
            if e:
                out.append(e)
        return sort_events(out)


# --- a CalDAV account ---------------------------------------------------------------------------
class CalDAVCalendars(CalendarSource):
    key = "caldav"

    def __init__(self, url: str, username: str, password: str, only: str = "", client=None):
        if not url or not username or not password:
            raise ProviderError("CalDAV needs a server address, username and app password.")
        import caldav
        if client is None:
            url = check_public_url(url, "The server address")
        self.client = client or caldav.DAVClient(url=url, username=username, password=password, timeout=20)
        self.username = username
        self.only = only.strip()

    def _calendars(self):
        try:
            cals = self.client.principal().calendars()
        except Exception as exc:
            raise ProviderError(f"Couldn't reach the CalDAV server: {exc}") from exc
        out = []
        for c in cals:
            try:
                comps = c.get_supported_components()
            except Exception:
                comps = ["VEVENT"]
            name = c.get_display_name() or "Calendar"
            if "VEVENT" in comps and (not self.only or name.lower() == self.only.lower()):
                out.append((name, c))
        return out

    def check(self) -> dict:
        cals = self._calendars()
        if not cals:
            raise ProviderError("Signed in, but found no calendars" + (f" named {self.only}" if self.only else ""))
        return {"account": self.username, "calendars": sorted(n for n, _ in cals)}

    def events(self, day: dt.date) -> list[dict]:
        start = dt.datetime.combine(day, dt.time()).astimezone()
        end = start + dt.timedelta(days=1)
        out = []
        for name, cal in self._calendars():
            try:
                found = cal.search(start=start, end=end, event=True, expand=True)
            except Exception as exc:
                raise ProviderError(f"Couldn't read {name}: {exc}") from exc
            for item in found:
                for comp in item.icalendar_instance.walk("VEVENT"):
                    e = event_from_component(comp, day, name)
                    if e:
                        out.append(e)
        return sort_events(out)


# --- the list of kinds people can add ---------------------------------------------------------
KINDS = [
    {"key": "ics", "name": "Calendar link (.ics)", "status": "ready",
     "also": "Google Calendar, Outlook, iCloud, Proton, Yahoo, Fastmail: any calendar with a subscription link",
     "fields": [{"name": "url", "label": "Subscription link", "secret": True,
                 "help": "Google: Settings → your calendar → Integrate calendar → Secret address in iCal format. "
                         "Outlook: Settings → Calendar → Shared calendars → Publish a calendar → ICS link."},
                {"name": "name", "label": "Name", "optional": True, "help": "e.g. Work"}]},
    {"key": "caldav", "name": "CalDAV account", "status": "ready",
     "also": "iCloud (app-specific password), Fastmail, Nextcloud, Synology",
     "fields": [{"name": "url", "label": "Server address",
                 "help": "iCloud: https://caldav.icloud.com/ · Fastmail: https://caldav.fastmail.com/"},
                {"name": "username", "label": "Username", "help": "iCloud: your Apple ID email"},
                {"name": "password", "label": "App password", "secret": True,
                 "help": "iCloud: appleid.apple.com → Sign-In and Security → App-Specific Passwords"},
                {"name": "only", "label": "Only this calendar", "optional": True}]},
]


def build_calendar(key: str, config: dict, secrets: dict) -> CalendarSource:
    if key == "ics":
        return IcsFeed(secrets.get("url", ""), config.get("name", ""))
    if key == "caldav":
        return CalDAVCalendars(config.get("url", ""), config.get("username", ""), secrets.get("password", ""),
                               config.get("only", ""))
    from .providers import oauth
    if key in oauth.CALENDARS:
        return oauth.CALENDARS[key](secrets)
    raise ProviderError(f"{key} calendars aren't available yet")
