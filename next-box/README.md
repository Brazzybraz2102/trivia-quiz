# Next Box

Print your to-do list on a Brother QL-1110NWB. Mark it up with a pen, snap a photo, and the
changes go back to your to-do app. Everything runs on your desktop, on your home network only.

Each person signs in and connects **their own** to-do app:
- **Todoist**
- **any CalDAV task list**: Nextcloud Tasks, Fastmail, Synology, Zoho, Radicale, DAVx⁵, and older
  iCloud Reminders lists

Google Tasks, Microsoft To Do and TickTick are next. Each needs a one-time sign-in setup.
Apple Reminders (current lists), Things, Any.do and Google Keep don't allow other apps to connect.

## Install on the desktop (about 15 minutes)

You'll need:
- the printer's IP address (hold the printer's info button to print its settings, or check your router)
- an Anthropic API key, for photo read-back (console.anthropic.com)

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
scripts/install.sh          # first run: builds the venv, runs the tests, creates .env, then stops
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # copy this: it's your NEXTBOX_KEY
nano .env                   # paste PRINTER_IP, NEXTBOX_KEY, ANTHROPIC_API_KEY
```

### 3. Create your account and connect your to-do app
```bash
.venv/bin/nextbox user add mike                 # first account = superadmin (you)
.venv/bin/nextbox connect mike todoist          # or: caldav. Prompts for the token or password
```
You can also connect later in the web app, under **Settings → Your to-do app**.

### 4. Check it without printing
```bash
.venv/bin/nextbox status                 # want "printer_reachable": true
.venv/bin/nextbox --dry-run today        # prints a PNG path; open it to see the label
xdg-open "$(.venv/bin/nextbox --dry-run --json today | python3 -c 'import sys,json;print(json.load(sys.stdin)["png"])')"
```

### 5. First real print
Load 62 mm continuous tape (DK-22205), then:
```bash
.venv/bin/nextbox today
```
If it comes out faint, cramped or rotated, tell Claude and it'll tune `nextbox/render.py`.

### 6. Run it as a service (starts at boot)
```bash
scripts/install.sh          # second run: installs the systemd user service and enables linger
```
The install script ends by printing the web app address, `http://<desktop-ip>:8787/app`. Open
that on any phone or laptop on your Wi-Fi and sign in. You stay signed in for 30 days per device.

The first account you create is the **superadmin**, which is you. After that, add people from
the web app (**Admin → Add someone**). They get a one-time password and pick their own when they
first sign in. There's no public sign-up page.

| Role | Can do |
|---|---|
| user | print, read back, their own Settings, send feedback |
| admin | support: add people, reset passwords, turn accounts off/on, sign people out, mark beta testers, see activity and feedback |
| superadmin | debug: everything above, plus roles, per-user debug mode, diagnostics, the error log, debug bundles, pause all printing, auto-print switch, banner |

If you're ever locked out of the web app, fix it from the desktop:
| Want | Run |
|---|---|
| Add someone | `nextbox user add <name> [user\|admin\|superadmin]` |
| Change a password (signs out their devices) | `nextbox user passwd <name>` |
| Change a role | `nextbox user role <name> admin` |
| Turn an account off/on | `nextbox user disable <name>` / `nextbox user enable <name>` |
| Remove someone | `nextbox user remove <name>` |
| See accounts | `nextbox user list` |

### Helping a beta tester
1. Admin tab: mark them **beta**. They get a Feedback button on every screen.
2. When they report a problem, read it in the **Feedback inbox** along with their recent errors.
   **Activity** shows what they did.
3. For anything odd, a superadmin turns on **Debug** for that person, asks them to repeat it, then:
   - looks at the **Debug tab → Event log** (filter by their name)
   - or downloads their **Debug bundle** and hands it to Claude
4. To stop real prints while you investigate, use **Debug tab → Pause all printing**.

Everyone connects their own to-do app under **Settings → Your to-do app**. Tokens and passwords
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
3. Replace `binary_sensor.CHANGEME_office_presence` and `input_button.CHANGEME_nextbox_button`
   with your entity IDs, then reload YAML.
4. Test that the guard works. The first call prints, the second returns `skipped`:
   ```bash
   curl -XPOST -H "X-NextBox-Key: $KEY" -H 'content-type: application/json' \
        -d '{"source":"auto","dry_run":true}' http://localhost:8787/print/today
   ```
   Run it twice. Dry-run and real auto prints are tracked separately, so this test doesn't use up
   the real print for the day.

## Everyday use
| Want | Do |
|---|---|
| Today's list | web app → Print today, or `nextbox today` |
| A project or filter | `nextbox list '#Groceries'` |
| One task | `nextbox task <id>` |
| A note | `nextbox text "Call Sam back"` |
| Clipboard → to-do app + ticket | your hotkey |
| Read back | Mark ✓ done, → tomorrow or ✗ drop, then web app → Read back → photo. Deletes and anything uncertain wait for Confirm/Skip. CLI: `nextbox scan photo.jpg`, then `nextbox confirm <scan-id>` |
| History | web app → Printed, or `nextbox printed` |
| Sign out | web app → Account → Sign out |

## Troubleshooting
- `printer_reachable: false`: check that the printer is on the same Wi-Fi and give it a DHCP reservation so its IP stays fixed.
- Service logs: `journalctl --user -u nextbox -f`
- Service won't start, with status 218 or "namespace" errors: your system doesn't allow the
  service's sandboxing. Delete the `ProtectSystem=`, `ReadWritePaths=` and `PrivateTmp=` lines in
  `~/.config/systemd/user/nextbox.service`, then run `systemctl --user daemon-reload`.
- "Connect your to-do app first": that person hasn't connected one yet (Settings → Your to-do app).
- Old tickets and scans are deleted after 90 days (`NEXTBOX_RETENTION_DAYS`). Run `nextbox prune` to clean up now.
- After changing code or `.env`: `systemctl --user restart nextbox`
