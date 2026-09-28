# Working on ticket

Read SPEC.md first. Its §1 rules override everything:

- Todoist is the only source of truth. Don't add a task cache or database.
- Never send a real print unless Mike says "print it". Use `ticket --dry-run ...` or `TICKET_DRY_RUN=1`.
- Never add an automatic print, cron or retry. The only automatic print is HA's `source="auto"`, guarded by `Store.claim_auto`.
- Deleting a Todoist task always needs Mike's explicit confirmation.
- Never print, echo or log `.env` values. `Settings.redacted()` is the only thing that's safe to show.
- LAN only.

Dev loop:
```
.venv/bin/python -m pytest -q
.venv/bin/ticket --dry-run today      # PNG path printed; open it
.venv/bin/ticket status
systemctl --user restart ticket       # after code changes, if the service is installed
```

Layout: `ticket/render.py`. Print paths: `ticket/jobs.py`, which the CLI and the server share.
Read-back: `ticket/scan.py`. Tests use `FakeTodoist` and a fake vision function, and a
fixture fails the test if anything tries to reach the printer.
