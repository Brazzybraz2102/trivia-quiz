# np_lyrics.py - find and parse lyrics for the Now Playing plugin
#
# Lookup order: a .lrc file next to the song, the local cache, then the
# free LRCLIB service (https://lrclib.net), which often has time-synced
# lyrics. No GTK imports so it can be tested on its own.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

LRCLIB = "https://lrclib.net/api/"
USER_AGENT = "rhythmbox-now-playing/1.0 (https://github.com/brazzybraz2102/trivia-quiz)"
TIMEOUT = 10

_TIME_TAG = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")
_META_TAG = re.compile(r"^\[[a-zA-Z]+:.*\]$")
_OFFSET_TAG = re.compile(r"^\[offset:\s*([+-]?\d+)\]$", re.I)


class Lyrics:
    """Plain lines, plus a start time in seconds for each line when synced."""

    def __init__(self, lines, times=None, source=""):
        self.lines = lines
        self.times = times
        self.source = source

    @property
    def synced(self):
        return self.times is not None

    def line_at(self, seconds):
        """Index of the line being sung at `seconds`, or -1 before the first."""
        if not self.synced:
            return -1
        lo, hi = 0, len(self.times)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.times[mid] <= seconds:
                lo = mid + 1
            else:
                hi = mid
        return lo - 1


def parse_lrc(text):
    """Parse LRC text. Lines with several time tags are repeated at each time."""
    offset = 0.0
    timed = []
    for raw in text.splitlines():
        line = raw.strip()
        m = _OFFSET_TAG.match(line)
        if m:
            offset = int(m.group(1)) / 1000.0
            continue
        tags = []
        while True:
            m = _TIME_TAG.match(line)
            if not m:
                break
            minutes, secs = m.group(1), m.group(2).replace(":", ".")
            tags.append(int(minutes) * 60 + float(secs))
            line = line[m.end():]
        for t in tags:
            timed.append((max(0.0, t - offset), line.strip()))
    if not timed:
        return None
    timed.sort(key=lambda pair: pair[0])
    return Lyrics([l for _, l in timed], [t for t, _ in timed])


def parse_any(text, source=""):
    """Synced if the text has LRC time tags, otherwise plain lines."""
    if not text or not text.strip():
        return None
    lyrics = parse_lrc(text)
    if lyrics is None:
        lines = [l.rstrip() for l in text.strip().splitlines()
                 if not _META_TAG.match(l.strip())]
        lyrics = Lyrics(lines)
    lyrics.source = source
    return lyrics


def clean_title(title):
    """Strip '(Remastered 2011)', '- Live', 'feat. X' and so on for searching."""
    t = re.sub(r"\s*[\(\[][^\)\]]*(remaster|live|version|edit|mix|feat|mono|stereo|deluxe)[^\)\]]*[\)\]]",
               "", title, flags=re.I)
    t = re.sub(r"\s+-\s+.*(remaster|live|version|edit|mix|mono|stereo).*$", "", t, flags=re.I)
    t = re.sub(r"\s+(feat\.?|ft\.)\s+.*$", "", t, flags=re.I)
    return t.strip() or title


def sidecar_path(location):
    """Path of a .lrc file next to a local song, from its file:// URI."""
    if not location or not location.startswith("file://"):
        return None
    path = urllib.parse.unquote(urllib.parse.urlparse(location).path)
    return os.path.splitext(path)[0] + ".lrc"


def cache_path(cache_dir, artist, title):
    safe = lambda s: re.sub(r"[^\w\- ]+", "_", s.casefold()).strip()[:100] or "_"
    return os.path.join(cache_dir, safe(artist), safe(title) + ".lrc")


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _from_record(record):
    if not record:
        return None
    if record.get("instrumental"):
        return "[00:00.00]♪ Instrumental ♪"
    return record.get("syncedLyrics") or record.get("plainLyrics")


def fetch_lrclib(artist, title, album="", duration=0):
    """Return lyrics text from LRCLIB, or None. Raises on network errors."""
    title = clean_title(title)
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(duration)
    text = _from_record(_get_json(LRCLIB + "get?" + urllib.parse.urlencode(params)))
    if text:
        return text

    results = _get_json(LRCLIB + "search?" + urllib.parse.urlencode(
        {"artist_name": artist, "track_name": title})) or []
    # Prefer synced results whose length is close to ours.
    def rank(r):
        close = abs((r.get("duration") or 0) - duration) <= 3 if duration else True
        return (not close, not r.get("syncedLyrics"))
    for record in sorted(results, key=rank):
        text = _from_record(record)
        if text:
            return text
    return None


def find_lyrics(artist, title, album, duration, location, cache_dir):
    """Blocking lookup; run it off the main thread. Returns Lyrics or None."""
    sidecar = sidecar_path(location)
    if sidecar and os.path.isfile(sidecar):
        with open(sidecar, encoding="utf-8", errors="replace") as f:
            found = parse_any(f.read(), "file")
        if found:
            return found

    cached = cache_path(cache_dir, artist, title)
    if os.path.isfile(cached):
        with open(cached, encoding="utf-8") as f:
            text = f.read()
        # An empty cache file records "looked, found nothing".
        return parse_any(text, "cache") if text.strip() else None

    text = fetch_lrclib(artist, title, album, duration)
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    with open(cached, "w", encoding="utf-8") as f:
        f.write(text or "")
    return parse_any(text, "LRCLIB")
