"""The first-run setup questions (SPEC §27): which settings each answer changes.

Answers are turned into ordinary settings and then forgotten. In particular "what helps you most"
is never stored or logged as such (it can hint at ADHD, dyslexia or autism): only the settings it
turns on or off are saved, the same ones anybody can switch in Settings.
"""
from __future__ import annotations

from .auth import PREFS

# How will you print? Printing is the default: none of the answers turns on "Always preview only".
# With no printer in the household, every ticket is a preview anyway (jobs._finish), so a regular printer
# or no printer yet leaves that setting as the person has it. upkeep.preview_default turns it off once
# for people whose answer turned it on in older versions.
PRINT = {
    "label": {"print_with": "label", "always_dry_run": False},
    "paper": {"print_with": "paper"},   # previews come from having no label printer, then Print on paper
    "none": {"print_with": "none"},
}
PAPERS = ("a5", "half_letter", "letter", "a4")  # Do you keep a paper journal or planner? (or "none")
HELPS = ("gentle", "reading", "routine", "basics")  # neutral words, never a diagnosis


class QuizError(ValueError):
    """An answer the questions don't offer, in words the person can act on."""


def changes_for(answers: dict) -> dict:
    """The settings these answers change. A question that wasn't answered (missing or null) changes nothing."""
    if not isinstance(answers, dict):
        raise QuizError("Those answers didn't come through. Try again.")
    out: dict = {}
    how = answers.get("print")
    if how is not None:
        if how not in PRINT:
            raise QuizError("Pick how you'll print from the list.")
        out.update(PRINT[how])
    journal = answers.get("journal")
    if journal is not None:
        if journal == "none":
            out["journal_mode"] = False
        elif journal in PAPERS:
            out.update(paper_size=journal, journal_mode=True)
        else:
            raise QuizError("Pick a journal size from the list.")
    rows = answers.get("rows")
    if rows is not None:
        if type(rows) is not int or not 1 <= rows <= 20:
            raise QuizError("Pick how many tasks a day: 3, 5 or 8.")
        out["max_rows"] = rows
    helps = answers.get("helps")
    if helps:  # an empty pick is a skipped question
        if not isinstance(helps, list) or any(h not in HELPS for h in helps):
            raise QuizError("Pick what helps from the list.")
        if "basics" in helps and len(set(helps)) > 1:
            raise QuizError("“Just the basics” goes on its own.")
        if "basics" in helps:
            out.update(gentle_words=False, reading_mode=False, routine_mode=False)
        else:
            out.update(reading_mode="reading" in helps, routine_mode="routine" in helps)
            if "gentle" in helps:  # gentle words are on for everyone already; only "the basics" turns them off
                out["gentle_words"] = True
    for key, pref in (("plan_at", "routine_morning"), ("review_at", "routine_evening")):
        at = answers.get(key)
        if at is not None:
            if not PREFS[pref][1](at):
                raise QuizError("Pick a time, like 08:00.")
            out[pref] = at
    return out
