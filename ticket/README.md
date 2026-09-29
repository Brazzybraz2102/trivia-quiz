# ticket

Print your Todoist lists on a Brother QL-1110NWB. Mark them up with a pen, snap a photo, and the
changes go back to Todoist. Everything runs on your desktop, on your home network only.

## Install on the desktop (about 15 minutes)

You'll need your Todoist API token (Todoist → Settings → Integrations → Developer), the
printer's IP address (hold the printer's info button to print its settings, or check your
router), and an Anthropic API key (console.anthropic.com).

### 1. Get the code
```bash
cd ~/Desktop/"custom projects"
git clone -b claude/ticket-todoist-printer-nldz8a https://github.com/brazzybraz2102/trivia-quiz.git ticket-src
cp -r ticket-src/ticket ./ticket && rm -rf ticket-src
cd ticket
```

### 2. Install and fill in secrets
```bash
sudo apt install -y python3-venv wl-clipboard libnotify-bin   # Fedora: sudo dnf install python3 wl-clipboard libnotify
scripts/install.sh          # first run: builds the venv, runs the tests, creates .env, then stops
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # copy this: it's your TICKET_KEY
nano .env                   # paste TODOIST_TOKEN, PRINTER_IP, TICKET_KEY, ANTHROPIC_API_KEY
```

### 3. Check it without printing
```bash
.venv/bin/ticket status                 # want "printer_reachable": true
.venv/bin/ticket --dry-run today        # prints a PNG path; open it to see the label
xdg-open "$(.venv/bin/ticket --dry-run --json today | python3 -c 'import sys,json;print(json.load(sys.stdin)["png"])')"
```

### 4. First real print
Load 62 mm continuous tape (DK-22205), then:
```bash
.venv/bin/ticket today
```
If it comes out faint, cramped or rotated, tell Claude and it'll tune `ticket/render.py`.

### 5. Run it as a service (starts at boot)
```bash
scripts/install.sh          # second run: installs the systemd user service and enables linger
```
Then create your sign-in (the password is typed twice and never shown):
```bash
.venv/bin/ticket user add mike
```
The install script ends by printing the web app address, `http://<desktop-ip>:8787/app`. Open
that on any phone or laptop on your Wi-Fi and sign in. You stay signed in for 30 days per device.

Accounts are managed only on the desktop; the web app has no sign-up page:
| Want | Run |
|---|---|
| Add someone | `ticket user add <name>` |
| Change a password (signs out their devices) | `ticket user passwd <name>` |
| Remove someone | `ticket user remove <name>` |
| See accounts | `ticket user list` |

Everyone who signs in uses the same Todoist account, the one whose token is in `.env`.

### 6. Hotkey: copy text, press a key, it's a task with a ticket
```bash
scripts/hotkey.sh '<Super><Shift>t' --dry-run   # try it safely first
scripts/hotkey.sh '<Super><Shift>t'             # then for real
```

### 7. Home Assistant
1. Copy `ha/ticket.yaml` to `<ha config>/packages/ticket.yaml`, and make sure
   `configuration.yaml` has `homeassistant: { packages: !include_dir_named packages }`.
2. Add `ticket_print_today_url: http://<desktop-ip>:8787/print/today` and `ticket_key: ...` to `secrets.yaml`.
3. Replace `binary_sensor.CHANGEME_office_presence` and `input_button.CHANGEME_ticket_button`
   with your entity IDs, then reload YAML.
4. Test that the guard works. The first call prints, the second returns `skipped`:
   ```bash
   curl -XPOST -H "X-Ticket-Key: $KEY" -H 'content-type: application/json' \
        -d '{"source":"auto","dry_run":true}' http://localhost:8787/print/today
   ```
   Run it twice. Dry-run and real auto prints are tracked separately, so this test doesn't use up
   the real print for the day.

## Everyday use
| Want | Do |
|---|---|
| Today's list | web app → Print today, or `ticket today` |
| A project or filter | `ticket list '#Groceries'` |
| One task | `ticket task <id>` |
| A note | `ticket text "Call Sam back"` |
| Clipboard → Todoist + ticket | your hotkey |
| Read back | Mark ✓ done, → tomorrow or ✗ drop, then web app → Read back → photo. Deletes and anything uncertain wait for Confirm/Skip. CLI: `ticket scan photo.jpg`, then `ticket confirm <scan-id>` |
| History | web app → Printed, or `ticket printed` |
| Sign out | web app → Account → Sign out |

## Troubleshooting
- `printer_reachable: false`: check that the printer is on the same Wi-Fi and give it a DHCP reservation so its IP stays fixed.
- Service logs: `journalctl --user -u ticket -f`
- After changing code or `.env`: `systemctl --user restart ticket`
