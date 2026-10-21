#!/usr/bin/env python3
"""Next Box status square for the desktop's top bar.

  green   All good
  yellow  Online, needs attention (it still works; something is worth fixing)
  red     Offline
  grey    Still checking

Run by the system python3, because it needs PyGObject: `scripts/install-tray.sh` installs what it needs
and adds it to the desktop's autostart. It never imports Next Box. It only asks:
  `nextbox doctor --json` (the deep check: at start, every 10 minutes and on "Check now"),
  http://127.0.0.1:<port>/health and <public address>/version (every minute), and
  `systemctl --user is-active nextbox` and `systemctl is-active cloudflared`.
It prints no labels, calls no AI, never opens Next Box's settings file and writes no log.

Everything above `main()` is plain Python with no PyGObject, so the tests import it on its own.
"""
from __future__ import annotations

import http.client
import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

HERE = Path(os.path.abspath(__file__))  # abspath, not resolve: through the ~/.local/share/nextbox/app link
APP_DIR = HERE.parent.parent  # so it follows the app when that link is pointed at a new folder
NEXTBOX = APP_DIR / ".venv" / "bin" / "nextbox"
CACHE = Path.home() / ".cache" / "nextbox-tray"  # the icons, the pid file and the "Show details" script

FAST_EVERY = 60  # seconds: service, local page, public page, tunnel
DEEP_EVERY = 600  # seconds: nextbox doctor
LOCAL_TIMEOUT = 3
PUBLIC_TIMEOUT = 8
SLOW = 5  # a public answer slower than this (seconds) is yellow
DOCTOR_TIMEOUT = 90
DEFAULT_PORT = 8787
MAX_REASONS = 8  # in the menu; "Show details" has the rest
SHORT = 80  # characters per menu line
FIX_LINES = 2  # a fix wraps onto at most this many lines (GNOME shows no tooltips on menu items)

GREEN, YELLOW, RED, GREY = "green", "yellow", "red", "grey"
HEADLINES = {GREEN: "All good", YELLOW: "Online, needs attention", RED: "Offline", GREY: "Checking…"}
DESCRIPTIONS = {  # what a screen reader says for the square
    GREEN: "Next Box status: all good",
    YELLOW: "Next Box status: online, needs attention",
    RED: "Next Box status: offline",
    GREY: "Next Box status: checking",
}
FILL = {GREEN: "#33d17a", YELLOW: "#f6d32d", RED: "#e01b24", GREY: "#9a9996"}
RUNNING = {"active", "reloading", "refreshing"}  # `systemctl is-active` answers that mean it's up
STATES = RUNNING | {"inactive", "failed", "activating", "deactivating", "maintenance"}

RESTART = "systemctl --user restart nextbox (Show details says why it stopped)"
SERVICE_WORDS = {"inactive": "is stopped", "failed": "stopped after an error", "activating": "is starting",
                 "deactivating": "is stopping", "maintenance": "is in maintenance"}
TUNNEL_FIX = "the tunnel isn't running: sudo systemctl restart cloudflared"
NET_FIX = "check this computer's internet connection"


# ------------------------------------------------------------------ what was found, and what colour that is
@dataclass(frozen=True)
class Checks:
    """One round of checks. None means "couldn't tell" (no systemd here, say), which never turns it red."""
    port: int = DEFAULT_PORT
    service: str | None = None  # `systemctl --user is-active nextbox`
    local_ok: bool = False  # http://127.0.0.1:<port>/health answered {"ok": true}
    public_url: str | None = None  # NEXTBOX_PUBLIC_URL, as doctor reports it; None: home network only
    public_ok: bool | None = None  # <public_url>/version answered in time; None: not checked
    public_seconds: float | None = None
    public_misses: int = 0  # public checks failed in a row, this one included
    cloudflared: str | None = None  # `systemctl is-active cloudflared` (checked only with a public_url)
    doctor: dict | None = None  # the last `nextbox doctor --json` that worked
    doctor_error: str = ""  # why the last doctor run didn't, in plain words


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or url


def local_online(c: Checks) -> bool:
    return (c.service is None or c.service in RUNNING) and c.local_ok


