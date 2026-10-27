"""The top-bar status square (tray/nextbox_tray.py) and `nextbox doctor --json`, which it reads.

The tray runs on the system python3 with PyGObject; here it's imported by path under .venv, without
PyGObject, and only its plain logic runs. Nothing here starts a GUI, touches ~/.cache or ~/.config, or
reaches the network.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from nextbox import cli, db, doctor, printer, scan
from nextbox.auth import Accounts
from nextbox.config import PROJECT_DIR, Settings, app_version
from nextbox.connections import Connections
from nextbox.dbtools import describe_location, is_postgres
from nextbox.printer import legacy_config
from nextbox.printers import EXAMPLE_PRINTER_IP, Printers
from nextbox.providers import oauth

TRAY_FILE = PROJECT_DIR / "tray" / "nextbox_tray.py"
INSTALL_TRAY = PROJECT_DIR / "scripts" / "install-tray.sh"


def load_tray(name="nextbox_tray_under_test"):
    spec = importlib.util.spec_from_file_location(name, TRAY_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


tray = load_tray()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ it stands alone
def test_the_tray_imports_without_pygobject_or_next_box(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)  # any `import gi` now fails
    for name in [m for m in sys.modules if m == "nextbox" or m.startswith("nextbox.")]:
        monkeypatch.setitem(sys.modules, name, None)  # and so would `import nextbox`
    module = load_tray("nextbox_tray_without_gi")
    assert module.evaluate(None)[0] == module.GREY
    sys.modules.pop("nextbox_tray_without_gi", None)


def test_the_tray_never_reads_settings_calls_ai_or_prints_labels():
    source = TRAY_FILE.read_text()
    assert not re.search(r"^\s*(import|from)\s+nextbox", source, re.MULTILINE)  # talks to Next Box only from outside
    for banned in (".env", "dotenv", "anthropic", "ANTHROPIC", "import logging", "_send_raster", "lpr", "pkill",
                   "killall"):
        assert banned not in source, banned
    # The only Next Box command it runs is doctor (as JSON, and in a terminal for Show details).
    assert re.findall(r"\[str\(self\.nextbox\), ([^\]]*)\]", source) == ['"doctor", "--json"']
    assert ".venv/bin/nextbox doctor\\n" in source
    assert set(re.findall(r"\bnextbox ([a-z][\w-]*)", source)) == {"doctor"}
    assert source.index("import gi") > source.index("def main(")  # PyGObject only in the GUI part
    assert "GLib.idle_add" in source and "threading.Thread" in source  # the bar never waits on a check


# ------------------------------------------------------------------ the colour rules
DOCTOR_OK = {"version": "0.14.5", "port": 8787, "public_url": "https://nextbox.example.com",
             "checks": [{"mark": "note", "text": "Next Box 0.14.5", "fix": ""},
                        {"mark": "ok", "text": "ANTHROPIC_API_KEY is set", "fix": ""}], "problems": 0}
KEY_MISSING = {"mark": "bad", "text": "ANTHROPIC_API_KEY is missing", "fix": "put it in your settings"}


def checks(**changes):
    values = dict(port=8787, service="active", local_ok=True, public_url="https://nextbox.example.com",
                  public_ok=True, public_seconds=0.4, public_misses=0, cloudflared="active", doctor=DOCTOR_OK,
                  doctor_error="")
    values.update(changes)
    return tray.Checks(**values)


def with_doctor(*bad):
    return {**DOCTOR_OK, "checks": DOCTOR_OK["checks"] + list(bad), "problems": len(bad)}


RULES = [
    # (what's different from "all good", colour, words in the reasons, in order)
    ({}, "green", []),
    ({"service": "inactive"}, "red", ["service is stopped"]),
    ({"service": "failed"}, "red", ["service stopped after an error"]),
    ({"service": "activating"}, "red", ["service is starting"]),
    ({"service": "inactive", "local_ok": False}, "red", ["service is stopped"]),  # one reason, not two
    ({"local_ok": False}, "red", ["doesn't answer on this computer (port 8787)"]),
    ({"service": None}, "green", []),  # no systemd here: the page decides
    ({"service": None, "local_ok": False}, "red", ["doesn't answer on this computer"]),
    ({"service": "reloading"}, "green", []),
    # the public address: one miss is a blip (yellow), two in a row is offline
    ({"public_ok": False, "public_misses": 1}, "yellow", ["didn't answer once"]),
    ({"public_ok": False, "public_misses": 2}, "red", ["doesn't answer from the internet"]),
    ({"public_ok": False, "public_misses": 5}, "red", ["doesn't answer from the internet"]),
    ({"public_ok": False, "public_misses": 0}, "yellow", ["didn't answer once"]),
    # slow: over 5 s
    ({"public_seconds": 5.0}, "green", []),
    ({"public_seconds": 5.1}, "yellow", ["nextbox.example.com is slow to answer (5.1 s)"]),
    # the tunnel's service is down but the address still answers
    ({"cloudflared": "inactive"}, "yellow", ["cloudflared (the tunnel) isn't running"]),
    ({"cloudflared": "failed", "public_seconds": 6.0}, "yellow", ["cloudflared", "slow"]),
    ({"cloudflared": None}, "green", []),  # couldn't tell (no systemctl): not a problem by itself
    ({"cloudflared": "inactive", "public_ok": False, "public_misses": 2}, "red", ["doesn't answer from the internet"]),
    # doctor
    ({"doctor": with_doctor(KEY_MISSING)}, "yellow", ["ANTHROPIC_API_KEY is missing"]),
    ({"doctor": with_doctor({"mark": "note", "text": "HEIC can't be opened", "fix": "x"})}, "green", []),
    ({"doctor": None}, "green", []),
    ({"doctor_error": "The full check (nextbox doctor) took too long"}, "yellow", ["took too long"]),
    ({"doctor": with_doctor(KEY_MISSING), "doctor_error": "The full check (nextbox doctor) didn't finish"},
     "yellow", ["ANTHROPIC_API_KEY", "didn't finish"]),
    # red comes first, then the rest
    ({"service": "failed", "doctor": with_doctor(KEY_MISSING)}, "red", ["service stopped", "ANTHROPIC_API_KEY"]),
    ({"public_ok": False, "public_misses": 2, "doctor": with_doctor(KEY_MISSING)}, "red",
     ["doesn't answer from the internet", "ANTHROPIC_API_KEY"]),
    # this computer down: the public address failing too says nothing new
    ({"local_ok": False, "public_ok": False, "public_misses": 3, "cloudflared": "inactive"}, "red",
     ["doesn't answer on this computer"]),
    # home network only (no public address): only this computer's checks decide red or green
    ({"public_url": None, "public_ok": None, "cloudflared": "inactive", "public_misses": 4}, "green", []),
    ({"public_url": None, "public_ok": None, "local_ok": False}, "red", ["doesn't answer on this computer"]),
    ({"public_url": None, "public_ok": None, "service": "failed"}, "red", ["service stopped"]),
    ({"public_url": None, "public_ok": None, "doctor": with_doctor(KEY_MISSING)}, "yellow", ["ANTHROPIC_API_KEY"]),
]


@pytest.mark.parametrize("changes,color,words", RULES)
def test_colour_rules(changes, color, words):
    got, headline, reasons = tray.evaluate(checks(**changes))
    assert got == color
    assert headline == {"green": "All good", "yellow": "Online, needs attention", "red": "Offline"}[color]
    assert len(reasons) == len(words), reasons
    for (text, fix), word in zip(reasons, words, strict=True):
        assert word in text and fix, (text, fix)


def test_grey_until_the_first_answer_and_fixes_that_say_what_to_do():
    assert tray.evaluate(None) == (tray.GREY, "Checking…", [])
    _, _, [(_, fix)] = tray.evaluate(checks(service="failed"))
    assert "systemctl --user restart nextbox" in fix
    _, _, [(_, fix)] = tray.evaluate(checks(public_ok=False, public_misses=2, cloudflared="inactive"))
    assert fix == tray.TUNNEL_FIX and "sudo systemctl restart cloudflared" in fix
    _, _, [(_, fix)] = tray.evaluate(checks(public_ok=False, public_misses=1))
    assert fix == tray.NET_FIX
    _, _, [(_, fix)] = tray.evaluate(checks(cloudflared="inactive"))
    assert "sudo systemctl enable --now cloudflared" in fix
    _, _, [(text, fix)] = tray.evaluate(checks(doctor=with_doctor(KEY_MISSING)))
    assert (text, fix) == (KEY_MISSING["text"], KEY_MISSING["fix"])


# ------------------------------------------------------------------ the checks, with fake answers
class Fake:
    """Answers for systemctl, doctor and the two web pages, and a list of what was asked."""

    def __init__(self, tmp_path, report=None):
        self.nextbox = tmp_path / "app" / ".venv" / "bin" / "nextbox"
        self.nextbox.parent.mkdir(parents=True)
        self.nextbox.write_text("#!/bin/sh\nexit 99\n")  # never run: run() below answers for it
        self.nextbox.chmod(0o755)
        self.report = DOCTOR_OK if report is None else report
        self.service, self.cloudflared, self.invocation = "active", "active", "inv-1"
        self.local, self.public, self.delay = True, [], 0.2
        self.doctor_answer = None  # (code, stdout) to override the report
        self.calls, self.urls, self.now = [], [], 1000.0

    def run(self, cmd, timeout):
        self.calls.append(cmd)
        if cmd[:3] == [str(self.nextbox), "doctor", "--json"]:
            return self.doctor_answer or (1, json.dumps(self.report) + "\n")
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            return 0, self.service + "\n"
        if cmd[:3] == ["systemctl", "--user", "show"]:
            return 0, self.invocation + "\n"
        if cmd[:2] == ["systemctl", "is-active"]:
            return 0, self.cloudflared + "\n"
        raise AssertionError(cmd)

    def get(self, url, timeout, local=False):
        self.urls.append((url, timeout, local))
        if url.endswith("/health"):
            return (200, '{"ok": true}') if self.local else (None, "")
        assert url.endswith("/version") and not local
        self.now += self.delay
        ok = self.public.pop(0) if self.public else True
        return (200, '{"version": "0.14.5", "build": "abc"}') if ok else (530, "")

    def clock(self):
        return self.now

    def monitor(self):
        return tray.Monitor(self.nextbox, run=self.run, get=self.get, clock=self.clock)

    def doctor_runs(self):
        return sum(c[1:2] == ["doctor"] for c in self.calls)


def test_two_public_misses_in_a_row_turn_it_red_and_one_answer_resets_them(tmp_path):
    fake = Fake(tmp_path)
    m = fake.monitor()
    fake.public = [True, False, False, False, True, False, True]
    colors = [tray.evaluate(m.cycle())[0] for _ in range(7)]
    assert colors == ["green", "yellow", "red", "red", "green", "yellow", "green"]


def test_public_misses_only_count_while_this_computer_is_up(tmp_path):
    fake = Fake(tmp_path)
    m = fake.monitor()
    fake.local, fake.public = False, [False, False, False]
    assert [tray.evaluate(m.cycle())[0] for _ in range(2)] == ["red", "red"]
    fake.local = True  # back: the first public miss now is just one
    assert tray.evaluate(m.cycle())[:2] == ("yellow", "Online, needs attention")


def test_slow_public_answers_and_the_tunnel(tmp_path):
    fake = Fake(tmp_path)
    m = fake.monitor()
    fake.delay = 6.0
    color, _, [(text, _)] = tray.evaluate(m.cycle())
    assert color == "yellow" and "slow to answer (6.0 s)" in text
    fake.delay, fake.cloudflared = 0.1, "inactive"
    color, _, [(text, _)] = tray.evaluate(m.cycle())
    assert color == "yellow" and "cloudflared" in text
    fake.delay = 20.0  # far past the timeout: counted as no answer
    assert tray.evaluate(m.cycle())[0] == "yellow" and m.public_misses == 1


def test_what_the_checks_ask_and_where(tmp_path):
    fake = Fake(tmp_path, {**DOCTOR_OK, "port": 9123, "public_url": "https://nb.example.org/"})
    m = fake.monitor()
    m.cycle()
    assert fake.calls[0] == [str(fake.nextbox), "doctor", "--json"]  # doctor first: it says the port and address
    assert ("http://127.0.0.1:9123/health", tray.LOCAL_TIMEOUT, True) in fake.urls  # never through a proxy
    assert ("https://nb.example.org/version", tray.PUBLIC_TIMEOUT, False) in fake.urls
    assert ["systemctl", "--user", "is-active", "nextbox.service"] in fake.calls
    assert ["systemctl", "is-active", "cloudflared"] in fake.calls
    assert tray.open_url(m.last) == "https://nb.example.org/app/"


def test_home_network_only_never_fetches_a_public_address_or_asks_about_the_tunnel(tmp_path):
    fake = Fake(tmp_path, {**DOCTOR_OK, "public_url": None})
    fake.cloudflared = "inactive"
    m = fake.monitor()
    assert tray.evaluate(m.cycle())[0] == "green"
    assert [u for u, _, _ in fake.urls] == ["http://127.0.0.1:8787/health"]
    assert ["systemctl", "is-active", "cloudflared"] not in fake.calls
    assert tray.open_url(m.last) == "http://localhost:8787/app/"
    fake.local = False
    assert tray.evaluate(m.cycle())[0] == "red"


def test_doctor_runs_at_start_on_request_and_when_the_service_changes(tmp_path):
    fake = Fake(tmp_path)
    m = fake.monitor()
    m.cycle()
    m.cycle()
    m.cycle()
    assert fake.doctor_runs() == 1  # once at the start; the quick checks don't run it
    m.cycle(deep=True)  # every 10 minutes, and Check now
    assert fake.doctor_runs() == 2
    fake.service = "failed"  # went down: ask doctor why
    assert tray.evaluate(m.cycle())[0] == "red" and fake.doctor_runs() == 3
    fake.service = "active"  # came back: doctor's answers from the outage are old now
    fake.invocation = "inv-2"
    assert tray.evaluate(m.cycle())[0] == "green" and fake.doctor_runs() == 4
    m.cycle()
    assert fake.doctor_runs() == 4
    fake.invocation = "inv-3"  # restarted between two looks (someone fixed something): ask again
    m.cycle()
    assert fake.doctor_runs() == 5


def test_doctor_that_hangs_fails_or_is_missing_is_yellow_and_keeps_what_it_knew(tmp_path):
    fake = Fake(tmp_path, with_doctor(KEY_MISSING))
    m = fake.monitor()
    assert tray.evaluate(m.cycle())[0] == "yellow"
    fake.doctor_answer = (None, "")  # took too long
    color, _, reasons = tray.evaluate(m.cycle(deep=True))
    assert color == "yellow" and [t for t, _ in reasons] == [KEY_MISSING["text"],
                                                             "The full check (nextbox doctor) took too long"]
    assert m.port == 8787 and m.public_url == "https://nextbox.example.com"  # still known
    fake.doctor_answer = (1, "Traceback (most recent call last):\n")
    assert "didn't finish" in tray.evaluate(m.cycle(deep=True))[2][-1][0]
    fake.nextbox.unlink()
    color, _, reasons = tray.evaluate(m.cycle(deep=True))
    assert color == "yellow" and "isn't installed at" in reasons[-1][0]


def test_parse_doctor_only_trusts_a_web_address(tmp_path):
    good = tray.parse_doctor(json.dumps({**DOCTOR_OK, "public_url": "https://nb.example.org/"}))
    assert good["public_url"] == "https://nb.example.org" and good["port"] == 8787 and good["version"] == "0.14.5"
    for url in ("file:///etc/passwd", "javascript:alert(1)", "nb.example.org", "", 7):
        assert tray.parse_doctor(json.dumps({**DOCTOR_OK, "public_url": url}))["public_url"] is None
    assert tray.parse_doctor(json.dumps({**DOCTOR_OK, "port": "8787; rm"}))["port"] == 8787
    for junk in ("", "not json", "[]", '{"checks": 3}', "Traceback"):
        assert tray.parse_doctor(junk) is None


def test_check_once_says_the_colour_and_why(tmp_path):
    fake = Fake(tmp_path, with_doctor(KEY_MISSING))
    out = tray.check_once(fake.monitor())
    assert out.splitlines()[0] == "yellow: Online, needs attention"
    assert f"- {KEY_MISSING['text']}" in out and f"→ {KEY_MISSING['fix']}" in out


# ------------------------------------------------------------------ menu, alerts, icons and files
def test_notifications_only_when_it_goes_red_or_comes_back():
    note = tray.notification
    reasons = [("The Next Box service is stopped", "x")]
    assert note("green", "red", reasons) == ("Next Box is offline", "The Next Box service is stopped")
    assert note("yellow", "red", reasons)[0] == "Next Box is offline"
    assert note("red", "green", []) == ("Next Box is back online", "All good.")
    assert note("red", "yellow", reasons)[0] == "Next Box is back online"
    # A fresh start (logging in) that finds it down says so; one that finds it up says nothing.
    assert note("grey", "red", reasons) == ("Next Box is offline", "The Next Box service is stopped")
    for old, new in (("grey", "green"), ("grey", "yellow"), ("green", "yellow"), ("yellow", "green"),
                     ("red", "red"), ("green", "green"), ("green", "grey"), ("red", "grey")):
        assert note(old, new, reasons) is None, (old, new)


def test_an_update_restart_keeps_its_colour_so_pop_ups_never_repeat():
    assert tray.was_color([]) == "grey"  # a fresh start
    assert tray.restart_argv([], "red") == ["--was=red"]
    again = tray.restart_argv(["--was=red"], "green")  # restarted twice: only the newest colour
    assert again == ["--was=green"] and tray.was_color(again) == "green"
    for junk in (["--was=purple"], ["--was="], ["--was"], ["--check"]):
        assert tray.was_color(junk) == "grey", junk
    reasons = [("The Next Box service is stopped", "x")]
    for before, after, says in (("red", "red", None), ("green", "red", "Next Box is offline"),
                                ("red", "green", "Next Box is back online"), ("yellow", "green", None),
                                ("green", "green", None)):
        note = tray.notification(tray.was_color(tray.restart_argv([], before)), after, reasons)
        assert (note and note[0]) == says, (before, after)
    source = TRAY_FILE.read_text()
    assert "restart_argv(sys.argv[1:], self.known)" in source and "was=was_color(argv)" in source
    assert "notification(self.known, color, reasons)" in source


# What the top bar does with a menu line's label after D-Bus. libdbusmenu-gtk sends a plain label with each
# "_" doubled and a mnemonic one as it is (checked over D-Bus with com.canonical.dbusmenu GetLayout).
def _over_dbus(label, mnemonic):
    return label if mnemonic else label.replace("_", "__")


def _gnome_bar(sent):  # the AppIndicator extension's dbusMenu.js: label.replace(/_([^_])/, '$1')
    return re.sub(r"_([^_])", r"\1", sent, count=1)


def _spec_bar(sent):  # the dbusmenu spec (KDE): "__" is one "_", a single "_" marks the mnemonic
    return re.sub(r"_(.)", lambda m: m.group(1), sent)


MENU_TEXTS = ["ANTHROPIC_API_KEY is missing: Snap (marks and handwriting) and the AI helpers won't work",
              "      → put your printer's IP in .env, delete NEXTBOX_DRY_RUN=1, then: systemctl --user restart",
              "Next Box 0.14.5: Online, needs attention", "Open Next Box", "Quit status icon", "_odd", "a__b"]


@pytest.mark.parametrize("desktop", ["ubuntu:GNOME", "GNOME", "GNOME-Classic:GNOME", "pop:GNOME"])
def test_menu_lines_show_their_underscores_on_gnome(desktop):
    assert tray.gnome_shell(desktop)
    for text in MENU_TEXTS:
        label, mnemonic = tray.menu_label(text, desktop)
        assert (label, mnemonic) == ("_" + text, True)
        assert _gnome_bar(_over_dbus(label, mnemonic)) == text, text
    # what it showed before: a plain label, whose doubled underscores GNOME only half undoes
    assert _gnome_bar(_over_dbus(MENU_TEXTS[0], False)).startswith("ANTHROPIC_API__KEY")


@pytest.mark.parametrize("desktop", ["KDE", "XFCE", "Budgie:GNOME", "GNOME-Flashback:GNOME", "X-Cinnamon", ""])
def test_menu_lines_elsewhere_stay_plain_labels(desktop):
    assert not tray.gnome_shell(desktop)
    for text in MENU_TEXTS[:2]:
        label, mnemonic = tray.menu_label(text, desktop)
        assert (label, mnemonic) == (text, False)
        assert _spec_bar(_over_dbus(label, mnemonic)) == text


def test_menu_items_are_built_with_the_leading_underscore_on_gnome(monkeypatch):
    made = []

    class Item:
        def __init__(self, how, label):
            made.append((how, label))
            self.sensitive, self.handlers = True, []

        def set_sensitive(self, on):
            self.sensitive = on

        def connect(self, signal, handler):
            self.handlers.append(signal)

    class MenuItem:
        new_with_mnemonic = staticmethod(lambda label: Item("mnemonic", label))
        new_with_label = staticmethod(lambda label: Item("label", label))

    menu = []
    fake = type("FakeTray", (), {"Gtk": type("Gtk", (), {"MenuItem": MenuItem}), "menu": type(
        "Menu", (), {"append": staticmethod(menu.append)})()})()
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    tray.Tray._item(fake, "ANTHROPIC_API_KEY is missing", None)
    tray.Tray._item(fake, "Check now", lambda: None)
    assert made == [("mnemonic", "_ANTHROPIC_API_KEY is missing"), ("mnemonic", "_Check now")]
    assert menu[0].sensitive is False and menu[1].sensitive is True and menu[1].handlers == ["activate"]
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    tray.Tray._item(fake, "PRINTER_IP is still the example address", None)
    assert made[-1] == ("label", "PRINTER_IP is still the example address")


def test_menu_lines_are_short_and_say_the_version():
    header, lines = tray.menu_lines(checks(doctor=with_doctor(KEY_MISSING)))
    assert header == "Next Box 0.14.5: Online, needs attention"
    assert lines == [(KEY_MISSING["text"], [KEY_MISSING["fix"]])]
    assert tray.menu_lines(None) == ("Next Box: Checking…", [])
    assert tray.menu_lines(checks())[0] == "Next Box 0.14.5: All good"
    words = "put your printer's IP in the settings (hold its info button to print it) or add it in Admin → Printers"
    many = [{"mark": "bad", "text": f"problem {i}: " + words, "fix": words + ", then restart. " + words}
            for i in range(12)]
    header, lines = tray.menu_lines(checks(doctor=with_doctor(*many)))
    assert len(lines) == tray.MAX_REASONS + 1 and lines[-1] == ("…and 4 more (Show details)", [])
    for text, fix in lines[:-1]:
        assert len(text) <= tray.SHORT and text.endswith("…") and not text.endswith(" …")
        assert len(fix) == tray.FIX_LINES and all(len(line) <= tray.SHORT for line in fix)
        assert fix[0].startswith("put your printer's IP") and fix[-1].endswith("…")
    assert tray.short("word " * 30).endswith("word…")  # cut between words


def test_icons_are_filled_squares_in_each_colour_with_a_description(tmp_path):
    paths = tray.write_icons(tmp_path / "cache")
    assert set(paths) == {"green", "yellow", "red", "grey"}
    fills = set()
    for color, path in paths.items():
        assert Path(path).parent == tmp_path / "cache" and path.endswith(f"nextbox-{color}.svg")
        root = ET.parse(path).getroot()  # well-formed SVG
        rect = root.find("{http://www.w3.org/2000/svg}rect")
        assert rect is not None and rect.get("width") == rect.get("height")  # a square
        fills.add(rect.get("fill"))
        assert root.find("{http://www.w3.org/2000/svg}title").text == tray.DESCRIPTIONS[color]
        assert tray.DESCRIPTIONS[color].startswith("Next Box status: ")
    assert len(fills) == 4
    tray.write_icons(tmp_path / "cache")  # again (every start): replaced, no leftovers
    assert sorted(p.name for p in (tmp_path / "cache").iterdir()) == sorted(Path(p).name for p in paths.values())


def test_show_details_runs_doctor_in_a_terminal_until_enter(tmp_path):
    script = tray.details_script(Path("/home/me/.local/share/nextbox/app"))
    assert "cd /home/me/.local/share/nextbox/app" in script and ".venv/bin/nextbox doctor\n" in script
    assert "read -r _" in script and "--json" not in script
    (tmp_path / "d.sh").write_text(script)
    assert subprocess.run(["sh", "-n", str(tmp_path / "d.sh")]).returncode == 0
    assert "'/home/my files/app'" in tray.details_script(Path("/home/my files/app"))  # quoted
    def have(*names):
        return lambda n: f"/usr/bin/{n}" if n in names else None
    assert tray.terminal_command("/s.sh", have("x-terminal-emulator", "gnome-terminal")) == \
        ["x-terminal-emulator", "-e", "/s.sh"]
    assert tray.terminal_command("/s.sh", have("gnome-terminal")) == ["gnome-terminal", "--", "/s.sh"]
    assert tray.terminal_command("/s.sh", have()) is None


def test_one_copy_at_a_time_through_the_pid_file(tmp_path):
    pidfile = tmp_path / "cache" / "tray.pid"
    first = tray.claim(pidfile)
    assert first is not None and pidfile.read_text() == f"{os.getpid()}\n"
    assert oct(pidfile.stat().st_mode & 0o777) == "0o600"
    assert tray.claim(pidfile) is None  # a second copy steps aside
    os.close(first)  # the first one ended (or restarted itself with os.execv, which closes it)
    second = tray.claim(pidfile)
    assert second is not None
    os.close(second)


def test_it_restarts_itself_only_on_a_real_update_that_compiles(tmp_path):
    path = tmp_path / "nextbox_tray.py"
    path.write_text("x = 1\n")
    mtime = path.stat().st_mtime_ns
    assert not tray.changed_since(path, mtime)
    path.write_text("x = (\n")  # a broken update: keep running the old one
    os.utime(path, ns=(mtime + 10**9, mtime + 10**9))
    assert not tray.changed_since(path, mtime)
    path.write_text("x = 2\n")
    os.utime(path, ns=(mtime + 2 * 10**9, mtime + 2 * 10**9))
    assert tray.changed_since(path, mtime)
    path.unlink()  # mid-update
    assert not tray.changed_since(path, mtime)
    assert "os.execv(sys.executable" in TRAY_FILE.read_text()


# ------------------------------------------------------------------ doctor --json
SECRETS = {"ANTHROPIC_API_KEY": "sk-ant-FAKE-tray-1", "NEXTBOX_KEY": "nbkey-FAKE-tray-2",
           "TODOIST_TOKEN": "todoist-FAKE-tray-3", "GOOGLE_CLIENT_SECRET": "gsecret-FAKE-tray-4",
           "MICROSOFT_CLIENT_SECRET": "msecret-FAKE-tray-5", "TICKTICK_CLIENT_SECRET": "tsecret-FAKE-tray-6"}


def test_doctor_json_is_one_object_with_no_secrets_and_no_outside_calls(tmp_path, monkeypatch, capsys):
    port = _free_port()
    env_file = tmp_path / ".env"
    in_file = {k: v for k, v in SECRETS.items() if k not in ("GOOGLE_CLIENT_SECRET",)}
    env_file.write_text("".join(f"{k}={v}\n" for k, v in in_file.items())
                        + f"NEXTBOX_PUBLIC_URL=https://nextbox.example.com/\nNEXTBOX_PORT={port}\n"
                          "GOOGLE_CLIENT_ID=gid-FAKE-tray-7\nPRINTER_IP=192.168.1.50\n")
    for name in [*in_file, "NEXTBOX_PUBLIC_URL", "NEXTBOX_PORT", "GOOGLE_CLIENT_ID", "PRINTER_IP"]:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)  # the .env file sets it; undone after the test
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", SECRETS["GOOGLE_CLIENT_SECRET"])  # and one from the environment
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(env_file))
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("NEXTBOX_DRY_RUN", raising=False)
    monkeypatch.setattr(doctor, "_service_state", lambda: "no-systemd")

    def boom(*a, **k):
        raise AssertionError("doctor must not reach this")
    monkeypatch.setattr(printer, "is_reachable", boom)  # the example address is never knocked on
    monkeypatch.setattr(Connections, "provider_for", boom)  # no linked app
    monkeypatch.setattr(scan, "claude_vision", boom)  # no AI
    try:
        import anthropic
        monkeypatch.setattr(anthropic, "Anthropic", boom)
    except ImportError:
        pass

    code = cli.main(["doctor", "--json"])
    out, err = capsys.readouterr()
    assert out.endswith("\n") and out.count("\n") == 1  # one JSON object, one line, nothing else
    report = json.loads(out)
    assert set(report) == {"version", "port", "public_url", "checks", "problems"}
    assert report["version"] == app_version() and report["port"] == port
    assert report["public_url"] == "https://nextbox.example.com"
    assert all(set(c) == {"mark", "text", "fix"} and c["mark"] in ("ok", "bad", "note") for c in report["checks"])
    assert report["problems"] == sum(c["mark"] == "bad" for c in report["checks"]) >= 1 and code == 1
    texts = [c["text"] for c in report["checks"]]
    assert texts[0] == f"Next Box {app_version()}" and "ANTHROPIC_API_KEY is set" in texts
    assert "NEXTBOX_KEY is set" in texts and "PRINTER_IP is still the example address" in texts
    for value in [*SECRETS.values(), "gid-FAKE-tray-7"]:
        assert value not in out and value not in err, value
    assert "FAKE" not in out + err

    # `nextbox --json doctor` is the same thing; plain `nextbox doctor` stays text
    assert cli.main(["--json", "doctor"]) == 1 and json.loads(capsys.readouterr().out)["port"] == port
    cli.main(["doctor"])
    plain = capsys.readouterr().out
    assert plain.startswith(f"• Next Box {app_version()}\n") and "{" not in plain and "FAKE" not in plain

    # what the tray makes of it: yellow, with doctor's own words
    parsed = tray.parse_doctor(out)
    assert parsed["port"] == port and parsed["public_url"] == "https://nextbox.example.com"
    color, _, reasons = tray.evaluate(tray.Checks(port=port, service="active", local_ok=True,
                                                  public_url=parsed["public_url"], public_ok=True,
                                                  public_seconds=0.3, cloudflared="active", doctor=parsed))
    assert color == "yellow" and ("PRINTER_IP is still the example address" in [t for t, _ in reasons])


def test_doctor_json_still_answers_when_a_check_breaks(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "_service_state", lambda: (_ for _ in ()).throw(RuntimeError("secret words")))
    monkeypatch.setattr(doctor, "_page_answers", lambda port: False)
    settings = Settings(server_key="k", data_dir=tmp_path, port=_free_port())
    assert doctor.run_doctor(settings, Accounts(tmp_path), as_json=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checks"][-1] == {"mark": "bad", "text": "doctor stopped with an error (RuntimeError)",
                                    "fix": "run .venv/bin/nextbox doctor to see it"}
    assert "secret words" not in json.dumps(report)


def test_doctor_json_answers_when_the_database_is_down(tmp_path, monkeypatch, capsys):
    """PostgreSQL stopped (or not up yet at login): still one object, with the database's ✗ and its fix."""
    port = _free_port()  # nothing listens there
    password = "pw-FAKE-db-9"
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in SECRETS.items())
                        + "NEXTBOX_PUBLIC_URL=https://nextbox.example.com\nNEXTBOX_PORT=8791\n")
    for name in [*SECRETS, "NEXTBOX_PUBLIC_URL", "NEXTBOX_PORT"]:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)  # the .env file sets it; undone after the test
    monkeypatch.setenv("NEXTBOX_ENV_FILE", str(env_file))
    monkeypatch.setenv("NEXTBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DATABASE_URL", f"postgresql://nbuser:{password}@127.0.0.1:{port}/nb")

    def boom(*a, **k):
        raise AssertionError("doctor must not reach this")
    monkeypatch.setattr(doctor, "_service_state", boom)  # it stops at the database, like plain doctor
    monkeypatch.setattr(printer, "is_reachable", boom)

    code = cli.main(["doctor", "--json"])
    out, err = capsys.readouterr()
    assert code == 1 and out.endswith("\n") and out.count("\n") == 1
    report = json.loads(out)
    assert report["version"] == app_version() and report["port"] == 8791
    assert report["public_url"] == "https://nextbox.example.com"  # so the tray still checks the address
    assert report["checks"][0] == {"mark": "note", "text": f"Next Box {app_version()}", "fix": ""}
    assert report["checks"][1]["mark"] == "bad" and report["problems"] == 1 and len(report["checks"]) == 2
    assert report["checks"][1]["text"].startswith("can't open the PostgreSQL database: ")
    assert report["checks"][1]["fix"] == "sudo systemctl start postgresql"
    for value in [password, *SECRETS.values()]:
        assert value not in out and value not in err, value
    assert "Traceback" not in err

    # What the tray makes of it: yellow while the page still answers, with doctor's own words, the version
    # in the header, and the public address checked.
    parsed = tray.parse_doctor(out)
    c = tray.Checks(port=parsed["port"], service="active", local_ok=True, public_url=parsed["public_url"],
                    public_ok=True, public_seconds=0.3, cloudflared="active", doctor=parsed)
    color, _, reasons = tray.evaluate(c)
    assert color == "yellow" and reasons == [(report["checks"][1]["text"], "sudo systemctl start postgresql")]
    assert tray.menu_lines(c)[0] == f"Next Box {app_version()}: Online, needs attention"


