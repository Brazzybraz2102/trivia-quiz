#!/usr/bin/env bash
# Move Next Box to another PC, step 1 of 2. Run this on the OLD PC, from its Next Box folder:
#   ~/.local/share/nextbox/app/scripts/move-out.sh          make one locked move file; Next Box goes off here
#   ~/.local/share/nextbox/app/scripts/move-out.sh --undo   turn Next Box back on here instead
# Step 2 is scripts/move-in.sh on the new PC (README "Move Next Box to another PC").
# Nothing is deleted. Never prints .env values.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$HERE"
NB="$HERE/.venv/bin/nextbox"
AUTOSTART="$HOME/.config/autostart/nextbox-tray.desktop"

if [[ ! -x "$NB" ]]; then
  echo "Run this in the Next Box folder on the old PC (the one with .venv in it):"
  echo "  ~/.local/share/nextbox/app/scripts/move-out.sh"
  exit 1
fi

undo() {
  local day off answer what
  if ! day="$("$NB" move marker show --date 2>/dev/null)"; then
    echo "Next Box wasn't moved away from this PC, so there's nothing to undo."
    return 0
  fi
  off="$("$NB" move marker show --turned-off)"
  echo "This turns Next Box back on here, with what it had when it moved${day:+ ($day)}."
  echo "First switch it off on the new PC (there: systemctl --user disable --now nextbox, and"
  echo "sudo systemctl disable --now cloudflared if you moved the tunnel), or there would be two copies."
  echo "Anything changed on the new PC since then stays there."
  read -r -p "Type UNDO to go on: " answer || answer=""
  if [[ "$answer" != "UNDO" ]]; then
    echo "Nothing changed."
    return 1
  fi
  "$NB" move marker clear
  for what in service tray cloudflared; do  # Next Box first, so the tunnel never points at nothing
    [[ " $off " == *" $what "* ]] || continue
    case "$what" in
      service)
        if systemctl --user enable --now nextbox.service; then
          echo "Next Box is on again here."
        else
          echo "Couldn't start it. Run scripts/install.sh, which sets it up again."
        fi
        ;;
      tray)
        scripts/install-tray.sh || echo "Add the status square again from the desktop: scripts/install-tray.sh"
        ;;
      cloudflared)
        echo "Turning the internet tunnel back on here (it may ask for this PC's password)."
        sudo systemctl enable --now cloudflared || echo "Please run: sudo systemctl enable --now cloudflared"
        ;;
    esac
  done
  echo "Done. Check it with: .venv/bin/nextbox doctor"
}

case "${1:-}" in
  "") ;;
  --undo) undo; exit $? ;;
  *) echo "Usage: scripts/move-out.sh [--undo]"; exit 2 ;;
esac

echo "This moves Next Box off this PC ($(hostname 2>/dev/null || echo "this computer")):"
echo "  1. Next Box goes offline here, and stays off (also after a restart or an update)."
echo "  2. It makes one locked file with everything in it, for the new PC. You pick its passphrase."
echo "  3. Nothing is deleted. Changed your mind later? scripts/move-out.sh --undo puts it back."
read -r -p "Type MOVE to go on: " ANSWER || ANSWER=""
if [[ "$ANSWER" != "MOVE" ]]; then
  echo "Nothing changed."
  exit 1
fi

WAS_ON=no
if systemctl --user is-active --quiet nextbox.service 2>/dev/null \
    || systemctl --user is-enabled --quiet nextbox.service 2>/dev/null; then
  WAS_ON=yes
fi
echo "Stopping Next Box here..."
systemctl --user stop nextbox.service 2>/dev/null || true

PATHFILE="$(mktemp)"
ERRFILE="$(mktemp)"
trap 'rm -f "$PATHFILE" "$ERRFILE"' EXIT
if ! "$NB" move pack --path-file "$PATHFILE" 2> >(tee "$ERRFILE" >&2); then
  sleep 0.2  # let tee finish writing
  if grep -q "Stop Next Box first" "$ERRFILE"; then
    echo "The service is stopped, but a Next Box still answers here: one started by hand (nextbox serve)?"
    echo "Stop that one (Ctrl+C in its window), then run this again."
  fi
  if [[ $WAS_ON == yes ]]; then
    systemctl --user start nextbox.service 2>/dev/null || true
    echo "No move file was made, so Next Box is back on here. Nothing else changed."
  else
    echo "No move file was made. Nothing else changed."
  fi
  exit 1
fi
FILE="$(cat "$PATHFILE")"

# From here on Next Box stays off on this PC, even after an update (nextbox serve and install.sh check this).
OFF=()
[[ $WAS_ON == yes ]] && OFF=(--off service)
"$NB" move marker set --file "$(basename "$FILE")" ${OFF[@]+"${OFF[@]}"}
systemctl --user disable nextbox.service 2>/dev/null || true
if [[ -f "$AUTOSTART" ]]; then
  scripts/install-tray.sh --remove >/dev/null 2>&1 || true
  if [[ ! -f "$AUTOSTART" ]]; then
    "$NB" move marker set --off tray
    echo "Removed the status square from the top bar."
  fi
fi
if systemctl is-active --quiet cloudflared 2>/dev/null; then
  echo "Turning off the internet tunnel here, or Cloudflare would keep sending some visitors to this PC."
  echo "(It may ask for this PC's password.)"
  if sudo systemctl disable --now cloudflared; then
    "$NB" move marker set --off cloudflared
  else
    echo "Couldn't turn it off. Please run: sudo systemctl disable --now cloudflared"
  fi
fi

SIZE="$(du -h "$FILE" | cut -f1)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
if [[ -z "$IP" ]]; then
  IP="<this PC's IP>"
fi
echo
echo "Done. Next Box is off on this PC."
echo "The move file: $FILE ($SIZE)"
echo "Copy it to the new PC, one of two ways:"
echo "  - with a USB stick: copy it onto the stick, then into the new PC's home folder"
if systemctl is-active --quiet ssh 2>/dev/null || systemctl is-active --quiet sshd 2>/dev/null; then
  echo "  - over the network, on the new PC: scripts/move-in.sh $USER@$IP:$FILE"
  echo "    (or just copy it there: scp $USER@$IP:$FILE ~/)"
else
  echo "  - over the network: this PC needs SSH first (here: sudo apt install -y openssh-server), then"
  echo "    on the new PC: scripts/move-in.sh $USER@$IP:$FILE   (or: scp $USER@$IP:$FILE ~/)"
fi
echo "Then on the new PC follow README \"Move Next Box to another PC\"."
echo "Changed your mind? scripts/move-out.sh --undo turns Next Box back on here."