def evaluate(c: Checks | None) -> tuple[str, str, list[tuple[str, str]]]:
    """(colour, headline, [(reason, fix), ...]), worst first.

    RED: the service isn't running, the page doesn't answer on this computer, or the public address
    failed twice in a row (once is yellow, so one network blip doesn't flash red).
    YELLOW: online, but doctor found something, the public address failed once or was slow, or the
    tunnel's service isn't running while the address still answers.
    No public address (home network only): only the checks on this computer decide red or green.
    """
    if c is None:
        return GREY, HEADLINES[GREY], []
    red: list[tuple[str, str]] = []
    yellow: list[tuple[str, str]] = []
    if c.service is not None and c.service not in RUNNING:
        red.append((f"The Next Box service {SERVICE_WORDS.get(c.service, 'is ' + c.service)}", RESTART))
    elif not c.local_ok:
        red.append((f"Next Box doesn't answer on this computer (port {c.port})",
                    "systemctl --user restart nextbox, then Show details"))
    if c.public_url and not red:  # with this computer offline, the public address fails too: that says nothing new
        host = host_of(c.public_url)
        tunnel_down = c.cloudflared is not None and c.cloudflared not in RUNNING
        if c.public_ok:
            if tunnel_down:
                yellow.append(("cloudflared (the tunnel) isn't running as a service",
                               f"{host} still answers for now; to keep it up: sudo systemctl enable --now cloudflared"))
            if c.public_seconds is not None and c.public_seconds > SLOW:
                yellow.append((f"{host} is slow to answer ({c.public_seconds:.1f} s)", NET_FIX))
        elif c.public_ok is False:
            fix = TUNNEL_FIX if tunnel_down else NET_FIX
            if c.public_misses >= 2:
                red.append((f"{host} doesn't answer from the internet", fix))
            else:
                yellow.append((f"{host} didn't answer once (checking again in a minute)", fix))
    for check in (c.doctor or {}).get("checks", []):
        if check.get("mark") == "bad":
            yellow.append((check.get("text", ""), check.get("fix", "")))
    if c.doctor_error:
        yellow.append((c.doctor_error, "Show details runs it, so you can see why"))
    if red:
        return RED, HEADLINES[RED], red + yellow
    if yellow:
        return YELLOW, HEADLINES[YELLOW], yellow
    return GREEN, HEADLINES[GREEN], []


def notification(old: str, new: str, reasons: list[tuple[str, str]]) -> tuple[str, str] | None:
    """(title, body) when the colour changes to red, or from red back to green or yellow. Nothing for the
    first answer after starting (grey), so logging in or an update never pops one up."""
    if old == new or GREY in (old, new):
        return None
    if new == RED:
        return "Next Box is offline", reasons[0][0] if reasons else ""
    if old == RED:
        return "Next Box is back online", "All good." if new == GREEN else "Online, but something needs attention."
    return None


def short(text: str, limit: int = SHORT) -> str:
    """One menu line: cut at a word, with "…", when it's too long."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:") + "…"


def wrapped(text: str, limit: int = SHORT, most: int = FIX_LINES) -> list[str]:
    lines = textwrap.wrap(" ".join(str(text).split()), limit)
    return lines if len(lines) <= most else lines[:most - 1] + [short(" ".join(lines[most - 1:]), limit)]


def menu_lines(c: Checks | None) -> tuple[str, list[tuple[str, list[str]]]]:
    """The header line, and each reason as one short line with its fix wrapped under it."""
    _, headline, reasons = evaluate(c)
    version = ((c.doctor if c else None) or {}).get("version")
    header = f"Next Box {version}: {headline}" if version else f"Next Box: {headline}"
    lines = [(short(text), wrapped(fix)) for text, fix in reasons[:MAX_REASONS]]
    if len(reasons) > MAX_REASONS:
        lines.append((f"…and {len(reasons) - MAX_REASONS} more (Show details)", []))
    return header, lines


def open_url(c: Checks | None) -> str:
    """For Open Next Box: the public address when there is one, otherwise this computer's."""
    if c and c.public_url:
        return c.public_url + "/app/"
    return f"http://localhost:{c.port if c else DEFAULT_PORT}/app/"