# ------------------------------------------------------------------ plain doctor output is exactly as before
class _Ran:
    def __init__(self, out):
        self.stdout, self.returncode = out, 0


def _scenario(name, tmp, mp):
    for app in oauth.APPS:
        mp.delenv(f"{app.upper()}_CLIENT_ID", raising=False)
        mp.delenv(f"{app.upper()}_CLIENT_SECRET", raising=False)
    mp.delenv("NEXTBOX_PUBLIC_URL", raising=False)
    mp.delenv("NEXTBOX_HOSTING", raising=False)
    mp.setattr(scan, "heic_supported", lambda: True)
    mp.setattr(doctor, "_lan_ip", lambda: "192.168.1.20")
    mp.setattr("nextbox.movecheck.local_zone", lambda: "America/New_York")  # a home PC (a UTC clock adds a note)
    mp.setattr(doctor, "_last_error", lambda: "")
    mp.setattr(doctor.shutil, "which", lambda n: None)
    mp.setattr(printer, "is_reachable", lambda p: True)
    accounts = Accounts(tmp)
    if name == "all_good":
        accounts.set_password("boss", "boss password", create=True)
        mp.setattr(doctor, "_service_state", lambda: "active")
        mp.setattr(doctor, "_page_answers", lambda port: True)
        return Settings(server_key="k", anthropic_api_key="sk-x", data_dir=tmp, port=8799,
                        printer_ip="192.168.1.77"), accounts
    if name == "everything_wrong":
        mp.setenv("NEXTBOX_PUBLIC_URL", "https://nextbox.example.com/")
        mp.setenv("GOOGLE_CLIENT_ID", "gid")
        mp.setenv("GOOGLE_CLIENT_SECRET", "gsecret")
        mp.setattr(scan, "heic_supported", lambda: False)
        mp.setattr(doctor, "_service_state", lambda: "failed")
        mp.setattr(doctor, "_last_error", lambda: "Error: it broke")
        mp.setattr(doctor, "_page_answers", lambda port: False)
        return Settings(server_key="", anthropic_api_key="", data_dir=tmp, port=8798,
                        printer_ip=EXAMPLE_PRINTER_IP, dry_run=True), accounts
    if name == "lan_trouble":
        accounts.set_password("boss", "boss password", create=True)
        accounts.set_password("sam", "sam password", create=True)
        mp.setenv("NEXTBOX_PUBLIC_URL", "https://nextbox.example.com")
        mp.setenv("NEXTBOX_HOSTING", "Cloudflare")
        mp.setattr(doctor, "_service_state", lambda: "no-systemd")
        mp.setattr(doctor, "_page_answers", lambda port: True)
        mp.setattr(doctor.shutil, "which", lambda n: "/usr/sbin/ufw" if n == "ufw" else None)
        mp.setattr(doctor.subprocess, "run", lambda *a, **k: _Ran("Status: active\n22/tcp ALLOW Anywhere\n"))
        mp.setattr(printer, "is_reachable", lambda p: False)
        Printers(tmp, Settings(data_dir=tmp)).add({"name": "Office", "driver": "zpl", "address": "192.168.1.77"})
        return Settings(server_key="k", anthropic_api_key="sk-x", data_dir=tmp, port=8797, host="127.0.0.1",
                        printer_ip=EXAMPLE_PRINTER_IP), accounts
    if name == "seeded_example":
        accounts.set_password("boss", "boss password", create=True)
        mp.setattr(doctor, "_service_state", lambda: "inactive")
        mp.setattr(doctor, "_page_answers", lambda port: False)
        old = Settings(printer_ip=EXAMPLE_PRINTER_IP, data_dir=tmp)
        Printers(tmp, old)._insert(legacy_config(old), "home")
        Printers(tmp, old).add({"name": "Office", "driver": "zpl", "address": "192.168.1.77"})
        return Settings(server_key="k", anthropic_api_key="sk-x", data_dir=tmp, port=8796, printer_ip=""), accounts
    if name == "no_printer":
        accounts.set_password("boss", "boss password", create=True)
        mp.setattr(doctor, "_service_state", lambda: "active")
        mp.setattr(doctor, "_page_answers", lambda port: True)
        return Settings(server_key="k", anthropic_api_key="sk-x", data_dir=tmp, port=8795, printer_ip=""), accounts
    assert name == "db_down"

    class Down:
        def connect(self):
            raise RuntimeError("connection refused\nsecond line")
    mp.setattr(db, "database", lambda *a, **k: Down())
    return Settings(server_key="k", data_dir=tmp, port=8794), accounts


