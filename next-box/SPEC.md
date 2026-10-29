# Next Box: spec

Each person's to-do app ↔ Brother QL-1110NWB label printer, running on Mike's desktop and reachable only on the home LAN.

## 1. Rules (these override everything else)
1. **Each task lives in exactly one list, and that list is its source of truth.** Everyone has a
   built-in list ("My list", the `tasks` table, §17). A linked app (Todoist, CalDAV) is optional;
   its tasks are shown and printed next to the built-in ones but never copied, cached or synced
   into the database. The database (§16) also keeps accounts, print history, ticket manifests
   (row → task ID), scan results, the auto-print guard, printers, tags, feedback, the activity log,
   journal pages (notes and photos, private to their owner, §25), wins (one row per finished task;
   text only for My list tasks), `task_slides` (task ids and counts, never text) and friends and the
   notes they send each other (private to the two of them, §34).
2. **Nothing prints unless Mike asks.** The one exception is the once-a-day `source="auto"` print
   of today's list when Home Assistant sees him enter the office. The server guards it
   (`Store.claim_auto`), so a second auto call that day returns `skipped`. There are no
   crons, no retries that reprint, and no other automatic prints.
3. **Deleting a task always needs explicit confirmation**, including when read-back finds one and
   on the built-in list (`DELETE /list/{id}` refuses without `confirm=true`).
4. **Dry-run for development**: `--dry-run`, `NEXTBOX_DRY_RUN=1`, or `dry_run: true` in the API.
5. **Server secrets live only in `.env`** (chmod 600). Each person's to-do app token or password is
   encrypted with `secret.key` (data dir, 600) in `users.connection`. Neither is ever shown, logged
   or put in a debug bundle. The data dir is 700.
6. **LAN only**: no cloud relay and no remote access. The server binds to 0.0.0.0:8787.
7. **Sign-in**: people sign in to the web app with local accounts that live only on the desktop.
   New accounts need a one-time invite code (`/auth/signup`); there's no open sign-up and no
   cloud identity provider. One account can be the **protected admin** (`nextbox user protect`):
   it always stays an active superadmin and only the computer's CLI can change that. Home Assistant and scripts use the
   shared `X-NextBox-Key` header instead, and that key never reaches admin features.
8. **Roles**: `user` < `admin` (support) < `superadmin` (debug). See §10.
9. **Privacy between people**: each person prints, sees and reads back only their own tickets
   and scans. Home Assistant and the CLI act as the **owner** (the oldest active superadmin)
   unless the CLI gets `--user`. Friends (§34) are the one deliberate, consented way to reach someone
   outside your household: a note goes only to a friend who accepted, and prints only when they tap Print.

## 2. Hardware
- Brother QL-1110NWB on Wi-Fi, raw TCP port 9100.
- 62 mm continuous tape (DK-22205), 696 px wide at 300 dpi. Labels are 1-bit and cut after each print.
  Die-cut rolls (e.g. the 4x6 DK-1241: 102 × 152 mm, 1164 px wide) work too: see §14. The DK-1247
  (103 × 164 mm) is a different size that Next Box doesn't offer yet.

## 3. Print types
| Type | CLI | API | Manifest |
|---|---|---|---|
| Today (+ overdue) | `nextbox today` | `POST /print/today {source}` | yes |
| Auto today (HA) | `nextbox today --auto` | `POST /print/today {"source":"auto"}` | yes |
| Any filter | `nextbox list '#Groceries'` | `POST /print/list {query,title}` | yes |
| One task | `nextbox task <id>` | `POST /print/task {task_id}` | yes (1 row) |
| Note | `nextbox text ...` | `POST /print/text {text}` | no |
| New task + ticket | `nextbox text --todo ...`, `nextbox clip --todo` | `POST /print/text {text,todo:true}` | yes |

The API also takes `"dry_run": true` on every print endpoint.

## 4. Label layout (today and list prints)
- **Header:** the big day name and the date, then the legend: the person's own marks (§32), which is
  `✓ done → move ✗ drop` on Simple, everyone's default.
- **Rows:** each row has a time column (a blank write-in line if the task has no time), the task
  text, and a checkbox on the right.
- **Urgent tasks** (p1/p2) are bold with a leading `!`.
- **Tags** after the text: `overdue Nd` and `↻` for recurring tasks.
- **Row order:**
  1. today's timed tasks, by time
  2. urgent tasks
  3. the rest of today
  4. overdue tasks, most recently due first
- **Overflow:** at most 10 rows. The rest are listed under "Also waiting (N)" and can't be marked.
- **Footer:** `#YYMMDD-XXXX` at the bottom left, where XXXX is 4 characters with no 0/O/1/I.
  This is the label ID that read-back uses to find the manifest.
- **Rows aren't numbered on paper.** Read-back counts the task rows from the top, and the manifest
  stores that same order.
- **Marks outside the legend** (for example ↑) come back below 0.7 confidence, so they always wait
  for confirmation.

## 5. Endpoints
`GET /health` · `GET /status` · `GET /tasks?query=` · `POST /print/{today,list,task,text}` ·
`GET /printed?limit=&offset=&since=` (`since`: local `YYYY-MM-DDTHH:MM[:SS]`) · `GET /printed/{id}/png[?download=1]` · `POST /scan` (multipart `photo`, optional `label_id`) ·
`GET /scans` · `GET /scan/{id}` · `POST /scan/{id}/confirm {decisions:{row:"confirm"|"skip"}}` ·
`/app` (web app) · `/docs` (OpenAPI).

Auth: `POST /auth/login {username,password}` sets an HttpOnly, SameSite=Lax (§20) `nextbox_session`
cookie that lasts 30 days. With `want_token: true` it returns a bearer token instead, for the
native app (`Authorization: Bearer ...`). `POST /auth/logout` ends the session and `GET /auth/me`
says who's signed in. Home Assistant and scripts send `X-NextBox-Key`. Keys are never accepted in
URLs. The key can send a photo (`POST /scan`) and list read-backs, but never sees journal notes or which
page a photo made; confirming, relabelling, retrying and undoing a read-back need a signed-in person.

Security details:
- Passwords are hashed with scrypt, and the `sessions` table only holds SHA-256 hashes of tokens.
- The SQLite database file is chmod 600.
- After 5 failed logins from one IP in 5 minutes, that IP gets 429.
- Changing or removing an account signs out all of its sessions.
- A cookie-authenticated write whose `Origin` is a different site is refused with 403.
- CORS doesn't allow credentials, so cookies only work on the same origin.

## 11. To-do app connections
| App | Status | How |
|---|---|---|
| Todoist | ready | API token |
| CalDAV task lists (Nextcloud, Fastmail, Synology, Zoho, Radicale, DAVx⁵, older iCloud lists) | ready | server URL, username, app password, optional single list |
| Google Tasks, Microsoft To Do, TickTick | next | OAuth: the owner registers Next Box once with each service (NEEDS MIKE) |
| Apple Reminders (current), Things, Any.do, Google Keep | not possible | no API for other apps |

Providers (`nextbox/providers/`) all implement `TaskProvider` and return Todoist-shaped tasks, so
printing and read-back don't care which app a person uses.

**Connecting:** `PUT /connection` (or `nextbox connect`) runs `check()` before saving, so a bad
token is never stored. `GET /connection` and `DELETE /connection`; `GET /providers` drives the
Settings form.

**Not connected:** every task action returns 409 "Connect your to-do app in Settings first".
`TODOIST_TOKEN` in `.env` still serves the owner only, as a migration path.

**CalDAV behavior:**
- "Today" means pending tasks due today or earlier.
- A list query is a list name, or "all".
- Completing a repeating task advances it to its next occurrence. We compute that ourselves,
  because the library's own mode is broken.
- Moving one day of a repeating task is refused with an explanation. Most apps store that kind
  of change differently.

**Todoist behavior:**
- Retries 429, 5xx and connection errors with backoff, reusing the same `X-Request-Id` so a write
  happens at most once.
- Task IDs are validated before they go into URLs.

## 12. Reliability and safety details
- **Printing:** one job at a time, via a thread lock plus a file lock in the data dir, shared by
  the CLI and the server.
- **Auto print:** if the printer is offline, it returns `skipped` *without* using up the day, so
  the next walk-in prints.
- **Read-back photos:**
  - turned upright from the phone's rotation data, shrunk to 2048 px on the long edge, re-encoded as JPEG
  - anything over 50 MP, or not an image, is refused with 415
  - the vision call times out after 90 s
- **Temporary passwords:** the server refuses everything except `/auth/*` until the person
  chooses their own password.
- **Login:** "account turned off" is shown only with the correct password. Every other failure
  is a plain 401 that counts toward the 5-in-5-minutes lockout.
- **Admins:** can read activity only for people they manage.
- **Sessions:** slide forward on use, so monthly-or-more users stay signed in.
- **Web security headers:** CSP with `frame-ancestors 'none'`, nosniff, no-referrer.
- **Retention:** tickets and scans older than `NEXTBOX_RETENTION_DAYS` (90) are pruned at startup
  and by `nextbox prune`.
- **Dependencies:** pinned in `constraints.txt` (python-zeroconf and ifaddr for Set up a printer's search, §14). The installer also upgrades pip and setuptools.
- **systemd:** `Restart=always` (but not after exit 78, moved to another PC, §33), `UMask=0077`,
  `ProtectSystem=strict` with write access only to the data dir.

## 14. Printers and label colors
**Drivers** (`nextbox/printer.py`) and how they connect:

| Driver | Connects by |
|---|---|
| `brother_ql` | network, or `usb://0x04f9:…`; optional black+red ink |
| `escpos` | GS v 0 raster, network or `/dev/usb/lp*` |
| `zpl` | ^GF graphic field over port 9100 |
| `tspl` | BITMAP, network or device |
| `cups` | `lp -d <queue> -o fit-to-page`: Dymo, Rollo, any printer with a driver |

**How a ticket is printed:**
- Tickets are drawn at 696 px, then `finalize()` fits them to each printer's dot width.
- Two-color Brother rolls keep red pixels; everything else is 1-bit.
- **Brother die-cut and round labels** (62 × 29, 102 × 152 "4x6", d24, ...) have one fixed size, and
  brother_ql refuses an image of any other. `printer.label_pages` fits each ticket to the label's
  printable dots from `brother_ql.labels` (never hardcoded): scaled to the label's width (aspect
  kept); a short ticket is padded with white below it, content at the top; one up to 30% too long
  (`SHRINK_ONTO_ONE`, as Print on paper does) is shrunk onto one label, centred, so its tick boxes
  and its #code stay together (an 8-row Today ticket on 4x6). A longer one is split over several
  labels between rows of white (`paper._break_at`: just below a row's dotted line when there is
  one), so a line of text or a checkbox is never cut in half, and white below the last line never
  costs a label. Each of those labels keeps a strip at its bottom edge for "n/N" (left) and the
  ticket's "#code" (right), so any one label can be snapped and read back on its own: `jobs._finish`
  tags each image with its code (`printer.with_code`), and it travels with the image to the preview
  and the send. A round label gets the whole ticket shrunk and centred inside its circle.
  The labels go to `convert()` as one job, one page each, so the printer feeds (and cuts) between
  them. The saved ticket image and the preview show the same labels, with cut marks between them.
  Continuous rolls are byte-for-byte as before.
- When a Brother printer doesn't take a job, the "didn't answer" note adds `printer.trouble_hint`:
  a flashing red light usually means the roll doesn't match the Label size (it names the one set),
  and switching the printer off and on clears it. Admin → Printers says where to find the roll:
  the printer's own web page (Media Type) or the roll's sticker.
- Every send goes through `_send_raster`, one job at a time.
- Only the Brother path is hardware-tested. The others are tested at the byte level, plus a real
  TCP send and a stand-in `lp`.

**Printer list:** the `printers` table, managed by admins. Each printer has a name, driver, address,
width and dpi, plus the **label color loaded** and ink. Addresses must be one of:
- host[:port]
- `/dev/usb/lp*`, `/dev/lp*`, `ttyUSB`, `ttyACM` or `rfcomm`
- `usb://0xVVVV:0xPPPP`, for Brother only
- a CUPS queue name

Anything else is refused. On the first run after upgrading, `PRINTER_IP` becomes the first printer,
unless it's still the .env.example address (`printers.EXAMPLE_PRINTER_IP`, 192.168.1.50), which is never
added. A first printer an older version seeded from that example address takes the real `PRINTER_IP` once
it's set. `nextbox doctor` calls the example address what it is (not a printer that doesn't answer), and a
superadmin on a preview-only Next Box (`NEXTBOX_DRY_RUN=1`) is told how to leave it.