# ------------------------------------------------------------------ reading the answers
def parse_doctor(out: str) -> dict | None:
    """`nextbox doctor --json`'s object, checked and tidied; None when it isn't one."""
    try:
        data = json.loads(out.strip())
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("checks"), list):
        return None
    checks = [{"mark": str(c.get("mark", "")), "text": str(c.get("text", "")), "fix": str(c.get("fix", "") or "")}
              for c in data["checks"] if isinstance(c, dict)]
    port = data.get("port")
    url = data.get("public_url")
    if not (isinstance(url, str) and urllib.parse.urlsplit(url).scheme in ("http", "https")
            and urllib.parse.urlsplit(url).hostname):
        url = None  # only a web address is ever fetched
    return {"version": str(data.get("version") or ""),
            "port": port if isinstance(port, int) and 0 < port < 65536 else DEFAULT_PORT,
            "public_url": url.rstrip("/") if url else None,
            "checks": checks,
            "problems": sum(c["mark"] == "bad" for c in checks)}


def unit_state(answer: tuple[int | None, str]) -> str | None:
    """`systemctl is-active`'s word, or None when systemctl couldn't say (not installed, no session bus)."""
    word = answer[1].strip()
    return word if word in STATES else None


def json_of(body: str) -> dict:
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def run(cmd: list[str], timeout: float) -> tuple[int | None, str]:
    """(exit code, stdout); (None, "") when it couldn't run or took too long. stderr is dropped, unread."""
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None, ""
    return r.returncode, r.stdout


def get(url: str, timeout: float, local: bool = False) -> tuple[int | None, str]:
    """(HTTP status, start of the body); (None, "") when nothing answered in time."""
    # This computer's own page never goes through a proxy. Cloudflare turns away urllib's default name.
    opener = urllib.request.build_opener(*([urllib.request.ProxyHandler({})] if local else []))
    request = urllib.request.Request(url, headers={"User-Agent": "NextBox-status-icon/1"})
    try:
        with opener.open(request, timeout=timeout) as r:
            return r.status, r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (OSError, ValueError, http.client.HTTPException):
        return None, ""


class Monitor:
    """Runs the checks and keeps what the rules need from one round to the next."""

    def __init__(self, nextbox: Path = NEXTBOX, run=run, get=get, clock=time.monotonic):
        self.nextbox = Path(nextbox)
        self.run, self.get, self.clock = run, get, clock
        self.doctor: dict | None = None
        self.doctor_error = ""
        self.public_misses = 0
        self.invocation = ""
        self.last: Checks | None = None

    @property
    def port(self) -> int:
        return (self.doctor or {}).get("port") or DEFAULT_PORT

    @property
    def public_url(self) -> str | None:
        return (self.doctor or {}).get("public_url")

    def deep(self) -> None:
        """`nextbox doctor --json`. Only the worker thread calls it, so there's never more than one."""
        if not os.access(self.nextbox, os.X_OK):
            self.doctor_error = f"Next Box isn't installed at {self.nextbox.parent.parent.parent}"
            return
        code, out = self.run([str(self.nextbox), "doctor", "--json"], DOCTOR_TIMEOUT)
        report = parse_doctor(out)
        if report is None:
            self.doctor_error = ("The full check (nextbox doctor) took too long" if code is None
                                 else "The full check (nextbox doctor) didn't finish")
        else:
            self.doctor, self.doctor_error = report, ""

    def fast(self) -> Checks:
        service = unit_state(self.run(["systemctl", "--user", "is-active", "nextbox.service"], 10))
        code, body = self.get(f"http://127.0.0.1:{self.port}/health", LOCAL_TIMEOUT, local=True)
        local_ok = code == 200 and json_of(body).get("ok") is True
        url, public_ok, seconds, cloudflared = self.public_url, None, None, None
        if url:
            start = self.clock()
            code, body = self.get(url + "/version", PUBLIC_TIMEOUT)
            seconds = self.clock() - start
            public_ok = code == 200 and "version" in json_of(body) and seconds <= PUBLIC_TIMEOUT + 1
            cloudflared = unit_state(self.run(["systemctl", "is-active", "cloudflared"], 10))
        online = (service is None or service in RUNNING) and local_ok
        # Misses count only while this computer is up: with it down, the public address fails for that reason.
        self.public_misses = self.public_misses + 1 if (url and online and not public_ok) else 0
        return Checks(port=self.port, service=service, local_ok=local_ok, public_url=url, public_ok=public_ok,
                      public_seconds=seconds, public_misses=self.public_misses, cloudflared=cloudflared,
                      doctor=self.doctor, doctor_error=self.doctor_error)

    def restarted(self) -> bool:
        """True when the service started again since the last look (an update, or someone fixed something)."""
        code, out = self.run(["systemctl", "--user", "show", "nextbox.service", "--property=InvocationID",
                              "--value"], 10)
        now = out.strip() if code == 0 else ""
        before, self.invocation = self.invocation, now or self.invocation
        return bool(before and now and now != before)

    def cycle(self, deep: bool = False) -> Checks:
        """One round: doctor first when asked (or never run yet), then the quick checks. If this computer just
        went down, came back or restarted, doctor's answers are from before, so it's asked again."""
        if deep or (self.doctor is None and not self.doctor_error):
            self.deep()
            deep = True
        misses = self.public_misses
        checks = self.fast()
        restarted = self.restarted()
        changed = self.last is not None and local_online(self.last) != local_online(checks)
        if (changed or restarted) and not deep:
            where = (self.port, self.public_url)
            self.deep()
            if (self.port, self.public_url) != where:  # the settings changed: look again with the new ones
                self.public_misses = misses if self.public_url == where[1] else 0
                checks = self.fast()
            checks = replace(checks, doctor=self.doctor, doctor_error=self.doctor_error)
        self.last = checks
        return checks