SIGN_INS_OFF = """\
• Sign in with Google is off (needs GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, NEXTBOX_PUBLIC_URL in .env)
• Sign in with Microsoft is off (needs MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET, NEXTBOX_PUBLIC_URL in .env)
• Sign in with TickTick is off (needs TICKTICK_CLIENT_ID, TICKTICK_CLIENT_SECRET, NEXTBOX_PUBLIC_URL in .env)
"""
# What `nextbox doctor` printed before --json existed (0.14.5), word for word.
GOLDEN = {
    "all_good": (0, """\
• Next Box {version}
{database}
✓ NEXTBOX_KEY is set
✓ ANTHROPIC_API_KEY is set
✓ 1 sign-in account(s)
✓ the background service is running
✓ the web app answers on this computer: http://localhost:8799/app
• on your phone (same Wi-Fi): http://192.168.1.20:8799/app
""" + SIGN_INS_OFF + """\
✓ printer Label printer (192.168.1.77) answers

All good.
"""),
    "everything_wrong": (1, """\
• Next Box {version}
{database}
✗ NEXTBOX_KEY is missing
    → run scripts/install.sh again; it makes one for you
✗ ANTHROPIC_API_KEY is missing: Snap (marks and handwriting) and the AI helpers won't work
    → put it in .env (console.anthropic.com → API keys), then: systemctl --user restart nextbox
• iPhone HEIC photos can't be opened (people are asked to send a JPEG)
    → to open them, in the Next Box folder: .venv/bin/pip install -c constraints.txt -e '.[heic]', then: systemctl --user restart nextbox
✗ no sign-in accounts yet
    → .venv/bin/nextbox user add <your name>
✗ the background service is failed: Error: it broke
    → fix the line above, then: systemctl --user restart nextbox
✗ nothing answers at http://localhost:8798
    → see the service line above
✓ Sign in with Google is on (sign-in comes back to https://nextbox.example.com/connect/google/callback)
• Sign in with Microsoft is off (needs MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET in .env)
• Sign in with TickTick is off (needs TICKTICK_CLIENT_ID, TICKTICK_CLIENT_SECRET in .env)
• NEXTBOX_PUBLIC_URL is set but NEXTBOX_HOSTING isn't, so the Privacy and About pages may not name the service that carries traffic
    → using a Cloudflare Tunnel? Add NEXTBOX_HOSTING=Cloudflare to .env, then: systemctl --user restart nextbox
• preview only (NEXTBOX_DRY_RUN=1): nothing is sent to a printer
✗ PRINTER_IP is still the example address
    → put your printer's IP in .env (hold its info button to print it) or add it in Admin → Printers, delete NEXTBOX_DRY_RUN=1, then: systemctl --user restart nextbox. No label printer? Delete the PRINTER_IP line instead

6 thing(s) to fix above.
"""),
    "lan_trouble": (1, """\
• Next Box {version}
{database}
✓ NEXTBOX_KEY is set
✓ ANTHROPIC_API_KEY is set
✓ 2 sign-in account(s)
• no background service here (that's fine if you start it with .venv/bin/nextbox serve)
✓ the web app answers on this computer: http://localhost:8797/app
• on your phone (same Wi-Fi): http://192.168.1.20:8797/app
✗ NEXTBOX_HOST is 127.0.0.1, so phones can't reach it
    → set NEXTBOX_HOST=0.0.0.0 in .env
✗ the firewall is on and doesn't allow port 8787, so phones can't connect
    → sudo ufw allow from 192.168.0.0/16 to any port 8797 proto tcp
• Sign in with Google is off (needs GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET in .env)
• Sign in with Microsoft is off (needs MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET in .env)
• Sign in with TickTick is off (needs TICKTICK_CLIENT_ID, TICKTICK_CLIENT_SECRET in .env)
• PRINTER_IP in .env is still the example address, so it isn't used
    → delete that line, or put your printer's IP there
✗ printer Office (192.168.1.77) does not answer
    → turn it on, check it's on the same Wi-Fi, and check the address (hold the printer's info button to print its settings)

3 thing(s) to fix above.
"""),
    "seeded_example": (1, """\
• Next Box {version}
{database}
✓ NEXTBOX_KEY is set
✓ ANTHROPIC_API_KEY is set
✓ 1 sign-in account(s)
✗ the background service is inactive
    → fix the line above, then: systemctl --user restart nextbox
✗ nothing answers at http://localhost:8796
    → see the service line above
""" + SIGN_INS_OFF + """\
✗ printer Label printer still has the example address (192.168.1.50)
    → change it to your printer's address, or remove it, in Admin → Printers
✓ printer Office (192.168.1.77) answers

3 thing(s) to fix above.
"""),
    "no_printer": (0, """\
• Next Box {version}
{database}
✓ NEXTBOX_KEY is set
✓ ANTHROPIC_API_KEY is set
✓ 1 sign-in account(s)
✓ the background service is running
✓ the web app answers on this computer: http://localhost:8795/app
• on your phone (same Wi-Fi): http://192.168.1.20:8795/app
""" + SIGN_INS_OFF + """\
• no printer added yet: add one in the web app under Admin → Printers

All good.
"""),
    "db_down": (1, """\
• Next Box {version}
✗ can't open the {kind} database: connection refused
    → {db_fix}
"""),
}


