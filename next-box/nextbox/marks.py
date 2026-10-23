"""How a person marks their list (SPEC §32): the symbols on their label's legend, what each one means to
them, and what Next Box does when Snap reads it on a row.

A style is one of PRESETS, or "custom": the person's own list (the `marks` pref). Each mark is
{symbol, meaning, action}, plus look="strike" when a line struck through the task counts as that mark
(on its own, with no symbol, or as well as the symbol). Everyone starts on "simple", which is exactly the
✓ done → move ✗ drop Next Box always printed and read.

The label draws the legend from these (render.py), and Snap's prompt says what each one looks like
(scan.build_prompt). A drop always waits for the person to confirm, whatever its symbol; a "note" mark
changes nothing and is only shown in the read-back. Nothing here prints anything or calls the AI.
"""
from __future__ import annotations

import copy
import unicodedata

from .render import can_draw

ACTIONS = ("done", "tomorrow", "drop", "note")
ACTION_NAMES = {  # what the Settings select says: short enough for a phone with Extra large text
    "done": "Finish it",
    "tomorrow": "Move to tomorrow",
    "drop": "Delete it",  # with "Next Box always asks you before it deletes." under it (web/app.js MARK_DROP)
    "note": "Just note it",
}
LOOKS = {"strike": "a line struck through the task"}  # marks that aren't a typed symbol
MAX_MARKS = 8
MAX_SYMBOL = 3
MAX_MEANING = 60
CUSTOM = "custom"

# Hand-drawn, these look the same: two of them in one list can't be told apart on paper (and Snap's prompt
# describes them in the same words). web/app.js MARK_ALIKE is the same list.
LOOK_ALIKE = [set("✗✘×xX"), set("✓✔√"), {"→", "->", "➔", "➜"}, {"-", "–", "—"}, {"•", "●"}, {"○", "O", "o", "0"}]

# Symbols Settings offers to pick from (all drawn by the label's font; letters are typed).
PICKER = ["✓", "✗", "→", ">", "<", "×", "•", "○", "●", "◐", "★", "!", "?", "–", "/"]


def _mark(symbol: str, meaning: str, action: str, look: str = "") -> dict:
    m = {"symbol": symbol, "meaning": meaning, "action": action}
    if look:
        m["look"] = look
    return m


# id -> name, a short "how to use it" in plain words, and its marks (in legend order).
PRESETS: dict[str, dict] = {
    "simple": {
        "name": "Simple",
        "blurb": "Three marks, nothing to learn. Tick the box when it's done. "
                 "Draw an arrow to move it to tomorrow, or cross it out to drop it.",
        "marks": [_mark("✓", "done", "done"), _mark("→", "move", "tomorrow"), _mark("✗", "drop", "drop", "strike")],
    },
    "bullet_journal": {
        "name": "Bullet journal",
        "blurb": "Ryder Carroll's Bullet Journal, in Next Box's boxes. Put a / in the box when you start, "
                 "and make it an × when it's done. A > moves it to tomorrow, and a line through the task drops it.",
        "marks": [_mark("×", "done", "done"), _mark(">", "migrate", "tomorrow"), _mark("/", "started", "note"),
                  _mark("", "drop", "drop", "strike")],
    },
    "planner": {
        "name": "Paper planner",
        "blurb": "Like a Franklin-Covey day planner. Tick what's done, → forwards it to tomorrow, and X deletes it. "
                 "A dot means you're working on it, and D means you handed it to someone else.",
        "marks": [_mark("✓", "done", "done"), _mark("→", "forwarded", "tomorrow"), _mark("X", "deleted", "drop"),
                  _mark("•", "in progress", "note"), _mark("D", "delegated", "note")],
    },
    "progress": {
        "name": "Fill the box",
        "blurb": "Fill in the box when it's done, or half fill it when you start. An arrow moves it to tomorrow "
                 "and ✗ drops it. One quick stroke, so it's good for ADHD and quick ticking.",
        "marks": [_mark("●", "done", "done"), _mark("◐", "half done", "note"), _mark("→", "move", "tomorrow"),
                  _mark("✗", "drop", "drop")],
    },
}
DEFAULT_STYLE = "simple"
STYLES = (*PRESETS, CUSTOM)


def preset_marks(style: str) -> list[dict]:
    return copy.deepcopy(PRESETS.get(style, PRESETS[DEFAULT_STYLE])["marks"])


def effective(prefs: dict | None) -> list[dict]:
    """The marks this person uses now: their preset's, or their own list when the style is custom.
    Anything unexpected (an empty or broken list) is the simple preset, as Next Box always was."""
    prefs = prefs or {}
    style = prefs.get("mark_style", DEFAULT_STYLE)
    if style == CUSTOM:
        own = prefs.get("marks")
        if own and problem(own) is None:
            return copy.deepcopy(own)
        return preset_marks(DEFAULT_STYLE)
    return preset_marks(style)