# ------------------------------------------------------------------ files in ~/.cache/nextbox-tray
def icon_svg(color: str) -> str:
    """A filled square with a thin dark edge, so it shows on a light or a dark bar."""
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
            f'<title>{DESCRIPTIONS[color]}</title>'
            f'<rect x="1.5" y="1.5" width="13" height="13" rx="1.5" fill="{FILL[color]}" '
            'stroke="#000000" stroke-opacity="0.4" stroke-width="1"/></svg>\n')


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)  # never half a file


def write_icons(folder: Path) -> dict[str, str]:
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = {}
    for color in (GREEN, YELLOW, RED, GREY):
        path = folder / f"nextbox-{color}.svg"
        _write(path, icon_svg(color), 0o644)
        paths[color] = str(path)
    return paths


def details_script(app_dir: Path) -> str:
    """For Show details: doctor's own words in a terminal, until Enter is pressed."""
    return ("#!/bin/sh\n"
            f"cd {shlex.quote(str(app_dir))} || exit 1\n"
            ".venv/bin/nextbox doctor\n"
            "echo\n"
            "printf 'Press Enter to close. '\n"
            "read -r _\n")


def terminal_command(script: str, which=shutil.which) -> list[str] | None:
    for name, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"), ("ptyxis", "--"),
                       ("kgx", "--"), ("konsole", "-e"), ("xfce4-terminal", "-x"), ("xterm", "-e")):
        if which(name):
            return [name, flag, script]
    return None