@pytest.mark.parametrize("name", list(GOLDEN))
def test_plain_doctor_output_is_unchanged_and_json_has_the_same_checks(name, tmp_path, monkeypatch, capsys):
    kind = "PostgreSQL" if is_postgres(db.url_for(tmp_path)) else "SQLite"
    where = describe_location(tmp_path)
    settings, accounts = _scenario(name, tmp_path, monkeypatch)
    code = doctor.run_doctor(settings, accounts)
    out = capsys.readouterr().out
    want_code, want = GOLDEN[name]
    want = (want.replace("{version}", app_version()).replace("{database}", f"✓ database: {kind} at {where}")
            .replace("{kind}", kind)
            .replace("{db_fix}", "sudo systemctl start postgresql" if kind == "PostgreSQL"
                     else "check the data folder's permissions"))
    assert (code, out) == (want_code, want)

    assert doctor.run_doctor(settings, accounts, as_json=True) == want_code
    report = json.loads(capsys.readouterr().out)
    marks = {"ok": "✓", "bad": "✗", "note": "•"}
    lines = "".join(f"{marks[c['mark']]} {c['text']}\n" + (f"    → {c['fix']}\n" if c["fix"] else "")
                    for c in report["checks"])
    summary = ("", "\nAll good.\n", f"\n{report['problems']} thing(s) to fix above.\n")
    assert out.startswith(lines) and out[len(lines):] in summary


