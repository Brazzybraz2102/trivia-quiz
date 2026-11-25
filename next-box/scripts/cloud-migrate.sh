#!/usr/bin/env bash
# Copy Next Box's data from this computer into the hosted database (Render): DEPLOY.md step 3. Run it here, on the
# computer Next Box runs on today, from its Next Box folder:
#   ~/.local/share/nextbox/app/scripts/cloud-migrate.sh           check the hosted database, then copy everything into it
#   ~/.local/share/nextbox/app/scripts/cloud-migrate.sh --check   only check it (reachable? encrypted? empty?), copy nothing
#   ~/.local/share/nextbox/app/scripts/cloud-migrate.sh --copy secret      put secret.key's line on the clipboard
#   ~/.local/share/nextbox/app/scripts/cloud-migrate.sh --copy anthropic   put .env's ANTHROPIC_API_KEY on the clipboard
#     (for Render's Blueprint form, DEPLOY.md step 2: the value is never shown. Without a clipboard, e.g. over ssh,
#     it says which file to open in a text editor instead.)
# The database address holds its password: it goes only into this script's hidden prompt (never on the command
# line, in a chat or a note), this script never shows it, and nothing on this computer is changed or deleted.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$HERE"
S="~/.local/share/nextbox/app/scripts"  # how hints name these scripts: the app link every install has
NB="$HERE/.venv/bin/nextbox"
PY="$HERE/.venv/bin/python"
MODE="${1:-}"
WHICH="${2:-}"

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
if [[ -n "$MODE" && "$MODE" != --check && "$MODE" != --copy ]] \
   || [[ "$MODE" == --copy && "$WHICH" != secret && "$WHICH" != anthropic ]]; then
  echo "Usage: $S/cloud-migrate.sh [--check | --copy secret | --copy anthropic]"
  exit 1
fi

DATA="$("$PY" -c 'from nextbox.config import load_settings; print(load_settings().data_dir)' 2>/dev/null \
  || echo "$HOME/.local/share/nextbox")"

# A value read from its file goes straight onto the clipboard: never echoed, never on a command line.
to_clipboard() {  # reads the value on stdin
  if [[ -n "${WAYLAND_DISPLAY:-}" ]] && command -v wl-copy >/dev/null; then wl-copy 2>/dev/null
  elif [[ -n "${DISPLAY:-}" ]] && command -v xclip >/dev/null; then xclip -selection clipboard 2>/dev/null
  elif [[ -n "${DISPLAY:-}" ]] && command -v xsel >/dev/null; then xsel --clipboard --input 2>/dev/null
  else return 1; fi
}
if [[ "$MODE" == --copy ]]; then
  if [[ "$WHICH" == secret ]]; then
    NAME=NEXTBOX_SECRET_KEY
    if [[ -f "$DATA/secret.key" ]]; then
      FILE="$DATA/secret.key"; VALUE="$(tr -d '\r\n' < "$FILE")"
    else  # this computer keeps its key in .env instead
      FILE="$HERE/.env"; VALUE="$(grep -m1 '^NEXTBOX_SECRET_KEY=' "$FILE" 2>/dev/null | cut -d= -f2- | tr -d '\r"' || true)"
    fi
  else
    NAME=ANTHROPIC_API_KEY; FILE="$HERE/.env"
    VALUE="$(grep -m1 '^ANTHROPIC_API_KEY=' "$FILE" 2>/dev/null | cut -d= -f2- | tr -d '\r"' || true)"
  fi
  if [[ -z "$VALUE" ]]; then
    echo "There's no $NAME here to copy (looked in $FILE)."
    if [[ "$WHICH" == anthropic ]]; then
      echo "Leave ANTHROPIC_API_KEY empty in Render's form: Snap and the AI helpers then say they need a key."
    fi
    exit 1
  fi
  if printf '%s' "$VALUE" | to_clipboard; then
    unset VALUE
    echo "Copied to your clipboard (not shown): paste it into Render's form as $NAME."
    echo "It goes only there: never into a chat, a note or another computer. Copy something else afterwards."
    exit 0
  fi
  unset VALUE
  echo "This terminal has no clipboard (over ssh, say), so open this file in a text editor and copy the value there:"
  echo "  $FILE"
  if [[ "$FILE" == *.env ]]; then echo "  (the part after $NAME= on its line; close the editor without saving)"; fi
  echo "Paste it only into Render's form as $NAME, never into a chat. If it ever shows in this terminal, run: clear"
  exit 0
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

echo
echo "Done: your data is in the hosted database. Next, in this order (DEPLOY.md step 3 has the pictures):"
echo "1. Render → the Next Box service → Manual Deploy → Deploy latest commit, so the hosted Next Box starts again"
echo "   with your data. Wait until it says Live."
echo "2. Sign in at the .onrender.com address as before (same usernames and passwords). Settings → Linked app"
echo "   should still say connected. If it can't unlock the connection, NEXTBOX_SECRET_KEY on Render's Environment"
echo "   page isn't secret.key's line: $S/cloud-migrate.sh --copy secret puts it on your clipboard"
echo "   (never on screen); paste it there (Edit), save, and deploy again. Never into a chat or a note."
echo "3. The address: DEPLOY.md step 4 (Render: Settings → Custom Domains; Cloudflare: DNS → Records)."
echo "4. Checks: https://nextbox.braznarkel.com/health shows ok: true; sign in on your phone; make a preview."
echo "Next Box here stays stopped (step 3 stopped it); DEPLOY.md step 7 turns it and its tunnel off for good."
