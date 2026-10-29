"""Friends and notes in the web app (SPEC §34): the Friends tab, the full-screen feed, the note sheet, Print.

It runs in the browser and was checked there end to end (two people in two households, phone and computer
sizes, 320 px with Extra large text, dark, reduced motion). These tests pin what must never change: a note
prints only from the Print button (a real print asks "Send this to the printer?" first), "seen" is only a note
that was on screen, and the screens and settings the rest of the app relies on are there."""
import collections
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
JS = (WEB / "app.js").read_text()
HTML = (WEB / "index.html").read_text()
START = "// ---------------------------------------------------------------- Friends and notes (SPEC §34)"
END = "// ---------------------------------------------------------------- FE: the routine card"
FR = JS[JS.index(START):JS.index(END)]


def fn(name: str, src: str = JS) -> str:
    m = re.search(rf"^(?:async )?function {name}\(.*?\n}}\n", src, re.S | re.M)
    assert m, name
    return m.group(0)


def test_the_tab_screens_and_sheets_are_there():
    assert '<button data-tab="friends">Friends<span class="navcount" data-friends hidden></span></button>' in HTML
    assert '<button data-go="friends">' in HTML and HTML.count("data-friends hidden") == 2  # the badge, top and bottom
    for el in ("friends", "frSeg", "frNotesView", "frPeopleView", "frSentView", "frWatch", "frTiles", "frLink", "frShare", "frCopy",
               "frQr", "frRotate", "frAddForm", "frName", "frReqs", "frList", "frBlocked", "frSentList", "feedDialog", "feedList",
               "composeDialog", "composeKinds", "composeText", "composeCanvas", "composeCam", "composeLib", "composeTo",
               "composePrintBox", "composeSend", "frPrintDialog", "frPrintGo", "frSheet", "frAskDialog", "frQrDialog",
               "friendsSetCard"):
        assert f'id="{el}"' in HTML, el
    assert 'data-pref="friend_receipts"' in HTML and "Tell friends when I've seen or printed their notes" in HTML
    assert 'data-jump="friendsSetCard"' in HTML
    # one note per screen, swiped like a video app; motion only when it's wanted
    assert "scroll-snap-type:y mandatory" in HTML and "scroll-snap-stop:always" in HTML
    assert "@media (prefers-reduced-motion: reduce) { .fheart .big, .cheart { animation:none; } .fburst { display:none; } }" in HTML
    assert 'loaders.friends = () => frLoad();' in JS


def test_every_element_the_script_asks_for_exists():
    ids = set(re.findall(r'id="([^"]+)"', HTML)) | set(re.findall(r'id="([^"$]+)"', JS))
    used = set(re.findall(r'\$\("#([A-Za-z][\w-]*)', JS)) | set(re.findall(r'\$\(`#([A-Za-z][\w-]*)[ `\[]', JS))
    assert sorted(u for u in used if u not in ids) == []
    names = [a or b for a, b in re.findall(r"^(?:async )?function (\w+)|^(?:const|let) (\w+)", JS, re.M)]
    assert [n for n, k in collections.Counter(names).items() if k > 1] == []
    own = [a or b for a, b in re.findall(r"^(?:async )?function (\w+)|^(?:const|let) (\w+)", FR, re.M)]
    assert own and all(re.match(r"(fr|FR_)", n) for n in own), [n for n in own if not re.match(r"(fr|FR_)", n)]


def test_a_note_prints_only_from_the_print_button():
    """One call to POST /notes/{id}/print, in frPrintGo, which only the Print button runs. A real print asks
    first, like every print in Next Box; a preview (when only a preview can happen) can't print anything."""
    assert FR.count("/print`") == 1 and JS.count("/print`") == 1
    go = fn("frPrintGo", FR)
    assert 'const dry_run = !!frPrintWhy();' in go
    assert go.index('if (!dry_run && !confirm("Send this to the printer?")) return;') < go.index("/print`")
    assert "json: { confirm: true, dry_run }" in go
    assert re.findall(r"frPrintGo\(", JS) == ["frPrintGo("] * 2  # its definition and the button's onclick
    assert '$("#frPrintGo").onclick = () => frPrintGo();' in FR
    assert 'const frPrintWhy = () => ($("#dry").dataset.forced ? $("#dryWhy").textContent : "");' in FR
    # nothing on a timer sends, prints or changes a note; the one repeating thing only reads the badge's numbers
    assert FR.count("setInterval(") == 1
    assert 'setInterval(() => { if (document.visibilityState === "visible") frCountLoad(); }, 60000);' in FR
    assert "async function frCountLoad" in FR and "/print" not in fn("frCountLoad", FR)


