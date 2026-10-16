"""Round 2, batch B: keyboard, dialogs, focus, screen-reader announcements, contrast and layout (W3-01 to W3-15, R2-04).

The one server change is a setting: `keyboard_shortcuts` (W3-04), tested through the API. The rest happens in the
browser and was checked there (Chromium, keyboard only, with the accessibility tree); these tests pin the pieces that
make each fix work, so a later change doesn't quietly undo one."""
import dataclasses
import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nextbox import server
from nextbox.auth import PREFS, Accounts, default_prefs
from nextbox.server import create_app

WEB = Path(server.WEB_DIR)


@pytest.fixture(scope="module")
def js():
    return (WEB / "app.js").read_text()


@pytest.fixture(scope="module")
def html():
    return (WEB / "index.html").read_text()


def tag(html, id_):
    """The opening tag of the element with this id."""
    m = re.search(r'<[a-z0-9]+\b[^>]*\bid="%s"[^>]*>' % re.escape(id_), html)
    assert m, id_
    return m.group(0)


def between(text, start, end):
    """The text from `start` up to `end` (the first one after it)."""
    i = text.index(start)
    return text[i:text.index(end, i + len(start))]


# ------------------------------------------------------------------ W3-04: one-key shortcuts can be turned off
def site(ctx):
    ctx = dataclasses.replace(ctx, tasks_factory=None, today=date.today)
    ctx.settings.todoist_token = ""
    a = Accounts(ctx.settings.data_dir)
    a.create("sam", "sam password")
    app = create_app(ctx)
    c = TestClient(app, base_url="http://testserver")
    assert c.post("/auth/login", json={"username": "sam", "password": "sam password"}).status_code == 200
    return a, c


def test_keyboard_shortcuts_setting_is_on_unless_turned_off(ctx):
    assert default_prefs()["keyboard_shortcuts"] is True
    ok = PREFS["keyboard_shortcuts"][1]
    assert ok(True) and ok(False) and not any(ok(v) for v in (0, 1, "false", None, []))
    a, sam = site(ctx)
    assert sam.get("/auth/me").json()["prefs"]["keyboard_shortcuts"] is True  # existing accounts get it, on
    r = sam.patch("/settings", json={"keyboard_shortcuts": False})
    assert r.status_code == 200 and r.json()["keyboard_shortcuts"] is False
    assert sam.get("/auth/me").json()["prefs"]["keyboard_shortcuts"] is False
    assert sam.patch("/settings", json={"keyboard_shortcuts": "no"}).status_code == 400
    assert a.get("sam")["prefs"]["keyboard_shortcuts"] is False  # the bad value changed nothing


def test_keyboard_shortcuts_switch_is_in_general_and_the_handler_obeys_it(js, html):
    general = between(html, '<div class="card" id="setGeneral">', '<div class="card" id="tagCard">')
    assert 'data-pref="keyboard_shortcuts"' in general and "One-key shortcuts" in general
    handler = between(js, "const KEYS_TABS", "function keysGo")
    # Checked before any letter does anything (and a half-typed "g" is forgotten).
    assert "if (me.prefs?.keyboard_shortcuts === false) { keysG = 0; return; }" in handler
    assert handler.index("keyboard_shortcuts === false") < handler.index("quickOpen()")
    keys = between(html, '<dialog id="keysDialog"', "</dialog>")
    assert "Settings → General → One-key shortcuts" in keys


# ------------------------------------------------------------------ W3-01: Esc never closes the data notice
def test_data_notice_closes_only_by_agreeing_or_signing_out(js, html):
    assert 'closedby="none"' in tag(html, "noticeDialog")
    notice = between(js, '$("#noticeOk").onclick', "// ---------------------------------------------------------------- revision mark")
    # Esc is stopped as a key (before it becomes a close request Chrome may not let the page refuse)...
    assert '$("#noticeDialog").addEventListener("keydown", (e) => { if (e.key === "Escape") e.preventDefault(); });' in notice
    # ...and if it closes anyway while the notice waits for an answer, it opens again.
    assert re.search(r'addEventListener\("close", \(\) => \{\s*if \(me && \(!me\.consented \|\| me\.terms_ok === false\)'
                     r' && !\$\("#appShell"\)\.hidden\) \$\("#noticeDialog"\)\.showModal\(\);', notice)
    # Agreeing marks it agreed before it closes, so it doesn't come straight back.
    agree = between(notice, '$("#noticeOk").onclick', '$("#noticeNo")')
    assert agree.index("me.consented = true") < agree.index('$("#noticeDialog").close()')
    # Signing out forgets the person before dialogs close (clearPerson), so the notice doesn't reopen then.
    clear = between(js, "function clearPerson()", "showOnly(")
    assert clear.index("me = null") < clear.index('$$("dialog[open]").forEach((d) => d.close())')