# ------------------------------------------------------------------ install-tray.sh
def _bash(*args, env=None, home=None):
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), **(env or {})}
    return subprocess.run(["bash", str(INSTALL_TRAY), *args], env=base, capture_output=True, text=True, timeout=60)


def test_install_tray_script_is_strict_and_safe():
    script = INSTALL_TRAY.read_text()
    assert subprocess.run(["bash", "-n", str(INSTALL_TRAY)]).returncode == 0
    assert "\nset -euo pipefail\n" in script and os.access(INSTALL_TRAY, os.X_OK)
    assert "pkill" not in script and "killall" not in script
    assert "--remove)" in script
    assert ".env" not in script.replace("Never reads or prints .env.", "")
    # Autostart: the system python3 and the path through the app link, so it moves with the app.
    assert 'LINK="$HOME/.local/share/nextbox/app"' in script and 'TRAY="$LINK/tray/nextbox_tray.py"' in script
    assert "PY=/usr/bin/python3" in script and 'Exec=$PY "$TRAY"' in script
    assert 'AUTOSTART="$HOME/.config/autostart/nextbox-tray.desktop"' in script
    # Started once, detached; "already running" comes from the pid file, checked against /proc.
    assert 'PIDFILE="$CACHE/tray.pid"' in script and '/proc/$pid/cmdline' in script
    assert 'nohup setsid "$PY" "$TRAY" </dev/null >"$LOG" 2>&1 &' in script
    for pkg in ("gir1.2-ayatanaappindicator3-0.1", "python3-gi", "libnotify-bin"):
        assert pkg in script
    assert "sudo apt-get install" in script and "Fedora" in script  # and words for other systems
    assert "gnome-extensions list --enabled" in script
    assert "gnome-extensions enable ubuntu-appindicators@ubuntu.com" in script


