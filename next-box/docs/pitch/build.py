"""Builds the Next Box pitch deck (.pptx) and product description (.docx).

    pip install python-pptx python-docx
    python docs/pitch/build.py

The label pictures in img/ are real Next Box renders (nextbox/render.py), not mockups.
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches as DIn, Pt as DPt, RGBColor as DRGB
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

HERE = Path(__file__).parent
IMG = HERE / "img"
VERSION = "0.9.6"

INK = RGBColor(0x1B, 0x1B, 0x1B)
MUTED = RGBColor(0x6B, 0x6B, 0x6B)
PAPER = RGBColor(0xFA, 0xFA, 0xF7)
ACCENT = RGBColor(0x1F, 0x5E, 0xFF)
NOTE = RGBColor(0xFF, 0xF6, 0xD6)
LINE = RGBColor(0xDC, 0xDC, 0xD4)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
TAGS = {"red": "D93025", "orange": "EF7D1A", "yellow": "E8B500", "green": "2E9D4F",
        "blue": "2A6FF0", "pink": "E0509B", "purple": "8A4FD8"}

# ---------------------------------------------------------------------------------------------
# Shared content
# ---------------------------------------------------------------------------------------------
NAME = "Next Box"
TAGLINE = "Your to-do list, on paper you can hold."
ONE_LINER = ("Next Box prints your to-do list on small labels and stickers, made for people with ADHD. "
             "You check things off with a pen, take a photo, and your list updates itself.")

PROBLEM = [
    ("Out of sight, out of mind", "For many people with ADHD, a task inside an app on a locked phone "
     "may as well not exist. To-do apps depend on you remembering to open them."),
    ("Too much at once", "A screen holding 47 overdue tasks in red is overwhelming, and that feeling "
     "stops people before they start."),
    ("Paper works, but it doesn't sync", "Sticky notes and paper planners are visible and satisfying "
     "to check off. But they get lost, they go stale, and they never match your phone."),
    ("Guilt, not help", "Most apps say 'OVERDUE' in red. For someone who already struggles with time, "
     "that reads as shame, not as a nudge."),
]

SOLUTION = ("Next Box connects the to-do app you already use to a small label printer. With one tap "
            "it prints today's list, or one sticker per task to put where the task happens: on the "
            "fridge, the front door, the laptop. Check boxes with a pen, take a photo, and Next Box "
            "marks those tasks done in your app. You don't have to type them in.")

LOOP = [
    ("1  Plan", "Add tasks in Next Box's own list or in your usual app. Type a whole brain dump at once."),
    ("2  Print", "Tap to print the daily list, stickers, tear-off strips or one big 'just one thing' card."),
    ("3  Do", "Stick it where you'll see it. Check boxes, arrow things to tomorrow, cross out what's dropped."),
    ("4  Snap", "Take a photo. Next Box reads your pen marks, shows what it found, and updates your list."),
]

PRINTS = [
    ("Daily ticket", "daily.png", "Today on one label: your calendar, up to 5 tasks, the next tiny step for "
     "each, and yesterday's wins."),
    ("Stickers", "stickers.png", "One task per sticker with a tag color band, cut apart, to put where the "
     "task happens."),
    ("Tear-off strips", "strips.png", "A list on one label with dashed cut lines. Tear off one task at a time."),
    ("Just one thing", "focus.png", "For overwhelming days: one task, printed huge, with tiny steps and a "
     "'start by' time."),
]

ADHD = [
    ("Five things, not fifty", "The daily ticket shows at most 5 tasks. The rest go in a small 'Not today' "
     "line, so they aren't forgotten but don't take over."),
    ("Gentle words", "It says 'waiting 3d', not 'OVERDUE'. Repeating tasks move ahead instead of piling up."),
    ("The next tiny step", "Under each task: the first small action (\"Open the draft\"). Starting is "
     "the hard part."),
    ("Wins first", "\"Yesterday you finished 4. ✓\" is printed on today's ticket."),
    ("Brain dump", "Type everything at once, one task per line. Next Box understands #tags, !priority, "
     "'tomorrow 3pm', '~15m' and 'every weekday'."),
    ("Colored tags you can see", "Red for urgent, yellow for errands, blue for calls and more. Each tag "
     "prints as its own pattern, so color shows up even on a black-and-white printer."),
    ("Paper you can touch", "Checking a real box with a real pen gives a small reward that tapping a "
     "screen doesn't."),
    ("One automatic print, by design", "Nothing prints unless you ask, except today's list, once a day, "
     "when you walk into your office (through Home Assistant)."),
]

TODO_APPS = [
    ("Next Box's own list", "Built in, no account needed"),
    ("Todoist", "Ready"),
    ("Any CalDAV list", "Ready: Nextcloud, Fastmail, Synology, Zoho, older iCloud lists"),
    ("Notion", "Ready: any database with a title and a checkbox or status column"),
    ("Asana", "Ready: tasks assigned to you"),
    ("Google Tasks", "Built; switches on after a one-time Google sign-in setup"),
    ("Microsoft To Do", "Built; switches on after a one-time Microsoft sign-in setup"),
    ("TickTick", "Built; switches on after a one-time TickTick sign-in setup"),
    ("Apple Reminders, Things, Any.do, Google Keep", "Not possible: they don't let other apps connect"),
]

CALENDARS = [
    ("Any calendar link (.ics)", "Ready: Google, iCloud, Outlook and school or work calendars"),
    ("CalDAV calendars", "Ready"),
    ("Google Calendar, Outlook", "Built; switch on after the one-time sign-in setup"),
]

PRINTERS = [
    ("Brother QL / PT label printers", "QL-800 series can print red ink too"),
    ("ESC/POS receipt printers", "Epson, Star, most 58/80 mm printers"),
    ("ZPL label printers", "Zebra and compatibles"),
    ("TSPL label printers", "TSC, Munbyn, iDPRT, Xprinter"),
    ("Anything installed on the computer", "Dymo, Rollo and USB printers, through CUPS"),
    ("Any home or office printer", "'Print on paper' makes a Letter or A4 PDF with real-size stickers and cut lines"),
]

TRUST = [
    ("Your tasks stay where they are", "Tasks from a linked app are read live and never copied into Next Box. "
     "Calendars are read-only and never stored."),
    ("Invite-only", "No open sign-up. Every new person needs a one-time invite code."),
    ("Secrets sealed", "To-do app tokens are encrypted at rest. Passwords are hashed. Secrets never appear "
     "in logs or debug bundles."),
    ("You own your data", "Download everything as one file, or delete your account yourself."),
    ("Deleting needs a yes", "No task is ever deleted, even from a photo, without you confirming it."),
    ("Anonymous feedback", "The feedback board doesn't show who posted. Only one protected table links a "
     "post to a person."),
    ("Safe links", "Calendar and server addresses people type are checked so they can't point Next Box "
     "at private networks."),
    ("Honest data notice", "Everyone agrees, in plain words, to exactly what the admin can see."),
]

ADMIN = [
    "Households: families or teams share printers; each person keeps their own list.",
    "Roles: person, household admin, superadmin. The protected owner account can never be locked out.",
    "Super admin panel: every person, search and filters, reset passwords, sign out devices, bulk "
    "actions, CSV export. It shows task counts, never task text.",
    "Every admin action is recorded in an audit log.",
    "Built-in health check (`nextbox doctor`) and a 'new version available' banner.",
]

APP_STANDARDS = [
    "Profile page, full settings, light and dark theme, text size",
    "Phone bottom tab bar, installable on the home screen, offline banner",
    "Getting-started guide for new people, plus help, privacy, terms, about and 404 pages",
    "Sign in with username or email, change password, signed-in devices",
]

STATUS = [
    ("Live beta", "Running at nextbox.braznarkel.com through Cloudflare, invite-only."),
    (f"Version {VERSION}", "Python and FastAPI server, PostgreSQL database, web app for phone and desktop."),
    ("Tested", "150+ automated tests, run against both SQLite and PostgreSQL."),
    ("Self-hosted", "Runs on one home PC today. A cloud version is on the roadmap."),
]

MARKET = [
    ("Adults with ADHD", "Often estimated at about 4–5% of adults in the US, and many more undiagnosed. "
     "They are underserved by screen-only productivity tools."),
    ("Paper-planner people", "People who already buy planners, sticky notes and label makers, and want them "
     "to stay in sync."),
    ("Busy households", "Families who share chores and errands, and teens building routines."),
    ("Small teams and makers", "Workshops, kitchens and front desks where a printed ticket beats a screen."),
]

COMPETITION = [
    ("To-do apps (Todoist, TickTick, Things)", "Great lists, but screen-only. Next Box works with them "
     "instead of replacing them."),
    ("Paper planners and sticky notes", "Tangible but disconnected. Next Box adds sync, tags and read-back."),
    ("DIY receipt-printer to-do hacks", "Hobby projects that print a list. None read the paper back, "
     "work with many apps, or design for ADHD."),
    ("ADHD apps (timers, body doubling)", "Help with focus in the moment. Next Box is the physical planning "
     "layer and works next to them."),
]

MODEL = [
    ("Hosted subscription", "Monthly plan for the cloud version: linked apps, photo read-back and calendars."),
    ("Starter kit", "A label printer bundled with Next Box, set up out of the box."),
    ("Label refills", "Colored label rolls and sticker sheets, matched to the tag colors."),
    ("Free self-hosted edition", "For tinkerers. It builds trust and community."),
]

ROADMAP = [
    ("Now", "Live beta with invited testers. First real prints on the Brother QL-1110NWB."),
    ("Next", "Turn on Google, Microsoft and TickTick sign-in. Print straight from a phone. "
     "Publish the braznarkel.com homepage."),
    ("Later", "Cloud hosting, AI 'break it down' into tiny steps, a focus timer, color printing on paper."),
    ("Launch", "Legal review of the privacy policy and terms, pricing, and a starter-kit partner."),
]

OPEN_QUESTIONS = [
    "Pricing and which model to lead with (subscription versus kit)",
    "Cloud hosting provider and when to move off the home PC",
    "Which printer to recommend in a starter kit",
    "Legal review of the privacy policy and terms before charging money",
    "Validating demand: beta tester feedback and usage numbers",
]


# ---------------------------------------------------------------------------------------------
# PowerPoint
# ---------------------------------------------------------------------------------------------
W, H = Inches(13.333), Inches(7.5)


def _bg(slide, color=PAPER):
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = color


def _text(slide, x, y, w, h, text, size=18, bold=False, color=INK, align=PP_ALIGN.LEFT,
          anchor=MSO_ANCHOR.TOP, font="Calibri"):
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Inches(0.05)
    lines = text if isinstance(text, list) else [text]
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        r = p.add_run()
        r.text = line
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.color.rgb = color
        r.font.name = font
    return box


def _rich(slide, x, y, w, h, items, size=16, gap=8, head_color=INK):
    """Bold head + normal body, one paragraph per item."""
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    for i, (head, body) in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(gap)
        a = p.add_run()
        a.text = head + "  "
        a.font.bold = True
        a.font.size = Pt(size)
        a.font.color.rgb = head_color
        a.font.name = "Calibri"
        b = p.add_run()
        b.text = body
        b.font.size = Pt(size)
        b.font.color.rgb = MUTED
        b.font.name = "Calibri"
    return box


def _rect(slide, x, y, w, h, fill, line=None, shape=MSO_SHAPE.ROUNDED_RECTANGLE):
    s = slide.shapes.add_shape(shape, x, y, w, h)
    s.fill.solid()
    s.fill.fore_color.rgb = fill
    if line is None:
        s.line.fill.background()
    else:
        s.line.color.rgb = line
        s.line.width = Pt(1)
    s.shadow.inherit = False
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        s.adjustments[0] = 0.08
    return s


def _title(slide, kicker, title):
    _bg(slide)
    _text(slide, Inches(0.7), Inches(0.45), Inches(11), Inches(0.4), kicker.upper(), 13, True, ACCENT)
    _text(slide, Inches(0.7), Inches(0.8), Inches(12), Inches(1), title, 34, True, INK)
    # tag-color stripe, the brand mark
    x = Inches(0.7)
    for c in TAGS.values():
        _rect(slide, x, Inches(1.72), Inches(0.42), Inches(0.07), RGBColor.from_string(c), shape=MSO_SHAPE.RECTANGLE)
        x += Inches(0.42)


def _footer(slide, n):
    _text(slide, Inches(0.7), Inches(7.0), Inches(6), Inches(0.3), f"{NAME}  ·  {TAGLINE}", 10, color=MUTED)
    _text(slide, Inches(11.6), Inches(7.0), Inches(1), Inches(0.3), str(n), 10, color=MUTED, align=PP_ALIGN.RIGHT)


def _picture(slide, path, x, y, h=None, w=None, frame=True):
    if frame:
        from PIL import Image
        iw, ih = Image.open(path).size
        if h is not None:
            w = Emu(int(h * iw / ih))
        else:
            h = Emu(int(w * ih / iw))
        pad = Inches(0.12)
        _rect(slide, x - pad, y - pad, w + 2 * pad, h + 2 * pad, WHITE, LINE)
    return slide.shapes.add_picture(str(path), x, y, width=w, height=h)


def _notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text


def build_pptx(out: Path) -> None:
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    blank = prs.slide_layouts[6]
    n = 0

    def new():
        nonlocal n
        n += 1
        return prs.slides.add_slide(blank)

    # 1 Title
    s = new()
    _bg(s, INK)
    x = Inches(0.8)
    for c in TAGS.values():
        _rect(s, x, Inches(2.2), Inches(0.6), Inches(0.12), RGBColor.from_string(c), shape=MSO_SHAPE.RECTANGLE)
        x += Inches(0.6)
    _text(s, Inches(0.8), Inches(2.5), Inches(7.4), Inches(1.4), NAME, 72, True, WHITE)
    _text(s, Inches(0.8), Inches(3.8), Inches(7.5), Inches(0.8), TAGLINE, 28, False, RGBColor(0xDD, 0xDD, 0xDD))
    _text(s, Inches(0.8), Inches(4.7), Inches(7), Inches(1.2),
          "A paper-first to-do system for ADHD brains: thermal labels, colored tags and photo read-back, "
          "connected to the apps you already use.", 17, color=RGBColor(0xB0, 0xB0, 0xB0))
    _text(s, Inches(0.8), Inches(6.5), Inches(7), Inches(0.4),
          f"Product overview and pitch  ·  v{VERSION}  ·  nextbox.braznarkel.com", 12,
          color=RGBColor(0x90, 0x90, 0x90))
    _picture(s, IMG / "stickers.png", Inches(8.5), Inches(0.9), h=Inches(5.7))
    _notes(s, ONE_LINER)

    # 2 Problem
    s = new()
    _title(s, "The problem", "To-do apps are built for brains that remember to open them")
    for i, (head, body) in enumerate(PROBLEM):
        cx = Inches(0.7 + (i % 2) * 6.1)
        cy = Inches(2.15 + (i // 2) * 2.3)
        _rect(s, cx, cy, Inches(5.8), Inches(2.05), WHITE, LINE)
        _text(s, cx + Inches(0.3), cy + Inches(0.22), Inches(5.2), Inches(0.5), head, 20, True)
        _text(s, cx + Inches(0.3), cy + Inches(0.75), Inches(5.2), Inches(1.2), body, 15, color=MUTED)
    _footer(s, n)
    _notes(s, "Most productivity tools assume executive function the user may not have. "
              "The fix isn't a better app. It's getting the list out of the phone and into the room.")

    # 3 Solution
    s = new()
    _title(s, "The solution", "Put the list in the room, and keep it in sync")
    _text(s, Inches(0.7), Inches(2.1), Inches(6.6), Inches(2.2), SOLUTION, 18, color=INK)
    _rect(s, Inches(0.7), Inches(4.5), Inches(6.6), Inches(1.9), NOTE)
    _text(s, Inches(1.0), Inches(4.65), Inches(6.1), Inches(1.7),
          ["Not another to-do app.",
           "Next Box adds paper on top of the app you already use: Todoist, Notion, Google Tasks and "
           "others, or its own simple built-in list."], 16, color=RGBColor(0x5A, 0x43, 0x00))
    _picture(s, IMG / "daily.png", Inches(8.4), Inches(2.1), h=Inches(4.6))
    _footer(s, n)

    # 4 How it works
    s = new()
    _title(s, "How it works", "Plan → Print → Do → Snap")
    for i, (head, body) in enumerate(LOOP):
        cx = Inches(0.7 + i * 3.05)
        _rect(s, cx, Inches(2.3), Inches(2.8), Inches(3.6), WHITE, LINE)
        _rect(s, cx, Inches(2.3), Inches(2.8), Inches(0.12), RGBColor.from_string(list(TAGS.values())[i * 2]),
              shape=MSO_SHAPE.RECTANGLE)
        _text(s, cx + Inches(0.25), Inches(2.65), Inches(2.4), Inches(0.6), head, 24, True)
        _text(s, cx + Inches(0.25), Inches(3.4), Inches(2.4), Inches(2.4), body, 15, color=MUTED)
    _text(s, Inches(0.7), Inches(6.2), Inches(12), Inches(0.5),
          "Read-back works on every print: each box has a row number, and the label code ties the photo "
          "to exactly what was printed.", 14, color=MUTED)
    _footer(s, n)

    # 5 What it prints
    s = new()
    _title(s, "What it prints", "Four formats, one tap each")
    for i, (head, img, body) in enumerate(PRINTS):
        cx = Inches(0.7 + i * 3.1)
        _picture(s, IMG / img, cx + Inches(0.25), Inches(2.2), w=Inches(2.3))
        _text(s, cx, Inches(5.45), Inches(2.85), Inches(0.4), head, 18, True)
        _text(s, cx, Inches(5.85), Inches(2.85), Inches(1.1), body, 12, color=MUTED)
    _footer(s, n)
    _notes(s, "These are real renders from the Next Box code, not mockups.")

    # 6 ADHD design
    s = new()
    _title(s, "Designed for ADHD", "Every default is chosen to lower the barrier to starting")
    _rich(s, Inches(0.7), Inches(2.1), Inches(5.9), Inches(4.8), ADHD[:4], 15, 12)
    _rich(s, Inches(6.9), Inches(2.1), Inches(5.9), Inches(4.8), ADHD[4:], 15, 12)
    _footer(s, n)

    # 7 Color tags
    s = new()
    _title(s, "Colored tags without color ink", "Color that works on a black-and-white printer")
    _text(s, Inches(0.7), Inches(2.1), Inches(6.5), Inches(1.4),
          "Thermal printers only print black. Next Box gives each tag color two jobs:", 18)
    _rich(s, Inches(0.7), Inches(2.9), Inches(6.5), Inches(3.5), [
        ("Routing.", "Load red labels in one printer and white in another. Urgent stickers come out on red "
                     "labels automatically."),
        ("Pattern.", "On any roll, each color prints as its own pattern: red is solid, yellow is dots, "
                     "green is stripes, purple is crosshatch, and so on. You can tell tags apart at a glance."),
        ("Your colors.", "Tags match your Todoist labels or calendar categories by name, so a tag keeps "
                         "the same color everywhere."),
    ], 16, 12)
    names = ["urgent", "orange", "errand", "home", "call", "pink", "work"]
    for i, (cname, hexc) in enumerate(TAGS.items()):
        y = Inches(2.1 + i * 0.62)
        _rect(s, Inches(8.0), y, Inches(0.5), Inches(0.45), RGBColor.from_string(hexc))
        _text(s, Inches(8.7), y, Inches(4), Inches(0.45), f"{cname}  ·  #{names[i]}", 16, anchor=MSO_ANCHOR.MIDDLE)
    _footer(s, n)

    # 8 Read-back
    s = new()
    _title(s, "Close the loop", "Take a photo of the paper and your app updates")
    _rich(s, Inches(0.7), Inches(2.1), Inches(6.8), Inches(4.6), [
        ("Mark it up.", "✓ done   → move to tomorrow   ✗ drop. The legend is printed on every daily ticket."),
        ("Snap it.", "Take a photo from the phone app. AI vision reads each numbered box."),
        ("Review it.", "Next Box shows what it found before changing anything, and you confirm."),
        ("Safe by default.", "Dropping a task never deletes it without an explicit yes."),
    ], 17, 14)
    _rect(s, Inches(8.1), Inches(2.1), Inches(4.5), Inches(4.3), WHITE, LINE)
    _text(s, Inches(8.4), Inches(2.35), Inches(4), Inches(4), [
        "Read-back found 3 marks",
        "",
        "✓  Call the dentist → done",
        "→  Pick up prescription → tomorrow",
        "✗  Text Sam back → drop?",
        "",
        "[ Apply ]   [ Fix one ]",
    ], 16, color=INK)
    _footer(s, n)

    # 9 Integrations
    s = new()
    _title(s, "Works with what you already use", "Connect your to-do app and calendar, or use the built-in list")
    _text(s, Inches(0.7), Inches(2.05), Inches(6), Inches(0.4), "TO-DO APPS", 13, True, ACCENT)
    _rich(s, Inches(0.7), Inches(2.45), Inches(6.2), Inches(4.5), TODO_APPS, 13, 5)
    _text(s, Inches(7.3), Inches(2.05), Inches(5), Inches(0.4), "CALENDARS", 13, True, ACCENT)
    _rich(s, Inches(7.3), Inches(2.45), Inches(5.4), Inches(2.0), CALENDARS, 13, 5)
    _rect(s, Inches(7.3), Inches(4.5), Inches(5.3), Inches(1.9), NOTE)
    _text(s, Inches(7.55), Inches(4.65), Inches(4.9), Inches(1.7),
          "Tasks stay in your app. Next Box reads them live and never makes a copy. Calendars are "
          "read-only and print as a SCHEDULE box on the daily ticket.", 14, color=RGBColor(0x5A, 0x43, 0x00))
    _footer(s, n)

    # 10 Printers
    s = new()
    _title(s, "Prints on almost anything", "From a $60 label printer to the office laser printer")
    _rich(s, Inches(0.7), Inches(2.1), Inches(6.8), Inches(4.6), PRINTERS, 16, 10)
    _picture(s, IMG / "strips.png", Inches(8.3), Inches(2.3), w=Inches(4.2))
    _footer(s, n)

    # 11 Trust
    s = new()
    _title(s, "Built for trust", "Privacy and safety are part of the design")
    _rich(s, Inches(0.7), Inches(2.1), Inches(5.9), Inches(4.8), TRUST[:4], 15, 12)
    _rich(s, Inches(6.9), Inches(2.1), Inches(5.9), Inches(4.8), TRUST[4:], 15, 12)
    _footer(s, n)

    # 12 Accounts & admin
    s = new()
    _title(s, "A complete app, not a script", "Accounts, households and admin tools are already built")
    _text(s, Inches(0.7), Inches(2.05), Inches(6), Inches(0.4), "FOR EVERYONE", 13, True, ACCENT)
    _text(s, Inches(0.7), Inches(2.45), Inches(5.9), Inches(4.4), ["•  " + a for a in APP_STANDARDS], 15)
    _text(s, Inches(6.9), Inches(2.05), Inches(6), Inches(0.4), "FOR ADMINS", 13, True, ACCENT)
    _text(s, Inches(6.9), Inches(2.45), Inches(5.9), Inches(4.4), ["•  " + a for a in ADMIN], 15)
    _footer(s, n)

    # 13 Market
    s = new()
    _title(s, "Who it's for", "People whose plans live better on paper than on a screen")
    for i, (head, body) in enumerate(MARKET):
        cx = Inches(0.7 + (i % 2) * 6.1)
        cy = Inches(2.15 + (i // 2) * 2.3)
        _rect(s, cx, cy, Inches(5.8), Inches(2.05), WHITE, LINE)
        _text(s, cx + Inches(0.3), cy + Inches(0.22), Inches(5.2), Inches(0.5), head, 20, True)
        _text(s, cx + Inches(0.3), cy + Inches(0.75), Inches(5.2), Inches(1.2), body, 15, color=MUTED)
    _footer(s, n)
    _notes(s, "The adult ADHD prevalence figure is a commonly cited estimate; check the latest source before "
              "using it in a pitch to investors.")

    # 14 Competition
    s = new()
    _title(s, "Why Next Box is different", "It works with today's tools instead of replacing them")
    _rich(s, Inches(0.7), Inches(2.1), Inches(7.4), Inches(4.8), COMPETITION, 16, 14)
    _rect(s, Inches(8.5), Inches(2.1), Inches(4.1), Inches(4.3), INK)
    _text(s, Inches(8.8), Inches(2.35), Inches(3.6), Inches(4), [
        "Only Next Box does all of these:", "",
        "✓  Paper you can stick up",
        "✓  Sync with real apps",
        "✓  Read back from a photo",
        "✓  Designed for ADHD",
        "✓  Any printer",
    ], 17, color=WHITE)
    _footer(s, n)

    # 15 Business model
    s = new()
    _title(s, "Business model (options)", "Several ways to earn, still to be decided")
    for i, (head, body) in enumerate(MODEL):
        cx = Inches(0.7 + i * 3.05)
        _rect(s, cx, Inches(2.3), Inches(2.8), Inches(3.4), WHITE, LINE)
        _text(s, cx + Inches(0.25), Inches(2.55), Inches(2.4), Inches(0.9), head, 20, True)
        _text(s, cx + Inches(0.25), Inches(3.5), Inches(2.4), Inches(2.1), body, 15, color=MUTED)
    _text(s, Inches(0.7), Inches(6.0), Inches(12), Inches(0.6),
          "Label refills add recurring revenue that follows usage: the more someone relies on Next Box, "
          "the more labels they print.", 14, color=MUTED)
    _footer(s, n)

    # 16 Status
    s = new()
    _title(s, "Where we are today", "A working product, live with real users")
    for i, (head, body) in enumerate(STATUS):
        cx = Inches(0.7 + i * 3.05)
        _rect(s, cx, Inches(2.3), Inches(2.8), Inches(2.6), WHITE, LINE)
        _text(s, cx + Inches(0.25), Inches(2.55), Inches(2.4), Inches(0.6), head, 22, True, ACCENT)
        _text(s, cx + Inches(0.25), Inches(3.3), Inches(2.4), Inches(1.6), body, 14, color=MUTED)
    _text(s, Inches(0.7), Inches(5.3), Inches(12), Inches(1.3), [
        "Built so far: built-in list and 8 to-do app connections, 3 calendar types, 5 printer families "
        "plus paper PDF, 4 print formats, photo read-back, accounts, invites, households, super admin "
        "panel, data export, account deletion, legal pages and a phone-ready web app."], 15)
    _footer(s, n)

    # 17 Roadmap
    s = new()
    _title(s, "Roadmap", "From home beta to a product people pay for")
    for i, (head, body) in enumerate(ROADMAP):
        y = Inches(2.15 + i * 1.15)
        _rect(s, Inches(0.7), y, Inches(1.6), Inches(0.9), RGBColor.from_string(list(TAGS.values())[[4, 3, 6, 0][i]]))
        _text(s, Inches(0.7), y, Inches(1.6), Inches(0.9), head, 20, True, WHITE, PP_ALIGN.CENTER, MSO_ANCHOR.MIDDLE)
        _text(s, Inches(2.6), y, Inches(10), Inches(0.9), body, 17, anchor=MSO_ANCHOR.MIDDLE)
    _footer(s, n)

    # 18 Close
    s = new()
    _bg(s, INK)
    x = Inches(0.8)
    for c in TAGS.values():
        _rect(s, x, Inches(2.0), Inches(0.6), Inches(0.12), RGBColor.from_string(c), shape=MSO_SHAPE.RECTANGLE)
        x += Inches(0.6)
    _text(s, Inches(0.8), Inches(2.3), Inches(11), Inches(1.2), "Get the list out of the phone.", 48, True, WHITE)
    _text(s, Inches(0.8), Inches(3.5), Inches(11), Inches(1.0), "And into the room, where it gets done.", 28,
          color=RGBColor(0xDD, 0xDD, 0xDD))
    _text(s, Inches(0.8), Inches(4.9), Inches(11), Inches(1.4), [
        "Try the beta: nextbox.braznarkel.com (invite only)",
        "Braznarkel  ·  braznarkel.com"], 18, color=RGBColor(0xB0, 0xB0, 0xB0))

    prs.save(out)


# ---------------------------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------------------------
def _shade(cell, hexcolor):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hexcolor)
    tcPr.append(shd)


def _table(doc, head, rows, widths=(2.3, 4.2)):
    t = doc.add_table(rows=1, cols=len(head))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(head):
        c = t.rows[0].cells[i]
        c.text = ""
        r = c.paragraphs[0].add_run(h)
        r.bold = True
        r.font.color.rgb = DRGB(0xFF, 0xFF, 0xFF)
        _shade(c, "1B1B1B")
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = v
            if i == 0:
                cells[i].paragraphs[0].runs[0].bold = True
    for row in t.rows:
        for i, w in enumerate(widths):
            row.cells[i].width = DIn(w)
    doc.add_paragraph()


def _items(doc, items):
    for head, body in items:
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(head + ". " if not head.endswith(".") else head + " ").bold = True
        p.add_run(body)


def build_docx(out: Path) -> None:
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = DPt(11)
    for name, size in (("Heading 1", 18), ("Heading 2", 14)):
        hs = doc.styles[name]
        hs.font.name = "Calibri"
        hs.font.size = DPt(size)
        hs.font.color.rgb = DRGB(0x1B, 0x1B, 0x1B)
    for sec in doc.sections:
        sec.left_margin = sec.right_margin = DIn(1)
        sec.top_margin = sec.bottom_margin = DIn(0.9)
        fp = sec.footer.paragraphs[0]
        fp.text = f"{NAME} — Product description and pitch — v{VERSION}"
        fp.runs[0].font.size = DPt(9)
        fp.runs[0].font.color.rgb = DRGB(0x6B, 0x6B, 0x6B)

    # Cover
    p = doc.add_paragraph()
    p.paragraph_format.space_before = DPt(120)
    r = p.add_run(NAME)
    r.bold = True
    r.font.size = DPt(44)
    p = doc.add_paragraph()
    r = p.add_run(TAGLINE)
    r.font.size = DPt(20)
    r.font.color.rgb = DRGB(0x6B, 0x6B, 0x6B)
    p = doc.add_paragraph()
    for name, hexc in TAGS.items():
        r = p.add_run("■ ")
        r.font.size = DPt(22)
        r.font.color.rgb = DRGB.from_string(hexc)
    doc.add_paragraph()
    p = doc.add_paragraph()
    p.add_run("Product description and pitch\n").bold = True
    p.add_run(f"Version {VERSION}  ·  October 2026\nBraznarkel  ·  nextbox.braznarkel.com")
    doc.add_page_break()

    # 1 Executive summary
    doc.add_heading("1. Executive summary", 1)
    doc.add_paragraph(ONE_LINER)
    doc.add_paragraph(
        "Most productivity software assumes you will remember to open it. For the millions of adults with "
        "ADHD, that assumption is the problem. Next Box takes the list out of the phone and puts it on the "
        "fridge, the desk and the front door, as daily tickets, stickers and tear-off strips from an "
        "inexpensive label printer. A photo of the marked-up paper brings the changes back, so paper and "
        "app never drift apart.")
    doc.add_paragraph(
        f"Next Box is a working product today (version {VERSION}). It runs live at nextbox.braznarkel.com "
        "for invited beta testers. It works with Todoist, Notion, Asana and CalDAV lists, with Google Tasks, "
        "Microsoft To Do and TickTick ready to switch on, and with calendar feeds. It prints to five "
        "families of label and receipt printers, or to any regular printer as a PDF.")
    _table(doc, ["At a glance", ""], [
        ("What", "Paper-first to-do system: label printer + web app + photo read-back"),
        ("Who", "Adults with ADHD first; paper-planner fans, households and small teams next"),
        ("Why it wins", "Keeps the list visible, works with existing apps, designed to reduce overwhelm"),
        ("Stage", f"Live invite-only beta, v{VERSION}, 150+ automated tests"),
        ("Model (proposed)", "Hosted subscription, printer starter kit, label refills"),
    ])

    # 2 Problem
    doc.add_heading("2. The problem", 1)
    _items(doc, PROBLEM)

    # 3 Solution
    doc.add_heading("3. The solution", 1)
    doc.add_paragraph(SOLUTION)
    doc.add_heading("How it works", 2)
    _items(doc, [(h.split("  ")[1], b) for h, b in LOOP])
    doc.add_picture(str(IMG / "daily.png"), width=DIn(3.0))
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap = doc.add_paragraph("A real daily ticket: schedule, five tasks with next steps and tag patterns, "
                            "a gentle 'Not today' line and yesterday's win.")
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap.runs[0].italic = True

    # 4 What it prints
    doc.add_heading("4. What it prints", 1)
    for head, img, body in PRINTS:
        doc.add_heading(head, 2)
        doc.add_paragraph(body)
        doc.add_picture(str(IMG / img), width=DIn(2.6))
        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph(
        "Every print is manual. The only exception is today's list, which prints once per day when Home "
        "Assistant sees you walk into your office. A server-side guard stops it from ever printing twice.")

    # 5 ADHD
    doc.add_heading("5. Designed for ADHD", 1)
    doc.add_paragraph("Every default is chosen to make starting easier and to avoid shame:")
    _items(doc, ADHD)
    doc.add_heading("Colored tags on a black-and-white printer", 2)
    doc.add_paragraph(
        "Thermal printers print only black, so each tag color does two jobs. First, routing: if one printer "
        "holds red labels, urgent stickers go to it. Second, pattern: on any roll each color prints as its "
        "own pattern (red solid, orange diagonal, yellow dots, green horizontal stripes, blue vertical "
        "stripes, pink checker, purple crosshatch), so tags are easy to tell apart. Tags match the names of "
        "Todoist labels and calendar categories, so colors stay the same everywhere. Brother QL-800 series "
        "printers can also print real red ink.")

    # 6 Read-back
    doc.add_heading("6. Photo read-back", 1)
    doc.add_paragraph(
        "Every printed box carries a row number, and every label has a short code (for example #K7Q2) that "
        "ties it to exactly what was printed. Mark boxes with ✓ (done), → (move to tomorrow) or ✗ (drop), "
        "and take a photo in the app. An AI vision model reads the marks. Next Box shows its reading for "
        "review, and only then updates the task list. Dropping a task never deletes it without an "
        "explicit confirmation.")

    # 7 Integrations
    doc.add_heading("7. Works with what people already use", 1)
    doc.add_heading("To-do apps", 2)
    _table(doc, ["App", "Status"], TODO_APPS)
    doc.add_heading("Calendars", 2)
    _table(doc, ["Calendar", "Status"], CALENDARS)
    doc.add_paragraph(
        "Each person has Next Box's built-in list plus, optionally, one linked app. Tasks in a linked app "
        "stay there: Next Box reads them live and sends changes back, and never keeps a copy. Calendars are "
        "read-only, never stored, and print as a SCHEDULE box on the daily ticket.")
    doc.add_heading("Printers", 2)
    _table(doc, ["Printer", "Notes"], PRINTERS)

    # 8 Trust
    doc.add_heading("8. Privacy, safety and trust", 1)
    _items(doc, TRUST)

    # 9 App
    doc.add_heading("9. A complete app", 1)
    doc.add_heading("For everyone", 2)
    for a in APP_STANDARDS:
        doc.add_paragraph(a, style="List Bullet")
    doc.add_heading("For admins", 2)
    for a in ADMIN:
        doc.add_paragraph(a.replace("`", ""), style="List Bullet")

    # 10 Technology
    doc.add_heading("10. Technology", 1)
    _table(doc, ["Layer", "Built with"], [
        ("Server", "Python 3.12, FastAPI, SQLAlchemy"),
        ("Database", "PostgreSQL (SQLite for development); automatic upgrades for new columns"),
        ("Web app", "Phone- and desktop-ready, installable to the home screen, light and dark themes"),
        ("Label rendering", "Pillow; real-size layouts for each printer, plus Letter/A4 PDF"),
        ("Read-back", "Claude vision model reads pen marks against the printed manifest"),
        ("Integrations", "Provider plug-ins (tokens or OAuth with PKCE), CalDAV, iCalendar"),
        ("Hosting", "One home PC behind a Cloudflare Tunnel, with no open ports"),
        ("Home automation", "Home Assistant prints the daily list when you enter your office"),
        ("Quality", "150+ automated tests on SQLite and PostgreSQL; built-in 'doctor' health check"),
    ])

    # 11 Market
    doc.add_heading("11. Market", 1)
    _items(doc, MARKET)
    p = doc.add_paragraph()
    r = p.add_run("Note: market sizes here are rough public estimates. They should be checked against "
                  "current sources before being used with investors.")
    r.italic = True
    r.font.size = DPt(9)

    # 12 Competition
    doc.add_heading("12. Competition and positioning", 1)
    _table(doc, ["Alternative", "How Next Box compares"], COMPETITION)
    doc.add_paragraph(
        "Next Box doesn't compete with to-do apps. It makes them work better for people who need their list "
        "to be visible. It is the only product that combines physical labels, live sync with mainstream "
        "apps, photo read-back and an ADHD-first design.")

    # 13 Business model
    doc.add_heading("13. Business model (options under consideration)", 1)
    _items(doc, MODEL)

    # 14 Status & roadmap
    doc.add_heading("14. Current status", 1)
    _items(doc, STATUS)
    doc.add_heading("15. Roadmap", 1)
    _items(doc, ROADMAP)
    doc.add_heading("16. Open questions", 1)
    for q in OPEN_QUESTIONS:
        doc.add_paragraph(q, style="List Bullet")

    doc.add_heading("Contact", 1)
    doc.add_paragraph("Braznarkel  ·  braznarkel.com\nBeta (invite only): nextbox.braznarkel.com")

    doc.save(out)


if __name__ == "__main__":
    build_pptx(HERE / "Next-Box-Pitch.pptx")
    build_docx(HERE / "Next-Box-Product-Description.docx")
    print("wrote", HERE / "Next-Box-Pitch.pptx", "and", HERE / "Next-Box-Product-Description.docx")