# ------------------------------------------------------------------ W3-02: Cancel on "Close without…?" keeps the dialog
@pytest.mark.parametrize("dlg,ask", [("taskDialog", "tdAsk"), ("fbDialog", "fbAsk")])
def test_esc_in_edit_and_feedback_is_a_key_and_cancel_keeps_it_open(js, dlg, ask):
    # Esc is handled on keydown and never becomes a close request.
    m = re.search(r'\$\("#%s"\)\.addEventListener\("keydown", \(e\) => \{\s*if \(e\.key !== "Escape" \|\| e\.defaultPrevented\) return;'
                  r'\s*e\.preventDefault\(\);\s*if \((!tdBusy\(\) && )?%s\(\)\) \$\("#%s"\)\.close\(\);' % (dlg, ask, dlg), js)
    assert m, dlg
    # The back gesture is still a close request: when the browser won't let it be refused, it opens again as it was.
    keep = "tdKeep" if dlg == "taskDialog" else "fbKeep"
    assert f"if (!e.cancelable) {keep} = document.activeElement;" in js
    assert re.search(r'\$\("#%s"\)\.addEventListener\("close", \(\) => \{\s*if \(!%s\) return;\s*const el = %s; %s = null;'
                     r'\s*\$\("#%s"\)\.showModal\(\);' % (dlg, keep, keep, keep, dlg), js)


def test_feedback_keeps_what_was_typed_until_it_is_sent_or_thrown_away(js):
    opening = between(js, "function openFeedback()", '$("#fbDialog").showModal();')
    # Cleared only after a send (Posted… was showing), not every time it opens.
    assert 'if (!$("#fbDone").hidden) { $("#fbForm").querySelectorAll("textarea").forEach((t) => t.value = "");' in opening
    assert opening.count('querySelectorAll("textarea")') == 1
    # "OK" to "Close without sending? What you typed will be lost." does lose it.
    ask = between(js, "function fbAsk()", "let fbKeep")
    assert ask.index('confirm("Close without sending? What you typed will be lost.")') < ask.index('t.value = ""')


# ------------------------------------------------------------------ W3-03: focus is never under the sticky Jump to row
def test_settings_scroll_padding_clears_the_jump_row(js, html):
    rule = re.search(r"html:has\(#settings\.on\) \{ scroll-padding-top:calc\(var\(--jumpH, 56px\) \+ var\(--remindH, 0px\) \+ 12px\) !important; \}", html)
    assert rule  # !important: app.js sets an inline scroll-padding-top while a reminder shows
    assert "html:has(#settings.on) #settings > .card { scroll-margin-top:0; }" in html  # not counted twice for Jump to
    assert re.search(r'new ResizeObserver\(\(\) => \{\s*if \(\$\("#setJump"\)\.offsetHeight\) document\.documentElement\.style'
                     r'\.setProperty\("--jumpH", `\$\{\$\("#setJump"\)\.offsetHeight\}px`\);\s*\}\)\.observe\(\$\("#setJump"\)\);', js)


# ------------------------------------------------------------------ W3-05: Delete my account errors are said and tied to the box
def test_delete_account_errors_are_announced_and_mark_the_field(js):
    for msg, pw, name in (("meMsg", "meDelPw", "meDelName"), ("pDelMsg", "pDelPw", "pDelName")):
        assert f'<span class="err" id="{msg}" role="alert"></span>' in js
        assert f'catch (err) {{ delAccountErr(err, $("#{pw}"), $("#{name}"), $("#{msg}")); }}' in js
    fn = between(js, "function delAccountErr(", "\n}\n")
    assert '/password didn\'t match/i.test(err.message) ? pw : /username/i.test(err.message) ? name : null' in fn
    assert 'f.setAttribute("aria-invalid", "true"); f.setAttribute("aria-describedby", msg.id);' in fn
    assert 'f.removeAttribute("aria-invalid"); f.removeAttribute("aria-describedby");' in fn and "bad?.focus();" in fn


