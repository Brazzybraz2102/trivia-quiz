#!/usr/bin/env bash
# Move Next Box to another PC, step 2 of 2. Run this on the NEW PC, inside a fresh copy of the Next Box code
# (README "Move Next Box to another PC" has the one line that gets it):
#   scripts/move-in.sh ~/nextbox-move-2026-10-27.nbmove
#   scripts/move-in.sh mike@192.168.68.50:/home/mike/nextbox-move-2026-10-27.nbmove   (copies it here first)
#   add --replace to put it in place of a Next Box that's already on this PC (that one is kept in backups/)
# It checks this PC first, then sets everything up (scripts/install.sh) and says what's left to do.
# Never prints .env values.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$HERE"
PORT="${NEXTBOX_PORT:-8787}"  # Next Box's port (8787 unless you run it on another one)

usage() {
  echo "Usage: scripts/move-in.sh <move file> [--replace]"
  echo "  e.g. scripts/move-in.sh ~/nextbox-move-2026-10-27.nbmove"
  echo "  or   scripts/move-in.sh mike@192.168.68.50:/home/mike/nextbox-move-2026-10-27.nbmove"
}

# Say what's wrong and exactly what to do, then stop.
stop() {
  echo
  echo "$1"
  shift
  local line
  for line in "$@"; do echo "  $line"; done
  exit 1
}

SRC=""
REPLACE=""
for arg in "$@"; do
  case "$arg" in
    --replace) REPLACE=--replace ;;
    -h|--help) usage; exit 0 ;;
    -*) usage; exit 2 ;;
    *) if [[ -n "$SRC" ]]; then usage; exit 2; fi; SRC="$arg" ;;
  esac
done
if [[ -z "$SRC" ]]; then
  usage
  exit 2
fi

# --- 1. Is this PC ready? -----------------------------------------------------------------------------------
echo "Checking this PC ($(hostname 2>/dev/null || echo "this computer"))..."
if [[ -f "$HOME/.local/share/nextbox/MOVED-AWAY" ]]; then
  stop "This is the PC Next Box moved away from. Run this on the new PC." \
       "(To bring Next Box back to this one instead: scripts/move-out.sh --undo)"
fi
if [[ "$(id -u)" == 0 ]]; then
  stop "Don't run this as root or with sudo." \
       "Run it as the person who'll look after Next Box on this PC: scripts/move-in.sh $SRC"
fi
if [[ "$(uname -s)" != Linux ]]; then
  stop "This PC runs $(uname -s), but Next Box needs Linux (Ubuntu or Debian is easiest)."
fi
if ! command -v systemctl >/dev/null; then
  stop "This Linux doesn't have systemd, which keeps Next Box running and starts it with the PC." \
       "Ubuntu, Debian, Mint and Fedora all have it."
fi
if ! systemctl --user show-environment >/dev/null 2>&1; then
  stop "systemctl --user doesn't work in this terminal, so Next Box can't be set up to run by itself." \
       "Log in to this PC as yourself (on its screen, or with ssh as yourself, not with su or sudo)," \
       "then run this again."
fi
if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  HAVE="$(python3 -V 2>&1 || echo "no Python")"
  stop "Next Box needs Python 3.10 or newer (this PC has: $HAVE)." \
       "Ubuntu/Debian: sudo apt install -y python3 python3-venv" \
       "Fedora:        sudo dnf install -y python3" \
       "Arch:          sudo pacman -S python" \
       "Then run this again."
fi
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  stop "Python on this PC can't make Next Box's own package folder (venv) yet." \
       "Ubuntu/Debian: sudo apt install -y python3-venv" \
       "Fedora:        sudo dnf install -y python3" \
       "Arch:          sudo pacman -S python" \
       "Then run this again."
fi

# A Next Box answering on this PC's port? (asks /version on 127.0.0.1 only)
answers_like_nextbox() {
  python3 - "$PORT" <<'PY'
import json, sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(f"http://127.0.0.1:{sys.argv[1]}/version", timeout=2) as r:
        sys.exit(0 if "version" in json.loads(r.read(4096) or b"{}") else 1)
except Exception:
    sys.exit(1)
PY
}
# Is anything at all listening on the port? (tries to take it for a moment, the way Next Box would)
port_taken() {
  python3 - "$PORT" <<'PY'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    sys.exit(0)
finally:
    s.close()
sys.exit(1)
PY
}

if systemctl --user is-active --quiet nextbox.service 2>/dev/null || answers_like_nextbox; then
  if [[ -z "$REPLACE" ]]; then
    stop "Next Box is already running on this PC." \
         "To put the moved one in its place (what's here now is kept in ~/.local/share/nextbox/backups/):" \
         "  scripts/move-in.sh $SRC --replace"
  fi
  echo "Stopping the Next Box that's on this PC now (--replace)..."
  systemctl --user stop nextbox.service 2>/dev/null || true
  sleep 1
  if answers_like_nextbox; then
    stop "A Next Box is still answering on port $PORT (started by hand in a terminal?)." \
         "Stop it there (Ctrl+C), then run this again."
  fi
