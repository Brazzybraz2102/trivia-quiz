# Working on Next Box

Read SPEC.md first. Its §1 rules override everything:

- Each task lives in one list: the built-in list (`tasks` table, `nextbox/mylist.py`, SPEC §17) or a linked app.
  Never copy, cache or sync a linked app's tasks into the database.
  New apps go in `nextbox/providers/` behind `TaskProvider`; calendars behind `CalendarSource` (read-only, never stored).
  Never let one person reach another's tasks, calendars or tickets.
- Any address a person types (CalDAV, .ics links) goes through `providers.http.check_public_url` before Next Box fetches it.
- Never send a real print unless Mike says "print it". Use `nextbox --dry-run ...` or `NEXTBOX_DRY_RUN=1`.
- Never add an automatic print, cron or retry. The only automatic print is HA's `source="auto"`, guarded by `Store.claim_auto`.
- Deleting a task always needs the person's explicit confirmation.
- Never print, echo or log `.env` values. `Settings.redacted()` is the only thing that's safe to show.
- LAN only for now. People sign in with local accounts (`nextbox/auth.py`). New accounts need an invite code; never add
  open sign-up or a cloud login. The protected admin (`Accounts.protected()`) must always stay an active superadmin.
- The .env printer belongs to the Home household only (`jobs._fallback`). Other households preview until they add one.
- Thermal printers print black: a tag's color picks the roll (printer) and its print pattern (`render.PATTERNS`).
- Roles are enforced server-side in `server.py` (`require`, `can_manage`). Don't add "log in as user".
  Never put hashes, tokens or `.env` values in events, diagnostics or debug bundles. Log admin actions with `audit()`.
- Everyone accepted the data notice (server.py `DATA_NOTICE`): the superadmin may see their tickets, scans,
  settings and activity. If you make more visible, update the notice and bump `DATA_NOTICE_VERSION`.
- Printers: every real send goes through `printer._send_raster`. Validate addresses in `printers.validate`.
- Journal pages (`nextbox/journal.py`) are private to their owner: no admin view, export only to the owner,
  masked in `nextbox db sql`. `task_slides` keeps task ids and counts only, never text.
- Wins (`wins` table, `journal.Wins`) are private like journal pages: owner-scoped, no admin view, export only
  to the owner. Keep `content` only for My list tasks; a linked app's task text is never stored there.
- AI (Anthropic) is called only when the person taps: the AI helpers (`nextbox/coach.py`: Break it down,
  Look back on my week) and Snap's read-back. Never add an automatic or background AI call.
  - Send only what that one helper needs. Journal notes go only with `include_notes`; crisis words
    (`coach.CRISIS`) are never sent or logged.
  - Never log or store what was sent or what came back: events carry only `{ms, steps}` or the kind of failure.
  - Break it down never writes to a linked app (a new My list task, or steps printed on a card, instead).
  - No `ANTHROPIC_API_KEY`: say so (`coach.NO_KEY`, `coach.NO_KEY_OWNER` for a superadmin) instead of calling.
- Feedback is anonymous: the `feedback_identities` table is the ONLY place a person is linked to feedback.
  Never log feedback to events, never put usernames in `feedback`, and audit reveals by feedback id only.
- `nextbox db sql` stays read-only on the real database and keeps masking hashes and secrets (`dbtools.py`).

Dev loop:
```
.venv/bin/python -m pytest -q
DATABASE_URL=postgresql://... .venv/bin/python -m pytest -q   # same suite on PostgreSQL
.venv/bin/nextbox --dry-run today      # PNG path printed; open it
.venv/bin/nextbox status
systemctl --user restart nextbox       # after code changes, if the service is installed
```

Layout: `nextbox/render.py`. Print paths: `nextbox/jobs.py`, which the CLI and the server share.
Read-back: `nextbox/scan.py`. Connections: `nextbox/connections.py` + `nextbox/vault.py`.
Tests use `FakeTodoist`, a fake vision function (`create_app(vision=...)`), a fake coach
(`create_app(coach=...)`) and a real in-process Radicale server for CalDAV. They never reach Anthropic
(a conftest fixture hides any `ANTHROPIC_API_KEY` in your environment), and a fixture fails the test if
anything tries to reach the printer.