def test_install_tray_outside_a_desktop_says_so_and_changes_nothing(tmp_path):
    r = _bash(home=tmp_path)
    assert r.returncode == 0 and "isn't a desktop session" in r.stdout
    assert list(tmp_path.iterdir()) == []
    r = _bash("--bogus", home=tmp_path)
    assert r.returncode == 2 and "Usage" in r.stdout


def test_install_tray_remove_stops_only_the_status_icon(tmp_path):
    autostart = tmp_path / ".config" / "autostart" / "nextbox-tray.desktop"
    cache = tmp_path / ".cache" / "nextbox-tray"
    autostart.parent.mkdir(parents=True)
    cache.mkdir(parents=True)
    other = subprocess.Popen(["sleep", "30"])  # some other program whose id is in the pid file
    try:
        autostart.write_text("[Desktop Entry]\n")
        (cache / "tray.pid").write_text(f"{other.pid}\n")
        r = _bash("--remove", home=tmp_path)
        assert r.returncode == 0 and "Removed the Next Box status icon" in r.stdout
        assert not autostart.exists() and not cache.exists()
        time.sleep(0.2)
        assert other.poll() is None  # not the status icon: left alone
    finally:
        other.kill()
        other.wait()
    fake_tray = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "nextbox_tray.py"])
    try:
        cache.mkdir(parents=True)
        (cache / "tray.pid").write_text(f"{fake_tray.pid}\n")
        assert _bash("--remove", home=tmp_path).returncode == 0
        assert fake_tray.wait(timeout=10) is not None  # the status icon itself is stopped
    finally:
        if fake_tray.poll() is None:
            fake_tray.kill()
            fake_tray.wait()
    assert _bash("--remove", home=tmp_path).returncode == 0  # again: nothing to do, still fine