def claim(pidfile: Path) -> int | None:
    """Take the one-copy lock and write this process's id; None when another copy has it. The lock goes
    with the process (and with os.execv, which closes it), so a crash never leaves it stuck."""
    import fcntl
    pidfile.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(pidfile, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def changed_since(path: Path, mtime_ns: int) -> bool:
    """True when the file was updated and still compiles, so starting it again is safe."""
    try:
        if path.stat().st_mtime_ns == mtime_ns:
            return False
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        return False
    return True


def spawn(cmd: list[str]) -> bool:
    """Start a program and forget it (a thread waits for it, so it never lingers as a zombie)."""
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except OSError:
        return False
    threading.Thread(target=p.wait, daemon=True).start()
    return True


def check_once(monitor: Monitor | None = None) -> str:
    """`nextbox_tray.py --check`: one full round as text, for a terminal."""
    color, headline, reasons = evaluate((monitor or Monitor()).cycle(deep=True))
    return "\n".join([f"{color}: {headline}"] + [f"- {t}" + (f"\n    → {f}" if f else "") for t, f in reasons])


# ------------------------------------------------------------------ the top bar (PyGObject from here on)
class Tray:
    def __init__(self, Gtk, GLib, AppIndicator, icons: dict[str, str], monitor: Monitor, lock_fd: int):
        self.Gtk, self.GLib, self.icons, self.monitor, self.lock_fd = Gtk, GLib, icons, monitor, lock_fd
        self.indicator = AppIndicator.Indicator.new("nextbox-status", icons[GREY],
                                                    AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_title("Next Box status")
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)
        self.checks: Checks | None = None
        self.color = GREY
        self.busy = True
        self.wake = threading.Event()
        self.force = threading.Event()
        self.mtime = HERE.stat().st_mtime_ns
        self.show()
        threading.Thread(target=self.work, name="checks", daemon=True).start()

    # ---- worker thread: the only place that runs checks; it hands results to GTK with idle_add
    def work(self) -> None:
        next_deep = 0.0
        while True:
            deep = self.force.is_set() or time.monotonic() >= next_deep
            self.force.clear()
            try:
                checks = self.monitor.cycle(deep=deep)
            except Exception:  # never take the bar down; the next round tries again
                checks = None
            if deep:
                next_deep = time.monotonic() + DEEP_EVERY
            if checks is not None:
                self.GLib.idle_add(self.apply, checks)
            if changed_since(HERE, self.mtime):  # updated: start again on the new code, no logout needed
                self.GLib.idle_add(self.restart)
                return
            self.wake.wait(FAST_EVERY)
            self.wake.clear()

    # ---- GTK main thread
    def apply(self, checks: Checks) -> bool:
        color, _, reasons = evaluate(checks)
        note = notification(self.color, color, reasons)
        if note and shutil.which("notify-send"):
            spawn(["notify-send", "-a", "Next Box", "-i", self.icons[color], *note])
        self.checks, self.color = checks, color
        self.busy = self.force.is_set()  # "Check now" was pressed during this round: one more is coming
        self.show()
        return False

    def show(self) -> None:
        Gtk = self.Gtk
        for child in self.menu.get_children():
            self.menu.remove(child)
            child.destroy()
        header, lines = menu_lines(self.checks)
        self._item(header + (" (checking…)" if self.busy and self.checks else ""), None)
        for text, fix in lines:
            self._item(text, None)
            for n, line in enumerate(fix):
                self._item(("      → " if n == 0 else "         ") + line, None)
        self.menu.append(Gtk.SeparatorMenuItem())
        self._item("Open Next Box", self.open)
        self._item("Check now", self.check_now)
        self._item("Show details", self.details)
        self.menu.append(Gtk.SeparatorMenuItem())
        self._item("Quit status icon", self.quit)
        self.menu.show_all()
        self.indicator.set_icon_full(self.icons[self.color], DESCRIPTIONS[self.color])

    def _item(self, label: str, action) -> None:
        item = self.Gtk.MenuItem.new_with_label(label)  # dbusmenu shows its underscores as they are
        if action is None:
            item.set_sensitive(False)
        else:
            item.connect("activate", lambda _item: action())
        self.menu.append(item)

    def open(self) -> None:
        spawn(["xdg-open", open_url(self.checks)])

    def check_now(self) -> None:
        self.busy = True
        self.show()
        self.force.set()
        self.wake.set()  # a round already running finishes first: still one doctor at a time

    def details(self) -> None:
        script = CACHE / "details.sh"
        _write(script, details_script(APP_DIR), 0o700)
        cmd = terminal_command(str(script))
        if (cmd is None or not spawn(cmd)) and shutil.which("notify-send"):
            spawn(["notify-send", "-a", "Next Box", "No terminal found",
                   f"Open one and run: {APP_DIR}/.venv/bin/nextbox doctor"])

    def restart(self) -> bool:
        os.execv(sys.executable, [sys.executable, str(HERE), *sys.argv[1:]])
        return False

    def quit(self) -> None:
        try:
            (CACHE / "tray.pid").unlink()
        except OSError:
            pass
        self.Gtk.main_quit()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--check"]:
        print(check_once())
        return 0
    lock_fd = claim(CACHE / "tray.pid")
    if lock_fd is None:
        return 0  # one is already in the top bar
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3 as AppIndicator
        except (ValueError, ImportError):
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3 as AppIndicator
        from gi.repository import GLib, Gtk
    except (ImportError, ValueError):
        sys.stderr.write("The Next Box status icon needs PyGObject and AyatanaAppIndicator3. "
                         "Run scripts/install-tray.sh to add them.\n")
        return 1
    import signal
    tray = Tray(Gtk, GLib, AppIndicator, write_icons(CACHE), Monitor(), lock_fd)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: tray.quit() or False)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
