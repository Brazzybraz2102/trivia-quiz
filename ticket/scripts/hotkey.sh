#!/usr/bin/env bash
# Bind a GNOME shortcut to `ticket clip --todo`.
#   scripts/hotkey.sh '<Super><Shift>t'
# Copy some text, press the key: it becomes a Todoist task and its ticket prints.
# Add --dry-run as a second argument to bind a dry-run version while testing.
set -euo pipefail
KEY="${1:?usage: hotkey.sh '<Super><Shift>t' [--dry-run]}"
DRY="${2:-}"
BIN="$HOME/.local/share/ticket/app/.venv/bin/ticket"
[[ -x "$BIN" ]] || { echo "Run scripts/install.sh first"; exit 1; }

SCHEMA=org.gnome.settings-daemon.plugins.media-keys
PATH_ID=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/ticket-clip/
CUR="$(gsettings get $SCHEMA custom-keybindings)"
if [[ "$CUR" != *"$PATH_ID"* ]]; then
  if [[ "$CUR" == "@as []" || "$CUR" == "[]" ]]; then NEW="['$PATH_ID']"
  else NEW="${CUR%]}, '$PATH_ID']"; fi
  gsettings set $SCHEMA custom-keybindings "$NEW"
fi
K="$SCHEMA.custom-keybinding:$PATH_ID"
gsettings set "$K" name 'ticket: clipboard to Todoist + print'
gsettings set "$K" command "$BIN $DRY clip --todo"
gsettings set "$K" binding "$KEY"
echo "Bound $KEY -> ticket $DRY clip --todo"
