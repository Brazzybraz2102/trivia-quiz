# Working on Next Box

Read SPEC.md first. Its §1 rules override everything:

- Each task lives in one list: the built-in list (`tasks` table, `nextbox/mylist.py`, SPEC §17) or a linked app.
  Never copy, cache or sync a linked app's tasks into the database.
  New apps go in `nextbox/providers/` behind `TaskProvider`. Never let one person reach another's tasks or tickets.
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
Tests use `FakeTodoist`, a fake vision function and a real in-process Radicale server for CalDAV, and a
fixture fails the test if anything tries to reach the printer.