**Set up a printer** (the walkthrough, `app.js` `openPrinterSetup`) is how admins add a printer. It opens from
Settings → Printers & label colors (**Add a printer**: "No printer added", and also once there are printers), the Get
started printer step and Admin → Printers (**Add a printer**). **Edit** on a printer opens it again at step 3 with its
values. Admin → Printers keeps the full form, collapsed, as **Advanced (type everything yourself)** (also linked from
step 1, and from step 3 when editing). Who may add printers is unchanged (admins and staff; `_restricted` outside Home).
One decision per screen in a dialog, phone-first, with Back and Next:
1. **What kind of printer?** Brother label printer (QL / PT); another label printer (then which one: Zebra → `zpl`;
   TSC, Munbyn, iDPRT, Xprinter → `tspl`; a receipt printer → `escpos`; Dymo, Rollo or one set up on this computer →
   `cups`); a regular printer (no setup: it explains Print on paper, §21, and finishes); I don't have one yet (the
   same, and where to come back). Finishing those from Get started puts the printer step away.
2. **Find your printer.** On arrival it runs `POST /admin/printers/discover` (and **Search again**): Bonjour (mDNS)
   browsing for `_pdl-datastream._tcp`, `_printer._tcp` and `_ipp._tcp` (`nextbox/discover.py`), with python-zeroconf
   (pinned), or `avahi-browse -r -t -p -k` when zeroconf isn't installed, and "Next Box can't search the network on
   this computer…" when neither is there. About 4 s (`TIME_LIMIT`); one search at a time; it only listens. Each printer
   is listed once (its services merged by IPv4 address; link-local addresses left out), label printers first, with
   the model from its TXT records (`usb_MDL`, else `ty`, else `product`): "Brother QL-1110NWB · 192.168.68.110 · on
   your network · BRW5413795FE5CF.local". The printers that fit the kind picked in step 1 are listed to pick from,
   and exactly one that fits is picked for you; otherwise the person picks. The rest are under **Other printers on
   your network**, never picked for you: one that can't be that kind is shown greyed out and can't be picked (for a
   Brother, anything but a Brother; for the other kinds, a Brother), and an unknown model can still be picked for
   the other kinds. So a Brother that's asleep never leaves the HP or the TV picked in its place.
   **Type its address instead** says how to find it: the printer's settings page (hold its info button), the router's
   list of devices, a Brother's name starting with BRW. A CUPS printer asks for its name on this computer instead.
