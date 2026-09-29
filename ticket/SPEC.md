# ticket: spec

Todoist ↔ Brother QL-1110NWB label printer, running on Mike's desktop and reachable only on the home LAN.

## 1. Rules (these override everything else)
1. **Todoist is the only source of truth.** There's no local task database. The data dir only
   keeps print history, label manifests (row → task ID), scan results and the auto-print guard.
2. **Nothing prints unless Mike asks.** The one exception is the once-a-day `source="auto"` print
   of today's list when Home Assistant sees him enter the office. The server guards it
   (`Store.claim_auto`), so a second auto call that day returns `skipped`. There are no
   crons, no retries that reprint, and no other automatic prints.
3. **Deleting a task always needs explicit confirmation**, including when read-back finds one.
4. **Dry-run for development**: `--dry-run`, `TICKET_DRY_RUN=1`, or `dry_run: true` in the API.
5. **Secrets live only in `.env`** (chmod 600). `/status` and `ticket status` show whether each
   one is set, never its value.
6. **LAN only**: no cloud relay and no remote access. The server binds to 0.0.0.0:8787.
7. **Sign-in**: people sign in to the web app with local accounts that live only on the desktop
   (`ticket user add/passwd/remove/list`). There's no web sign-up and no cloud identity provider.
   Home Assistant and scripts use the shared `X-Ticket-Key` header instead.

## 2. Hardware
- Brother QL-1110NWB on Wi-Fi, raw TCP port 9100.
- 62 mm continuous tape (DK-22205), 696 px wide at 300 dpi. Labels are 1-bit and cut after each print.

## 3. Print types
| Type | CLI | API | Manifest |
|---|---|---|---|
| Today (+ overdue) | `ticket today` | `POST /print/today {source}` | yes |
| Auto today (HA) | `ticket today --auto` | `POST /print/today {"source":"auto"}` | yes |
| Any filter | `ticket list '#Groceries'` | `POST /print/list {query,title}` | yes |
| One task | `ticket task <id>` | `POST /print/task {task_id}` | yes (1 row) |
| Note | `ticket text ...` | `POST /print/text {text}` | no |
| New task + ticket | `ticket text --todo ...`, `ticket clip --todo` | `POST /print/text {text,todo:true}` | yes |

The API also takes `"dry_run": true` on every print endpoint.

## 4. Label layout
Title and date, then numbered checkbox rows with tags on the right (`late`, `p1`/`p2`, `↻` for
recurring), then the legend `✓ done → tomorrow ✗ drop` and a `#code` in the bottom-right corner.
The code is the label ID that read-back uses to find the manifest.

## 5. Endpoints
`GET /health` · `GET /status` · `GET /tasks?query=` · `POST /print/{today,list,task,text}` ·
`GET /printed` · `GET /printed/{id}/png` · `POST /scan` (multipart `photo`, optional `label_id`) ·
`GET /scans` · `GET /scan/{id}` · `POST /scan/{id}/confirm {decisions:{row:"confirm"|"skip"}}` ·
`/app` (web app) · `/docs` (OpenAPI).

Auth: `POST /auth/login {username,password}` sets an HttpOnly, SameSite=Strict `ticket_session`
cookie that lasts 30 days. With `want_token: true` it returns a bearer token instead, for the
native app (`Authorization: Bearer ...`). `POST /auth/logout` ends the session and `GET /auth/me`
says who's signed in. Home Assistant and scripts send `X-Ticket-Key`. Keys are never accepted in
URLs.

Security details:
- Passwords are hashed with scrypt, and the session file only holds SHA-256 hashes of tokens.
- `users.json` and `sessions.json` are chmod 600.
- After 5 failed logins from one IP in 5 minutes, that IP gets 429.
- Changing or removing an account signs out all of its sessions.
- A cookie-authenticated write whose `Origin` is a different site is refused with 403.
- CORS doesn't allow credentials, so cookies only work on the same origin.

## 8. Photo read-back (Phase 2)
1. `POST /scan` with a photo. Claude vision (`TICKET_VISION_MODEL`, default `claude-sonnet-5-5`)
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
- [x] A1 service, venv, pytest green (28 tests)
- [x] Web sign-in: local accounts, 30-day sessions, bearer tokens for native apps
- [ ] Sign-in: `ticket user add mike` on the desktop (NEEDS MIKE)
- [ ] A1 `.env` filled on the desktop (NEEDS MIKE)
- [ ] A2 `ticket status` → dry-run today → first real print → layout tuning
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