fi
if port_taken; then
  WHO=""
  if command -v ss >/dev/null; then
    WHO="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'users:(("[^"]*",pid=[0-9]*' | head -1 || true)"
  fi
  if [[ -n "$WHO" ]]; then
    PROG="${WHO#users:((\"}"
    PROG="${PROG%%\"*}"
    PID="${WHO##*pid=}"
    UNIT="$(ps -o uunit= -p "$PID" 2>/dev/null | tr -d ' ' || true)"
    SYSUNIT="$(ps -o unit= -p "$PID" 2>/dev/null | tr -d ' ' || true)"
    if [[ -n "$UNIT" && "$UNIT" != "-" && "$UNIT" == *.service ]]; then
      HOW="It's your service $UNIT. Stop it for good with: systemctl --user disable --now $UNIT"
    elif [[ "$SYSUNIT" == *.service && "$SYSUNIT" != user@* ]]; then
      HOW="It's the system service $SYSUNIT. Stop it for good with: sudo systemctl disable --now $SYSUNIT"
    else
      HOW="Close that program (or: kill $PID), then run this again."
    fi
    stop "Port $PORT, which Next Box uses, is taken by $PROG (process $PID)." "$HOW"
  fi
  stop "Port $PORT, which Next Box uses, is taken by another program." \
       "See which one with: sudo ss -ltnp 'sport = :$PORT'" \
       "Stop it (if it's a service: sudo systemctl disable --now <its name>), then run this again."
fi

# --- 2. The move file -----------------------------------------------------------------------------------------
if [[ -f "$SRC" ]]; then
  FILE="$(cd "$(dirname "$SRC")" && pwd -P)/$(basename "$SRC")"
  chmod 600 "$FILE" 2>/dev/null || true  # a copy from a USB stick is often readable by everyone
elif [[ "$SRC" == *:* ]]; then
  command -v scp >/dev/null || stop "scp isn't installed on this PC." "sudo apt install -y openssh-client, then run this again."
  echo "Copying the move file from the old PC (it may ask for the old PC's password)..."
  if ! scp "$SRC" "$HOME/"; then
    stop "Couldn't copy it. Check the address, and that the old PC has SSH (there: sudo apt install -y openssh-server)." \
         "Or copy the file with a USB stick and run: scripts/move-in.sh ~/<the file's name>"
  fi
  FILE="$HOME/$(basename "${SRC#*:}")"
  [[ -f "$FILE" ]] || stop "The copy didn't arrive at $FILE." "Copy it with a USB stick, then: scripts/move-in.sh ~/<the file's name>"
  chmod 600 "$FILE"
else
  stop "Can't find $SRC." "Give the move file's path, e.g.: scripts/move-in.sh ~/nextbox-move-2026-10-27.nbmove"
fi

# --- 3. Python packages, then the move ------------------------------------------------------------------------
echo "Setting up Next Box's Python packages (a few minutes)..."
NO_PACKAGES="Couldn't download Next Box's Python packages. Nothing was moved yet."
python3 -m venv .venv || stop "$NO_PACKAGES" "Ubuntu/Debian: sudo apt install -y python3-venv, then run this again."
.venv/bin/pip install -q --upgrade pip setuptools || stop "$NO_PACKAGES" "Check this PC's internet connection, then run this again."
if ! .venv/bin/pip install -q -c constraints.txt -e '.[dev,postgres,heic]'; then
  echo "Couldn't add iPhone (HEIC) photos, so installing without them: people send JPEGs instead."
  .venv/bin/pip install -q -c constraints.txt -e '.[dev,postgres]' \
    || stop "$NO_PACKAGES" "Check this PC's internet connection, then run this again."
fi
echo
.venv/bin/nextbox move info "$FILE" || stop "This copy of Next Box can't open that file." "Get the newest code (README), then run this again."
echo
if ! .venv/bin/nextbox move unpack "$FILE" $REPLACE --no-next-steps; then
  stop "Nothing was set up yet. Fix what it says above, then run this again."
fi

# --- 4. Install: the service, starting at boot (keeps the moved .env) ------------------------------------------
echo
if ! scripts/install.sh; then
  stop "Your lists and accounts are in place, but the install didn't finish." \
       "Fix what it says above, then run: scripts/install.sh"
fi
echo
.venv/bin/nextbox doctor || true

# --- 5. What's left ---------------------------------------------------------------------------------------------
echo
echo "Next Box has moved to this PC. What's left:"
.venv/bin/nextbox move next-steps | sed 's/^/  /'
if grep -q '^NEXTBOX_PUBLIC_URL=.' .env 2>/dev/null; then
  echo
  echo "The internet address: in your browser open dash.cloudflare.com → Zero Trust → Networks → Tunnels,"
  echo "click your tunnel → Configure, and keep the page with its install command (Debian) open."
  read -r -p "Move the tunnel to this PC now? [y/N] " ANSWER || ANSWER=""
  if [[ "$ANSWER" == [yY]* ]]; then
    scripts/go-online.sh || echo "The tunnel isn't set up yet. Run scripts/go-online.sh again when you're ready."
  else
    echo "Later, with that page open: scripts/go-online.sh"
  fi
fi
if [[ -n "${XDG_CURRENT_DESKTOP:-}${DISPLAY:-}${WAYLAND_DISPLAY:-}" && ! -f "$HOME/.config/autostart/nextbox-tray.desktop" ]]; then
  echo
  echo "The status square in the top bar: scripts/install-tray.sh"
fi
echo
read -r -p "Delete the move file now? It's locked, but it holds everything. [y/N] " ANSWER || ANSWER=""
if [[ "$ANSWER" == [yY]* ]]; then
  rm -f "$FILE"
  echo "Deleted $FILE."
else
  echo "Delete it once you've checked everything is here: rm '$FILE'"
fi
echo "Also delete it from the USB stick, if you used one. Keep the old PC as it is for a week, then see"
echo "README \"Move Next Box to another PC\" → After a week."
