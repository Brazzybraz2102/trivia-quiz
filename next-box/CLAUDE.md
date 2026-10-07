# Working on Next Box

Read SPEC.md first. Its §1 rules override everything:

- Each person's own to-do app is the only source of truth. Don't add a task cache or database.
  New apps go in `nextbox/providers/` behind `TaskProvider`. Never let one person reach another's tasks or tickets.
- Never send a real print unless Mike says "print it". Use `nextbox --dry-run ...` or `NEXTBOX_DRY_RUN=1`.
- Never add an automatic print, cron or retry. The only automatic print is HA's `source="auto"`, guarded by `Store.claim_auto`.
- Deleting a task always needs the person's explicit confirmation.
- Never print, echo or log `.env` values. `Settings.redacted()` is the only thing that's safe to show.
- LAN only. People sign in with local accounts (`nextbox/auth.py`). Don't add web sign-up or a cloud login.
- Roles are enforced server-side in `server.py` (`require`, `can_manage`). Don't add "log in as user".
  Never put hashes, tokens or `.env` values in events, diagnostics or debug bundles. Log admin actions with `audit()`.
- Feedback is anonymous: `feedback_identities.json` is the ONLY place a person is linked to feedback.
  Never log feedback to events, never put usernames in `feedback.json`, and audit reveals by feedback id only.

Dev loop:
```
.venv/bin/python -m pytest -q
.venv/bin/nextbox --dry-run today      # PNG path printed; open it
.venv/bin/nextbox status
systemctl --user restart nextbox       # after code changes, if the service is installed
```

Layout: `nextbox/render.py`. Print paths: `nextbox/jobs.py`, which the CLI and the server share.
Read-back: `nextbox/scan.py`. Connections: `nextbox/connections.py` + `nextbox/vault.py`.
Tests use `FakeTodoist`, a fake vision function and a real in-process Radicale server for CalDAV, and a
fixture fails the test if anything tries to reach the printer.
