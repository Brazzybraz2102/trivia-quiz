#!/usr/bin/env bash
# Copy Next Box's data from this computer into the hosted database (Render): DEPLOY.md step 3. Run it here, on the
# computer Next Box runs on today, from its Next Box folder:
#   ~/.local/share/nextbox/app/scripts/cloud-migrate.sh           check the hosted database, then copy everything into it
#   ~/.local/share/nextbox/app/scripts/cloud-migrate.sh --check   only check it (reachable? encrypted? empty?), copy nothing
# The database address holds its password: it goes only into this script's hidden prompt (never on the command
# line, in a chat or a note), this script never shows it, and nothing on this computer is changed or deleted.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$HERE"
S="~/.local/share/nextbox/app/scripts"  # how hints name these scripts: the app link every install has
NB="$HERE/.venv/bin/nextbox"
PY="$HERE/.venv/bin/python"
MODE="${1:-}"

if [[ "$(id -u)" == 0 ]]; then
  echo "Don't run this with sudo or as root: Next Box belongs to you, not to root. Run it as yourself:"
  echo "  $S/cloud-migrate.sh ${MODE}"
  exit 1
fi
if [[ ! -x "$NB" ]]; then
  echo "Run this on the computer Next Box runs on today, in its Next Box folder (the one with .venv in it):"
  echo "  $S/cloud-migrate.sh ${MODE}"
  exit 1
fi
if [[ -n "$MODE" && "$MODE" != --check ]]; then
  echo "Usage: $S/cloud-migrate.sh [--check]"
  exit 1
fi

echo "This copies everything Next Box keeps here (accounts, lists, journals, printers, tickets, friends' notes) into"
echo "the empty database Render made for you. Nothing here is changed or deleted: this computer keeps working as before."
echo "You need the database's address from Render: the database page → Connect → External Database URL. It starts"
echo "with postgresql:// and holds a password, so paste it only below. Nothing shows while you paste; that's on purpose."
echo
printf 'Paste the External Database URL, then press Enter: '
IFS= read -rs PASTED || PASTED=""
echo
# Forgiving: the address alone, or a line like DATABASE_URL=postgresql://... or "psql postgresql://..." pasted whole.
URL="$(printf '%s' "$PASTED" | tr -d '\r' | grep -oE "postgres(ql)?://[^[:space:]'\"]+" | head -1 || true)"
unset PASTED
if [[ -z "$URL" ]]; then
  echo "That wasn't a database address (it starts with postgresql://), so nothing was copied."
  echo "On Render open the database, then Connect → External Database URL, copy it, and run this again."
  exit 1
fi

echo "Checking the hosted database (nothing is copied yet)..."
if ! printf '%s' "$URL" | "$NB" db copy-to --check -; then
  echo
  echo "Nothing was copied. Fix what it says above, then run this again."
  exit 1
fi
if [[ "$MODE" == --check ]]; then
  echo
  echo "That's the check. To copy everything into it: $S/cloud-migrate.sh"
  exit 0
fi

echo
echo "Everything Next Box keeps here goes into that database now. This computer isn't changed."
printf 'Type COPY to go ahead: '
IFS= read -r ANSWER || ANSWER=""
if [[ "${ANSWER^^}" != "COPY" ]]; then
  echo "Nothing was copied."
  exit 1
fi
echo "Copying (a minute or two; longer with many journal photos)..."
if ! printf '%s' "$URL" | "$NB" db copy-to -; then
  echo
  echo "The copy didn't finish, so the hosted database is as it was (a copy is all or nothing). Run this again;"
  echo "if it says the same, send Claude the lines above: they never hold the address."
  exit 1
fi
unset URL

DATA="$("$PY" -c 'from nextbox.config import load_settings; print(load_settings().data_dir)' 2>/dev/null \
  || echo "$HOME/.local/share/nextbox")"
echo
echo "Done: your data is in the hosted database. Next, in this order (DEPLOY.md step 3 has the pictures):"
echo "1. The one secret to carry over, so everyone's saved to-do app connections unlock there: the content of"
echo "   secret.key. Open Render → the Next Box service → Environment → NEXTBOX_SECRET_KEY (Edit it, or"
echo "   + Add Environment Variable when it isn't there) and paste the one line this command shows:"
if [[ -f "$DATA/secret.key" ]]; then
  echo "     cat $DATA/secret.key"
else
  echo "     grep '^NEXTBOX_SECRET_KEY=' $HERE/.env      (this computer keeps its key in .env: the part after =)"
fi
echo "   It goes only into Render's form: never into a chat, a note or a terminal on another computer."
echo "2. Save it, then Render → the service → Manual Deploy → Deploy latest commit, so the hosted Next Box restarts"
echo "   with your data and your key. Wait until it says Live."
echo "3. Sign in at the .onrender.com address as before (same usernames and passwords). Settings → Linked app"
echo "   should still say connected."
echo "4. The address: DEPLOY.md step 4 (Render: Settings → Custom Domains; Cloudflare: DNS → Records)."
echo "5. Checks: https://nextbox.braznarkel.com/health answers ok; sign in on your phone; make a preview."
echo "This computer keeps running as before. Turn its tunnel off once the hosted one works (DEPLOY.md step 7)."
