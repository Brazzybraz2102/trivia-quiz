# Next Box: spec

Each person's to-do app ↔ Brother QL-1110NWB label printer, running on Mike's desktop and reachable only on the home LAN.

## 1. Rules (these override everything else)
1. **Each task lives in exactly one list, and that list is its source of truth.** Everyone has a
   built-in list ("My list", the `tasks` table, §17). A linked app (Todoist, CalDAV) is optional;
   its tasks are shown and printed next to the built-in ones but never copied, cached or synced
   into the database. The database (§16) also keeps accounts, print history, ticket manifests
   (row → task ID), scan results, the auto-print guard, printers, tags, feedback and the activity log.
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
   unless the CLI gets `--user`.

## 2. Hardware
- Brother QL-1110NWB on Wi-Fi, raw TCP port 9100.
- 62 mm continuous tape (DK-22205), 696 px wide at 300 dpi. Labels are 1-bit and cut after each print.

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
- **Header:** the big day name and the date, then the legend `✓ done → move ✗ drop`.
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
`GET /printed` · `GET /printed/{id}/png` · `POST /scan` (multipart `photo`, optional `label_id`) ·
`GET /scans` · `GET /scan/{id}` · `POST /scan/{id}/confirm {decisions:{row:"confirm"|"skip"}}` ·
`/app` (web app) · `/docs` (OpenAPI).

Auth: `POST /auth/login {username,password}` sets an HttpOnly, SameSite=Strict `nextbox_session`
cookie that lasts 30 days. With `want_token: true` it returns a bearer token instead, for the
native app (`Authorization: Bearer ...`). `POST /auth/logout` ends the session and `GET /auth/me`
says who's signed in. Home Assistant and scripts send `X-NextBox-Key`. Keys are never accepted in
URLs.

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
- **Dependencies:** pinned in `constraints.txt`. The installer also upgrades pip and setuptools.
- **systemd:** `Restart=always`, `UMask=0077`, `ProtectSystem=strict` with write access only to the data dir.

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
- Every send goes through `_send_raster`, one job at a time.
- Only the Brother path is hardware-tested. The others are tested at the byte level, plus a real
  TCP send and a stand-in `lp`.

**Printer list:** the `printers` table, managed by admins. Each printer has a name, driver, address,
width and dpi, plus the **label color loaded** and ink. Addresses must be one of:
- host[:port]
- `/dev/usb/lp*`, `/dev/lp*`, `ttyUSB`, `ttyACM` or `rfcomm`
- `usb://0xVVVV:0xPPPP`, for Brother only
- a CUPS queue name

Anything else is refused. On the first run after upgrading, `PRINTER_IP` becomes the first printer.

**Routing:** every ticket has a reason: overdue, urgent, today (daily list), list, task, note or
new_task. Each person's `color_rules` maps reasons to label colors (default: overdue → red), and
`choose()` sends the ticket to a printer with that color loaded. If none has it, the ticket goes
to their usual printer with a note.

**Overdue on their own ticket:** with `split_overdue` on, today's list prints overdue tasks as a
separate "Overdue" ticket. One auto print can therefore produce two tickets; it's still one
guarded auto print.

## 15. Admin visibility (everyone agreed)
- **The notice:** at first sign-in everyone sees `DATA_NOTICE` and taps "I understand". It's
  recorded as `consent: {version, at}`. Bump `DATA_NOTICE_VERSION` if what's visible changes.
- **The superadmin can see:**
  - `/super/usage`: per-person activity, tickets by reason, read-back outcomes, settings, last
    active, notice date, and a daily chart
  - `/super/users/{name}/tickets`: everyone's ticket images and scans
