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
   There's no web sign-up and no cloud identity provider. Home Assistant and scripts use the
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