def test_delete_account_messages_match_what_the_page_looks_for(ctx):
    """The page picks the box to mark from these words: keep them in step."""
    a, sam = site(ctx)
    r = sam.post("/auth/delete-account", json={"password": "nope", "confirm": "sam"})
    assert re.search(r"password didn't match", r.json()["detail"], re.I)
    r = sam.post("/auth/delete-account", json={"password": "sam password", "confirm": "x"})
    assert re.search("username", r.json()["detail"], re.I) and not re.search(r"password didn't match", r.json()["detail"], re.I)


# ------------------------------------------------------------------ W3-06: a new one-time password or invite code is announced
def test_new_secrets_take_focus(js):
    temp = between(js, "function renderTemp(", "function showTemp(")
    assert '<p tabindex="-1" data-temphead>Give <b>' in temp
    assert 'el.querySelector("[data-temphead]").focus({ preventScroll: true });' in temp
    invite = between(js, "async function makeInvite()", "loadInvites();")
    assert '<p tabindex="-1" data-temphead>Send them this code or link. It works once.</p>' in invite
    assert '$("#ivOut [data-temphead]").focus();' in invite


# ------------------------------------------------------------------ W3-07, R2-04: focus after a button hides itself, or a day goes
def test_buttons_that_hide_themselves_move_focus_on(js):
    assert re.search(r'\$\("#guideMini"\)\.onclick = \(\) => \{\s*setGuideOpen\(true\);\s*focusFirst\(\$\("#guideSteps \.next \.title"\), \$\("#guideHead h2"\)\);', js)
    assert '$("#guideLess").onclick = () => { setGuideOpen(false); focusFirst($("#guideMini")); };' in js
    assert '$("#nowMore").hidden = true; focusFirst($("#nowText"));' in js
    back = between(js, '$("#jBackGo").onclick', "\n};")
    assert "await done;" in back and 'focusFirst($("#journal h2"))' in back


def test_focus_that_moves_on_is_never_left_off_screen(js):
    """W3-07 follow-up: Show less (a long checklist folding away above) and Older days (days pushing the button down)
    left focus off screen. focusFirst scrolls just enough to show it, inside the scroll padding (the phone's tab bar,
    a reminder note, Settings' Jump to row), and leaves the page alone when it's already in view."""
    fn = between(js, "function focusFirst(", "\n}\n")
    assert "el.focus({ preventScroll: true });" in fn
    assert re.search(r'const r = el\.getBoundingClientRect\(\), pad = getComputedStyle\(document\.documentElement\);\s*'
                     r'if \(r\.top < \(parseFloat\(pad\.scrollPaddingTop\) \|\| 0\) \|\| r\.bottom > innerHeight - '
                     r'\(parseFloat\(pad\.scrollPaddingBottom\) \|\| 0\)\) \{\s*el\.scrollIntoView\(\{ block: "nearest" \}\);', fn)
    assert fn.index("el.focus(") < fn.index("scrollIntoView")


def test_older_days_focuses_the_first_day_it_added(js):
    older = between(js, '$("#jOlder").onclick', "\n};")
    assert older.index('const had = $$("#jDays .jday").length;') < older.index("await loadJournal(true);")
    # Only when focus is still on the button, or was lost with it (not if they've moved on while it loaded).
    assert 'if (a === $("#jOlder") || !a?.offsetParent) focusFirst($$("#jDays .jday")[had]?.querySelector("h3"), $("#jOlder"), $("#jEnd"));' in older
    # The days go in above the button, so the first new one takes its place on screen.
    assert '$("#jDays").insertAdjacentHTML("beforeend", html);' in js


def test_notice_docs_say_what_the_back_gesture_does(js):
    """closedby="none" turns the notice's close watcher off: Esc does nothing, but the phone's back gesture isn't caught
    and goes back as on any page. The SPEC and the code comment say so, not that back does nothing."""
    spec = (WEB.parent / "SPEC.md").read_text()
    notice = between(spec, "- **The notice:**", "- **The superadmin can see:**")
    assert "Esc does nothing" in notice and "doesn't catch the phone's back" in notice
    assert not re.search(r"back gesture\s+do nothing", notice)
    comment = between(js, "// Agree, or sign out: nothing else closes it (W3-01).", '$("#noticeDialog").addEventListener("keydown"')
    assert "the phone's back gesture isn't caught here" in comment and "stops Esc and the" not in comment