- **Still never visible:** to-do app secrets. Feedback identity stays in
  `feedback_identities` and is never part of usage.
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
- **Admins:** set a status (new / seen / planned / fixed / won't fix / hidden), reply publicly,
  or remove a post. Hidden posts are seen only by admins and the author. Admins never see who
  wrote anything.
- **Superadmins:** can look up who sent a specific post ("Who sent this?"). Each lookup is
  written to the audit log by feedback ID only, so the audit log never names the sender.

**Limit:** scrubbing can't catch everything, such as a stranger's name or a street address. The
form asks people to leave personal details out, and admins can hide any post that slips through.

## 10. Roles, settings, support and debugging
**Everyone** (Settings tab):
- **Account:** change password (signs out other devices), list signed-in devices, sign out
  other devices.
- **Printing:** always preview only; rows per ticket (3–20); show "Also waiting"; 24-hour
  times; default list filter.
- **Read-back:** apply confident marks automatically, on or off; confidence needed (50–95%).
  Drops always wait for confirmation, whatever these say.
- **Theme:** per device. **Feedback:** see §13.

**Admin** (support):
- add people with a one-time temporary password (the person must choose their own at first sign-in)
- reset passwords the same way
- turn accounts off or on
- sign people out
- mark beta testers
- read a person's activity, without tracebacks
- respond to feedback publicly, set its status, hide posts that slipped personal details through (§13)

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
   returns structured output: the label code and, for each row, a mark (done, tomorrow, drop or
   none) with a confidence score.
2. The server finds the label by `label_id`, falling back to the code it read. If neither matches,
   nothing changes.
3. Rows marked ✓ done or → tomorrow with confidence ≥ 0.7 are applied right away: `close`, or
   `due_date` = tomorrow. It sets `due_date`, never `due_string`, so recurring rules survive.
4. **Every ✗ drop and every row below 0.7** goes to `needs_confirmation`. The app shows
   Confirm/Skip for each one, and `POST /scan/{id}/confirm` applies only rows still `pending`,
   so a double tap can't delete twice.
5. Row numbers that aren't on the label are ignored and reported.

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
### Phase B: phone apps (Expo): not started
- [ ] B1–B5. The Expo app from `ticket-app.zip` isn't in this repo yet. Its `src/api.ts` should call
      the endpoints above; the Read back screen maps onto `/scan` + `/scan/{id}/confirm`.

## 16. Database
Everything Next Box keeps is in one database (`nextbox/db.py`, SQLAlchemy Core):
- **SQLite** by default: `<data dir>/nextbox.db` (`~/.local/share/nextbox/nextbox.db`), WAL mode,
  chmod 600. Nothing to install.
- **PostgreSQL** when `DATABASE_URL` is set (`pip install -e '.[postgres]'`). Same code, same tables.

Tables: households, users, sessions, invites, tickets (record JSON + PNG), scans, auto_guard,
events, server_settings, printers, feedback, feedback_identities. `nextbox db diagram` draws them.

**Learning kit** (`nextbox/dbtools.py`, tutorial in `DATABASE.md`):
- `nextbox db demo` builds a separate practice database (`<data dir>/practice/nextbox.db`) full
  of fake households, people, tickets and events.
- `nextbox db sql "..."` is read-only on the real database. Writes need `--practice --write`.
- Output always hides password hashes, salts, session hashes and to-do app secrets.
- `nextbox db backup` makes an online copy in `<data dir>/backups/` (600).

## 17. My list, tags and ADHD support
**My list** (`nextbox/mylist.py`): every person's built-in to-do list. Built-in task ids look like
`nb:12`; linked apps' ids are used as they are. `Merged` sends each action (done, move, delete) to
the list the task came from. New tasks go to My list unless the `new_tasks_to` pref says `linked`.
Repeating tasks (daily, weekdays, weekly, monthly) jump to the next date after today when done, so
they never pile up as overdue.

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
- **Tear-off strips**: one label, one strip per task with dashed cut lines.
- **Just one thing**: one task, huge, with its tiny steps and "start by" time (due time minus the
  estimate). Picks the first task today's ticket would list unless one is chosen.
Read-back works on all of them: each box has its printed row number.

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
- Bulk: sign out, turn off, turn on, beta on/off for everyone selected. Bulk skips the caller.
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
lines, as many pages as needed; any other ticket 3.5 in wide, split across pages if long. Sticker
images are kept per ticket in `tickets.parts` (a small zip of PNGs). Next Box never sends the PDF to
a printer; the person prints it from their device.

## 22. Your account (the round button, top right)
Profile and email (`PATCH /auth/profile`; sign in with username or email), change password, signed-in
devices (sign out the others), settings, help, feedback, refresh, **download my data**
(`GET /auth/export`: account, settings, tags, tasks, linked app and calendar names, tickets without
images, read-backs, devices; never passwords, hashes or tokens), sign out, and **delete my account**
(`POST /auth/delete-account`: password + typed username; removes the account, tasks, tags,
tickets, scans and activity; feedback stays, unlinked; the protected admin and the last superadmin
can't). The login page has show-password and "Forgot your password?" (an admin gives a one-time
password). Superadmins see a banner when a newer version is published (`GET /super/update-check`,
reads the published pyproject.toml at most every 6 hours).

## 23. Profile page and settings
**Profile** tab (also from the account menu): name to show (`display_name` pref), email, password,
signed-in devices, what the admin can see, download my data, delete my account. **Settings** has a
jump bar (General, Focus, Tags, Calendars, Linked app, Printing, Read-back, Help); General holds
theme, text size (`text_size`), the screen it opens to (`start_tab`), 24-hour times and where new
tasks go. `/app/` is served with `app.js?v=<version>-<build>` and every `/app` response says
`Cache-Control: no-cache`, so phones and Cloudflare never keep showing an old version.

## 24. Standard app pages and navigation
Static pages in `web/`: privacy.html, terms.html, about.html, guide.html, 404.html (shared
pages.css/pages.js; operator, contact, version and retention come from the public `GET /about`, set
by `NEXTBOX_OPERATOR` and `NEXTBOX_CONTACT`). Unknown addresses get the 404 page when a browser asks for
HTML, JSON otherwise. `POST /auth/signup` needs `agree: true`; the sign-up event records
`terms_version` (`TERMS_VERSION`). Phones (≤720 px) get a bottom tab bar with a More sheet holding
the other tabs and the legal/help links; an offline banner follows the browser's online state.

## 25. Journal, wins and slugs (the main app's daily routine)
`nextbox/journal.py`. The routine: today's label on the left page of a paper journal, notes on the right,
one photo of both pages at night.
- **Read-back reads notes too**: `ReadBack.notes` holds handwritten text outside the label, transcribed
  as written (`[unclear]` for unreadable words). `scan_photo` keeps the photo as a journal page when there
  are notes, or always when the `journal_mode` pref is on (default). A photo with notes but no label is
  saved to the journal instead of failing. A daily ticket's page is filed under the ticket's day.
- **Journal pages** (`journal` table): id, owner, household, day, notes (editable), photo (JPEG),
  scan_id, label_id. Strictly private: every query is scoped to the owner, no admin or superadmin
  endpoint shows them, and `nextbox db sql` masks `notes` and `photo`. Exported (without photos) by
  `/auth/export`; deleted with the account. Deleting a page needs `confirm=true`.
- **Wins** (`wins_by_day` in server.py): `task_done` events + My list `done_at` + read-back "done" marks
  (applied or confirmed). Names come only from My list and read-backs; linked-app names are never stored,
  so those show as "+N more".
- **Slugs**: `task_slides` counts moves to a later day per task id (no text), from read-back → marks and
  from My list due-date changes to a later day; cleared when the task is done. A slug is moved
  `slug_after` (default 3) times or waiting 7+ days. The daily label shows "slid N×" and the Journal tab
  lists them with "Make it smaller" (My list steps) and "Print just this" (focus card).
- **Journal mode** label: a "Today's win:" write-in line and "Notes on the right page → snap both pages".
- **Paper sizes**: `a5` and `half_letter` join letter and a4 for Print on paper.
- Endpoints: `GET /journal?until=&days=`, `GET /journal/wins?days=`, `GET /journal/slugs`,
  `POST /journal` (typed note), `PATCH|DELETE /journal/{id}`, `GET /journal/{id}/photo`.
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
  a short snippet as `parts` `[{t, m}]` with the matches marked. Opening one shows that day and older, with
  "Back to today".
- **Each day** in `GET /journal` also has `ticket` ({id, kind, title, dry_run}: the ticket a page that day
  was read back from, else that day's daily ticket, a real print before a preview) shown as a thumbnail with
  Print on paper, and `changes` ({moved, dropped}: task names read-back moved or dropped that day, undone ones
  left out). A day with only changes is listed too.
- **Download my journal**: `GET /journal/export?format=zip|pdf&since=&until=&paper=`. zip: a
  `next-box-journal-<date>/` folder with README.txt and a folder per day holding notes.txt (win, then each
  page with when it was snapped or written), photo-N.jpg and wins.txt. pdf: a page per day (more if long:
  "(continued)", "2 of 3") with the win, each photo, the typed notes and the wins, drawn with Pillow at 200 dpi
  on the `paper_size` paper (`paper.journal_pdf`; `paper.JpegPdf` writes one page at a time, so a long
  journal never sits in memory). `GET /journal/export/summary` says days, pages, photos, wins, the date range,
  about how big, and whether a download is allowed now. 10 downloads an hour; like Download my data, they wait
  3 days after an admin set a one-time password. Linked from the Journal tab, Profile and the delete-account
  panel ("Download your journal first").
- **Print my journal page**: `GET /journal/export?format=pdf&day=YYYY-MM-DD` opens that day's PDF (inline, 60
  an hour, no reset wait: it shows nothing the Journal tab doesn't).
- `Store.list_printed(until=)` bounds tickets by day, like `list_scans`.

## 27. FD: gentle AI helpers and monday.com
**AI helpers** (`nextbox/coach.py`): only when the person taps a button, never automatic, 20 an hour per
person for both together (in memory). Off with the `ai_helpers` pref (default on, Settings → Focus):
both endpoints then answer 409 and nothing is sent. Without `ANTHROPIC_API_KEY` they answer 503 "Ask your
Next Box owner to add an AI key." and `GET /auth/me` says `ai_ready: false`, so the app shows that instead
of a button. Model: `NEXTBOX_COACH_MODEL` (default `claude-opus-5-5`, in `Settings.redacted()` and
diagnostics). Each call: `client.beta.messages.parse` with a pydantic output format, effort low, the
server-side refusal fallback (`fallbacks="default"`), 60 s timeout, 2 SDK retries; stable instructions
(no dates or names) and the person's data in the user message inside `<task>`/`<week>`, marked as data.
A refusal, an unreadable answer, a rate limit, a timeout, a bad key or an outage each become a short,
plain message. Events record only that a helper ran (`ai_breakdown {ms, steps}`, `ai_look_back {ms}`) or
the kind of failure: never what was sent or what came back. `create_app(coach=...)` takes a fake in tests.
- **Break it down**: `POST /list/{id}/breakdown {hint?}` (My list or linked task, the caller's own) sends
  only the task's name, minutes, due day, steps and the optional "what makes it hard" (300 characters),
  never its description, and returns 3-6 tidied steps plus `task {id, content, local, steps, due_date,
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
  `empty` without asking the AI. When the notes (or a task name) hold crisis words (`coach.CRISIS`), or the
  model sets `concern`, the only answer is `support`: a kind nudge to reach out to someone they trust or a
  local crisis line (988 in the US); crisis words are never sent or logged. "Save to my journal" saves
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
- **Brain dump preview**: `POST /list/dump?preview=1` only parses: no task, tag (not even the starter tags) or
  event is written. It returns `lines` (each line as `parse_dump` makes it, problems and ticked lines too),
  `dropped` (lines with no task words, like "fri" alone), `more` (lines past 200), `to`, `new_tags`,
  `over_tags` (past the 30-tag limit: they stay in the words) and `notes`. The app shows it under Brain dump
  450 ms after typing stops (only the newest answer), with chips for day, time, repeat, how long, urgent and
  tags; it can be folded away (remembered on the device). A failed preview just hides.
- **Bulk actions**: with one or more tasks picked, the print bar adds ✓ Done, → Tomorrow, Tag… and Delete….
  They go one task at a time through the single-task endpoints, so linked tasks act in their own app; the bar
  shows progress and a failure names the task. Done and → Tomorrow each get one Undo for the whole batch. Tag…
  adds or takes off a tag on the picked My list tasks. Delete… lists the tasks and asks "Delete N tasks? This
  can't be undone." (Cancel has focus), then sends `DELETE /list/{id}?confirm=true` per task.
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
  screens with big buttons: how you'll print (label printer / regular printer / no printer yet → `print_with`,
  `always_dry_run`), a paper journal (A5 / half letter / letter / A4 / no → `paper_size`, `journal_mode`), how many tasks
  a day (3 / 5 / 8 → `max_rows`), what helps (any of: short lists and gentle words → `gentle_words`; easy-to-read text →
  `reading_mode`; same routine every day → `routine_mode`; or "just the basics", which turns all three off) and when you
  plan and look back (`routine_morning`, `routine_evening`, default 08:00 and 18:00). Any question, or the rest, can be
  skipped (Esc does what the Skip button says); the answers are saved together at the end, and the last screen lists
  what changed and where to change it. Skipping counts as done. People already using Next Box aren't interrupted:
  Settings → Help → Run setup again, and the Get started guide's "Make it fit your brain" step, open it. Answering how
  many tasks or what helps ticks that step; a regular printer or no printer puts the printer step away.
- **What helps is never kept as such.** Only the ordinary settings it changes are saved (anyone can switch them in
  Settings); the event log records which settings changed, never the answers.
- **Paper hints**: with `print_with` paper or none, the Preview box and each preview's note say what to do next
  ("tap Print on paper") instead of "Always preview only is on".
- **Easy-to-read text** (`reading_mode`, Settings → General): Atkinson Hyperlegible (SIL OFL; `web/fonts/` with
  OFL.txt), line height 1.6, letter spacing .03em, no all-capitals headers, bigger and darker small print, in light and
  dark. Labels drawn for that person use the same font (`render.reading_font`, set in `jobs._finish`), take the symbols
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
- **Routine card** (top of My list; `routine_card`, default on, Settings → Focus), from `GET /printed?limit=5`, `GET
  /scans?limit=1` and FC's `routine_morning`/`routine_evening`. Quiet before the plan time. From it, while there's no
  `today` ticket today (a preview counts, a failed print doesn't) and something is due: "Good morning. Print today's label",
  whose button is My list's Today's label (a preview, or "Send this to the printer?"), never a print by itself. From the
  look-back time with no snap since today's label: "How did today go? Snap your pages" → Snap. After that snap (or any snap
  today in the evening): "You finished N today ✓" (`done_today`), "Pages snapped ✓" when N is 0. Each has Not today /
  Hide, for today on this device (with Undo). The words match Schedule's "Plan your day" and "Look back" rows.
- **Focus timer** on Just one thing: "⏱ Start 5 min", plus "or N min" when the task has its own estimate (1–90). A
  countdown ring with Pause/Resume, Stop (Undo) and a chime switch; the card keeps that task while it runs, and the timer
  goes when the task is done, moved or set aside. The time comes from when it started and is kept on this device per
  person (`nb.timer:<user>`: the task id and times, never its words), so switching tabs, locking the phone or reloading
  doesn't lose it; one that ran out over 30 minutes ago is dropped. At the end: two soft notes made with Web Audio (only
  after a tap allowed sound, and only with the chime on), a whole green ring and Keep going / Done ✓ (Step done ✓ with an
  open step) / Not now ("5 minutes of focus. That counts ✓"). No ring motion with reduced motion; screen readers hear the
  start, pause, 1 minute left and the end. On another screen, or scrolled away, the note at the top says "Time's up".
- **Reminders** (`remind_before`: minutes, default 10, 0 = Off): a note at the top of every screen from N minutes before a
  task with a time today until 30 minutes after, with Done ✓, Tomorrow, Snooze 10m and ✕ (snoozed and dismissed ones are
  kept on this device for the day, ids only). Done and Tomorrow use the usual endpoints and show Undo in the note. Made
  from My list as last loaded, fetched again quietly when a timed task is near and the list is 5+ minutes old. **Show a
  notification too** (`remind_notify`, default off) asks the browser for permission only when it's switched on; they show
  only while Next Box is open in the background (https or the installed app; phones through the service worker).
- **Quick add**: the + in the header (or `n`) on every screen: Brain dump's rules and preview (`POST /list/dump?preview=1`),
  added with `POST /list/dump` (so `new_tasks_to` applies), Ctrl/⌘+Enter adds. Closing keeps what was typed.
- **App shortcuts and Share to Next Box** (`manifest.json`): Add a task (`/app/#add`), Snap a page (`#snap`), Print today
  (`#print`: the Print tab with Print today focused; nothing prints). `share_target` posts title, text, url and photo to
  `/app/share`. `web/sw.js`, the app's only service worker, answers that on the device: a photo waits in Cache Storage for
  a moment and opens Snap attached (read only after Read back); text opens Quick add filled in (`#add&text=`, each part
  once: `server.share_text`). It caches nothing else, has no push, and otherwise only brings Next Box forward when a
  notification is tapped. Without it, the server's `POST /app/share` (no sign-in, nothing kept or logged) does the same
  for text and sends a photo to Snap to pick again, without reading it. These addresses wait for sign-in, the data notice
  and the setup questions, then leave the address bar.
- **Keyboard shortcuts** (desktop): `n` or `+` Quick add, `/` search My list (Schedule switches to List for this visit),
  `g` then `l`/`p`/`s`/`j` My list, Print, Snap, Journal, `?` the list (also Settings → Help), Esc closes. Ignored while
  typing in a box or with a sheet open. Ctrl/⌘+Enter also saves a journal note.

