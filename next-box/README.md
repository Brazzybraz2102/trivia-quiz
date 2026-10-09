# Next Box

Print your to-do list on a thermal label printer. Mark it up with a pen, snap a photo, and the
changes go back to your to-do app. Everything runs on your desktop, on your home network only.

**Printers:**
- Brother QL / PT
- ESC/POS receipt printers: Epson, Star and most 58/80 mm models
- ZPL label printers: Zebra and compatibles
- TSPL label printers: TSC, Munbyn, iDPRT, Xprinter
- any printer installed on the computer, through CUPS: Dymo, Rollo, USB printers

Bluetooth-only phone printers like Phomemo and Niimbot can't be used: they keep how they work private.

Load different **label colors** in different printers and send each kind of ticket to its color.
The default sends overdue tasks to red labels.

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
scripts/install.sh          # builds everything, makes your NEXTBOX_KEY, starts the service
.venv/bin/nextbox doctor    # checks it all and says what to fix
```
Later, when you have them, put `PRINTER_IP` (and `ANTHROPIC_API_KEY` for photo read-back) in
`.env` with `nano .env`, delete the `NEXTBOX_DRY_RUN=1` line, and run
`systemctl --user restart nextbox`. Until then Next Box makes previews instead of printing.

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
| user | print, read back, their own Settings, send and read anonymous feedback |
| admin | support: add people, reset passwords, turn accounts off/on, sign people out, mark beta testers, see activity, reply to feedback (never sees who wrote it) |
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

### Managing everyone (super admin)
The **Users** tab (superadmins only) lists every person in every household. Search, filter by
role or status, or tap a number at the top (Turned off, Beta testers, Had errors...). **Manage**
opens one person: change their email, role or household, turn them off, reset their password,
sign them out everywhere, reset their settings, unlink their to-do app, see their recent activity
and devices, or delete them (type their username to confirm). Tick several people to sign out,
turn off/on, or change beta for all of them at once. **Export CSV** downloads the list.

### Helping a beta tester
1. Admin tab: mark them **beta**. Everyone has a **Feedback** button on every screen, guided
   or open-ended.
2. Problems show up on the **Feedback tab** for everyone, without names. Set a status and reply
   publicly. If you need to know who sent a post (for example, to look at their errors), a
   superadmin taps **Who sent this?**. That's recorded in the audit log. The private details,
   including any errors the person chose to attach, live only in the `feedback_identities` table.
   **Activity** (Admin tab) shows what a person did.
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

## Printers and label colors
- **Adding printers (admins):** **Admin → Printers → Add a printer**. Pick the kind, enter its
  address (an IP like `192.168.1.60`, a USB device like `/dev/usb/lp0`, or the CUPS printer name)
  and say which **label color** is loaded. Use **Test (preview)** first, then **Test print**.
  When you swap a roll, change its color there.
- **Choosing colors (everyone):** **Settings → Printers & label colors** sets which color each
  kind of ticket uses: overdue, urgent, daily list, lists, single tasks, notes, new tasks.
  **Overdue tasks on their own ticket** splits today's list, so expired tasks come out on red.
- **No printer with that color loaded?** The ticket goes to your usual printer and the app tells
  you why.
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

- **Just one thing** at the top: the one task to do now, with its next tiny step. Tap **Done ✓**,
  **Print it big**, or **Not now** to see the next one.
- **Brain dump**: type everything on your mind, one per line. Shortcuts are optional:
  `#errand` tag, `!` urgent, `tomorrow` / `fri`, `3pm`, `~15m` how long, `every day`.
- **Edit** a task to add tiny steps (the first one should take two minutes), a time, a repeat or tags.
- **Stickers**: one small label per task, to stick on the bill, the door, the car keys.
  **Tear-off strips**: one label you cut into strips. Pick tasks with the boxes on the left, or
  pick nothing to print today's.
- **Tags & colors** (Settings): each tag has a color. Stickers go to the printer loaded with that
  color of labels. On plain white labels each color prints as its own pattern (red is solid black,
  yellow is dots, blue is stripes...), so you can still tell them apart at a glance.
- The **daily ticket** shows 5 tasks at most, the next tiny step under each, gentle wording and
  yesterday's wins. Change any of it under Settings → Focus & ADHD helpers.

Linking Todoist or a CalDAV app (Settings) is optional: its tasks show up next to yours and stay
in that app.

Colored labels for the QL-1110NWB: it prints black only, so color comes from the roll. Brother
makes 62 mm yellow film tape (DK-22606). Other colors come from third-party 62 mm DK-compatible
rolls; check the listing says it fits the QL-1100/1110. Add each loaded roll as a printer (Admin → Printers) with its label color.

From the command line:
```bash
.venv/bin/nextbox add "Pay water bill #urgent today ~15m" "Call dentist fri 10am"
.venv/bin/nextbox --dry-run stickers          # today's tasks, one sticker each
.venv/bin/nextbox --dry-run strips '#errand'  # tear-off strips for a tag
.venv/bin/nextbox --dry-run focus             # Just one thing
.venv/bin/nextbox doctor                      # checks the setup and says what to fix
```

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

## The database (and learning SQL)
Everything Next Box keeps (accounts, printed tickets, scans, printers, feedback, activity) is in
one SQLite file: `~/.local/share/nextbox/nextbox.db`. Your tasks are never copied there; they stay
in each person's to-do app.

```bash
.venv/bin/nextbox db where        # where the database is
.venv/bin/nextbox db tables       # what's in it
.venv/bin/nextbox db demo         # build a practice database full of fake data
.venv/bin/nextbox db sql --practice "SELECT username, role FROM users"
.venv/bin/nextbox db backup       # safe copy, even while the service runs
```

`DATABASE.md` is a step-by-step SQL course on the practice database. Queries on the real database
are read-only, and password hashes and to-do app secrets are always hidden.

## Troubleshooting
- **Start here:** `.venv/bin/nextbox doctor` checks the key, accounts, service, web page, firewall and printers, and says what to fix.
- `printer_reachable: false`: check that the printer is on the same Wi-Fi and give it a DHCP reservation so its IP stays fixed.
- Service logs: `journalctl --user -u nextbox -f`
- Service won't start, with status 218 or "namespace" errors: your system doesn't allow the
  service's sandboxing. Delete the `ProtectSystem=`, `ReadWritePaths=` and `PrivateTmp=` lines in
  `~/.config/systemd/user/nextbox.service`, then run `systemctl --user daemon-reload`.
- "Connect your to-do app first": that person hasn't connected one yet (Settings → Your to-do app).
- Old tickets and scans are deleted after 90 days (`NEXTBOX_RETENTION_DAYS`). Run `nextbox prune` to clean up now.
- After changing code or `.env`: `systemctl --user restart nextbox`