def test_deleting_the_oldest_days_page_goes_to_the_day_above(js):
    jdel = between(js, '$$("#jDays [data-jdel]").forEach', "toast(\"Deleted\")")
    assert "prevDay = day?.previousElementSibling" in jdel
    order = ['next?.querySelector("textarea, button")', 'nextDay?.querySelector?.("h3")', 'prevDay?.querySelector?.("h3")', '$("#journal h2")']
    assert [jdel.index(x) for x in order] == sorted(jdel.index(x) for x in order)


# ------------------------------------------------------------------ W3-08: Esc or Cancel in Tag…/Delete… goes back to that button
def test_bulk_dialog_returns_focus_to_its_opener(js):
    assert "bulkOpener = document.activeElement;" in between(js, "function bulkDialog(", "\n}")
    close = between(js, '$("#bulkDialog").addEventListener("close"', "\n});")
    assert 'o?.isConnected && o.offsetParent && o.getAttribute("aria-disabled") !== "true" ? o : $("#printStickers")' in close
    # Doing something (a tag, Delete N) changes the picks: Stickers, as before.
    assert js.count('bulkOpener = null; $("#bulkDialog").close();') == 2


# ------------------------------------------------------------------ W3-09, W3-14: reflow and target sizes
def test_tag_rows_wrap_and_phone_targets_are_44px(html):
    assert re.search(r"\.tagrow \{ display:flex; flex-wrap:wrap;", html) and ".tagrow select { min-width:0; }" in html
    phone = between(html, "/* Phones: everything you tap is at least 44px high. */", "}\n  /* Small phones")
    assert "#setJump button, #filterTags button, .pwrow button { min-height:44px; }" in phone
    # "Show a notification too": the whole row is a label, like every other switch, so its words can be tapped.
    row = between(html, '<label class="setting" id="remindNotifyRow">', "</label>")
    assert 'id="remindNotify"' in row and 'aria-describedby="remindNotifyNote"' in row


# ------------------------------------------------------------------ W3-10: search boxes and placeholders are readable
def _lum(hexcolor):
    h = hexcolor.lstrip("#")
    h = "".join(c * 2 for c in h) if len(h) == 3 else h
    ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    ch = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_search_boxes_have_a_visible_edge_and_a_readable_placeholder(html):
    assert re.search(r"input\[type=search\] \{[^}]*border:1px solid var\(--fieldLine\)", html)
    assert "::placeholder { color:var(--muted); opacity:1; }" in html
    light = dict(re.findall(r"--(\w+):(#[0-9a-fA-F]{3,6})", between(html, ":root { color-scheme:light dark;", "}")))
    dark = dict(re.findall(r"--(\w+):(#[0-9a-fA-F]{3,6})", between(html, ':root[data-theme="dark"] {', "}")))
    for t in (light, dark):
        assert _ratio(t["muted"], t["bg"]) >= 4.5    # placeholder on the field's fill
        assert _ratio(t["fieldLine"], t["card"]) >= 3  # the field's edge on the card
    assert _ratio("#474747", light["bg"]) >= 4.5 and _ratio("#cfcfcf", dark["bg"]) >= 4.5  # Easy-to-read text's --muted


# ------------------------------------------------------------------ W3-11, W3-12, W3-13, W3-15: names, groups and headings
@pytest.mark.parametrize("field,words", [("pass", "Password"), ("joinPass", "Password (8+ characters)"),
                                         ("mcCur", "Temporary password"), ("mcNew", "New password")])
def test_password_fields_are_named_without_their_show_button(html, field, words):
    assert f'aria-labelledby="{field}Name"' in tag(html, field)
    assert f'<span id="{field}Name">{words}</span>' in html


def test_copy_link_name_contains_its_visible_words(js):
    assert 'aria-label="Copy link for ${esc(i.code)}">copy link</button>' in js and "Copy the link for" not in js


def test_edit_dialog_tags_are_a_named_group(html):
    assert '<span class="fieldlabel" id="tdTagsLabel">Tags</span><div class="chips" id="tdTags" role="group" aria-labelledby="tdTagsLabel">' in html
    assert "<label>Tags</label>" not in html


def test_journal_days_have_their_own_heading(html):
    days = between(html, '<div class="card" id="slugCard"', '<div id="jDays">')
    assert '<h2 class="sr-only" id="jDaysHead">Your pages</h2>' in days
    assert re.search(r"#journal\.searching #jBack, #journal\.searching #jDaysHead \{ display:none; \}", html)
