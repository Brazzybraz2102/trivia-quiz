# Next Box on Render: the guide for tomorrow morning

This takes Next Box off the home server and puts it at a hosting company, Render, at the same address as
before, `https://nextbox.braznarkel.com`. Your accounts, lists, journals, friends' notes and printer settings come
along. About an hour and a half, in eight steps, each one screen. Do them in order; every step ends with a check.

What changes for people: nothing in the app. What changes for printing: for now everyone prints from their
phone, with **Print on paper** (a PDF for any printer) and **Save label image** (a PNG for a label printer's own
app), the way households without a Next Box printer already do. The Brother at home prints again once the
small "print helper" for the home server exists; that's a later job, not tonight's.

Rules that stay: nothing prints without a person's tap and confirm; no open sign-up (testers get an invite);
secrets go only into Render's own settings forms and the migration script's hidden prompt, never into a chat
(the script puts the two keys on your clipboard without showing them).
Dashboard names are from Render's and Cloudflare's pages as of November 2026 (each step says its source; what
couldn't be checked is marked **unverified**). If a button has moved, look for the same word nearby.

Words used below: the **service** is the Next Box web app on Render (the Blueprint names it `nextbox`); the
**database** is the PostgreSQL Render makes next to it (named `nextbox-db`); the **Blueprint** is the file
`next-box/render.yaml` in the code, which tells Render what to build. Settings a step names (`NEXTBOX_HOSTED`,
`DATABASE_URL`, ...) are **environment variables**: name = value pairs on the service's **Environment** page.

## 1. A Render account (10 minutes)
**NEEDS MIKE:** your GitHub login.

1. Open https://dashboard.render.com/register and choose **Sign in with GitHub** (GitLab and Bitbucket are the
   other choices; GitHub is the right one here, because that's where the code is: Render then sees the repository
   without a second login). Allow Render on GitHub when it asks. (source: https://render.com/docs/github)
2. Render asks which repositories it may see. Allow **brazzybraz2102/trivia-quiz** (or all repositories). If the
   repository is private, Render installs its GitHub App for it; the same page handles that.
   (source: https://render.com/docs/github)
3. You land on the Dashboard. The Blueprint picks the smallest paid plans (about $13 a month for both; step 6
   says why and how to go Free instead), so have a card ready: Render asks for one before it makes anything paid
   (where exactly in the flow is **unverified**; signing up itself needs none).

Check: the Dashboard shows an empty workspace and your GitHub name at the top right.

## 2. Create the service and the database from the Blueprint (30 minutes)
**NEEDS MIKE:** at the home server (`braznarkel-ranchnet`) as `mike`, with Render open in its browser.

1. Get this version of the code onto the home server first (the script used below is new):
   ```bash
   ~/.local/share/nextbox/app/scripts/update.sh
   ```
   (It keeps `.env`, the accounts and the data; it runs the tests, 5-10 minutes.)
2. Dashboard → **+ New** (top right) → **Blueprint**. (source: https://render.com/docs/infrastructure-as-code,
   https://render.com/tutorials/firstdeploy/create-a-blueprint)
3. Pick **brazzybraz2102/trivia-quiz** → **Connect**. Name the Blueprint `next-box`. Set the **branch** to
   `claude/ticket-todoist-printer-nldz8a` and the **Blueprint Path** to `next-box/render.yaml` (Render looks for
   `render.yaml` at the top of the repository unless you give it the path; a custom path has been possible since
   February 2026: https://render.com/changelog/blueprints-now-support-custom-filenames-and-paths). Where the
   field sits on the form is **unverified**; it may also be on the Blueprint's **Settings** page afterwards.
4. Render reads the file and lists what it will make: one **Web Service** (Docker, root directory `next-box`,
   health check `/health`, the custom domain `nextbox.braznarkel.com`) and one **PostgreSQL** database. It asks
   for the three values the file marks as yours to fill in (`sync: false`), in a form, before anything is built.
   Render asks for these only this once, and later Blueprint syncs keep what you typed
   (source: https://render.com/docs/blueprint-spec, https://community.render.com/t/sync-false-in-render-blueprint-not-working/12445):
   - `NEXTBOX_SECRET_KEY`: the one line of the home server's `secret.key`, which unlocks everyone's saved to-do app
     connections (Todoist, CalDAV, ...). In a terminal on the home server run
     ```bash
     ~/.local/share/nextbox/app/scripts/cloud-migrate.sh --copy secret
     ```
     It puts the line on the clipboard without showing it; click into Render's field and paste (Ctrl+V). It never
     goes to Claude, a chat or a note. (Over ssh, without a clipboard, the script names the file to open in a text
     editor instead; copy the line from there and close the editor.)
   - `ANTHROPIC_API_KEY`: the same way, `~/.local/share/nextbox/app/scripts/cloud-migrate.sh --copy anthropic`,
     then paste. (Snap and the AI helpers; with no key at home, leave it empty and the app says so.)
   - `NEXTBOX_CONTACT`: how people reach you with questions (an email address, say), shown on the Privacy and About
     pages. Left empty, the pages say "ask the person who invited you"; you can add it later on the Environment page.
   Afterwards copy something else (any word), so the key isn't left on the clipboard.
   The file sets the rest for you: `NEXTBOX_HOSTED=1` (a Next Box at a hosting company: every visitor comes from
   the internet, there's no printer and no home network here), `DATABASE_URL` from the database it makes,
   `NEXTBOX_PUBLIC_URL=https://nextbox.braznarkel.com`, `NEXTBOX_HOSTING=Render` and `NEXTBOX_OPERATOR=Braznarkel`
   (named on the Privacy and About pages), `TZ=America/New_York` (so "today" turns over at your midnight, not
   UTC's), `NEXTBOX_FORWARDED_ALLOW_IPS=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1` (only Render's
   own proxies, which reach the service from private addresses, are believed about who a visitor is, so sign-in
   limits count each visitor's real address and nobody can make one up; step 4 checks it), a random `NEXTBOX_KEY`
   (the shared key Home Assistant uses at home; a hosted Next Box refuses it, so nothing uses it here), and nothing
   for `NEXTBOX_PORT`: Render tells the app its port with `PORT`, and Next Box listens there when `NEXTBOX_PORT`
   isn't set (source: https://render.com/docs/web-services).
5. Click **Deploy Blueprint** (the button may say **Apply**). Render builds the Docker image and starts it. The
   first build takes a few minutes, and the service may restart once or twice while the database is still being
   made; that's normal, don't redeploy. (source: https://render.com/tutorials/firstdeploy/create-a-blueprint)
6. Open the service (Dashboard → `nextbox`). Wait until the status says **Live**. Render only switches a new
   version on once `/health` answers; if it doesn't within 15 minutes, the deploy is cancelled and the **Logs**
   page says why (source: https://render.com/docs/health-checks).

Check: the service's address, `https://<something>.onrender.com` (shown under its name), opens the sign-in
page, and `https://<something>.onrender.com/health` shows `{"ok":true}`. Nobody can sign in yet: the database is
empty. Leave the `.onrender.com` address open; you'll use it in step 3.

## 3. Bring the data over from the home server (20 minutes)
**NEEDS MIKE:** at the home server (`braznarkel-ranchnet`), as `mike`, in a terminal.

1. Stop Next Box at home, so nothing changes there after the copy (step 7 turns it off for good):
   ```bash
   systemctl --user stop nextbox
   ```
2. In Render, open the **database** (Dashboard → `nextbox-db`). On its page, find **Connect** (top right;
   on some pages the same lives under **Info** → Connections) and copy the **External Database URL**. It starts
   with `postgresql://`, holds the database's password, and is only for connections from outside Render, like
   this one: Render only accepts them encrypted, and the script adds `sslmode=require` if the address doesn't
   say so. (source: https://render.com/docs/databases, https://render.com/tutorials/postgres-on-render/connection-strings)
3. On the home server:
   ```bash
   ~/.local/share/nextbox/app/scripts/cloud-migrate.sh
   ```
   It explains itself in four lines, then asks you to paste the External Database URL. Nothing shows while you
   paste; that's on purpose (the address holds the password, and it never appears on screen or in a command).
   It first only checks the database: reachable, encrypted, empty. Then it asks you to type `COPY`, copies every
   table (journal photos and friends' pictures included), and counts every table again on the other side before
   keeping anything. It ends with the counts and the next steps. Nothing on the home server's data changes.
   - "Empty: NO" means the hosted database already has an account (someone signed up, or a `NEXTBOX_FIRST_ADMIN`
     was added by hand). Nothing is copied. Step 8 says what to do.
4. Restart the hosted Next Box with the data: service → **Manual Deploy** (top right) → **Deploy latest commit**.
   Wait for **Live**. (source: https://render.com/docs/deploys)
5. Optional, once the Check below works: close the database to the outside. Render → `nextbox-db` → **Access
   Control** → remove the rule that allows every address (`0.0.0.0/0`). The service reaches the database from
   inside Render, so nothing in Next Box needs it open; the copy was the only outside use. (The page's wording is
   **unverified**; the database also stays protected by its password and encryption if you skip this.)
   (source: https://render.com/docs/postgresql-creating-connecting)

Check: at the `.onrender.com` address, sign in as yourself with your usual password. Your list and journal are
there, Settings → Linked app says connected, and Admin → Users shows everyone. If sign-in works but Linked app
says it can't unlock the connection, `NEXTBOX_SECRET_KEY` isn't exactly the file's line (step 8).

## 4. The address: nextbox.braznarkel.com (15 minutes, then a wait)
**NEEDS MIKE:** your Cloudflare login (dash.cloudflare.com).

1. Render: service → **Settings** → **Custom Domains**. `nextbox.braznarkel.com` is already listed (the
   Blueprint added it; don't add it again). Next to it Render shows the DNS record it wants: a **CNAME** from
   `nextbox` to the service's `<something>.onrender.com` address. Copy that target and leave this page open.
   (source: https://render.com/docs/custom-domains, https://render.com/docs/blueprint-spec)
2. Cloudflare: the old tunnel record has to go first, or the name points at the home server. **Zero Trust →
   Networks → Tunnels** (newer dashboards: **Networking → Tunnels**) → `Braznarkel` → **Routes** tab → delete
   the `nextbox.braznarkel.com` route. Then **DNS → Records** for `braznarkel.com`: if a `nextbox` record is
   still there (type CNAME, pointing at `<long id>.cfargotunnel.com`), delete it too. Deleting a route doesn't
   always remove the record it made (**unverified**; just look).
   (source: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/dns/)
3. Cloudflare → **DNS → Records** → **Add record**: type **CNAME**, name `nextbox`, target the
   `<something>.onrender.com` from Render, **Proxy status: DNS only** (the grey cloud, not the orange one) →
   **Save**. (With the grey cloud, Cloudflare's **SSL/TLS** mode doesn't come into it: Render's own certificate
   does the encrypting.) (source: https://render.com/docs/configure-cloudflare-dns)
   - Why grey: Render has to see the name point at itself to verify it and make its certificate; with the orange
     cloud, Cloudflare answers instead and the check fails. Render's docs say DNS only; its staff say switching
     to proxied afterwards works too (**unverified**, from a Render community thread). Recommendation: leave it
     grey. Nothing in Next Box needs Cloudflare in front any more (`NEXTBOX_HOSTED=1` already treats every
     visitor as coming from the internet), and one fewer moving part means one fewer thing to debug.
4. Back in Render → **Custom Domains** → **Verify**. DNS changes take a few minutes to spread; if it fails, wait
   five minutes and click Verify again. Once verified, Render makes the certificate (minutes).
   (source: https://render.com/docs/custom-domains)

Check: `https://nextbox.braznarkel.com/health` shows `{"ok":true}` in your phone's browser with Wi-Fi off, and the
app signs you in. Everyone's existing sign-ins on their phones keep working (same address, same accounts). The
`.onrender.com` address keeps working too; share only the real one.

Two quick checks that sign-in limits see real addresses (step 2's `NEXTBOX_FORWARDED_ALLOW_IPS`):
- On your phone with Wi-Fi off: Profile → **Signed-in devices**. The address next to this phone should be your
  phone company's (search "what is my IP" in the same browser to compare), not one starting with `10.`,
  `172.` or `192.168.`. If every device shows the same address that isn't yours, send Claude that address (it's
  Render's proxy, not a secret).
- On the home server, try a made-up visitor address six times with a wrong password:
  ```bash
  for i in 1 2 3 4 5 6; do curl -s -o /dev/null -w '%{http_code}\n' -H "X-Forwarded-For: 192.0.2.$i" \
    -H 'Content-Type: application/json' \
    -d '{"username":"nobody-check","password":"wrong"}' https://nextbox.braznarkel.com/auth/login; done
  ```
  The last line must say `429` (too many tries): the made-up addresses didn't help. Then wait 5 minutes before
  signing in from the home server's browser (its address is the one that was paused). If all six say `401`, tell
  Claude: the limits are being fooled.

## 5. Testers (10 minutes per person, mostly theirs)
Sign in as the admin → **Admin → Invite people** → **Make invite**, one per person. Tick **their own
household** for a tester who isn't family (own list, own printers; they can't see or print on yours), and
**beta** so they get the feedback button on every screen. Send each person their link (`/app/#join=...`); it
works once. They open it on their phone, choose a username and password, read and accept the data notice, and
land on the Get started checklist and the Quick start page (Settings → Help has it too). Invites are made only
in the app now: the home server's `nextbox` command no longer reaches the live data (an invite made there would
go into the old home database and never work).

What they see when they print: a preview, then **Print on paper** (a PDF their phone prints on any printer) and
**Save label image** (a PNG for a label printer's own app). The app says so in plain words: "This Next Box runs
online, so labels can't go straight to a printer. Tap Print on paper, or Save label image and open it in your label
printer's app (like Brother iPrint&Label)." Your Brother at home prints again once the print helper exists; until
then you print the same way as everyone else. **Admin → Printers** says this Next Box runs online and shows no
printers. Your Brother's settings were copied and wait for the print helper; nothing to do. "Find printers" is
off on a hosted Next Box (it would scan the hosting company's network, not yours).

Check: one tester signs in, adds a task, taps Print, gets the preview and the two buttons, and sends feedback.
You see the post on the Feedback board and their name under Admin → Users.

## 6. Cost, the free plan, updates, logs, stopping (read once)
The Blueprint picks the smallest paid plan for each thing: **Starter** for the service ($7 a month: 0.5 CPU,
512 MB, always awake; source: https://render.com/docs/compute-plans) and **Basic-256mb** for the database ($6 a
month plus $0.30 per GB of storage; source: https://render.com/pricing, https://render.com/docs/postgresql-refresh),
about $13 a month together. The review list (step 2.4) shows both before anything is made. Prices are what the
search engine quoted from Render's pages on 2026-11-24; the pages themselves weren't opened, so check
https://render.com/pricing once.

One real choice, with the Free plans:
- **Keep both paid** (recommended). Testers start tomorrow, and the first impression is the app answering at once.
- **Free service, paid database.** Saves $7. A **Free** web service **spins down** after 15 minutes without a
  visitor and takes about a minute to wake on the next one (the first tap of the day feels broken, then works); a
  workspace gets 750 free hours a month, enough for one service (source: https://render.com/docs/free). After the
  first deploy: service → **Settings** → **Instance Type** → Free. Go back to Starter the same way, as soon as a
  tester mentions the wait.
- **Everything Free.** Only to look, never for testers: a **Free** database expires 30 days after it's made (14
  days' grace, then Render deletes it and the data), has 1 GB and no backups (source: https://render.com/docs/free,
  https://render.com/changelog/free-postgresql-instances-now-expire-after-30-days-previously-90). If you ever
  do, put "upgrade the database" in your calendar for day 20.

Custom domains are included on free web services too (source: https://render.com/docs/free); each domain beyond the
workspace's included number costs $0.25 a month (**unverified** how many are included; you need one). Updates:
when new code is pushed to the branch, Render builds and deploys it by itself (auto-deploy, on by default; it
can be turned off under the service's **Settings**), with no downtime: the old version serves until the new one
answers `/health` (source: https://render.com/docs/deploys, https://render.com/docs/health-checks). Logs:
service → **Logs** (live, or a time range); each deploy's own log is on **Events**
(source: https://render.com/docs/logging). Stopping: service → **Settings**, at the very bottom, **Suspend**
(from a Render support answer; **unverified** whether a suspended paid service is still billed; a Free one
costs nothing either way). Deleting: the same page, **Delete**.

## 7. The home server afterwards (10 minutes, then a week)
Once step 4 checks out, two copies of Next Box exist: the hosted one everybody uses, and the old one at home
with the data as it was at step 3. Only one may be in use, or lists drift apart. Keep the old one as a backup
for a week, off:
```bash
~/.local/share/nextbox/app/.venv/bin/nextbox move marker set --off service --off cloudflared
systemctl --user disable --now nextbox
sudo systemctl disable --now cloudflared
```
The first line writes the same "moved away" marker `scripts/move-out.sh` writes, so Next Box refuses to start at
home (also after a reboot, or an `update.sh` out of habit) and `doctor` says it moved. The tunnel stays
installed but off; it was in a broken state anyway. Home Assistant's morning print stops (the key `NEXTBOX_KEY`
in Home Assistant is for the home network only, and a hosted Next Box refuses it from the internet); it comes
back with the print helper. Changed your mind? `~/.local/share/nextbox/app/scripts/move-out.sh --undo` turns
the home copy back on, but it refuses while Next Box answers at `nextbox.braznarkel.com`, so stop using the hosted
one first.

After a week with no problems: `~/.local/share/nextbox/app/scripts/move-out.sh --forget` deletes the old data
folder, the code, the service and the hotkey from the home server, and offers to remove the tunnel program. The
`.env` goes with it: copy `ANTHROPIC_API_KEY` to Render first if you haven't (step 2: `cloud-migrate.sh --copy
anthropic`, then service → Environment → Edit). Do keep one backup
somewhere: before `--forget`, `~/.local/share/nextbox/app/.venv/bin/nextbox db backup` makes a copy in
`~/.local/share/nextbox/backups/`; move that file to another disk.

## 8. When something doesn't work
- **Live, but the page says 503 or waits a long time.** A Free service waking up: wait a minute and try again. If
  it never comes, service → **Logs**: a `DATABASE_URL` the database refused, or `/health` not answering. **Manual
  Deploy → Deploy latest commit** after fixing the setting; **Clear build cache & deploy** if the build itself
  looks stale. (source: https://render.com/docs/deploys)
- **The migration says "Empty: NO".** The hosted database has an account: someone signed up (with an invite),
  or a `NEXTBOX_FIRST_ADMIN` was added by hand. Nothing was copied, and the script can be run again any number
  of times. Easiest fix: make a fresh database. Render → the database → **Settings** → **Delete Database**, then Dashboard → **Blueprints**
  → `next-box` → **Manual Sync** to make it again from the file (**unverified** that the sync recreates a deleted
  database; if not, **+ New → PostgreSQL**, then put its **Internal Database URL** into the service's
  `DATABASE_URL` on the Environment page). If `NEXTBOX_FIRST_ADMIN` is on the Environment page, delete it
  first, so it can't happen twice. Then run `cloud-migrate.sh` again.
- **The migration says "Reachable: no".** The address isn't the External Database URL (the internal one only
  works inside Render), or it was pasted with a piece missing, or the home server has no internet. Copy it again
  from the database's **Connect** menu. If Render's database has an **Access Control** list, this computer's
  address must be allowed (the default allows everyone; **unverified**). If you closed it in step 3.5, add your
  home address back for the copy, then remove it again.
- **Sign-in works, but Settings → Linked app says the connection can't be unlocked** (or Snap and the AI
  helpers say the key is wrong). `NEXTBOX_SECRET_KEY` isn't exactly the line from `secret.key`: a character
  missing, a space or a line break. On the home server run `cloud-migrate.sh --copy secret` again, then Render →
  service → Environment → `NEXTBOX_SECRET_KEY` → Edit → paste → save → Manual Deploy. Nothing is lost: the connections unlock as soon as the key is right. (A wrong key
  never shows anyone another person's data; it only keeps the connections locked.)
- **nextbox.braznarkel.com doesn't open, or shows a Cloudflare error page.** The record is still the tunnel's
  (step 4.2), or the new CNAME is proxied (orange cloud): set it to **DNS only**. Render → Custom Domains →
  **Verify** again, wait five minutes, try the phone with Wi-Fi off (your home network may still have the old
  answer cached). A certificate error means Render's certificate isn't made yet: wait, then Verify.
- **A tester's invite link says it was used.** Each link works once; make another.
- **Locked out of the admin account (forgot the password).** Another superadmin can't reset the protected admin;
  only a shell on the server can. Render → service → **Shell** (paid instances like Starter have it, Free ones
  don't; source: https://render.com/docs/ssh), then type `nextbox user passwd mike` and the new password twice
  (nothing shows while you type). No Shell? From the home server, with the database's External URL (Access
  Control must let the home address in, step 3.5): `read -rs DATABASE_URL` (paste, Enter; nothing shows), then
  `export DATABASE_URL NEXTBOX_DATA_DIR=$(mktemp -d)`, then
  `~/.local/share/nextbox/app/.venv/bin/nextbox user passwd mike`, then `unset DATABASE_URL NEXTBOX_DATA_DIR`.
  (The empty data folder gets past step 7's "moved away" marker; with `DATABASE_URL` set the command works on the
  hosted data, without it only on the old home copy.)
- **Starting from nothing (no home data to copy).** Not your path, but for a fresh copy: on the Environment page
  add `NEXTBOX_FIRST_ADMIN` = `username:password`, deploy, sign in (it asks for a new password), then delete the
  variable. It only ever makes an account while the database has none. `NEXTBOX_SECRET_KEY` can then be any 32+
  random characters.
- **Something else.** Service → **Logs**, copy the last lines (they never hold secrets), and send them to
  Claude with what you clicked. `nextbox doctor` on the home server knows nothing about Render.
