#!/usr/bin/env bash
# Move Next Box to another PC, step 1 of 2. Run this on the OLD PC, from its Next Box folder:
#   ~/.local/share/nextbox/app/scripts/move-out.sh           make one locked move file; Next Box goes off here
#   ~/.local/share/nextbox/app/scripts/move-out.sh --undo    changed your mind before using the new PC: on again here
#   ~/.local/share/nextbox/app/scripts/move-out.sh --forget  after a week: delete Next Box from this old PC for good
# Step 2 is scripts/move-in.sh on the new PC (README "Move Next Box to another PC").
# Moving back later is the same two steps the other way round. Never prints .env values.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$HERE"
S="${HERE/#"$HOME"/\~}/scripts"  # how hints name this folder's scripts: Mike types them from his home folder
NB="$HERE/.venv/bin/nextbox"
AUTOSTART="$HOME/.config/autostart/nextbox-tray.desktop"
BRANCH=claude/ticket-todoist-printer-nldz8a
REPO=https://github.com/brazzybraz2102/trivia-quiz.git

if [[ "$(id -u)" == 0 ]]; then
  echo "Don't run this with sudo or as root: Next Box belongs to you, not to root. Run it as yourself:"
  echo "  ~/.local/share/nextbox/app/scripts/move-out.sh ${1:-}"
  exit 1
fi
if [[ ! -x "$NB" ]]; then
  echo "Run this in the Next Box folder on the old PC (the one with .venv in it):"
  echo "  ~/.local/share/nextbox/app/scripts/move-out.sh ${1:-}"
  exit 1
fi

has_desktop() {
  [[ -n "${XDG_CURRENT_DESKTOP:-}${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]
}

undo() {
  local day off answer what
  if ! day="$("$NB" move marker show --date 2>/dev/null)"; then
    echo "Next Box wasn't moved away from this PC, so there's nothing to undo."
    return 0
  fi
  off="$("$NB" move marker show --turned-off)"
  echo "This turns Next Box back on here, with the lists it had when it moved${day:+ ($day)}."
  echo "It's for when Next Box on the new PC wasn't used yet: what was added there doesn't come back here."
  echo "  Used the new PC already? Move back instead: run ~/next-box/scripts/move-out.sh on the new PC, then here:"
  echo "  ~/.local/share/nextbox/app/scripts/move-in.sh <the file it made> --replace"
  echo "Ran move-in.sh on the new PC? First switch Next Box off there for good: ~/next-box/scripts/move-out.sh there"
  echo "(delete the file it makes), or there would be two copies."
  read -r -p "Type UNDO to turn it back on here: " answer || answer=""
  if [[ "${answer^^}" != "UNDO" ]]; then
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
          echo "Couldn't start it. Run $HERE/scripts/install.sh, which sets it up again."
        fi
        ;;
      tray)
        if has_desktop; then
          scripts/install-tray.sh || echo "Add the status square again: $HERE/scripts/install-tray.sh"
        else
          echo "The status square: run this in a terminal on this PC's own screen:"
          echo "  ~/.local/share/nextbox/app/scripts/install-tray.sh"
        fi
        ;;
      cloudflared)
        echo "Turning the internet tunnel back on here (it may ask for this PC's password)."
        sudo systemctl enable --now cloudflared || echo "Please run: sudo systemctl enable --now cloudflared"
        ;;
    esac
  done
  echo "Done. Check it with: $HERE/.venv/bin/nextbox doctor"
}

