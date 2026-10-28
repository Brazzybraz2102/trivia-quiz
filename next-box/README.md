# Next Box

Print your to-do list on a thermal label printer. Mark it up with a pen, snap a photo, and the
changes go back to your list. Everything runs on your desktop: on your home network, or online
through your own Cloudflare Tunnel (see [Online](#online-at-nextboxbraznarkelcom-cloudflare-tunnel)).

**Printers:**
- Brother QL / PT
- ESC/POS receipt printers: Epson, Star and most 58/80 mm models
- ZPL label printers: Zebra and compatibles
- TSPL label printers: TSC, Munbyn, iDPRT, Xprinter
- any printer installed on the computer, through CUPS: Dymo, Rollo, USB printers

Bluetooth-only phone printers like Phomemo and Niimbot can't be used: they keep how they work private.

Load different **label colors** in different printers and send each kind of ticket to its color.
The default sends overdue tasks to red labels.

Everyone gets **My list**, built in, and can link **their own** to-do app if they like:
- **Todoist**, **Notion** (a to-do database), **Asana** and **monday.com**
- **any CalDAV task list**: Nextcloud Tasks, Fastmail, Synology, Zoho, Radicale, DAVx⁵, and older
  iCloud Reminders lists
- **Google Tasks**, **Microsoft To Do** and **TickTick**, after a one-time owner setup

Apple Reminders (current lists), Things, Any.do and Google Keep don't allow other apps to connect.
The full list, and calendars: [Which apps and calendars work](#which-apps-and-calendars-work).

## Install on the desktop (about 15 minutes)

You'll need:
- your label printer switched on and on the same Wi-Fi as the computer. You don't need its address:
  Next Box finds it for you (see step 5)
- an Anthropic API key, for Snap (marks and handwriting) and the AI helpers (console.anthropic.com →
  API keys). Without it Snap and the AI helpers are off and the app says so.

Everyone connects their own to-do app later, from the web app.

### 1. Get the code
```bash
cd ~/Desktop/"custom projects"
git clone -b claude/ticket-todoist-printer-nldz8a https://github.com/brazzybraz2102/trivia-quiz.git next-box-src
cp -r next-box-src/next-box ./next-box && rm -rf next-box-src
cd next-box
```

### 2. Install and fill in secrets
```bash
sudo apt install -y python3-venv wl-clipboard libnotify-bin   # Fedora: sudo dnf install python3 wl-clipboard libnotify
scripts/install.sh          # builds everything, makes your NEXTBOX_KEY, starts the service
.venv/bin/nextbox doctor    # checks it all and says what to fix
```
Next Box needs Python 3.11 or newer. Ubuntu 24.04 and Debian 12 have it. On Ubuntu 22.04 also run
`sudo apt install -y python3.11 python3.11-venv`: `install.sh` then uses that one.
Later, when you have it, put `ANTHROPIC_API_KEY` (for Snap and the AI helpers) in `.env` with
`nano .env`. When you're ready to print, delete the `NEXTBOX_DRY_RUN=1` line there and run
`systemctl --user restart nextbox`. Until then Next Box makes previews instead of printing.
Your printer is added in the web app (step 5). No label printer? Delete the `PRINTER_IP` line: everyone
can still **Print on paper**.

`install.sh` also adds iPhone (HEIC) photo support for Snap (the `heic` extra, `pillow-heif`). If
that can't be installed on your computer, it says so and carries on; people then send JPEGs
(on an iPhone: Settings app → Camera → Formats → Most Compatible).

### 3. Create your accounts
Keep running Next Box separate from using it: one admin account that always stays in charge, and
your everyday account.
```bash
.venv/bin/nextbox user add usersuperadmin superadmin   # the admin view
.venv/bin/nextbox user protect usersuperadmin          # it always stays a superadmin
.venv/bin/nextbox user add mike user                   # your everyday account
.venv/bin/nextbox user owner mike                      # Home Assistant prints Mike's list
```
Linking Todoist is optional: Settings → Linked app, or `nextbox connect mike todoist`.

### Let other people in to test
Sign in as `usersuperadmin` → **Admin → Invite people** → **Make invite**, and send them the
link or code. It works once. Tick "their own household" if they should get their own list and
printers instead of joining yours. From the computer: `.venv/bin/nextbox invite --beta --note "Sam"`.
People in other households can't print on your printer; they see previews until their household
adds its own.
For now, friends print from their own device: **Print on paper**, or **Save label image** for their label
printer's app, since this computer can't reach a printer at their home.

### Move to PostgreSQL (recommended for learning databases)
```bash
scripts/setup-postgres.sh
```
Installs the free PostgreSQL database server, copies everything over and switches Next Box to it.
See `DATABASE.md` lesson 13.

### 4. Check it without printing
```bash
.venv/bin/nextbox status                 # want "printer_reachable": true
.venv/bin/nextbox --dry-run today        # prints a PNG path; open it to see the label
xdg-open "$(.venv/bin/nextbox --dry-run --json today | python3 -c 'import sys,json;print(json.load(sys.stdin)["png"])')"
```

### 5. Add your printer, then the first real print
Sign in to the web app as the admin, then **Settings → Add a printer and follow the steps**:
1. **What kind of printer?** Tap Brother label printer (or another kind).
2. **Find your printer.** Next Box searches your Wi-Fi by itself and picks it (for example
   "Brother QL-1110NWB · 192.168.68.110"). Not found? Type its address: hold the printer's info
   button to print its settings page, or look for a name starting with BRW in your router's list.
3. **Which labels are in it?** A Brother says which roll is loaded ("Your printer says: 102 × 152 mm
   labels (4 × 6 in). Use this?"). Tap Yes, or pick the roll from the list.
4. **Name it.** "Label printer" is filled in for you. Tap **Save printer**.
5. **Try it.** It says if anything would stop a print (Always preview only, `NEXTBOX_DRY_RUN=1`,
   printing paused) and how to fix it, then **Print a test label** asks "Send this to the printer?".
   If no label comes out, it asks what happened and takes you back to the right step.

Then, from the computer:
```bash
.venv/bin/nextbox today
```
If it comes out faint, cramped or rotated, tell Claude and it'll tune `nextbox/render.py`.

### 6. Run it as a service (starts at boot)
```bash
scripts/install.sh          # already done in step 2 if you ran it there; safe to run again
```
The install script ends by printing the web app address, `http://<desktop-ip>:8787/app`. Open
that on any phone or laptop on your Wi-Fi and sign in. You stay signed in for 30 days per device.

The first account you create is the **superadmin**, which is you. After that, add people from
the web app (**Users → Add a person**; a household admin uses **Admin → Add someone**). They get a
one-time password and pick their own when they first sign in. Or hand out a code from
**Admin → Invite people**, and they join with it. There's no open sign-up.

| Role | Can do |
|---|---|
| user | print, read back, their own Settings, send and read anonymous feedback |
| admin | support: add people, reset passwords, turn accounts off/on, sign people out, mark beta testers, see activity |
| superadmin | debug: everything above, plus roles, per-user debug mode, diagnostics, the error log, debug bundles, pause all printing, auto-print switch, banner, and moderating the feedback board (reply, set a status, hide; the board never shows who wrote a post) |

If you're ever locked out of the web app, fix it from the desktop:
| Want | Run |
|---|---|
| Add someone | `nextbox user add <name> [user\|admin\|superadmin]` |
| Change a password (signs out their devices) | `nextbox user passwd <name>` |
| Change a role | `nextbox user role <name> admin` |
| Turn an account off/on | `nextbox user disable <name>` / `nextbox user enable <name>` |
| Remove someone | `nextbox user remove <name>` |
| See accounts | `nextbox user list` |

### Managing everyone (super admin)
The **Users** tab (superadmins only) lists every person in every household. Search, filter by
role or status, or tap a number at the top (Turned off, Beta testers, Had errors...). **Manage**
opens one person: change their email, role or household, turn them off, reset their password,
sign them out everywhere, reset their settings, unlink their to-do app, see their recent activity
and devices, or delete them (type their username to confirm). Tick several people to sign out,
turn off/on, or change beta for all of them at once. **Export CSV** downloads the list.

### Helping a beta tester
1. Admin tab: mark them **beta**. Everyone has a **Send feedback** button on every screen, guided
   or open-ended.
2. Problems show up on the **Feedback board** for everyone, without names. Set a status and reply
   publicly. If you need to know who sent a post (for example, to look at their errors), a
   superadmin taps **Who sent this?**. That's recorded in the audit log. The private details,
   including any errors the person chose to attach, live only in the `feedback_identities` table.
   **Activity** (Admin tab) shows what a person did.
3. For anything odd, a superadmin turns on **Debug** for that person, asks them to repeat it, then:
   - looks at the **Debug tab → Event log** (filter by their name)
   - or downloads their **Debug bundle** and hands it to Claude
4. To stop real prints while you investigate, use **Debug tab → Pause all printing**.

Everyone can link their own to-do app under **Settings → Linked app**. Tokens and passwords
are checked first, then stored encrypted. Each person sees and prints only their own tasks and
tickets. Home Assistant and the hotkey use the owner's app (the first superadmin, which is you).
The key that encrypts those secrets is `secret.key` in the data folder, so back up the whole
folder, not just `.env`.

### 7. Hotkey: copy text, press a key, it's a task with a ticket
```bash
scripts/hotkey.sh '<Super><Shift>t' --dry-run   # try it safely first
scripts/hotkey.sh '<Super><Shift>t'             # then for real
```

### 8. Home Assistant
1. Copy `ha/nextbox.yaml` to `<ha config>/packages/nextbox.yaml`, and make sure
   `configuration.yaml` has `homeassistant: { packages: !include_dir_named packages }`.
2. Add `nextbox_print_today_url: http://<desktop-ip>:8787/print/today` and `nextbox_key: ...` to `secrets.yaml`.
   (Moved Next Box to another PC? Only this address changes: see [Move Next Box to another PC](#move-next-box-to-another-pc).)
3. Replace `binary_sensor.CHANGEME_office_presence` and `input_button.CHANGEME_nextbox_button`
   with your entity IDs, then reload YAML.
4. Test that the guard works. The first call prints, the second returns `skipped`:
   ```bash
   curl -XPOST -H "X-NextBox-Key: $KEY" -H 'content-type: application/json' \
        -d '{"source":"auto","dry_run":true}' http://localhost:8787/print/today
   ```
   Run it twice. Dry-run and real auto prints are tracked separately, so this test doesn't use up
   the real print for the day.

## Printers and label colors
- **Adding printers (admins):** **Settings → Add a printer and follow the steps** (also on Get started
  and in **Admin → Printers**). Next Box searches your network for the printer (Bonjour), asks a Brother
  which roll is loaded, works out the print width itself, and ends with a test print that asks first.
  Nothing to type but a name. **Edit** on a printer runs the same steps again from "Which labels are in it?"
  (do that when you swap a roll). Searching and reading the roll only work for printers on the Next Box
  computer's own network, so they're for staff and Home's admins.
- **Advanced (type everything yourself):** the full form is still in **Admin → Printers**, for an address
  like `/dev/usb/lp0` or a CUPS printer name, a model the steps don't list, or a print width in dots.
  Use **Test (preview)** first, then **Test print**.
- **Brother label size:** the steps read it from the printer. To check it yourself, type the printer's
  address into a browser: its own web page shows **Media Type** (for example
  `102mm x 152mm / 4.0" x 6.0"` is the 4x6 shipping-label roll, "102 × 152 mm labels").
  The sticker on the roll says it too. If the Label size doesn't match the roll, a Brother stops
  with a flashing red light and stops answering: switch it off and on, then fix the Label size.
  On die-cut labels (fixed-size labels like 4x6 or 62 × 29) a short ticket fills the top of one
  label, one a little too long is shrunk to fit, and a longer one carries on onto the next labels,
  split between lines, each label marked "1/3", "2/3", ... with the ticket's #code, so you can snap
  any of them. The preview shows the labels as they'll come out.
- **Choosing colors (everyone):** **Settings → Printers & label colors** sets which color each
  kind of ticket uses: overdue, urgent, daily list, lists, single tasks, notes, new tasks.
  **Overdue tasks on their own ticket** splits today's list, so expired tasks come out on red.
- **No printer with that color loaded?** The ticket goes to your usual printer and the app tells
  you why.
- **Printing is the default:** "Preview only" starts unticked and every real print asks "Send this to the
  printer?" first; with no printer, Settings says **No printer added** and who can add one.
- **Brother QL with a black+red roll (DK-22251):** tick "Black + red roll" and overdue tags print
  in red ink.
- **Hardware testing so far:** only the Brother QL path has been used with a real printer. The
  other drivers are built from their published command sets and tested by checking the bytes they
  produce. Do a Test print when you add one.

## What you can see as the admin
Everyone sees a notice on their first sign-in, and their "I understand" is recorded:
- **The notice says:** you can see their tickets (including the tasks on them), read-backs,
  settings and activity.
- **Your view:** **Usage** (super admin) shows per-person activity, tickets by kind, read-back
  results, settings and label colors. **View tickets** shows the actual tickets.
- **What stays private:** to-do app passwords and tokens are never visible. Feedback stays
  anonymous; only "Who sent this?" reveals a sender, and that's audited.

The small mark at the bottom of every page shows the version and build. Change its wording with
`NEXTBOX_CREDIT` in `.env`.

## My list, stickers and ADHD helpers
Next Box has its own to-do list, so it works without Todoist. Open the web app on your phone:
**My list** is the first screen.

- **Just one thing** at the top: the one task to do now, with its next tiny step. Tap **Done ✓**
  (or **Step done ✓**), **→ Tomorrow**, **Print just this**, **Break it down**, **⏱ Start 5 min**,
  or **Not now** to see the next one.
- **Brain dump**: type everything on your mind, one per line. Shortcuts are optional:
  `#errand` tag, `!` urgent, `tomorrow` / `fri`, `3pm`, `~15m` how long, `every day`.
- **Edit** a task to add tiny steps (the first one should take two minutes), a time, a repeat or tags.
- **Stickers**: one small label per task, to stick on the bill, the door, the car keys.
  **Tear-off strips**: one label you cut into strips. Pick tasks with the boxes on the left, or
  pick nothing to print today's.
- **Tags & colors** (Settings): each tag has a color. Stickers go to the printer loaded with that
  color of labels. On plain white labels each color prints as its own pattern (red is solid black,
  yellow is dots, blue is stripes...), so you can still tell them apart at a glance.
- The **daily label** shows 5 tasks at most, the next tiny step under each, gentle wording and
  yesterday's wins. Change any of it under Settings → Focus & ADHD helpers.
- **Break it down** (on Just one thing, a task's Edit and each slug): an AI helper suggests 3-6 tiny
  first steps. Tick the ones you want, change the words, then Save (Undo is right there). A linked
  app's task isn't changed: add the steps to My list as a new task, or print them on a Just one thing
  card. **Look back on my week** (Journal) writes a short, kind note about your week; your journal
  notes go only if you tick "Include my journal notes", and nothing is kept unless you tap Save to my
  journal. Both need `ANTHROPIC_API_KEY` in `.env` and only run when tapped (20 an hour per person).
  Without the key their buttons don't show, and Look back on my week says what's missing (to a
  superadmin: how to add the key). Turn them off under Settings → Focus → AI helpers.

Linking Todoist or a CalDAV app (Settings) is optional: its tasks show up next to yours and stay
in that app.

Colored labels for the QL-1110NWB: it prints black only, so color comes from the roll. Brother
makes 62 mm yellow film tape (DK-22606). Other colors come from third-party 62 mm DK-compatible
rolls; check the listing says it fits the QL-1100/1110. Add each loaded roll as a printer (Settings → Add a printer) with its label color.

From the command line:
```bash
.venv/bin/nextbox add "Pay water bill #urgent today ~15m" "Call dentist fri 10am"
.venv/bin/nextbox --dry-run stickers          # today's tasks, one sticker each
.venv/bin/nextbox --dry-run strips '#errand'  # tear-off strips for a tag
.venv/bin/nextbox --dry-run focus             # Just one thing
.venv/bin/nextbox doctor                      # checks the setup and says what to fix
```

## Online at nextbox.braznarkel.com (Cloudflare Tunnel)
Testers open `https://nextbox.braznarkel.com` on their phones. A Cloudflare Tunnel carries it to
this computer; nothing on your router changes. The shared `NEXTBOX_KEY` (Home Assistant) only works
on the home network, the API explorer is off (`NEXTBOX_API_DOCS=1` turns it on), and sign-in limits
use each visitor's real address.

1. dash.cloudflare.com → **Zero Trust** → **Networks → Tunnels** → **Create a tunnel** →
   Cloudflared → name it `home-pc`.
2. Choose Debian, copy the **install and run** command it shows, and paste it into this computer's
   terminal. It installs `cloudflared` as a service that starts at boot. (The long token in that
   command is a secret: don't share it.)
3. **Public hostname**: subdomain `nextbox`, domain `braznarkel.com`, type `HTTP`, URL
   `localhost:8787` → Save.
4. In `.env` in your Next Box folder (`nano ~/.local/share/nextbox/app/.env`) add these two lines,
   then run `systemctl --user restart nextbox`:
   ```
   NEXTBOX_PUBLIC_URL=https://nextbox.braznarkel.com
   NEXTBOX_HOSTING=Cloudflare
   ```
   `NEXTBOX_HOSTING` names Cloudflare on the Privacy and About pages, since it now carries everyone's
   traffic.
5. Open `https://nextbox.braznarkel.com/app/` on your phone with Wi-Fi off.

Steps 2 and 4 in one go, after steps 1 and 3: `scripts/go-online.sh` installs `cloudflared`, asks
you to paste the command from step 2 (it's never shown), and adds both lines to `.env`. Then it checks
from the internet that `https://nextbox.braznarkel.com` really reaches Next Box, and if not, says what to
fix in the dashboard (also when this PC already runs another tunnel for something else).

Later projects: add another public hostname to the same tunnel (e.g. `next` → `localhost:9000`).

## Which apps and calendars work
| App | How people connect | Works today? |
|---|---|---|
| Next Box's own list | built in | yes |
| Todoist | paste an API token | yes |
| Notion (a to-do database) | paste an integration token + the database link | yes |
| Asana (tasks assigned to you) | paste a personal access token | yes |
| monday.com (items assigned to you, on boards with a person, a date and a status column) | paste a personal API token (optional: one board's link) | yes |
| Nextcloud, Fastmail, Synology, Zoho task lists | CalDAV address + app password | yes |
| Google Tasks | Sign in with Google | after the owner setup below |
| Microsoft To Do (Outlook tasks) | Sign in with Microsoft | after the owner setup below |
| TickTick | Sign in with TickTick | after the owner setup below |
| ClickUp, Trello | not yet: tap "Ask for it" under Settings → Linked app → Other apps | use My list |
| Apple Reminders, Things, Any.do, Google Keep | not possible: these apps don't let other apps in | use My list |

| Calendar | How | Works today? |
|---|---|---|
| Google Calendar, Outlook, iCloud, Proton, Yahoo, Fastmail... | paste the calendar's subscription (.ics) link | yes |
| iCloud, Fastmail, Nextcloud | CalDAV account (iCloud: app-specific password) | yes |
| Google Calendar, Outlook / Microsoft 365 | Sign in with Google / Microsoft | after the owner setup below |

Each person links one to-do app and up to 8 calendars (Settings). Calendars are read-only: today's
events show on My list and print in a SCHEDULE box at the top of the daily label.

### Sign-in apps: one-time owner setup
"Sign in with Google / Microsoft / TickTick" needs Next Box registered with each company (free)
and a public https address for the sign-in to return to, so do this after the Cloudflare address
works. In `.env` set `NEXTBOX_PUBLIC_URL=https://nextbox.<your domain>`, then for each one you want:

- **Google** (Tasks + Calendar): console.cloud.google.com → new project → enable *Google Tasks API*
  and *Google Calendar API* → OAuth consent screen (External, add your testers' emails as test
  users) → Credentials → OAuth client ID → Web application → Authorized redirect URI
  `https://nextbox.<your domain>/connect/google/callback`. Put the id and secret in `.env` as
  `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
- **Microsoft** (To Do + Outlook Calendar): entra.microsoft.com → App registrations → New
  registration → "Accounts in any organizational directory and personal Microsoft accounts" →
  Redirect URI (Web) `https://nextbox.<your domain>/connect/microsoft/callback` → Certificates &
  secrets → New client secret. `.env`: `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`.
- **TickTick**: developer.ticktick.com → Manage Apps → New App → OAuth redirect URL
  `https://nextbox.<your domain>/connect/ticktick/callback`. `.env`: `TICKTICK_CLIENT_ID`, `TICKTICK_CLIENT_SECRET`.

Then `systemctl --user restart nextbox` and `.venv/bin/nextbox doctor` shows which are on.

## Standard app pages
- **Privacy Policy** (`/app/privacy.html`), **Terms of Use** (`/app/terms.html`), **About and contact**
  (`/app/about.html`), **Help** (`/app/guide.html`), and a friendly "page not found". Linked from the
  sign-in page, the footer and the More menu. Plain-language drafts: have a lawyer review them before
  you charge money.
- Set who runs it and how to reach you in `.env`: `NEXTBOX_OPERATOR=Braznarkel` and
  `NEXTBOX_CONTACT=mike@braznarkel.com`.
- Joining needs "I agree to the Terms of Use and Privacy Policy" (recorded with the sign-up).
- On phones: a bottom tab bar (My list, Print, Snap, Journal, More) like other apps; an offline notice
  when the connection drops.

## Print on any printer
Every ticket, sticker set and card has a **Print on paper** button. It opens a PDF laid out on US
Letter, A4, or a journal page size, A5 or half letter (Settings → Printing), that people print the normal way from their phone or computer.
Stickers keep their real label size in a grid with cut lines, so full-sheet sticker paper (Avery
5165 / L7167 or similar) becomes a sheet of stickers. Lists and cards print 3.5 inches wide and
continue on the next page if they're long, breaking between tasks so each task stays with its box.
Next Box never sends these to a printer itself: the person prints them.
Next to it, **Save label image** downloads the label as a PNG (stickers: all of them in one tall
image) to open in a label printer's own app, like Brother iPrint&Label. It never prints anything.

## The journal (the daily routine)
Print today's label, stick it on the **left page** of a paper journal, work the list, and write
notes about the day on the **right page**. Then **Snap** both pages in one photo:
- the marks update your list, as always;
- the handwritten notes are typed out (AI vision) and saved as that day's **journal page**, with
  the photo, so a lost notebook is never a lost record;
- the **Journal** tab shows each day's pages, the week's **wins**, and **slugs**: tasks that keep
  sliding (moved 3+ times or waiting 7+ days). Slugs also print "slid 3×" on the daily label.

Journal mode (Settings → Focus, on by default) adds a "Today's win" line to the daily label and keeps
every snapped photo. With it off, a photo is kept only when it has handwriting (notes or a win), as
that day's journal page. Paper sizes include **A5** and **half letter** for printing the day's list on a
regular printer and pasting it in. Journal pages are private: admins can't read them, and `nextbox db
sql` hides their notes and photos.

Closing the loop:
- **Add lines to my list** (on a Snap result and on every journal page): the page's lines as
  checkboxes, with bullets, `[ ]` boxes and `todo:` lines ticked for you and the date, time and #tags
  Brain dump will read. Only the ticked lines are added.
- **Search your journal** (top of the Journal tab): every word, any order, accents and case ignored.
  Tap a result to open that day.
- **Move to another day** on a page (written after midnight about yesterday), with Undo.
- Each day shows **its ticket** (tap it to see it whole, or Print on paper) and what read-back
  **moved or dropped**.
- **Print my journal page** on a day makes a PDF of that day (photos, notes, wins) for any printer.
- **Download my journal** (Journal tab, Profile, or before deleting your account): a .zip with a folder
  for each day (notes.txt, the photos, wins.txt) or one printable PDF, for everything or the last
  7/30/90/365 days. Each page says when it was snapped or written, with the date when that was another
  day (a label snapped the next morning).

## Getting new people started
- **Quick start page:** `https://nextbox.braznarkel.com/app/guide.html` explains everything in 11 short
  sections. Send it with an invite. It's also linked from the sign-in page and Settings → Help.
- **Get started checklist:** the top of My list shows 9 steps that tick themselves off: brain dump,
  home screen, link an app, add a calendar, printer, first stickers, today's label, mark it and snap
  it, and focus settings (the 5 quick questions, if they were skipped). The optional ones can be skipped. Hide it with **Hide** and bring it back in Settings → Help.
- **Home screen:** Next Box has its own icon and opens full screen when added to the home screen.

## Everyday use
| Want | Do |
|---|---|
| Today's list | web app → Print today, or `nextbox today` |
| A project or filter | `nextbox list '#Groceries'` |
| One task | `nextbox task <id>` |
| A note | `nextbox text "Call Sam back"` |
| Clipboard → to-do app + ticket | your hotkey |
| Read back | Mark ✓ done, → tomorrow or ✗ drop, then web app → Snap → Take a photo → Read back. Deletes and anything uncertain wait for Confirm/Skip. CLI: `nextbox scan photo.jpg`, then `nextbox confirm <scan-id>` |
| Mark it your way | Settings → How you mark your list: Simple, Bullet journal, Paper planner, Fill the box, or your own symbols with your own words (labels print them, Snap reads them) |
| Notes → tasks | Snap result or a journal page → Add lines to my list → tick → Add |
| Add one task with a day, tags and steps | My list → + Add task (Brain dump shows what it understood as you type) |
| Not today | → on the task (or Just one thing → Tomorrow). Undo is in the message |
| Tiny steps | Edit → Tiny steps; tick them on the row ("steps 1/3") or Just one thing → Step done ✓ |
| Find a task | My list → Search my list, or tap a tag |
| Many at once | Pick tasks → ✓ Done, → Tomorrow, Tag… or Delete…. One Undo for all; a task that didn't change is named, with why |
| Skip a repeat once | Edit → Skip this time (or on Just one thing) |
| Find something you wrote | Journal → Search your journal |
| A copy of your journal | Journal → Download my journal (.zip or PDF); one day: Print my journal page |
| A task that won't start | Just one thing, Edit or a slug → Break it down → tick the steps → Save |
| How did my week go? | Journal → Look back on my week (tick "Include my journal notes" if you like) → Save to my journal |
| Fit Next Box to you | New accounts answer 5 quick questions after the notice; any time: Settings → Help → Run setup again |
| Your day in time order | My list → Schedule: calendar events, timed tasks, a Now line and "Next: … in 20 min" |
| Easier reading | Settings → General → Easy-to-read text (screen and printed labels) |
| No surprises | Settings → Focus → Same words every time (opens My list on Schedule) |
| Remember the routine | The card at the top of My list: Print today's label in the morning, Snap your pages in the evening (Settings → Focus → Routine card, and the times) |
| Get going on a task | Just one thing → ⏱ Start 5 min: a countdown with Pause and Stop, a soft chime, then Keep going / Done ✓ / Not now |
| Don't miss a timed task | A note at the top 10 minutes before, with Done, Tomorrow and Snooze (Settings → Focus → Remind me before timed tasks; a notification too if you like) |
| Add something from any screen | The + at the top (or press n) |
| Speech input, or keys pressed by accident | Settings → General → One-key shortcuts off (n, /, g, ? do nothing) |
| From the phone's home screen | Long-press the Next Box icon: Add a task, Snap a page, Print today. Share a photo or text to Next Box from any app |
| Keyboard | Press ? for the list: n add, / search, g then l, p, s or j to switch screens |
| History | web app → Printed, or `nextbox printed` |
| Sign out | web app → Account → Sign out |

## The database (and learning SQL)
Everything Next Box keeps is in one database: accounts, My list and tags, everyone's journal (with
its photos) and wins, printed tickets, scans, printers, feedback and activity. By default that's one
SQLite file, `~/.local/share/nextbox/nextbox.db` (or PostgreSQL, after `scripts/setup-postgres.sh`).
Linked apps' tasks are never kept there; they stay in that app. Back up the whole data folder,
`~/.local/share/nextbox`: it also holds `secret.key`, which unlocks the linked-app passwords.

```bash
.venv/bin/nextbox db where        # where the database is
.venv/bin/nextbox db tables       # what's in it
.venv/bin/nextbox db demo         # build a practice database full of fake data
.venv/bin/nextbox db sql --practice "SELECT username, role FROM users"
.venv/bin/nextbox db backup       # safe copy, even while the service runs
```

`DATABASE.md` is a step-by-step SQL course on the practice database. Queries on the real database
are read-only, and password hashes and to-do app secrets are always hidden.

## Status icon in the top bar
A small square in the desktop's top bar shows how Next Box is doing:

| Square | Means |
|---|---|
| Green | All good |
| Yellow | Online, but something needs fixing (a missing key, a printer, a slow or blipped public address, the tunnel's service off) |
| Red | Offline: the service has stopped, the page doesn't answer on this computer, or `NEXTBOX_PUBLIC_URL` didn't answer twice in a row |
| Grey | Still checking (just after you log in) |

Click it for what's wrong and the fix for each, **Open Next Box**, **Check now**, **Show details**
(`nextbox doctor` in a terminal) and **Quit status icon**. You get a pop-up only when it turns red
(also when it's red as you log in), and when it comes back. It checks every minute and runs
`nextbox doctor` every 10 minutes. It never prints a label, never calls the AI and never shows
anything from `.env`. It keeps no log of its own; if `scripts/install-tray.sh` started it, any
warnings from the desktop go to `~/.cache/nextbox-tray/tray.log`.

```bash
scripts/install-tray.sh            # add it (installs what it needs, starts it now and at every login)
scripts/install-tray.sh --remove   # take it away
```
Updates reach it by themselves (it restarts itself on the new version).
It moves with the app: run `scripts/install-tray.sh` on the new PC.
Without a `NEXTBOX_PUBLIC_URL` (home network only), only the checks on this computer decide green
or red. No square in the bar? Turn on **Ubuntu AppIndicators** in the Extensions app (the script
tries to do it for you).

## Update to a new version
A superadmin sees a banner in the web app when a newer version is published. To update, on the Next
Box computer run:
```bash
~/.local/share/nextbox/app/scripts/update.sh
```
It downloads the newest Next Box into the same folder, updates the packages in `.venv`, runs the
tests (about 5-10 minutes) and restarts the service. If a check fails, it says so and leaves Next Box
running as it was: run it again, and if it fails the same way, send Claude the lines it shows. It keeps `.env` (your settings and keys) and the
data folder (`~/.local/share/nextbox`: accounts, lists, journals, tickets and `secret.key`). Then
check it with `~/.local/share/nextbox/app/.venv/bin/nextbox doctor`, whose first line is the version
you now have (`nextbox --version` says it too).

## Move Next Box to another PC
For example from your desktop to the EMNet Server PC. Everything comes along in one locked file:
accounts, lists, journals, printers, settings (`.env`) and `secret.key`. Nothing is deleted on the old
PC until you say so, and you can undo it. About 20 minutes.

The new PC needs Linux with Python 3.11 or newer (Ubuntu 24.04 or Debian 12 is easiest), the same
network as your printer and Home Assistant, the same time zone as the old PC, and you logged in as
yourself (on its screen, or with ssh). Step 1 checks all of this before anything changes, except the
printer. Test that on the new PC with your printer's address:
`timeout 3 bash -c '</dev/tcp/192.168.68.110/9100' && echo printer OK || echo "printer NOT reachable"`.

### 1. On the new PC: get the code and check the PC
Paste this one line (on Fedora, use `sudo dnf install -y git python3` for the first part):
```bash
sudo apt install -y git python3-venv && rm -rf ~/nb-src && git clone --depth 1 -b claude/ticket-todoist-printer-nldz8a https://github.com/brazzybraz2102/trivia-quiz.git ~/nb-src && mkdir -p ~/next-box && cp -a ~/nb-src/next-box/. ~/next-box/ && rm -rf ~/nb-src && ~/next-box/scripts/move-in.sh --check
```
It also works when there's a `~/next-box` already (its `.env` and `.venv` are kept). It ends with
"This PC is ready" or says exactly what to fix, for example:
- Ubuntu 22.04's Python 3.10 is too old: `sudo apt install -y python3.11 python3.11-venv`.
- The clock is on UTC: `sudo timedatectl set-timezone America/New_York` (the old PC's time zone).
  Otherwise "today" and Home Assistant's once-a-day print would change day at the wrong hour.
- Port 8787 is used by another program: don't stop a program you don't know. Add `--port 8788` in step 3.

Nothing is changed by `--check`.

### 2. On the old PC: move out
```bash
~/.local/share/nextbox/app/scripts/update.sh && ~/.local/share/nextbox/app/scripts/move-out.sh
```
It gets the newest Next Box first, then says what will happen and asks you to type `MOVE`. Then:
- Next Box stops here and stays off, even after a restart or an update.
- You pick a **passphrase**: three or more random words (it suggests four). You type it again on the
  new PC. Write it down, but not on the USB stick.
- It makes one locked file in your home folder, like `~/nextbox-move-2026-10-27.nbmove`.
- It removes the status square and turns off the internet tunnel here (it may ask for your password).
  First it asks whether that tunnel also carries other websites from this PC. Most don't: press Enter. If
  it does, the tunnel stays on, and it tells you to delete only Next Box's address from it in the
  dashboard and give the new PC a tunnel of its own.
- It ends with **one line to paste on the new PC**.

Ran it twice by mistake? The second time it says Next Box already moved, and asks before making another file.

### 3. On the new PC: move in
Paste the line `move-out.sh` printed. It fetches the file from the old PC over the network (`scp`): it asks for
the old PC's password, and if it asks "Are you sure you want to continue connecting", type `yes`. (The
old PC needs SSH for that: there, `sudo apt install -y openssh-server`.)

With a USB stick instead: copy the file into the new PC's home folder, then:
```bash
~/next-box/scripts/move-in.sh ~/nextbox-move-2026-10-27.nbmove
```
It checks the file first (seconds), sets up Next Box's Python packages (a minute or two), asks for the
passphrase, puts everything in place, asks for this PC's password (so Next Box starts with the PC), runs
the checks (about 5-10 minutes) and ends with a question about the internet address and what's left to
do. Stay nearby until that last question. Nothing is changed until the
file is unlocked and checked.
- Run it again with the same file? It says "already in place" and changes nothing. If the first run
  stopped before the install finished, it finishes the setup instead.
- Already tried Next Box on the new PC? If it has no accounts, it's set aside by itself (also while it
  runs). If it has accounts, add `--replace`, but only if that was a test copy: what's there goes to
  `~/.local/share/nextbox/backups/before-move-...`. A Next Box that was used after the move file was
  made is never replaced, unless you add `--replace-newer`.
- Another port: add `--port 8788`. It goes into `.env`, and Home Assistant, phones and the Cloudflare
  public hostname use 8788 instead of 8787 (`move-in.sh` prints each one).
- Forgot the passphrase? Run `move-out.sh` on the old PC again: it makes a new file with a new passphrase.

### 4. The internet address (nextbox.braznarkel.com)
`move-in.sh` offers to do this. Open dash.cloudflare.com → **Zero Trust** → **Networks → Tunnels** →
your tunnel → **Configure**, and keep the page with its install command (Debian) open. Then on the new
PC run `scripts/go-online.sh` and paste the command when it asks (nothing shows; that's on purpose).
It ends by checking that `https://nextbox.braznarkel.com` reaches Next Box on the new PC. If the new PC
already runs another tunnel (for something else), it says how to add Next Box's address to that one.
The public hostname (`localhost:8787`) stays as it is. Only one PC may run the tunnel: with two,
Cloudflare splits visitors between them. That's why `move-out.sh` turned it off on the old PC.

### 5. Home Assistant
The key stays the same; only the address changes. `move-in.sh` prints the exact line for Home
Assistant's `secrets.yaml`, with the new PC's address:
```yaml
nextbox_print_today_url: http://<new PC's IP>:8787/print/today
```
Then restart Home Assistant. Today's once-a-day print moved too, and both PCs use the same time zone,
so it never prints twice in one day. Tip: give the new PC a fixed address (a DHCP reservation in your
router).

### 6. Phones and the status square
- Phones on Wi-Fi: open `http://<new PC's IP>:8787/app` and sign in as before (add it to the home
  screen again). Through `https://nextbox.braznarkel.com` nothing changes.
- The status square: on the new PC's desktop run `scripts/install-tray.sh`.
- Not in the move file: the label-image cache (the web app shows each ticket's picture from the
  database), `backups/`, the practice database (`nextbox db backup`, `nextbox db demo` make new ones)
  and the hotkey (`scripts/hotkey.sh`). A USB printer stays with the old PC: set it up on the new one.
- PostgreSQL on the old PC? The new PC starts on SQLite; `scripts/setup-postgres.sh` moves it over.

### Undo, or move back
- **Changed your mind before using the new PC:** if `move-in.sh` ran there, first switch it off there for
  good with `~/next-box/scripts/move-out.sh` (delete the file it makes). Then on the old PC:
  `~/.local/share/nextbox/app/scripts/move-out.sh --undo`. It turns back on exactly what
  `move-out.sh` turned off, with the lists from moving day.
- **Used the new PC already:** move back the same way, the other way round. On the new PC:
  `~/next-box/scripts/move-out.sh`. Then on the old PC:
  `~/.local/share/nextbox/app/scripts/move-in.sh <that file> --replace`. Next Box comes back on there,
  with the tunnel if it was there before.

### Get back what was set aside
`--replace` keeps what was on the PC in `~/.local/share/nextbox/backups/before-move-<date and time>/`.
To put it back:
```bash
systemctl --user stop nextbox
cp ~/.local/share/nextbox/backups/before-move-<date and time>/* ~/.local/share/nextbox/
systemctl --user start nextbox
```
If the move kept an `.env.before-move` in the Next Box folder, rename it back to `.env` before the start.

### After a week
When everything works on the new PC, run this on the **old** PC:
```bash
~/.local/share/nextbox/app/scripts/move-out.sh --forget
```
It only works on the PC Next Box moved away from: on any other PC it refuses and deletes nothing. It
asks you to type the PC's name, then deletes Next Box there for good: the old lists, the code, the
service, the status square, the hotkey and the move file. It offers to remove the tunnel program and,
if you used PostgreSQL there, Next Box's PostgreSQL databases. Last, delete the move file on the new PC
(`rm ~/nextbox-move-*.nbmove`) and on the USB stick.

## Troubleshooting
- **Start here:** `.venv/bin/nextbox doctor` says the version and checks the database, `NEXTBOX_KEY`,
  `ANTHROPIC_API_KEY`, accounts, service, web page, firewall, sign-in apps and printers (including a
  `PRINTER_IP` that's still the example address), and says what to fix.
- Snap or Break it down says it needs an AI key: put `ANTHROPIC_API_KEY` in `.env`, then
  `systemctl --user restart nextbox`.
- `printer_reachable: false`: check that the printer is on the same Wi-Fi and give it a DHCP reservation so its IP stays fixed.
- Service logs: `journalctl --user -u nextbox -f`
- Service won't start, with status 218 or "namespace" errors: your system doesn't allow the
  service's sandboxing. Delete the `ProtectSystem=`, `ReadWritePaths=` and `PrivateTmp=` lines in
  `~/.config/systemd/user/nextbox.service`, then run `systemctl --user daemon-reload`.
- "Connect your to-do app in Settings first": that person hasn't linked one yet (Settings → Linked app).
- Old tickets and scans are deleted after 90 days (`NEXTBOX_RETENTION_DAYS`). Run `nextbox prune` to clean up now.
- After changing code or `.env`: `systemctl --user restart nextbox`
- "Next Box was moved to another PC": this PC's copy stays off on purpose after `scripts/move-out.sh`
  (so Home Assistant and phones never get old lists). Use the new PC, or bring it back here with
  `scripts/move-out.sh --undo`.