def test_seen_is_a_note_on_screen_for_about_a_second():
    observe = fn("frFeedObserve", FR)
    assert "threshold: [0.6]" in observe and "e.intersectionRatio >= 0.59" in observe
    assert "setTimeout(() => frSeen(id), 1000)" in observe and "clearTimeout(frSeenTimers.get(id))" in observe
    seen = fn("frSeen", FR)
    assert seen.index("0.59 * box.height") < seen.index("/seen`")  # still on screen when the second is up
    assert 'document.visibilityState !== "visible"' in seen and '!$("#feedDialog").open' in seen
    assert FR.count("/seen`") == 1


def test_the_feed_keys_and_esc():
    keys = FR[FR.index('$("#feedDialog").addEventListener("keydown"'):]
    keys = keys[:keys.index("\n});\n")]
    for k in ('"ArrowDown"', '"j"', '"ArrowUp"', '"k"', '"Escape"', "keysTyping(e.target)"):
        assert k in keys, k
    assert "behavior: smooth()" in fn("frFeedGo", FR)  # no glide with reduced motion (smooth())
    assert 'const KEYS_TABS = { l: "mylist", p: "print", s: "scan", j: "journal", f: "friends" };' in JS


def test_what_changed_says_friends_came_with_terms_6():
    note = JS[JS.index("TERMS_VERSION 3, 4, 5 and 6"):JS.index("Have a quick look before you carry on.")]
    assert "New: Friends." in note and "Only you and that friend see a note" in note and "30 days" in note
    assert "it prints only when you tap Print" in note
    guide = (WEB / "guide.html").read_text()
    assert 'href="#friends"' in guide and 'id="friends"' in guide and "only when you tap <b>Print</b> and say yes" in guide


def test_a_friend_link_is_read_once_and_asked_about():
    assert "history.replaceState(null, \"\", location.pathname + location.search);" in fn("frLinkRead", FR)
    assert "frLinkRun();" in fn("boot") and "frCountLoad();" in fn("boot")
    assert '$("#loginMsg").textContent = msg || frLinkHint();' in fn("showLogin")
    run = fn("frLinkRun", FR)
    assert run.index("frLinkForget();") < run.index("/friends/code/")  # asked once, even if it fails
    assert '$$("dialog[open]").length' in run  # never over the data notice or the setup questions
    assert "frForget();" in fn("clearPerson")


def run_js(code: str, prelude: str = ""):
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    clock = """const NOW = new Date("2026-10-29T14:00:00").getTime(), RealDate = Date;
globalThis.Date = class extends RealDate { constructor(...a) { if (a.length) super(...a); else super(NOW); } static now() { return NOW; } };
let me = { prefs: {} };
"""
    fmt = re.search(r"^const fmtClock = .*\n", JS, re.M).group(0)
    script = clock + fmt + prelude + f"\nconsole.log(JSON.stringify((() => {{ {code} }})()));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, env={**os.environ, "TZ": "UTC"})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_names_times_and_how_long_a_note_stays():
    at = int(datetime(2026, 10, 29, 14, 0, tzinfo=timezone.utc).timestamp())  # the clock run_js sets (TZ=UTC)
    prelude = fn("frList", FR) + fn("frWhen", FR) + fn("frKeepWords", FR)
    got = run_js(f"""const t = {at};
      return [frList(["Lee"]), frList(["Lee", "Ana"]), frList(["Lee", "Ana", "Sam"]), frList(["Lee", "Ana", "Sam", "Bo"]),
              frWhen(t - 3600), frWhen(t - 86400), frWhen(t - 40 * 86400),
              frKeepWords({{ kept: true }}), frKeepWords({{ kept: false, expires: t + 29.5 * 86400 }}), frKeepWords({{ kept: false, expires: t + 3600 }})];""",
                 prelude)
    assert got[:4] == ["Lee", "Lee and Ana", "Lee, Ana and Sam", "Lee, Ana and 2 others"]
    assert got[4] == "1:00p" and got[5] == "Yesterday 2:00p" and got[6] == "Sep 19"
    assert got[7:] == ["Kept: it stays until you delete it.", "Goes in 30 days unless you keep it.", "Goes today unless you keep it."]