# After a week on the new PC: delete Next Box from this old PC, but only here (the MOVED-AWAY marker says so).
forget() {
  local host answer data link pg files f
  if ! "$NB" move marker show >/dev/null 2>&1; then
    echo "Next Box wasn't moved away from this PC: this is a PC it runs on, so nothing was deleted."
    echo "Run this on the old PC, the one you moved Next Box away from."
    return 1
  fi
  data="$("$HERE/.venv/bin/python" -c 'from nextbox.config import load_settings; print(load_settings().data_dir)' \
    2>/dev/null || true)"
  if [[ -z "$data" || ! -f "$data/MOVED-AWAY" || "$data" == "$HOME" || "$data" == / ]]; then
    echo "Couldn't find this PC's Next Box data folder, so nothing was deleted."
    return 1
  fi
  link="$(readlink -f "$data/app" 2>/dev/null || true)"
  host="$(hostname 2>/dev/null || echo this-pc)"
  files=()
  for f in "$HOME"/nextbox-move-*.nbmove; do
    [[ -f "$f" ]] && files+=("$f")
  done
  echo "This deletes Next Box from this PC ($host) for good. Next Box on the new PC isn't touched."
  echo "  - the lists, accounts and journals from before the move: $data"
  echo "  - the code: $HERE"
  if [[ -n "$link" && "$link" != "$HERE" && -f "$link/scripts/move-out.sh" ]]; then
    echo "  - the code the service used: $link"
  fi
  echo "  - its background service, the status square and the hotkey"
  if [[ ${#files[@]} -gt 0 ]]; then
    echo "  - the move file(s) here: ${files[*]}"
  fi
  echo "Only do this once Next Box has worked on the new PC for a while (a week is good)."
  read -r -p "To go on, type this PC's name ($host): " answer || answer=""
  if [[ "$answer" != "$host" ]]; then
    echo "Nothing was deleted."
    return 1
  fi

  # The extras first, while the code and its .venv are still here.
  if [[ " $("$NB" move marker show --turned-off 2>/dev/null || true) " == *" cloudflared "* ]] \
      && command -v cloudflared >/dev/null; then
    read -r -p "Also remove the internet tunnel from this PC (it's on the new PC now)? [y/N] " answer || answer=""
    if [[ "$answer" == [yY]* ]]; then
      sudo cloudflared service uninstall || echo "Couldn't. Later: sudo cloudflared service uninstall"
    fi
  fi
  pg="$("$HERE/.venv/bin/python" - <<'PY' 2>/dev/null || true
import os
from urllib.parse import urlsplit
from nextbox.config import load_settings
load_settings()
url = urlsplit(os.environ.get("DATABASE_URL", ""))
local = (url.hostname or "localhost") in ("localhost", "127.0.0.1", "::1")
print("yes" if url.scheme.startswith("postgres") and local and url.path == "/nextbox" else "")
PY
)"
  if [[ "$pg" == yes ]]; then
    read -r -p "Also delete Next Box's PostgreSQL databases on this PC (nextbox, nextbox_practice)? [y/N] " answer || answer=""
    if [[ "$answer" == [yY]* ]]; then
      sudo -u postgres dropdb --if-exists nextbox || true
      sudo -u postgres dropdb --if-exists nextbox_practice || true
      sudo -u postgres dropuser --if-exists nextbox || true
    fi
  fi
  if [[ -f "$AUTOSTART" ]]; then
    scripts/install-tray.sh --remove >/dev/null 2>&1 || rm -f "$AUTOSTART"
  fi
  if command -v gsettings >/dev/null && "$HERE/.venv/bin/python" - <<'PY' 2>/dev/null; then
import ast, subprocess, sys
schema = "org.gnome.settings-daemon.plugins.media-keys"
mine = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/nextbox-clip/"
now = subprocess.run(["gsettings", "get", schema, "custom-keybindings"], capture_output=True, text=True).stdout
keys = ast.literal_eval(now.strip().removeprefix("@as ") or "[]")
if mine not in keys:
    sys.exit(1)
subprocess.run(["gsettings", "set", schema, "custom-keybindings", repr([k for k in keys if k != mine])], check=True)
subprocess.run(["gsettings", "reset-recursively", f"{schema}.custom-keybinding:{mine}"], check=False)
PY
    echo "Removed the Next Box hotkey."
  fi
  systemctl --user disable --now nextbox.service >/dev/null 2>&1 || true
  rm -f "$HOME/.config/systemd/user/nextbox.service"
  systemctl --user daemon-reload 2>/dev/null || true
  if [[ ${#files[@]} -gt 0 ]]; then
    rm -f "${files[@]}"
  fi
  rm -rf "$data"
  if [[ -n "$link" && "$link" != "$HERE" && -f "$link/scripts/move-out.sh" && "$link" != "$HOME" ]]; then
    rm -rf "$link"
  fi
  cd "$HOME"
  [[ -f "$HERE/scripts/move-out.sh" && -d "$HERE/nextbox" && "$HERE" != "$HOME" ]] && rm -rf "$HERE"
  echo "Done: Next Box is gone from this PC."
  echo "Last step: delete the move file on the new PC (rm ~/nextbox-move-*.nbmove there) and on the USB stick."
}

case "${1:-}" in
  "") ;;
  --undo) undo; exit $? ;;
  --forget) forget; exit $? ;;
  *) echo "Usage: $S/move-out.sh [--undo | --forget]"; exit 2 ;;
esac

if "$NB" move marker show >/dev/null 2>&1; then
  # Moved away already: a second file has the lists from that day, not what was added on the new PC since.
  DAY="$("$NB" move marker show --date 2>/dev/null || true)"
  NAME="$("$NB" move marker show --name 2>/dev/null || true)"
  echo "Next Box already moved away from this PC${DAY:+ on $DAY}${NAME:+ (file $NAME)}."
  echo "Its lists here are from that day: anything added on the new PC since isn't in them."
  echo "Make a new move file only if the first one or its passphrase is lost, and Next Box on the new PC"
  echo "wasn't used yet. (To use Next Box on this PC again instead: $S/move-out.sh --undo)"
  read -r -p "Type MOVE to make a new move file anyway: " ANSWER || ANSWER=""
else
  echo "This moves Next Box off this PC ($(hostname 2>/dev/null || echo "this computer")):"
  echo "  1. Next Box goes offline here, and stays off (also after a restart or an update)."
  echo "  2. It makes one locked file with everything in it, for the new PC. You pick its passphrase."
  echo "  3. Nothing is deleted. Changed your mind later? $S/move-out.sh --undo puts it back."
  read -r -p "Type MOVE to go on: " ANSWER || ANSWER=""
fi
if [[ "${ANSWER^^}" != "MOVE" ]]; then
  echo "Nothing changed."
  exit 1
fi

WAS_ON=no
if systemctl --user is-active --quiet nextbox.service 2>/dev/null \
    || systemctl --user is-enabled --quiet nextbox.service 2>/dev/null; then
  WAS_ON=yes
fi
# A closed terminal or a kill before the move file is made: Next Box comes back on here, as if nothing happened.
MARKED=no
on_signal() {
  if [[ $MARKED == no && $WAS_ON == yes ]]; then
    systemctl --user start nextbox.service 2>/dev/null || true
    echo "Stopped before the move file was made, so Next Box is on again here." 2>/dev/null || true
  fi
  exit 129
}
trap on_signal HUP TERM
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
MARKED=yes
systemctl --user disable nextbox.service 2>/dev/null || true
if [[ -f "$AUTOSTART" ]]; then
  scripts/install-tray.sh --remove >/dev/null 2>&1 || true
  if [[ ! -f "$AUTOSTART" ]]; then
    "$NB" move marker set --off tray
    echo "Removed the status square from the top bar."
  fi
fi
# Running now, or only set to start with the PC: either way it would connect again (two connectors split visitors).
# Unless it also carries other websites from this PC: then only Next Box's address leaves it (in the dashboard).
PUBLIC_HOST="$(sed -n 's|^NEXTBOX_PUBLIC_URL=[a-z]*://\([^/]*\).*|\1|p' .env 2>/dev/null | tail -1 || true)"
PUBLIC_HOST="${PUBLIC_HOST:-nextbox.braznarkel.com}"
if systemctl is-active --quiet cloudflared 2>/dev/null || systemctl is-enabled --quiet cloudflared 2>/dev/null; then
  SHARED=""
  if grep -q '^NEXTBOX_TUNNEL=shared$' .env 2>/dev/null; then
    SHARED=yes  # go-online.sh put Next Box on a tunnel that was already here for something else
  else
    echo
    echo "Next, the internet tunnel here gets turned off, or Cloudflare would keep sending some visitors to this PC."
    echo "Does this PC's tunnel also carry other websites, besides Next Box? Most don't: then just press Enter."
    read -r -p "Turn the tunnel off? [Y/n] " ANSWER || ANSWER=""
    [[ "$ANSWER" == [nN]* ]] && SHARED=yes
  fi
  if [[ -n "$SHARED" ]]; then
    echo "The tunnel stays on here, for your other websites. Two things instead:"
    echo "  1. Now, in dash.cloudflare.com → Zero Trust → Networks → Tunnels → this PC's tunnel → Public Hostname:"
    echo "     delete only $PUBLIC_HOST (leave the others)."
    echo "  2. On the new PC, Next Box gets its own tunnel: in the same place, Create a tunnel (name it after the"
    echo "     new PC), add $PUBLIC_HOST → HTTP → localhost:8787 to it, and paste its install command"
    echo "     into ~/next-box/scripts/go-online.sh there."
  else
    echo "(It may ask for this PC's password.)"
    if sudo systemctl disable --now cloudflared; then
      "$NB" move marker set --off cloudflared
      echo "The tunnel is off here."
    else
      echo "Couldn't turn it off. Please run: sudo systemctl disable --now cloudflared"
    fi
  fi
fi

# This PC's address on the home network (the route's own: not a Docker or VPN address).
ADDR="$("$NB" move address 2>/dev/null || true)"
IP="${ADDR% *}"
if [[ -z "$ADDR" || -z "$IP" ]]; then
  IP="<this PC's IP>"
fi
GET="rm -rf ~/nb-src && git clone -q --depth 1 -b $BRANCH $REPO ~/nb-src && mkdir -p ~/next-box && cp -a ~/nb-src/next-box/. ~/next-box/ && rm -rf ~/nb-src"
# Next Box came to this PC with a move: the PC it came from still has its old lists, so moving back replaces them.
REPLACE=""
DATA="$("$HERE/.venv/bin/python" -c 'from nextbox.config import load_settings; print(load_settings().data_dir)' \
  2>/dev/null || true)"
if [[ -n "$DATA" && -f "$DATA/MOVED-IN" ]]; then
  REPLACE=" --replace"
fi
echo
echo "Done. Next Box is off on this PC."
echo "The move file: $FILE"
if [[ -n "$REPLACE" ]]; then
  echo "(The lines below end with --replace: on the PC Next Box came from, that puts its old lists aside.)"
fi
if systemctl is-active --quiet ssh 2>/dev/null || systemctl is-active --quiet sshd 2>/dev/null \
    || systemctl is-active --quiet ssh.socket 2>/dev/null || [[ -x /usr/sbin/sshd ]]; then
  echo "On the new PC, paste this one line. It gets Next Box and fetches the file from here over the network"
  echo "(it asks for this PC's password; if it asks \"Are you sure you want to continue connecting\", type yes):"
  echo "  $GET && ~/next-box/scripts/move-in.sh $USER@$IP:$FILE$REPLACE"
  echo "Or copy the file with a USB stick into the new PC's home folder, and paste this line there instead:"
else
  echo "Copy it with a USB stick into the new PC's home folder, then on the new PC paste this one line"
  echo "(to fetch it over the network instead, this PC needs SSH first: sudo apt install -y openssh-server,"
  echo "and then the line ends with: ~/next-box/scripts/move-in.sh $USER@$IP:$FILE$REPLACE):"
fi
echo "  $GET && ~/next-box/scripts/move-in.sh ~/$(basename "$FILE")$REPLACE"
echo "(The new PC needs git for it: sudo apt install -y git, or on Fedora: sudo dnf install -y git)"
echo "Changed your mind? $S/move-out.sh --undo turns Next Box back on here."