3. **Which labels are in it?** For a Brother on the network, `GET /admin/printers/media?address=…` reads the printer's
   own status page (`http://<printer>/general/status.html`, else `/`): its **Media Type** (and the model, if the page
   names one) mapped to a `BROTHER_LABELS` id by `discover.media_label` ("102mm x 152mm" → `102x152`, "62mm" → `62`,
   "62mm x 100mm" → `62x100`, "29mm x 90mm" → `29x90`, the DK codes too; a size Next Box doesn't offer, like the
   DK-1247's 103 × 164 mm, is said plainly). "Your printer says: 102 × 152 mm labels (4 × 6 in). Use this?" Yes, or
   **No, let me pick**: the common rolls first (`printer.COMMON_ROLLS`, with their DK codes), the rest under **More
   sizes**, only those that fit the model (a P-touch asks "Which tape is in it?" and lists its tapes straight away).
   The model is asked only when neither the search nor the page said it.
   The read is one plain HTTP GET with a 3 s limit for all of it (`MEDIA_TIMEOUT`, from connecting to the last byte,
   so a page that answers slowly or a byte at a time is cut off), a 256 KB cap, no proxy and no redirects, and the
   page is read in one linear pass (`discover.page_text`: a page of unclosed tags can't hold up the server). Only to
   an address that passes `printers.validate` and is on the home network (private; never loopback, link-local,
   multicast or this computer).
   Other kinds: the label size from common choices (4 × 6 in, …, one long roll) or typed in mm or inches with the
   dpi, or the receipt paper width (80 mm → 576 dots, 58 mm → 384); the dots are worked out. Nobody types dots.
4. **Name it.** "Label printer" for a first printer, else the model. The label color (white by default, with one line
   on what color is for) shows with several printers and is under **More** for a first one. A first printer becomes
   the admin's usual printer (`default_printer`); later ones offer the tick. **Save printer** saves it here, so the
   test print uses it.
5. **Try it.** First what would stop a real print, fixed in place where it can be: **Always preview only** (**Turn it
   off**), printing paused (a superadmin can **Turn printing back on**; others are told who can), `NEXTBOX_DRY_RUN=1`
   (the owner is told to delete that line and run `systemctl --user restart nextbox`; `.env` is never shown). Then
   **Print a test label**, a real print only on that tap after "Send this to the printer?"
   (`POST /admin/printers/{id}/test?dry_run=false`), and "Did a label come out?" Yes: **All set**, with **Print
   today's label** (it opens Print; that button asks first too). No: what happened? A flashing red light (the roll may
   not match: back to step 3), nothing happened (asleep, off or the wrong address: back to step 2), or it printed but
   looks wrong (back to step 3). From there, Save changes and Try it again.
Both lookups are for staff and admins of Home only (the Next Box computer's network is Home's): anyone else gets 403
with `PRINT_AWAY`, and a household admin outside Home types the address. Both are audited (`printer_discover` with
how many were found, `printer_media` with the label), never with the address. Each step's heading gets focus; Esc,
Close or the back gesture ask first when something was picked and not saved; it works at 320 px with Extra large
text, in light and dark.

**Routing:** every ticket has a reason: overdue, urgent, today (daily list), list, task, note or
new_task. Each person's `color_rules` maps reasons to label colors (default: overdue → red), and
`choose()` sends the ticket to a printer with that color loaded. If none has it, the ticket goes
to their usual printer with a note.

**Overdue on their own ticket:** with `split_overdue` on, today's list prints overdue tasks as a
separate "Overdue" ticket. One auto print can therefore produce two tickets; it's still one
guarded auto print.

**Printing or a preview (the app's "Preview only" box):** printing is the default. The box starts
unticked on every device and for every person (no `checked` in index.html, no tick in `loadPrefs`;
signing in, or signing in as someone else, starts from unticked). `applyDry()` ticks and locks it,
with the reason next to it, whenever a real print can't happen anyway:
- printing paused by an admin;
- a household outside Home with no printer (`PRINT_AWAY`, §19);
- the person's own **Always preview only** (Settings → Printing): "Always preview only is on in Settings."
  (the ticket's note says the same, `jobs.ALWAYS_PREVIEW_NOTE`);
- `NEXTBOX_DRY_RUN=1` (a superadmin is told how to leave it);
- **no printer in the household** (`GET /status` `printer` null, `printers` 0): "No printer added: print
  previews on paper.", plus " Add one in Admin → Printers." for admins and staff (`can_add_printer`).

`GET /auth/me` carries the same flags as `GET /status` (`printing_paused`, `prints_here`, `dry_run_forced`,
`printers`), so the box is ticked and locked from the moment someone signs in or the page loads; `/status`,
which waits up to 2 s on whether the printer is online, is the newer source once it answers. There's no
moment where the box is unticked and a print would only be a preview.

When the reason goes away (a printer is added, the setting is turned off) the box is unticked again; a tick
the person made themselves stays. A real print always asks "Send this to the printer?" first, on every
print button. Nothing about this prints by itself.

**No printer added:** with no printer in a household that prints here (Home), the header says "No printer
added · print on paper" (a household outside Home keeps "Previews · print from your device"), and Settings
→ Printers & label colors says **No printer added.** with the next step: "Add one in Admin → Printers." and
an **Add a printer** button (admins and staff), or "Ask your Next Box admin to add one." (everyone else).
Outside Home the next step is `PRINT_AWAY`, once. Your usual printer and the color rules are hidden until
there's a printer. The Get started printer step (`GET /setup` `printers`) and a ticket's note (`jobs.NO_PRINTER`,
`NO_PRINTER_ADMIN`) say the same.

The setup questions never turn on Always preview only (§29). They used to, for "Regular printer" and "No
printer yet", so `upkeep.preview_default` (startup, once: `_upkeep:preview-default-v1`) turns it off for
everyone whose `always_dry_run` is on with `print_with` paper or none. Anyone else's own choice stays.

## 15. Admin visibility (everyone agreed)
- **The notice:** at first sign-in everyone sees `DATA_NOTICE` and taps "I understand". It's
  recorded as `consent: {version, at}`. Bump `DATA_NOTICE_VERSION` if what's visible changes.
  Until they agree or tap "No thanks, sign me out", nothing closes it: Esc does nothing (`closedby="none"`,
  and Esc is stopped as a key for browsers without it), and if it's closed any other way while still
  unanswered, it opens again. `closedby="none"` also means the notice doesn't catch the phone's back
  gesture: that goes back as on any page (out of Next Box), and the notice is there again next time.
- **The superadmin can see:**
  - `/super/usage`: per-person activity, tickets by reason, read-back outcomes, settings, last
    active, notice date, and a daily chart
  - `/super/users/{name}/tickets`: everyone's ticket images and scans
- **Still never visible:** to-do app secrets. Feedback identity stays in
  `feedback_identities` and is never part of usage. Journals (§25), and friends and their notes (§34):
  no admin or superadmin endpoint reads them, and a printed note is never a ticket.
- **Revision mark:** `/version` returns version, git build and `NEXTBOX_CREDIT`. It's shown at
  the bottom of every page.

## 13. Feedback: public, anonymous, one identity file
**Where people can give feedback:** a Feedback button on every screen for everyone, plus a link on
the sign-in page. There are two modes:
- **Guided:** what kind (something broke / confusing / idea / love it / other), an optional 1–5
  rating, then "what were you trying to do", "what happened" and "what did you expect".
- **Just tell us:** one open box.

The page it came from is recorded automatically. Rate limit: 10 posts an hour per person, or per
IP address before sign-in.

**Closing the sheet:** with something typed, Esc (or the phone's back gesture) asks "Close without
sending?"; Cancel keeps it open, however many times, and OK clears what was typed. Closed with Close
before Send, the words are still there the next time it opens (until the page reloads). After a send
it starts fresh. Signing out clears it.

**The board (Feedback tab):** everyone who's signed in sees all feedback without names. Each post
shows its kind, status, rating, the page and the **day** only (exact times could be matched
against other activity). The author sees a "yours" badge and can withdraw their own posts.

**What's removed before saving:**
- emails, links, IP addresses and phone numbers
- @handles and long tokens
- every account's username, plus the sender's own

Admin replies are scrubbed the same way.

**The `feedback_identities` table is the only place a person is linked to their feedback.** It
holds the sender's username (or nothing before sign-in), exact time, IP address, device, and any
error details they chose to attach.

Nothing else records who sent what:
- feedback is never written to `events`, and older versions' feedback events are moved out
  automatically
- the server's request log is off
- debug bundles leave feedback out
- deleting an account removes its identity entries; its posts stay, unlinked

**Who can do what:**
- **Superadmins** moderate: set a status (new / seen / planned / fixed / won't fix / hidden), reply
  publicly, or remove a post. Hidden posts are seen only by superadmins and the author. The board
  never shows who wrote anything. Admins and everyone else read and post like any user.
- **Superadmins** can also look up who sent a specific post ("Who sent this?"). Each lookup is
  written to the audit log by feedback ID only, so the audit log never names the sender.

**Limit:** scrubbing can't catch everything, such as a stranger's name or a street address. The
form asks people to leave personal details out, and a superadmin can hide any post that slips through.

## 10. Roles, settings, support and debugging
**Everyone** (Settings tab):
- **Account:** change password (signs out other devices), list signed-in devices, sign out
  other devices.
- **Printing:** always preview only; rows per ticket (1–20); show "Also waiting"; 24-hour
  times; default list filter.
- **Read-back:** apply confident marks automatically, on or off; confidence needed (50–95%).
  Drops always wait for confirmation, whatever these say.
- **How you mark your list:** a preset or their own symbols, meanings and actions (§32).
- **Theme:** per device. **Feedback:** see §13.

**Admin** (support):
- add people with a one-time temporary password (the person must choose their own at first sign-in)
- reset passwords the same way
- turn accounts off or on
- sign people out
- mark beta testers
- read a person's activity, without tracebacks

Admins only manage plain users, and can't change roles or debug mode.

**Superadmin** (debugging): everything an admin can do, on anyone but themselves, plus:
- change roles
- per-user **debug mode**: that person's events also record request arguments and results
- delete people
- **diagnostics**: printer, Todoist and Claude key checks, counts, storage, version, redacted config
- the full **event log**: activity, errors with tracebacks, audit and auth (never feedback, §13)
- a one-click **debug bundle** per person: profile, sessions, events, their tickets and scans with the raw read-back
- **server switches**: pause all printing (every print becomes a preview), Home Assistant auto
  print on or off, and an announcement banner

**Guardrails:**
- The last active superadmin can't be demoted, turned off or deleted.
- Nobody can change their own role or turn themselves off.
- Every admin action is written to the audit log.
- There's no "log in as user"; the debug bundle covers that need without the risk.
- Events and bundles never contain passwords, hashes, tokens or `.env` values.
- Tracebacks are visible to superadmins only.

**Recovery from the desktop:** `nextbox user add|passwd|role|enable|disable|remove|list`. The
first account ever created becomes the superadmin.

Storage: the `users` (holds prefs), `sessions`, `events` (kept 180 days) and `server_settings`
tables. See §16.

## 8. Photo read-back (Phase 2)
1. `POST /scan` with a photo. Claude vision (`NEXTBOX_VISION_MODEL`, default `claude-sonnet-5-5`)
   returns structured output: the label code and, for each row, a mark (done, tomorrow, drop, note or
   none), which of the person's marks it was (`which`) and a confidence score. The prompt lists that
   person's marks (§32); the person's mark decides what it does. Simple is asked exactly as before mark
   styles (`scan.PROMPT` and the same output format: no `which`, no "note"); other marks may only report
   the actions their marks have (`scan.reply_format`).
2. The server finds the label by `label_id`, falling back to the code it read. If neither matches,
   nothing changes. With no code, the result offers the person's 5 latest tickets with tasks to pick
   from (`candidates`, today's daily label first; notes and other tickets without tasks are skipped,
   looking back up to 500 tickets), "We couldn't read the ticket code. Pick the ticket below." With no
   ticket with tasks at all it says so: "We saw ticked boxes, but…" only when a ✓ was read, else "We saw
   a to-do label, but…".
3. Rows whose mark finishes the task (done, ✓ on Simple) or moves it (tomorrow, →) with confidence ≥ 0.7 are
   applied right away: `close`, or `due_date` = tomorrow. It sets `due_date`, never `due_string`, so recurring
   rules survive. A "note" mark changes nothing: the row is listed in `noted` with the person's symbol and words.
4. **Every drop (✗ on Simple), every row below 0.7**, and every row whose `which` and mark disagree (or whose
   action none of the person's marks has) goes to `needs_confirmation`. A disagreement waits under whichever
   of the two finishes or moves the task (the number's first), noted "unclear mark": it never becomes a drop,
   or quietly a note, that only one of them said. A note and a drop that disagree, or a note that isn't one of
   their marks (any "note" on Simple), change nothing: `ignored` with reason "unclear mark". The app shows
   Confirm/Skip for each one, and `POST /scan/{id}/confirm` applies only rows still `pending`,
   so a double tap can't delete twice.
5. Row numbers that aren't on the label are ignored and reported.
6. iPhone HEIC photos open when the optional `heic` extra (`pillow-heif`, pinned in constraints.txt) is
   installed; `scripts/install.sh` adds it when it can, and `nextbox doctor` notes when it's missing.
   Without it a HEIC photo gets 415 with `scan.HEIC_HELP` (the iPhone Settings app → Camera → Formats →
   Most Compatible, or share it as a JPEG).
7. Without an AI key Snap can't read anything: see §27 for the 503 and `scan_ready`.

## 9. Phase checklist
### Phase A: desktop
- [x] A1 service, venv, pytest green (97 tests)
- [x] Web sign-in: local accounts, 30-day sessions, bearer tokens for native apps
- [ ] Sign-in: `nextbox user add mike` on the desktop (NEEDS MIKE)
- [x] Settings, admin (support) and superadmin (debug) tools, §10 (19 tests)
- [x] Per-person to-do app connections: Todoist and CalDAV, encrypted secrets, per-person tickets (§11)
- [x] Audit fixes (§12)
- [x] Anonymous feedback on every page, guided and open, public board, single identity file (§13)
- [x] Any thermal printer, label colors by reason, overdue on red, black+red ink (§14)
- [x] Usage view + recorded data notice; revision mark (§15)
- [ ] Real-hardware test of the ESC/POS, ZPL, TSPL and CUPS drivers (NEEDS MIKE: a test print on each)
- [ ] Google Tasks / Microsoft To Do / TickTick (NEEDS MIKE: register Next Box once with each)
- [ ] HTTPS on the LAN via Tailscale; nightly data-dir backup
- [ ] A1 `.env` filled on the desktop (NEEDS MIKE)
- [ ] A2 `nextbox status` → dry-run today → first real print → layout tuning
- [x] A3 server changes: CORS, web app at `/app`, multipart `/scan`, python-multipart
- [x] A4 `/scan` + confirm + mocked-vision tests
- [ ] A4 live read-back with a real photo; confirm the Todoist `due_date` update keeps recurrence
- [x] A5 web app (plain HTML/JS in `web/`, served at `/app`)
- [ ] A5 systemd user service + linger installed (`scripts/install.sh`)
- [ ] A5 GNOME hotkey bound (`scripts/hotkey.sh`)
- [ ] A6 HA entity IDs filled; auto fired twice, second call `skipped`
- [ ] A7 demo checklist passed
- [x] Move to another PC: one locked file, a guard on the old PC (§33)
- [ ] Move to the EMNet Server PC for real (NEEDS MIKE: README "Move Next Box to another PC")
### Phase B: phone apps (Expo): not started
- [ ] B1–B5. The Expo app from `ticket-app.zip` isn't in this repo yet. Its `src/api.ts` should call
      the endpoints above; the Read back screen maps onto `/scan` + `/scan/{id}/confirm`.

## 16. Database
Everything Next Box keeps is in one database (`nextbox/db.py`, SQLAlchemy Core):
- **SQLite** by default: `<data dir>/nextbox.db` (`~/.local/share/nextbox/nextbox.db`), WAL mode,
  chmod 600. Nothing to install.
- **PostgreSQL** when `DATABASE_URL` is set (`pip install -e '.[postgres]'`). Same code, same tables.

Tables (21): households, users, sessions, invites, tickets (record JSON + PNG), scans, auto_guard,
events, server_settings, printers, feedback, feedback_identities, tasks (My list, §17), tags,
journal (§25), wins (§25), task_slides (§25), friend_codes, friendships, blocks and friend_notes (§34).
`nextbox db diagram` draws them.

**Learning kit** (`nextbox/dbtools.py`, tutorial in `DATABASE.md`):
- `nextbox db demo` builds a separate practice database (`<data dir>/practice/nextbox.db`) full
  of fake households, people, tickets and events.
- `nextbox db sql "..."` is read-only on the real database. Writes need `--practice --write`.
  Without `--write` it runs one statement at a time (SQLite refuses a second one; on PostgreSQL the query
  is sent with the extended protocol, in a psycopg pipeline, so `SELECT 1; COMMIT; UPDATE ...` is refused
  before any of it runs, whatever psycopg's `prepare_threshold` is: None for PgBouncer turns preparing off.
  Without pipelines (libpq before 14) it's a prepared statement, and with preparing off too nothing runs).
- Output always hides password hashes, salts, session hashes and to-do app secrets, and what journal pages
  and friends' notes say (`friend_notes.body`, `image`, `reaction`).
- `nextbox db backup` makes an online copy in `<data dir>/backups/` (600).

## 17. My list, tags and ADHD support
**My list** (`nextbox/mylist.py`): every person's built-in to-do list. Built-in task ids look like
`nb:12`; linked apps' ids are used as they are. `Merged` sends each action (done, move, delete) to
the list the task came from. New tasks go to My list unless the `new_tasks_to` pref says `linked`.
Repeating tasks (daily, weekdays, weekly, monthly) jump to the next date after today when done, so
they never pile up as overdue. The app's Done sends the day the screen showed (`POST /list/{id}/done
{due}`): a repeat that has moved on since (Done on another device) isn't done again, so it never skips
its next day or counts a second win. Without `due` (the CLI, Home Assistant) Done finishes the current day.
A Done that changes nothing (done, skipped or moved on another device since that screen loaded) replies
`{undo: null, stale: true}`: the app says "That changed on another device. Here's your list now." and
draws the list again, never "Done ✓", and bulk Done counts only the ones it finished ("Done 1 ✓ · 1 changed
on another device").

**Brain dump**: one task per line. Understands `#tag`, `!` / `!1` `!2` `!3`, `today` `tomorrow`
`fri` `2026-10-09`, `3pm` `15:30`, `~15m` `~1h`, `every day|weekday|week|month`.

**Tags** have a color (red, orange, yellow, green, blue, pink, purple, white). Todoist labels and
CalDAV categories with the same name get the same color. A new person starts with urgent (red),
errand (yellow), call (blue), home (green) and work (purple).

**Color on thermal labels**: thermal printers print black, so a tag's color does two things:
- **Routing**: stickers go to the printer whose loaded roll matches their tag color (one job per
  color), else the person's usual printer.
- **Pattern**: on any roll the tag prints as a patterned block with its name: red solid (white
  letters), orange diagonal, yellow dots, green horizontal stripes, blue vertical stripes, pink
  checker, purple crosshatch, white outline. Patterns stay readable on plain white labels.
Only the Brother QL-800/810W/820NWB print red ink; `printers.validate` refuses black+red for
other models (the QL-1110NWB prints black only).

**Print types** (all manual, all through `jobs._finish`):
- **Stickers**: one task per small label, cut apart, to stick where the task happens. Each shows
  its tag band, the task big, when, how long, the next tiny step, a numbered box and the code.
  "When · how long" sits beside the code with a clear gap, or on its own line above it when it would
  come closer (big stickers, "Mon Oct 19 12:45p ↻  ·  ~1h"), so nothing is printed over the code Snap reads.
- **Tear-off strips**: one label, one strip per task with dashed cut lines.
- **Just one thing**: one task, huge, with its tiny steps (every open one, up to 12) and "start by" time
  (due time minus the estimate; the when · about · start by line only breaks between those bits). Picks
  the first task today's ticket would list unless one is chosen. The TASK ticket lists every open step too.
Read-back works on all of them: each box has its printed row number. On every one, as on the daily label,
an urgent task (p1/p2) starts with `!` and a repeating one has `↻` after when it's due ("today ↻").

**Daily ticket for ADHD** (all prefs, all on by default): at most 5 tasks (`max_rows`, the rest
listed small under "Not today"), gentle words ("waiting 3d" not "overdue 3d"), the next tiny step
under each task, a tag swatch by each box, and "Yesterday you finished N. ✓".

## 18. Super admin panel (Users tab)
Superadmins only (`super_only`). Shows every person in every household:
- Overview tiles: people, active this week, turned off, beta, debug, temp password, had errors,
  households, prints (30 days), open tasks. Tap a tile to filter.
- Search by name, email or household; filter by role, status and household.
- Per person (Manage): email, role, household, turned off, beta, debug, must pick a new password
  (signs them out); reset password (one-time password shown once), sign out all devices, reset
  settings to defaults, unlink the to-do app (their built-in list stays), debug bundle, recent
  activity, devices and tickets; delete after typing the username.
- Bulk: sign out, turn off, turn on, beta on/off for everyone selected. Turn off and sign out skip the caller ("that's you").
- New household; add a person into any household; CSV export (no secrets; cells that start with
  `= + - @` are prefixed with `'` so spreadsheets don't run them).
Open task *counts* are shown, never task text. A superadmin can't turn off or demote themselves
here, and the last superadmin can't be demoted or turned off. Every change is an audit event.
Endpoints: `GET /super/overview`, `GET /super/users`, `GET /super/users.csv`,
`GET|PATCH /super/users/{u}`, `POST /super/users`, `POST /super/users/{u}/reset-settings`,
`POST /super/users/{u}/unlink`, `POST /super/users/bulk`, `GET|POST /super/households`.

## 19. Accounts, invites and the database server
- **Protected admin**: `nextbox user protect <name>` (must be an active superadmin). Nobody can
  demote, turn off or delete it from the web app or the normal CLI commands; `nextbox user
  unprotect` (on the computer) lifts it.
- **Who Home Assistant prints for**: `nextbox user owner <name>` (stored as `_print_owner`).
  Default: the oldest active superadmin. Falls back to that if the chosen person is turned off.
- **Invites**: admins make one-time codes in Admin → Invite people, or `nextbox invite`.
  Options: beta tester, works 1/7/30 days, and (superadmin only) join a chosen household or get
  their own household (they become its admin). Household admins invite into their own household
  only. Wrong codes count toward the same 5-tries-per-5-minutes lockout as sign-in.
- **The .env printer is the Home household's.** People in other households get previews until
  their household adds a printer, so a tester can never print on Mike's printer by accident.
- **Printing away from home (this test version).** Next Box runs on one computer, so it reaches only
  printers on that home's network. `prints_here` (on `GET /auth/me` and `GET /status`) is true for Home,
  and for any household with at least one printer (one living at that house); otherwise false. When it's
  false, every printing message says the same thing, `jobs.PRINT_AWAY` (the app's `PRINT_AWAY` is a copy):
  the ticket result's note, the Preview only line, Settings → Printers & label colors, the Get started
  printer step, the setup questions' label-printer answer and the header ("Previews · print from your
  device"). Household admins outside Home see it in Admin → Printers too, above the collapsed add-printer
  form and "Only add a printer here if it's on the same network as the Next Box computer." Staff and Home
  see the card as before. Who may add printers is unchanged (`_restricted`).
- **Database server**: `scripts/setup-postgres.sh` installs PostgreSQL, makes a `nextbox` login
  and `nextbox` + `nextbox_practice` databases, copies the SQLite data (`nextbox db copy-to`, which
  refuses a non-empty target and resets id counters), and sets `DATABASE_URL`. New columns in
  later versions are added automatically (`db._add_missing_columns`, nullable columns only).

## 20. Every major to-do app and calendar
**To-do apps** (`nextbox/providers/`, one linked app per person, next to My list):
- token: Todoist, CalDAV, Notion (`notion.py`: finds the title, a Checkbox or Status column for done,
  a Date column and a Multi-select for tags; delete = Notion's trash), Asana (`asana.py`: tasks
  assigned to you in one workspace).
- sign-in (`oauth.py`): Google Tasks, Microsoft To Do, TickTick. Authorization code + PKCE, a
  single-use `state` (10 minutes, in memory) that names the person who started it, so the
  callback needs no cookie. Only tokens are kept, sealed; access tokens are refreshed when needed
  and a rotated refresh token is saved back. Shown as "needs setup" until the owner puts
  `<APP>_CLIENT_ID`, `<APP>_CLIENT_SECRET` and `NEXTBOX_PUBLIC_URL` in `.env`.
- not possible: Apple Reminders, Things, Any.do, Google Keep (no way in for other apps).
Ids that need a list id too, or contain / + =, are packed (`providers/ids.py`: `gt.`/`ms.`/`tt.` +
base64url) so they're safe in URLs.

**Calendars** (`nextbox/calendars.py`, up to 8 per person, read-only, never stored):
subscription links (.ics, repeats expanded, 5 MB cap, 5-minute cache), CalDAV accounts, and
Google Calendar / Outlook by sign-in. `GET /calendars/today`; the daily ticket prints a SCHEDULE
box (no checkboxes, up to 8 events) above the tasks (`show_calendar` pref). A calendar that fails
is named in the ticket's note; the ticket still prints.

**Addresses people type are checked** (`providers/http.check_public_url`): https only, and never
a private, loopback or link-local address, including after redirects. `NEXTBOX_ALLOW_PRIVATE_URLS=1`
allows a CalDAV server on the home network.

The session cookie is SameSite=Lax (was Strict) so returning from a sign-in page keeps you
signed in; cookie-authenticated writes are still refused unless their Origin is this site.

## 21. Print on paper (any printer)
`GET /printed/{id}/pdf?paper=letter|a4` (owner or superadmin; default from the `paper_size` pref)
returns a PDF made by `nextbox/paper.py`: stickers at real size (300 dpi) in rows with dashed cut
lines, as many pages as needed; any other ticket 3.5 in wide, split across pages if long: between rows
(just below a list row's dotted line or a strip's dashes, so a task and its box stay on one page), else
at a blank line between lines of text (`paper._break_at`). Sticker
images are kept per ticket in `tickets.parts` (a small zip of PNGs). Next Box never sends the PDF to
a printer; the person prints it from their device.

**Save label image**, next to every Print on paper (results, Printed, Journal): `GET /printed/{id}/png?download=1`,
the same owner-or-superadmin rule and no-store headers, sent as `attachment; filename="next-box-<id>.png"` (without
`download` it's `inline` with that filename). It's the ticket's stored picture: stickers are all of them in one
tall image with dashed cut lines between (`render.stack_with_cuts`), tear-off strips are one image already. People
open it in their label printer's own app (Brother iPrint&Label) or print it. It's a download only: nothing prints.

## 22. Your account (the round button, top right)
Profile and email (`PATCH /auth/profile`; sign in with username or email), change password, signed-in
devices (sign out the others), settings, help, feedback, refresh, **download my data**
(`GET /auth/export`: account, settings, tags, tasks, linked app and calendar names, tickets, read-backs,
journal pages, wins, devices, and friends with your own sent and received notes (§34) as data.json;
`?format=zip` adds the ticket images (`tickets/*.png`), journal photos (`journal/*.jpg`) and notes' doodles and
photos (`notes/<id>.png|jpg`), 5 an hour; both are sent `no-store`; never passwords, hashes or tokens;
the app first asks `GET /auth/export/check`, which doesn't count, so a reset hold or the hourly limit is a
message in the app; it says "Getting your .zip ready…", ignores another tap for 8 s so a double tap is one
download, and starts the file through a download link, so a download that fails never replaces the app),
sign out, and **delete my account**
(`POST /auth/delete-account`: password + typed username; removes the account, tasks, tags,
tickets, scans, activity, friendships, requests, blocks and every note it sent or got; feedback stays, unlinked; the invite it joined with stays used, and the
invites it made stay, but none of them names it (`(deleted account)`; a one-time startup fix does the
same for accounts an older version deleted); the protected admin and the last superadmin
can't). The login page has show-password and "Forgot your password?" (an admin gives a one-time
password). Superadmins see a banner when a newer version is published (`GET /super/update-check`,
reads the published pyproject.toml at most every 6 hours).

## 23. Profile page and settings
**Profile** tab (also from the account menu): name to show (`display_name` pref), email, password,
signed-in devices, what the admin can see, download my data, delete my account. **Settings** has a
jump bar (General, Tags, Focus, Your marks, Calendars, Linked app, Label colors, Printing, Read-back, Help); General holds
theme, text size (`text_size`), the screen it opens to (`start_tab`), 24-hour times, one-key
shortcuts (`keyboard_shortcuts`, §30) and where new tasks go. The Jump to row sticks to the top, and
whatever you Tab to in Settings scrolls clear of it (`--jumpH`, its height as it wraps with big text).
Each setting saves as it changes (`PATCH /settings`; a typed box as it's left), one save at a time
in the order made; only the newest answer redraws Settings, and never the box being typed in. A typed value
that didn't save stays, with why in red next to it; if Next Box couldn't be reached, leaving the box again
tries again. When the setup questions (§29) change a setting, its red message goes.
`/app/` is served with `app.js?v=<version>-<build>` and every `/app` response says
`Cache-Control: no-cache`, so phones and Cloudflare never keep showing an old version.

## 24. Standard app pages and navigation
Static pages in `web/`: privacy.html, terms.html, about.html, guide.html, 404.html (shared
pages.css/pages.js; operator, contact, version and retention come from the public `GET /about`, set
by `NEXTBOX_OPERATOR` and `NEXTBOX_CONTACT`; `hosting`, the service named as carrying traffic on the
Privacy and About pages, is `NEXTBOX_HOSTING` (scripts/go-online.sh sets it to Cloudflare), else
"Cloudflare" when the request itself came through Cloudflare, else nothing). pages.js applies the Theme,
Text size and Easy-to-read text the app saved on this device (localStorage `theme`, `nb.textSize`,
`nb.reading`, written by app.js `applyLook`). Unknown addresses get the 404 page when a browser asks for
HTML, JSON otherwise. `POST /auth/signup` needs `agree: true`; the sign-up event records
`terms_version` (`TERMS_VERSION`). Phones (≤720 px) get a bottom tab bar with a More sheet holding
the other tabs and the legal/help links; an offline banner follows the browser's online state.

## 25. Journal, wins and slugs (the main app's daily routine)
`nextbox/journal.py`. The routine: today's label on the left page of a paper journal, notes on the right,
one photo of both pages at night.
- **Read-back reads notes too**: `ReadBack.notes` holds handwritten text outside the label, transcribed
  as written (`[unclear]` for unreadable words). `scan_photo` keeps the photo as a journal page when there
  is handwriting (notes or a win), or always when the `journal_mode` pref is on (default); the Privacy
  Policy and the Journal mode setting say so. A photo with notes but no label is
  saved to the journal instead of failing. A daily ticket's page is filed under the ticket's day.
- **Journal pages** (`journal` table): id, owner, household, day, notes (editable), photo (JPEG),
  scan_id, label_id, win, photo_sha. Strictly private: every query is scoped to the owner, no admin or
  superadmin endpoint shows them, and `nextbox db sql` masks `notes` and `photo`. In `/auth/export`'s
  data.json (the photos are in its `?format=zip`, and in Download my journal, §26); deleted with the
  account. Deleting a page needs `confirm=true`.
- **Wins** (`wins` table, `journal.Wins`; `wins_by_day` in server.py reads it): one row per finished task,
  filed under the day it was done (a read-back's under its ticket's day), with `source` nextbox, linked or
  readback. `content` is kept only for My list tasks; a linked app's task text is never stored, so a
  read-back's name comes from that scan while it's kept, and the rest show as "+N more". Private like the
  journal; kept until the account is deleted; Undo removes the row.
- **Slugs**: `task_slides` counts moves to a later day per task id (no text), from read-back → marks and
  from My list due-date changes to a later day; cleared when the task is done. A slug is moved
  `slug_after` (default 3) times or waiting 7+ days. The daily label shows "slid N×" and the Journal tab
  lists them with "Make it smaller" (My list steps) and "Print just this" (focus card).
- **Journal mode** label: a "Today's win:" write-in line and "Notes on the right page → snap both pages".
- **Paper sizes**: `a5` and `half_letter` join letter and a4 for Print on paper.
- Endpoints: `GET /journal?until=&days=`, `GET /journal/wins?days=`, `GET /journal/slugs`,
  `POST /journal` (typed note), `PATCH|DELETE /journal/{id}`, `GET /journal/{id}/photo`.
  The Journal tab asks for 14 days at a time; Older days adds the next ones below, and keyboard focus goes
  to the first day it added (or to "That's everything." when none came).
- Family Mode (later) reuses all of this per person; sharing a page will be an explicit choice.

## 26. FB: the journal loop (notes → tasks, download, search, moving pages)
Everything here is the owner's own journal: owner-scoped, never in events, admin screens or debug bundles.
- **Notes → tasks**: "Add lines to my list" on a Snap result and on each journal page. `POST /journal/lines
  {text}` (the notes as on screen; nothing saved or logged) returns each line (`journal.note_lines`) with
  `task` (what `parse_line` makes of it), `kind`, `done`, `unclear`, `on_list` and `suggest`. Suggested
  (pre-ticked): bullets (`-` `*` `•`), boxes (`[ ]` `□` `☐`), `todo:` lines and lines under a "To do:"
  heading. Not suggested: ticked lines (`[x]`, `☑`, a trailing ✓), lines already open on My list, lines with
  `[unclear]` (edit them first) and long prose. The sheet shows the parsed date/time/#tags, lets each line be
  edited, and "Add N" sends the ticked lines to `POST /list/dump` (so `new_tasks_to` applies). The vision
  prompt writes a hand-drawn box at the start of a note line as `[ ] ` (`[x] ` when ticked).
- **Moving pages**: `PATCH /journal/{id} {day}` (notes optional) moves a page to another day: a real date,
  today or earlier (not before 2000). The page's "Move to another day" opens a date field; the old day says
  "Moved a page to … Undo".
- **Search**: `GET /journal/search?q=` (2+ letters): pages whose notes or win hold every word, ignoring case
  and accents, newest first (the newest 5,000 pages are searched; 50 shown, with the total). Each result has
  a short snippet as `parts` `[{t, m}]` with the matches marked. 60 searches a minute (new typed pages: 300
  an hour). Opening one shows that day and older, with "Back to today".
- **Each day** in `GET /journal` also has `ticket` ({id, kind, title, dry_run}: the ticket a page that day
  was read back from, else that day's daily ticket, a real print before a preview) shown as a thumbnail with
  Print on paper, and `changes` ({moved, dropped}: task names read-back moved or dropped that day, undone ones
  left out). A day with only changes is listed too.
- **Download my journal**: `GET /journal/export?format=zip|pdf&since=&until=&paper=`. zip: a
  `next-box-journal-<date>/` folder with README.txt and a folder per day holding notes.txt (win, then each
  page with when it was snapped or written: "Snapped at 7:30 AM", or with the date when that wasn't the
  page's own day, "Written Oct 22, 7:30 AM", as the Journal tab shows it; the PDF says the same,
  `paper.made_when`), photo-N.jpg and wins.txt. pdf: a page per day (more if long:
  "(continued)", "2 of 3") with the win, each photo, the typed notes and the wins, drawn with Pillow at 200 dpi
  on the `paper_size` paper (`paper.journal_pdf`; `paper.JpegPdf` writes one page at a time, so a long
  journal never sits in memory; it's sent as it's drawn, so a long journal starts downloading at once, and a
  day stops after 20 sheets, noting the rest is in the .zip). `GET /journal/export/summary` says days, pages,
  photos, wins, the date range, about how big, and whether a download is allowed now. 10 downloads an hour; like Download my data, they wait
  3 days after an admin set a one-time password. Linked from the Journal tab, Profile and the delete-account
  panel ("Download your journal first").
- **Print my journal page**: `GET /journal/export?format=pdf&day=YYYY-MM-DD` opens that day's PDF (inline, 60
  an hour, no reset wait: it shows nothing the Journal tab doesn't). Only that: a day as a .zip (with its
  photos) waits out a reset like any download.
- `Store.list_printed(until=)` bounds tickets by day, like `list_scans`.

## 27. FD: gentle AI helpers and monday.com
**AI helpers** (`nextbox/coach.py`): only when the person taps a button, never automatic, 20 an hour per
person for both together (in memory). Off with the `ai_helpers` pref (default on, Settings → Focus):
both endpoints then answer 409 and nothing is sent. Without `ANTHROPIC_API_KEY` they answer 503
`coach.NO_KEY` ("Ask your Next Box owner to add an AI key.") or, to a superadmin, `coach.NO_KEY_OWNER` (add
the key to .env, then restart), and `GET /auth/me` says `ai_ready: false`: the app hides every Break it down
button and the Look back card says the same thing instead of its button. Snap needs the key too: without it
(and without a stand-in vision in tests) `POST /scan` and `/scan/{id}/retry` answer the same 503 before
anything is read or counted against the hour's photos, `GET /auth/me` says `scan_ready: false` (separate from
`ai_ready`, which also counts a stand-in coach), the Snap tab's photo buttons are off with that note, and
`nextbox scan` and `nextbox doctor` say how to add the key. Model: `NEXTBOX_COACH_MODEL` (default `claude-opus-5-5`, in `Settings.redacted()` and
diagnostics). Each call: `client.beta.messages.parse` with a pydantic output format, effort low, the
server-side refusal fallback (`fallbacks="default"`), 60 s timeout, 2 SDK retries; stable instructions
(no dates or names) and the person's data in the user message inside `<task>`/`<week>`, marked as data.
A refusal, an unreadable answer, a rate limit, a timeout, a bad key or an outage each become a short,
plain message. Events record only that a helper ran (`ai_breakdown {ms, steps}`, `ai_look_back {ms}`) or
the kind of failure: never what was sent or what came back. `create_app(coach=...)` takes a fake in tests.
- **Break it down**: `POST /list/{id}/breakdown {hint?}` (My list or linked task, the caller's own) sends
  only the task's name, minutes, due day, steps and the optional "what makes it hard" (300 characters),
  never its description. Crisis words in the name, its steps or the hint (or the model's `concern`) get only
  `support`, no steps. Otherwise it returns 3-6 tidied steps plus `task {id, content, local, steps, due_date,
  app}`. Nothing is saved: the sheet (Just one thing, Edit dialog, each slug) shows them ticked and
  editable. My list: Save sets the steps with `PATCH /list/{id}` (use instead or add after its steps;
  Undo puts the old ones back; new steps clear the slug count as before); from the Edit dialog they go
  into its steps field for its own Save. A linked task is never written to: "Add to My list" makes a new
  task "<task>: steps" with them, or "Print on a card" passes them as `steps` to `POST /print/focus`
  (linked tasks only; drawn on that card and not kept).
- **Look back on my week**: `POST /journal/look-back {include_notes}` sends the last 7 days' wins (counts
  and the names Next Box has), the slugs, and the journal notes (12,000 characters at most) only with
  `include_notes`, and returns `{week, note {went_well, slug, suggestion}, text}`: 2-3 sentences on what
  went well, one gentle sentence about a slug (empty when nothing slid) and one tiny suggestion. Coaching
  inspired by proven techniques, not therapy: no diagnosing, no medical advice. A quiet week answers
  `empty` without asking the AI. When the ticked notes, or a task name in the wins or slugs, hold crisis words
  (`coach.CRISIS`), or the model sets `concern`, the only answer is `support`: a kind nudge to reach out to
  someone they trust or a local crisis line (988 in the US); crisis words are never sent or logged. "Save to my journal" saves
  `text` with `POST /journal` (today's page); otherwise nothing is kept.

**monday.com** (`nextbox/providers/monday.py`, a `TaskProvider`): personal API token (+ optional board
link), GraphQL at `https://api.monday.com/v2` (`API-Version: 2026-01`) through the shared `Api` client. On
each board (the chosen one, or the 10 most recently used that qualify) it finds a person column, a date
column and a status column with a Done label (`done_colors`, else a label called Done/Complete/Finished),
plus Priority and Tags when present. Lists items assigned to you (`person-<id>` rule, checked again on what
comes back), not Done, up to 500 per board; dates with a time are UTC on monday.com and shown local. Done
sets the status to the Done label, moving changes the date (keeping its time), delete (after the usual
confirm) archives the item, new tasks go to the first board assigned to you. Items are never stored.
ClickUp and Trello are listed under Settings → Linked app → Other apps as "not yet" (status `later`) with
"Ask for it", which opens Feedback filled in as an idea.

## 28. FA: My list, faster (steps, one-tap moves, search, add, bulk, slugs, skip)
- **Tiny steps you can tick** (My list tasks only): the Edit dialog has a step list (tick, edit, ↑/↓, ✕, "Add a
  step"; a pasted list becomes one step per line; 12 at most; what's typed in "Add a step" is saved too). "steps
  1/3" on a row opens the steps inline with checkboxes; after the last one a message offers "Finish the task".
  Just one thing shows "Next tiny step (1 of 3)" with **Step done ✓** first and "Whole task done" second; after
  the last step it asks "All steps done. Finish the task?" (Yes / Not yet). `PATCH /list/{id}/steps/{n}
  {done, text}` ticks one step; `text` is the step as the screen showed it, so a step changed on another device
  is never ticked by mistake (409, nothing changes). A step is not a win. Only a *new* step clears a task's
  slides ("made smaller"); ticking, reordering or removing steps doesn't.
- **One-tap moves**: a → button on each open row that's due today, waiting or someday (it never moves a task
  sooner), → Tomorrow on Just one thing, and day chips in the Edit dialog (Today, Tomorrow, This weekend =
  Saturday or today at the weekend, Next week = Monday, No date). `POST /list/{id}/move {to: "tomorrow" |
  "today" | YYYY-MM-DD | null, undo}`: My list keeps the task's time (a weekday repeat skips the weekend);
  linked tasks go through the app's `set_due_date` (a repeating CalDAV task is refused with the §11 explanation;
  a CalDAV DUE keeps its time as the app shows it, in local time, and its own zone, e.g. UTC;
  a linked task's date can't be taken off here). A move to a later day counts as a slide (`Slides.bump`), as
  read-back's → does; scheduling a someday task doesn't. The reply's `undo` (the same call with `undo: true`)
  puts the day back and takes the slide back. Every move shows Undo; Done keeps its Undo.
  On a repeating My list task a move (and read-back's →) is this time only: the repeat keeps the day it belongs on
  (`tasks.repeat_day`: the weekday, or the day of the month), so "every Friday" moved to Saturday is next due on a
  Friday. Changing the Day in the Edit dialog changes the repeat's day.
- **Search and tag filter**: "Search my list" and **+ Add task** under the My list heading, then a row of tag
  chips (the tags on open tasks, most used first, and All). The filter runs in the browser over the words,
  #tags, notes and steps (every word must match; case and accents don't matter). A tag on a row is a button that
  shows only that tag. "Showing N of M … Show all" says a filter is on; picks survive it ("2 picked · 1 not
  shown"). With a filter on and nothing picked, Stickers and Tear-off strips print the tasks shown (the first
  30, by id). The last filter is remembered per person on the device (`localStorage nb.listFilter:<user>`).
- **+ Add task** opens the Edit dialog empty ("New task") and saves with `POST /list` (always My list; it says
  so when Brain dump sends new tasks to the linked app). A tag being shown starts ticked.
  With unsaved changes, Esc (or the phone's back gesture) asks "Close without saving?": Cancel keeps it open
  with what was typed, however many times (Esc is handled as a key; a back gesture the browser closes anyway
  opens it again as it was), OK closes it without saving.
- **Brain dump preview**: `POST /list/dump?preview=1` only parses: no task, tag (not even the starter tags) or
  event is written. It returns `lines` (each line as `parse_dump` makes it, problems and ticked lines too),
  `dropped` (lines with no task words, like "fri" alone), `more` (lines past 200), `to`, `new_tags`,
  `over_tags` (past the 30-tag limit: they stay in the words) and `notes`. The app shows it under Brain dump
  450 ms after typing stops (only the newest answer), with chips for day, time, repeat, how long, urgent and
  tags; it can be folded away (remembered on the device). A failed preview just hides.
- **Bulk actions**: with one or more tasks picked, the print bar adds ✓ Done, → Tomorrow, Tag… and Delete….
  They go one task at a time through the single-task endpoints, so linked tasks act in their own app; the bar
  shows progress and a failure names the task and why, whether or not others changed ("Moved 1 to tomorrow ·
  “Pick up prescription” didn't change: That task was already removed.", "and 2 more" when more didn't).
  A message with Undo stays 6 s (10 s with bigger text) plus 60 ms a character past the first 40, and waits while
  the pointer or focus is on it (after a key, until the next action or Esc).
  Done and → Tomorrow each get one Undo for the whole batch; it says how many went back ("Back where they
  were (1 of 2)") and names one that didn't, and why. Tag… adds or takes off a tag on the picked My list
  tasks (with only linked tasks picked, its sheet is "Tags are for My list tasks"). Delete… lists the tasks and asks "Delete N tasks? This
  can't be undone." (Cancel has focus), then sends `DELETE /list/{id}?confirm=true` per task. Closing Tag… or
  Delete… without doing anything puts focus back on that button; after a tag or a delete it goes to Stickers.
- **Slugs on My list**: `GET /list` also returns `slides` ({task id: moves} for open tasks moved `slug_after`+
  times; "Not a slug" ones left out) and `slug_after`. Those rows show "slid 3×", as the label and Journal do.
- **Skip this time** (repeating My list tasks; in the Edit dialog, which saves first, and on Just one thing):
  `POST /list/{id}/skip {due}` moves it to its next day (`next_due`, steps start fresh) without a win or a
  slide; `due` is the day the screen showed, so a double tap skips once. `POST /list/{id}/unskip {prev_due,
  prev_steps}` is its Undo. Clearing the Day of a repeating task says "Clearing the day stops it repeating"
  before Save.
- **Phones** (≤520 px): a row's words take the full width, and its when/steps share the next line with Edit, →
  and ✓. Every new control is 44 px tall on phones and has a label; moves, steps and skips can't be sent twice
  while one is on its way.

## 29. FC: setup questions, Easy-to-read text and Same words every time
- **Setup questions** (`nextbox/quiz.py`, `POST /setup/quiz`): after the data notice, a new account (no tasks of its
  own, no tickets, no linked app, `setup_quiz_done` off; `GET /auth/me` says `setup_quiz`) gets five one-question
  screens with big buttons: how you'll print (label printer / regular printer / no printer yet → `print_with`;
  a label printer also turns `always_dry_run` off; none of the answers turns it on, §14), a paper journal (A5 / half letter / letter / A4 / no → `paper_size`, `journal_mode`), how many tasks
  a day (3 / 5 / 8 → `max_rows`), what helps (any of: short lists and gentle words → `gentle_words`; easy-to-read text →
  `reading_mode`; same routine every day → `routine_mode`; or "just the basics", which turns all three off) and when you
  plan and look back (`routine_morning`, `routine_evening`, default 08:00 and 18:00). Any question, or the rest, can be
  skipped (Esc does what the Skip button says); the answers are saved together at the end, and the last screen lists
  what changed and where to change it. Skipping counts as done. People already using Next Box aren't interrupted:
  Settings → Help → Run setup again, and the Get started guide's "Make it fit your brain" step, open it. Answering how
  many tasks or what helps ticks that step; a regular printer or no printer puts the printer step away.
- **What helps is never kept as such.** Only the ordinary settings it changes are saved (anyone can switch them in
  Settings); the event log records which settings changed, never the answers.
- **Printing is the default** (§14): a regular printer or no printer yet doesn't make everything a preview. With no
  printer in the household every ticket is a preview anyway, and the Preview only line says "No printer added".
  Always preview only is only ever the person's own switch, so its note always says "Always preview only is on in
  Settings". The last screen's printing line says a real print asks "Send this to the printer?" first.
- **Easy-to-read text** (`reading_mode`, Settings → General): Atkinson Hyperlegible (SIL OFL; `web/fonts/` with
  OFL.txt), line height 1.6, letter spacing .03em, no all-capitals headers, bigger and darker small print, in light and
  dark. The Help, Privacy, Terms and About pages follow it, and Text size, on that device (§24). Labels drawn for that person use the same font (`render.reading_font`, set in `jobs._finish`), take the symbols
  it lacks (✓ → ✗ ↻) from DejaVu, fall back to DejaVu if the files are missing, and print SCHEDULE, JUST ONE THING,
  TASK, NEW TASK and NOTE in sentence case. Tag bands print the tag as it's written (work, not WORK).
- **Same words every time** (`routine_mode`, Settings → Focus): the Done message is always "Done ✓" (no random
  phrases), and My list opens on Schedule.
- **List | Schedule** on My list (everyone; the pick is kept per device, and switching `routine_mode` resets it to that
  mode's default): today's calendar events (read-only, from `/calendars/today`), today's timed tasks and the routine's
  "Plan your day" / "Look back" in one column by time, a Now line, "Now: … until …" and "Next: … in 20 min", finished
  events greyed; all-day events on top; untimed tasks due today or waiting under "Any time today" in ticket order
  (linked-app tasks slotted in by the same rule); "Later: N more" goes back to List. Computed in the browser and
  updated every 30 s only while it's on screen; a calendar that fails is named, with Try again. Today's calendar card
  steps aside while Schedule shows the events. Nothing is printed or sent by it.

## 30. FE: routine card, focus timer, reminders, Quick add and shortcuts
All of it is worked out in the browser while Next Box is open: no server timer, no push, no automatic print.
- **Routine card** (top of My list; `routine_card`, default on, Settings → Focus), from `GET /printed?since=<the routine
  day's start>&limit=100` (all of that day's tickets, so stickers or one-task prints don't hide today's label), `GET
  /scans?limit=1` and FC's `routine_morning`/`routine_evening`. The routine day starts halfway between the look-back time
  and the next plan time (1:00 with 8:00 and 18:00), so a night owl's day (plan 20:00, look back 6:00) is one day across
  midnight; from its start until the plan time the card is quiet. From the plan time, while there's no `today` ticket this
  routine day (a preview counts, a failed print doesn't) and something is due: "Good morning. Print today's label",
  whose button is My list's Today's label (a preview, or "Send this to the printer?"), never a print by itself. From the
  look-back time with no snap since today's label: "How did today go? Snap your pages" → Snap. After that snap (or any snap
  this routine day in the evening): "You finished N today ✓" (`done_since` from `GET /list?since=<the routine day's
  start>`: what was finished since the routine day began, by when it was ticked, so a night owl's ticks before and after
  midnight and the snap's own ✓ marks all count), "Pages snapped ✓" when N is 0; under it "Today's page is in your
  journal", or "Your page is in your journal" when the snap was filed under another day. Each has Not
  today / Hide (a ✕ on phones), for this routine day on this device (with Undo). The words match Schedule's "Plan your
  day" and "Look back" rows. On phones the card is just its line and buttons, so Just one thing starts on the first screen.
- **Focus timer** on Just one thing: "⏱ Start 5 min", plus "or N min" when the task has its own estimate (1–90). A
  countdown ring with Pause/Resume, Stop (Undo) and a chime switch; the card keeps that task while it runs, and the timer
  goes when the task is done, moved or set aside. The time comes from when it started and is kept on this device per
  person (`nb.timer:<user>`: the task id and times, never its words), so switching tabs, locking the phone or reloading
  doesn't lose it. One that ran out over 30 minutes ago (with Next Box closed or open, answered or not) is let go
  quietly, and so is one paused on an earlier day. At the end: two soft notes made with Web Audio (only
  after a tap allowed sound, and only with the chime on), a whole green ring and Keep going / Done ✓ (Step done ✓ with an
  open step) / Not now ("5 minutes of focus. That counts ✓"). No ring motion with reduced motion; screen readers hear the
  start, pause, 1 minute left and the end, on any screen. On another screen (or, where the note sticks to the top,
  scrolled away from the card) the note at the top says "Time's up".
- **Reminders** (`remind_before`: minutes, default 10, 0 = Off): a note at the top of every screen from N minutes before a
  task with a time until 30 minutes after, by the clock (a task at 0:15 is reminded from 23:xx the night before), with
  Done ✓, Tomorrow, Snooze 10m and ✕ (snoozed and dismissed ones are kept on this device until the day after the task's
  time, ids only; a snoozed one is said and notified again when it comes back). Done and Tomorrow use the usual
  endpoints and show Undo in the note (Undo puts you back on that reminder). On My list, Just one thing's own task isn't
  repeated in the note while the card is on screen (it's still announced and notified). On phones the note scrolls away
  with the page; on a computer it sticks to the top, with Settings' Jump to row and the bulk bars under it. Made from My
  list as last loaded, fetched again quietly when a timed task is near and the list is 5+ minutes old (in the background
  too). **Show a notification too** (`remind_notify`, default off) asks the browser for permission only when it's switched
  on; they show only while Next Box is open in the background (https or the installed app; phones through the service
  worker), never from a list over 5 minutes old (that waits for the fresh one), and signing out closes them.
- **Quick add**: the + in the header (or `n`) on every screen: Brain dump's rules and preview (`POST /list/dump?preview=1`),
  added with `POST /list/dump` (so `new_tasks_to` applies), Ctrl/⌘+Enter adds. Closing keeps what was typed.
- **App shortcuts and Share to Next Box** (`manifest.json`): Add a task (`/app/#add`), Snap a page (`#snap`), Print today
  (`#print`: the Print tab with Print today focused; nothing prints). `share_target` posts title, text, url and photo to
  `/app/share`. `web/sw.js`, the app's only service worker, answers that on the device: a photo waits in Cache Storage for
  a moment and opens Snap attached (read only after Read back); text opens Quick add filled in (`#add&text=`, each part
  once: `server.share_text`). A photo that's never picked up goes when someone signs in without one waiting, on sign-out,
  and after an hour when the worker starts again. It caches nothing else, has no push, and otherwise only brings Next Box
  forward when a notification is tapped. Without it, the server's `POST /app/share` (no sign-in, nothing kept or logged)
  does the same for text and sends a photo to Snap to pick again: it reads at most 256 KB like every other request, and a
  bigger body (a photo) is answered with Snap without being read. These addresses wait for sign-in, the data notice
  and the setup questions, then leave the address bar.
- **Keyboard shortcuts** (desktop): `n` or `+` Quick add, `/` search My list (Schedule switches to List for this visit),
  `g` then `l`/`p`/`s`/`j` My list, Print, Snap, Journal, `?` the list (also Settings → Help), Esc closes. Ignored while
  typing in a box or with a sheet open. Ctrl/⌘+Enter also saves a journal note. The one-key ones (`n`, `+`, `/`, `g`,
  `?`) can be turned off: Settings → General → One-key shortcuts (`keyboard_shortcuts`, default on), for speech input
  or keys pressed by accident (WCAG 2.1.4). Ctrl/⌘ shortcuts and Esc keep working.

## 31. Status icon in the top bar
`tray/nextbox_tray.py`: a filled square in the desktop's top bar (AyatanaAppIndicator3, else AppIndicator3, with
GTK 3; GNOME shows it through the Ubuntu AppIndicators extension). Optional: `scripts/install-tray.sh` adds it
(packages, the extension, `~/.config/autostart/nextbox-tray.desktop` through the `~/.local/share/nextbox/app` link,
started now with a pid file in `~/.cache/nextbox-tray/`); `--remove` takes it away. `install.sh` only suggests it.
- **Outside Next Box:** it runs on the system python3 (PyGObject) and never imports `nextbox`. It reads only
  `.venv/bin/nextbox doctor --json`, `http://127.0.0.1:<port>/health`, `<NEXTBOX_PUBLIC_URL>/version` and
  `systemctl --user is-active nextbox` / `systemctl is-active cloudflared`. No labels, no AI, no `.env`, no log of
  its own (when `install-tray.sh` starts it, GTK's warnings go to `~/.cache/nextbox-tray/tray.log`).
- **`nextbox doctor --json`:** the same checks as plain `nextbox doctor` (whose output doesn't change), as one object:
  `{version, port, public_url, checks: [{mark: ok|bad|note, text, fix}], problems}`. Secrets only as "set"/"missing".
- **When:** quick checks every 60 s (local page, service, and with a public address: `/version` within 8 s and
  the tunnel's service); doctor at start, every 10 minutes, on Check now, and when the service goes down, comes
  back or restarts. One worker thread does it all (never two doctor runs), handing results to GTK with `idle_add`.
- **Colours** (`evaluate`, a pure function): red Offline = the service isn't active, or `/health` doesn't answer,
  or the public `/version` failed two runs in a row (counted only while this computer is up). Yellow Online, needs
  attention = doctor has a ✗, the public check failed once or took over 5 s, or cloudflared isn't active while the
  address still answers, or doctor itself couldn't run. Green otherwise. No public address: only the checks on
  this computer decide red or green. Grey while the first round runs.
- **Menu:** "Next Box <version>: <headline>", one line per reason with its fix under it, Open Next Box (the public
  address + `/app/`, else `http://localhost:<port>/app/`), Check now, Show details (`nextbox doctor` in a terminal,
  until Enter), Quit status icon. On GNOME each line is a mnemonic label with one leading `_`, because the
  AppIndicator extension removes only the first `_` of a plain label's doubled ones (`ANTHROPIC_API__KEY`).
  A notification (notify-send, if there) only when it turns red or comes back, and when the first answer after
  a fresh start (login) is red. An update's restart passes the colour on (`--was=`), so it never repeats one.
- **Updates:** each minute it compares its own file's time; after an update (that compiles) it starts itself again
  with `os.execv`, so no logout is needed.

## 32. How you mark your list (mark styles)
`nextbox/marks.py`. Settings → **How you mark your list** (`mark_style`): one of the presets below, or `custom`, the
person's own `marks` (up to 8; `marks_from` is the preset it started from, for "Start again from …"). Everyone,
existing people included, is on `simple`, exactly as Next Box always printed and read. Picking a preset keeps their own
list, so "Your own" brings it back; changing a preset's marks makes them their own.
- **Presets** (`marks.PRESETS`, each with a 2-3 sentence "how to use it" shown in Settings and the guide):
  Simple (✓ done, → move, ✗ drop, a line through counts as ✗), Bullet journal (× done, > migrate = tomorrow,
  / started = note, a line through = drop), Paper planner (✓ done, → forwarded, X deleted, • in progress and
  D delegated = note), Fill the box (● done, ◐ half done = note, → move, ✗ drop).
- **A mark** is `{symbol, meaning, action}`: `symbol` 1-3 characters the label font can draw (`render.can_draw`
  checks each one in DejaVu, so nothing prints as an empty box), `meaning` their own words (one line, 60 at most),
  `action` done | tomorrow | drop | note. `look: "strike"` is a line struck through the task: alone (no symbol), or as
  well as the symbol. Symbols are used once (case, and look-alikes written by hand count as the same: ✗ × X,
  ✓ ✔ √, → ->, the dashes - – —, • ●, and ○ O 0; `marks.LOOK_ALIKE` = app.js `MARK_ALIKE`), one strike at most, and
  at least one mark finishes the task. A meaning has no control, private-use or unassigned characters (nor half of
  one). `PATCH /settings` says what's wrong by mark ("Mark 2: the label can't print “⭐”. Pick another symbol."),
  shown next to that mark; a message about the whole list shows by the box just changed. Any odd shape (a `look`
  that isn't text, a style that isn't a name) is a 400 with a message, never a 500. The event log says only how many
  marks there are.
- **Labels** print the person's legend (`render.draw_legend`): each symbol and its meaning, never past the edge; a
  strike mark is its words with a line through them. Only the longest meanings are shortened, a letter at a time,
  so short ones stay whole: on two lines while every meaning keeps 7 letters, then on three, then down to 4. Simple's
  legend is pixel-identical to before (tests/golden). The daily, journal and list labels and one-task tickets have
  it (stickers, strips and Just one thing have no legend); HA's auto print uses the owner's marks.
- **Snap** (`scan.build_prompt`): Simple sends `scan.PROMPT`, word for word what Next Box always sent. Other marks:
  the prompt lists them by number with what each looks like, the mark to report and, in quotes, their meaning (one
  line, control characters removed, 60 at most: a hint, never an instruction), and the output format offers only
  the actions their marks have. An empty box is "none"; anything else is the closest mark below 0.5 confidence, as
  before. The scan keeps the
  marks it was read with (`marks`), so changing them later never changes what an earlier photo meant. A "note" row
  shows "Row 2: ◐ half done: <task>. Noted, nothing changed."; applied and waiting rows say which mark it was in
  their words (not on Simple). Drops always wait for Confirm. The marks go to Anthropic only with a photo the person
  sent (TERMS_VERSION 5; privacy.html says so). Handwriting never goes into notes.
- `GET /settings/marks`: presets, actions, the symbols Settings offers (✓ ✗ → > < × • ○ ● ◐ ★ ! ? – /, and letters
  can be typed) and the person's marks now. `GET /settings/marks/legend.png?style=|marks=` draws the legend as the
  daily label prints it (in Easy-to-read text when that's on) for the "On your labels" preview; nothing prints.
- **Settings**: each style's card shows its legend as text (a line through a strike mark's words). Each mark row
  wraps at any text size, and "What it means to you" is never narrower than 14rem. "Delete it" has "Next Box always
  asks you before it deletes." under it. Changes save one at a time; a style picked (or Start again, or Undo)
  redraws the rows at once as they will be saved, and an older answer never redraws them.

## 33. Move to another PC
`nextbox/move.py`, `nextbox/movecheck.py`, `scripts/move-out.sh` (old PC) and `scripts/move-in.sh` (new PC); README
"Move Next Box to another PC". Everything moves in ONE locked file; nothing is deleted on the old PC until
`move-out.sh --forget`, and `move-out.sh --undo` puts it back. Moving back is the same move the other way round.
- **`nextbox move pack [--out PATH] [--while-running]`** makes `~/nextbox-move-YYYY-MM-DD.nbmove` (never over a file:
  -2, -3 ...; mode 600). It refuses while Next Box answers on `http://127.0.0.1:<port>/version` ("Stop Next Box first
  ..."), unless `--while-running`, and checks the database can be read before the passphrase is asked ("Couldn't
  read the database. Check it with: .venv/bin/nextbox doctor"; a database error's own words are never shown).
  Inside: `nextbox.db`, always SQLite (SQLite: the online backup API; PostgreSQL: `dbtools.copy_database` into a
  fresh SQLite file, one REPEATABLE READ snapshot, rows streamed, 5 at a time for tables with pictures:
  `dbtools.batch_for`), `secret.key`, the `.env` Settings loaded (`PROJECT_DIR/.env` or `NEXTBOX_ENV_FILE`) and
  `manifest.json` (format, app version, made_at, from_host, db_was, counts of accounts/households/printers/My list
  tasks/journal pages, had_public_url, sha256 and size of each file: no secret value, no username). Left out: `png/`
  (a cache: `GET /printed/{id}/png` falls back to `tickets.png` in the database), `backups/`, `practice/`,
  `.printer.lock`, the `app` link, `-wal`/`-shm`. The temporary copy lives in a 700 folder in the data folder and is
  always removed; a half-written move file is deleted. SIGHUP and SIGTERM during pack or unpack act like Ctrl+C (so
  that cleanup runs), the steps that put files in place hold them until done, and `.move-out-*`/`.move-in-*` folders
  an hour old (a kill -9) are removed by the next pack or unpack.
- **The passphrase** is asked with getpass (twice on pack); never from arguments, the environment or a log. It must
  be 10+ characters, not the same few characters over and over, not only digits, and three or more words when it's
  shorter than 16 characters; pack suggests four random words (`move.WORDS`, 476 words). Typed forms are made equal
  (Unicode NFC, spaces collapsed). Unpack allows 3 tries when interactive; the last one says how to get a new file
  ("Forgot it? ... run scripts/move-out.sh again").
- **Encryption** (the STREAM construction of Hoang, Reyhanitabar, Rogaway and Vizár, as in Tink and age): a plain
  header `NBMOVE` + 4-byte length + JSON {format, app_version, made_on, from_host, tz and utc_offset (the old PC's
  time zone), made_ts, data_ts (the newest change in its database: sign-ins, devices, tasks, journal, wins, prints,
  friends and their notes; not the events log), kdf {scrypt, n=2^17, r=8, p=1, 16-byte salt}, cipher, chunk, nonce_prefix, check}. Only r=8,
  p=1 and n from 2^15 to 2^18 are ever opened (at most 256 MiB), so a made-up header can't ask scrypt for gigabytes.
  Every text in the header must be plain (version, date, host `[A-Za-z0-9._-]`, zone name), so `move info` never
  shows terminal codes. scrypt gives 48 bytes: an AES-256-GCM key and a 16-byte `check` that tells a wrong passphrase
  ("That passphrase didn't open this move file.") from damage. The body is a tar stream (owner 0, no user names) in
  1 MiB chunks, each sealed with AES-256-GCM: nonce = 7-byte random prefix ‖ 4-byte chunk number ‖ 1 byte (1 on the
  last chunk), associated data = the whole header. A changed byte (header included), reordered, missing or extra
  chunks, or a file cut anywhere (also between chunks) give "This move file is damaged or incomplete. Copy it
  again."; an empty file "The move file is empty: the copy didn't finish. Copy it again." Never a traceback.
- **`nextbox/movecheck.py`** (standard library only) reads and checks the header, and what's on this PC.
  `move-in.sh` runs it with the system's python3 before anything is installed (`python3 -I nextbox/movecheck.py
  FILE DATA_DIR [--replace] [--replace-newer]`); `move unpack` uses the same functions. Exit codes, also `nextbox
  move unpack`'s and (3, 4) `move info`'s: 0 go on, 3 the file is from a newer Next Box (get the newest code),
  4 empty/damaged/not found (copy it again), 5 another time zone, 6 this very file is already in place here
  (`MOVED-IN`), 7 a Next Box with accounts is here (`--replace`), 8 that one was used after the file's data_ts
  (60 s slack; `--replace-newer`).
- **The time zone**: "today" and `Store.claim_auto` go by the PC's local date, so unpack refuses a PC whose zone
  (`TZ`, else `timedatectl`, else /etc/localtime, else /etc/timezone) keeps a different clock than the file's `tz`
  (aliases such as US/Eastern are the same), with the exact fix: `sudo timedatectl set-timezone <old PC's zone>`.
  `nextbox doctor` notes a clock on UTC.
- **`nextbox move info FILE`**: the plain header (version, day, which PC, time zone), no passphrase.
- **`nextbox move unpack FILE [--replace] [--replace-newer] [--port N]`**: refuses a file made by a newer Next Box
  ("Get the newest Next Box code first", README step 1, or scripts/update.sh where it's installed); an older one is fine (the server's start adds tables and columns and
  runs upkeep). Refuses another time zone, the same file twice ("This move file is already in place here (since
  <day>). Nothing to do."), and while a Next Box answers on this PC's port. The target is this PC's data folder
  (700). It holds data when its `nextbox.db` has an account, or there's a `secret.key` with no database beside it:
  then `--replace` is needed, and when that Next Box was used after the file's last change, `--replace-newer` (the
  file is older than what's here). What's there (the database with its -wal/-shm, `secret.key`) goes to
  `backups/before-move-<stamp>/` (700). A database with no accounts (install.sh ran first) and its key are set aside
  without asking. It decrypts into a 700 temp folder (always removed), checks each sha256 against the manifest,
  `PRAGMA quick_check` and that the vault key opens every saved linked-app and calendar secret (counted, never
  shown), and only then changes anything: `nextbox.db` and `secret.key` (600), and `.env` (600; a different one
  already there is kept as `.env.before-move`, 600) with `DATABASE_URL` and `NEXTBOX_DATA_DIR` commented out, each
  with a line saying why (setup-postgres.sh moves to PostgreSQL again), `NEXTBOX_PORT=N` with `--port N`, the
  `MOVED-IN` file (600: the header's salt, made_on, from_host, when) and, on a PC Next Box had moved away from, the
  `MOVED-AWAY` marker is removed (it's back). No value is ever printed. The summary: what moved (counts), the linked
  apps that unlock, each network printer from the printers table and `PRINTER_IP` tried with a TCP connect only
  (2 s, nothing sent; one per address; the fix is `doctor.printer_hint`, the same words doctor uses: on this PC's
  own network (`ip -4 -o addr`) it's the printer: asleep, off or a new address; otherwise the network), USB/CUPS printers
  named as staying behind, and the next steps (`nextbox move next-steps`): the phone address, Home Assistant's exact
  new `nextbox_print_today_url` line when it has printed (auto_guard or a `source="auto"` ticket in the last 60
  days), and `scripts/go-online.sh` when `NEXTBOX_PUBLIC_URL` is set (with the public hostname's new
  `localhost:<port>` when it isn't 8787). `auto_guard` moves too, so the once-a-day print can't happen twice on
  moving day.
- **`scripts/move-out.sh [--undo | --forget]`** (old PC; refuses root): says what happens in 3 lines and asks for
  `MOVE` (any case). Already moved away (the marker)? It says so, with the day and file, and asks again before a
  second file (only if the first or its passphrase is lost). It stops the service, packs (if that fails, or the
  terminal closes or it's killed before the marker is written, the service starts again and nothing else changes),
  writes the `MOVED-AWAY` marker (JSON: moved_at, file, turned_off), disables the service, removes the status
  square (`install-tray.sh --remove`) and, if cloudflared is active or enabled, `sudo systemctl disable --now
  cloudflared` (two connectors would split visitors). Before that it asks whether the tunnel also carries other
  websites ([Y/n], Enter turns it off); with `n`, or `NEXTBOX_TUNNEL=shared` in `.env` (go-online.sh writes it when
  it put Next Box on a tunnel that was already there), the tunnel stays on and it says to delete only Next Box's
  hostname from it and give the new PC a tunnel of its own. It ends with the file and the one line to paste on the new PC
  (get the code into `~/next-box` + `move-in.sh user@ip:path`, the address from `nextbox move address`, the route's
  own; or `move-in.sh ~/<file>` after a USB stick; both end with `--replace` when this PC has `MOVED-IN`, i.e.
  Next Box came here from the PC it's going back to). `--undo` is for "changed my mind before using the new PC": it
  says how to move back instead and to switch the new PC off with its own `move-out.sh`, asks for `UNDO`, removes
  the marker and turns back on exactly what it lists. `--forget` (after a week) refuses without the marker ("this
  is a PC it runs on, so nothing was deleted"), asks for the PC's name, then removes the data folder, the code, the
  unit (+ daemon-reload), the status square, the hotkey and the move files in `~`, and offers `sudo cloudflared
  service uninstall` (when it had turned the tunnel off) and dropping the local PostgreSQL `nextbox` and
  `nextbox_practice` databases (when DATABASE_URL pointed at them).
- **`scripts/move-in.sh [--check] FILE|user@host:path [--replace] [--replace-newer] [--port N]`** (new PC, in a copy
  of the code; a relative FILE is found from the folder it's run in): checks first, each failure saying what to
  do: not root, Linux, systemd with a working `systemctl --user`, Python 3.11+ with venv (`scripts/python.sh`: the
  system's python3, else the newest python3.14..3.11; Ubuntu 22.04 needs `python3.11 python3.11-venv`), port 8787
  (or N) free: else the program, its pid and service (via `sudo ss` when needed), "If you don't know what it is,
  don't stop it", and `--port` with a free port. On a PC Next Box moved away from it explains `--undo` or moving
  back with `--replace`. `--check` stops there (plus the time zone and pypi.org) and changes nothing. Then scp if
  needed ("type yes"), `movecheck.py` (seconds), the packages (`install_packages`: a pip failure says whether
  Python is too old, pypi.org can't be reached, or neither), `move info`, stopping a Next Box already running here
  (only now), `move unpack` (a failure restarts what it stopped; exit 6 with Next Box not running here means an
  earlier run stopped before the install, so it says "Finishing the setup..." and carries on with install.sh),
  `scripts/install.sh` (keeps the `.env`), turning
  the tunnel back on when the marker listed it, `nextbox doctor`, the next steps (`--tunnel-here` when it turned
  the tunnel back on: "The internet tunnel is on again here") with every ✗ from the summary
  repeated, the tunnel offered (y/N; the token only ever goes into go-online.sh's silent prompt), the status square
  suggested on a desktop, and when to `--forget` and delete the move file.
- **`scripts/go-online.sh`** installs cloudflared with apt (no apt: it says where to get it and stops before asking
  for the token), and when a tunnel already runs here compares its tunnel id (the token's `t`, from `systemctl cat
  cloudflared`) with the pasted one. It always ends by fetching `$PUBLIC/version`: "Done" only when that's Next
  Box; otherwise what to set in the dashboard (`<host> → HTTP → localhost:<port>`), or, when this PC already runs
  another tunnel: delete the hostname from Next Box's old tunnel first (and a leftover DNS record), then add it to
  this one; `NEXTBOX_TUNNEL=shared` goes into `.env`.
- **The MOVED-AWAY guard** (`<data dir>/MOVED-AWAY`, 600): `nextbox serve` (and `server.build_default_app`) exit with
  78 and "Next Box was moved to another PC on <day>, so it doesn't start here ... scripts/move-out.sh --undo";
  install.sh's unit has `RestartPreventExitStatus=78`, and with the marker install.sh skips the tests, leaves the
  service disabled and says why (so `update.sh` out of habit can't bring it back); `nextbox doctor` (and `--json`,
  for the status square) shows it as the first line, a note (•, not a problem: exit 0, "Nothing to fix here: Next
  Box runs on the new PC now."), and stops there. Every other CLI command that could print, serve
  or change data refuses too (exit 78; `clip` says it with notify-send for the hotkey; tests never call the real
  notify-send, a conftest fixture records it); `move`, `doctor`, `db`, `status` and `printed` still work. A marker
  that can't be read still counts.

## 34. Friends and notes
`nextbox/friends.py` (its top has the API contract: every path, request and answer), endpoints in `server.py`,
the label in `render.render_friend_note`, printing in `jobs.print_friend_note`. Snapchat-like: add friends, send
them a note, a doodle, a photo or a heart; read what they sent in a full-screen feed that scrolls like video apps.
- **Friends**: everyone has a friend link, `<NEXTBOX_PUBLIC_URL or this address>/app/#friend=CODE` (10 characters
  with no 0/O/1/I, `friend_codes`; **New link** rotates it and the old one stops working), to copy, share
  (`navigator.share`) or show as a QR code (`GET /friends/code/qr.png`, the `qrcode` package, pinned). Opening a
  link asks first (`GET /friends/code/{code}`, 60 an hour). Or type an exact username: the answer is always "If
  that name is right, they'll see your request.", whether the account exists, blocked you, or was already asked,
  and the asker's own list shows only the name typed (never a display name), so nobody can find out who has an
  account. A request to a name nobody has (or from someone blocked) is kept `hidden`: never shown to anyone,
  and someone who takes that name later never sees it; asking again once that account exists is a new request
  that reaches them (the account is newer than the request, so they never saw or declined it). A request must
  be accepted; if both ask, they're friends straight away, even at the same moment (`Friends.request` tries a
  second time and finds the other's request). Declining is quiet (the asker's request just fades), and it
  stands: taking the request back and asking again doesn't show it again. Requests nobody answers fade after
  30 days. Remove a friend or take a request back (`DELETE /friends/{name}`: a request taken back stays,
  shown to nobody, as `withdrawn` until it fades, so it still counts towards the day's 20). **Block** (only
  someone who is a friend, asked, or sent you a note) ends the friendship, takes their notes out of your feed,
  and hides their requests from you; a request they'd sent stays on their side, unanswered, like a quiet no,
  so they aren't told. The Blocked list shows a display name only when you could already see it (a friend, a
  request you saw, a note), else the username typed (`blocks.shown`), and upkeep never removes a block on a
  name with no account. Unblock doesn't make you friends again. Friends can be in any household: the one
  deliberate, consented way to reach someone outside yours (§1 rule 9). Only existing accounts (invite-only
  sign-up stays). 20 requests in any 24 hours, counted in the database (taken-back ones too).
- **Notes** (`friend_notes`): `note` (words, 280 characters and 12 lines at most, blank lines in a row kept to
  one, on a color from `render.NOTE_COLORS`, printed as that color's pattern band), `draw` (a finger doodle: black
  ink, kept as a 1-bit PNG, at most 1200 px), `photo` (camera or library, optional caption of 140) and `heart` (the
  ♥ next to a friend sends one in a tap; or from the note sheet). Sent to one or more accepted friends (20 at a
  time); 30 to one friend and 100 in all in any 24 hours, each over the limit a plain sentence (429, "…in the
  last 24 hours. Try again later."). The send screen shows "how it prints" (`POST /notes/preview`,
  `/notes/preview/image`: the 1-bit label on a 62 mm roll, nothing kept; a friend's die-cut printer may make it
  smaller, and the screen says so without showing whose printer).
- **Pictures are cleaned on the server** (`friends.clean_photo`, `clean_doodle`): opened by Pillow (anything else:
  415), only JPEG/PNG/WebP/GIF/HEIC (HEIC with the `heic` extra, else `scan.HEIC_HELP`) for photos and
  PNG/JPEG/WebP for doodles, turned upright, at most 1600 px and about 450 KB, re-encoded into a new image so no
  EXIF, location, ICC or XMP is kept. Photos up to 15 MB in (`/notes/image` is allowed 16 MB like `/scan`),
  doodles 3 MB and 4 megapixels; see-through canvases become black on white. A picture is decoded only after
  the cheap checks (`Notes.ready`: every recipient a friend, the day's limits), in a worker thread (never on
  the server's event loop) and two at a time at most (`picture_slots`); a big JPEG is decoded at 1/2 to 1/8 of
  its size (Pillow's draft), still at least 1600 px. Photo previews: 120 an hour.
- **The feed** (`GET /notes/feed`): unseen notes first, then the rest, each newest first, with a cursor. The app
  shows one per screen (CSS `scroll-snap-type: y mandatory`) with a right-side rail: ♥ react (`friends.REACTIONS`),
  🖨 Print, ↩ Reply (compose to that friend), 🔖 Keep, ⋯ (Delete for me, Block); the sender and time at the bottom
  left. A note is marked seen (`POST /notes/{id}/seen`) once 60% visible for about 1 s (IntersectionObserver);
  arrow keys and j/k on a desktop; prefers-reduced-motion respected; `GET /notes/count` for the tab's badge.
  The sender's **Sent** list says Delivered, Seen or Printed, or only Delivered when the recipient turned off
  `friend_receipts` ("Tell friends when I've seen or printed their notes", on by default). Reactions show there,
  except from someone you blocked (only Delivered, no reaction: nothing they do reaches you, and they aren't
  told).
- **Printing** (`POST /notes/{id}/print`): only by the recipient, only with `confirm: true`, which the app sends
  after "Send this to the printer?" (400 without). Nothing ever prints a note by itself (§1 rule 2): no automatic
  or per-friend auto-print. It goes to the recipient's own household printer (the "Notes from friends" label color
  rule, `printers.REASONS["friend"]`) through `printer.print_image` → `printer._send_raster`, with the same
  dry-run rules, Always preview only and the pause as every ticket (`jobs._printer_for`). The label is drawn at
  that printer's own width (a die-cut label's size, the picture, a heart and the words shrunk to fit one label;
  a long note runs onto a second only at the smallest size, `render._NOTE_SMALL`): "From <name> · <date>", the
  color band, then the words big, the doodle or the photo dithered to 1-bit with its caption, or a big heart and
  "<name> sent you a ♥". Emoji the label can't draw are left off (`render.printable`, `render.can_draw` and the
  fallback fonts). A household with no printer here gets a preview with `jobs.PRINT_AWAY` (or the no-printer
  note) and the app offers **Print on paper** (`GET /notes/{id}/paper.pdf`) and **Save label image**
  (`GET /notes/{id}/label.png?download=1`). A printed note is **not a ticket**: nothing goes in `tickets`, so no
  admin view shows it; only `printed` (a time) is set on the note.
- **Private**: notes are seen only by their sender and recipient. No admin or superadmin endpoint reads the four
  tables (`tests/test_friends.py` walks every admin, super, usage, debug and ticket endpoint); events carry
  counts only (`note_send {count}`, `note_print {status}`, `friend_request`, never a name, word, id or picture),
  and an unexpected error is logged as its type only (a message could quote a note). The words and pictures are
  stored nowhere but `friend_notes`, and `nextbox db sql` masks `body`, `image` and `reaction` (it still shows
  the Next Box computer's owner who is friends with whom and who sent what kind of note to whom, when; the
  privacy page says no admin *screen* shows them). Every `/friends`
  and `/notes` answer is `no-store`. The shared key gets 403.
- **Kept 30 days**: a note nobody **Kept** is deleted 30 days after it arrived, at startup upkeep and when a feed,
  the badge's count or Sent is read (at most once a minute): no timer or cron. Every read leaves out older ones
  anyway (`friends._alive`), so nothing past 30 days shows or opens before it's deleted. **Delete for me** takes it out of your feed or your Sent
  list; it's gone for good once both have. Download my data has your friends and your own sent and received
  notes (§22): a note you got with everything about it, one you sent with only what Sent shows (its status and
  reaction, never when it was seen or printed or whether it was kept); deleting an account deletes its friend link, friendships, requests, blocks and every note it sent
  or got; a household can only be removed once empty, and upkeep sweeps rows that name an account that's gone.
- **No AI** anywhere here.
- **In the app** (`web/app.js` "Friends and notes", `web/index.html` `#friends`): a **Friends** tab (in the phone's tab bar,
  `g` then `f`) with a number for unseen notes plus friend requests (`/notes/count`, read when Next Box opens, comes back
  to the front, and once a minute while it's in front: a read, nothing else). Three views: **Notes** (Watch, and a tile
  per note that opens the feed there), **Friends** (requests, your link with Share/Copy/QR code/New link, add by
  username, your friends with Send a note and ⋯ Remove/Block, Waiting for them, Blocked) and **Sent**. The feed is a
  full-screen dialog (Esc or ✕ closes it, focus goes back where it was); each note is a labelled region; a double tap
  sends ❤️. The note sheet (full screen on a phone) has Note/Draw/Photo/♥, the friends to send to, and "How it prints".
  Print opens a sheet with the label as it comes out of your printer, **Print** (a real print asks "Send this to the
  printer?" first), Print on paper and Save label image. When only a preview can happen (no printer here, the pause,
  previews only, or the person's own **Preview only** box), it says why and there's no Print button: the preview is
  already there, and Print on paper is the blue button. A friend link (`#friend=CODE`) is taken off the address, kept for this tab until you're signed in, and asked
  about once ("Add Lee as a friend?"), after the data notice and the setup questions.