def is_simple(marks: list[dict] | None) -> bool:
    return not marks or marks == PRESETS["simple"]["marks"]


def one_line(text: str, limit: int = MAX_MEANING) -> str:
    """A meaning as it may go into the prompt or onto a label: one line, no control characters, capped."""
    text = "".join(" " if unicodedata.category(ch)[0] in "CZ" else ch for ch in str(text or ""))
    return " ".join(text.split())[:limit]


def _what(i: int) -> str:
    return f"Mark {i}:"


def _sym_problem(i: int, symbol, look: str) -> str | None:
    if not isinstance(symbol, str):
        return f"{_what(i)} pick a symbol."
    if any(unicodedata.category(ch)[0] == "C" for ch in symbol):  # not said back: it may not even be whole text
        return f"{_what(i)} the label can't print that symbol. Pick another symbol."
    if symbol != symbol.strip():
        return f"{_what(i)} type the symbol without spaces around it."
    if unicodedata.normalize("NFC", symbol) != symbol:  # the app always sends one form of each character
        return f"{_what(i)} type that symbol again."
    if not symbol:
        return None if look else f"{_what(i)} pick a symbol."
    if len(symbol) > MAX_SYMBOL:
        return f"{_what(i)} a symbol is 1 to {MAX_SYMBOL} characters."
    if not can_draw(symbol):
        return f"{_what(i)} the label can't print “{symbol}”. Pick another symbol."
    return None


def _look_key(symbol: str) -> str:
    """The same for two symbols that can't be told apart written by hand, or that differ only in case."""
    for group in LOOK_ALIKE:
        if symbol in group:
            return min(group)
    return symbol.casefold()


def problem(marks) -> str | None:
    """What's wrong with a list of marks, in plain words (with which mark: "Mark 2: …"), or None."""
    if not isinstance(marks, list) or not marks:
        return "Add at least one mark."
    if len(marks) > MAX_MARKS:
        return f"Keep it to {MAX_MARKS} marks or fewer."
    seen: dict[str, str] = {}
    struck = 0
    for i, m in enumerate(marks, start=1):
        if not isinstance(m, dict) or set(m) - {"symbol", "meaning", "action", "look"}:
            return f"{_what(i)} that isn't a mark Next Box knows."
        look = m.get("look", "")
        if not isinstance(look, str) or (look and look not in LOOKS):
            return f"{_what(i)} that isn't a mark Next Box knows."
        symbol = m.get("symbol", "")
        bad = _sym_problem(i, symbol, look)
        if bad:
            return bad
        meaning = m.get("meaning")
        if not isinstance(meaning, str) or not meaning.strip():
            return f"{_what(i)} say what it means to you."
        if any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in meaning):
            return f"{_what(i)} keep what it means to one line."
        if any(unicodedata.category(ch)[0] == "C" for ch in meaning):  # half a character, private or unassigned
            return f"{_what(i)} what it means has a character Next Box can't use. Type it again."
        if meaning != meaning.strip():
            return f"{_what(i)} take the spaces off the ends of what it means."
        if len(meaning) > MAX_MEANING:
            return f"{_what(i)} keep what it means to {MAX_MEANING} characters or fewer."
        # The words print on every label's legend: a character the label font lacks (an emoji, say)
        # would come out as an empty box. Spaces between words are fine.
        if not all(can_draw(word) for word in meaning.split()):
            return f"{_what(i)} what it means has a character the label can't print (like an emoji). Use words."
        if m.get("action") not in ACTIONS:
            return f"{_what(i)} pick what Next Box does."
        if look:
            struck += 1
        if symbol:
            key = _look_key(symbol)
            if key in seen:
                first = seen[key]
                return (f"“{symbol}” is in the list twice. Use each symbol once." if first.casefold() == symbol.casefold()
                        else f"“{first}” and “{symbol}” look the same when you write them. Change one of them.")
            seen[key] = symbol
    if struck > 1:
        return "Only one mark can be a line through the task."
    if not any(m["action"] == "done" for m in marks):
        return "One mark needs to finish the task: pick “Finish it” for it."
    return None


def valid(marks) -> bool:
    return problem(marks) is None


def check_style(prefs: dict) -> str | None:
    """After a settings change: "custom" needs a list of the person's own."""
    if prefs.get("mark_style") == CUSTOM and not prefs.get("marks"):
        return "Pick a style, or add your own marks first."
    return None


def public() -> dict:
    """What Settings needs to draw the chooser and the editor."""
    return {"presets": [{"id": k, "name": p["name"], "blurb": p["blurb"], "marks": p["marks"]} for k, p in PRESETS.items()],
            "actions": [{"id": a, "name": ACTION_NAMES[a]} for a in ACTIONS], "picker": PICKER,
            "max_marks": MAX_MARKS, "max_meaning": MAX_MEANING, "max_symbol": MAX_SYMBOL}