def test_install_sh_only_suggests_the_status_icon():
    install = (PROJECT_DIR / "scripts" / "install.sh").read_text()
    lines = [ln.strip() for ln in install.splitlines() if "install-tray" in ln]
    assert lines and all(ln.startswith(("echo", "#")) for ln in lines)  # never runs it
    tail = install[install.index("# The top-bar status square"):]
    assert 'XDG_CURRENT_DESKTOP' in tail and '! -f "$HOME/.config/autostart/nextbox-tray.desktop"' in tail
    assert "install-tray" not in (PROJECT_DIR / "scripts" / "update.sh").read_text()  # it updates itself


# ------------------------------------------------------------------ docs
def test_docs_explain_the_status_icon():
    readme = (PROJECT_DIR / "README.md").read_text()
    section = readme.split("## Status icon in the top bar")[1].split("\n## ")[0]
    for words in ("Green", "Yellow", "Red", "scripts/install-tray.sh", "scripts/install-tray.sh --remove",
                  "run `scripts/install-tray.sh` on the new PC", "Check now", "Show details"):
        assert words in section, words
    spec = (PROJECT_DIR / "SPEC.md").read_text()
    section = spec.split("Status icon in the top bar")[1].split("\n## ")[0]
    for words in ("nextbox doctor --json", "two", "tray/nextbox_tray.py", "/health", "/version", "cloudflared"):
        assert words in section, words
