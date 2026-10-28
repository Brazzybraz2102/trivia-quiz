#!/usr/bin/env bash
# Move Next Box to another PC, step 2 of 2. Run this on the NEW PC, inside a fresh copy of the Next Box code
# (README "Move Next Box to another PC" has the one line that gets it):
#   scripts/move-in.sh --check                            is this PC ready? Run it before moving out of the old PC
#   scripts/move-in.sh ~/nextbox-move-2026-10-27.nbmove
#   scripts/move-in.sh mike@192.168.68.50:/home/mike/nextbox-move-2026-10-27.nbmove   (copies it here first)
#   --replace        put it in place of a Next Box with accounts on this PC (that one is kept in backups/)
#   --replace-newer  even when that Next Box was used after the move file's last change (only for a test copy)
#   --port N         run Next Box on port N here (when another program needs 8787)
# Moving back to a PC Next Box moved away from works the same way, with --replace.
# It checks this PC and the file first, then sets everything up (scripts/install.sh) and says what's left.
# Never prints .env values.
set -euo pipefail
START="$PWD"  # a move file given as a path from the folder Mike is in
HERE="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$HERE"
# How hints name this folder's scripts, typed from the home folder (quoted, so a space in a folder name pastes).
if [[ "$HERE" == "$HOME"/* ]]; then printf -v S '~/%q/scripts' "${HERE#"$HOME"/}"; else printf -v S '%q/scripts' "$HERE"; fi
source scripts/python.sh
DATA="${NEXTBOX_DATA_DIR:-$HOME/.local/share/nextbox}"

usage() {
  echo "Usage: $S/move-in.sh <move file> [--replace] [--port N]"
  echo "  e.g. $S/move-in.sh ~/nextbox-move-2026-10-27.nbmove"
  echo "  or   $S/move-in.sh mike@192.168.68.50:/home/mike/nextbox-move-2026-10-27.nbmove"
  echo "  or   $S/move-in.sh --check      (is this PC ready? nothing is changed)"
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
NEWER=""
CHECK=""
PORT_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --replace) REPLACE=--replace ;;
    --replace-newer) NEWER=--replace-newer ;;
    --check) CHECK=yes ;;
    --port) [[ $# -ge 2 ]] || { usage; exit 2; }; PORT_ARG="$2"; shift ;;
    --port=*) PORT_ARG="${1#--port=}" ;;
    -h|--help) usage; exit 0 ;;
    -*) usage; exit 2 ;;
    *) if [[ -n "$SRC" ]]; then usage; exit 2; fi; SRC="$1" ;;
  esac
  shift
done
if [[ -z "$SRC" && -z "$CHECK" ]]; then
  usage
  exit 2
fi
# NEXTBOX_PORT=8788 scripts/move-in.sh ... works like --port 8788: either way it goes into the .env.
PORT_ARG="${PORT_ARG:-${NEXTBOX_PORT:-}}"
if [[ -n "$PORT_ARG" ]] && ! [[ "$PORT_ARG" =~ ^[0-9]{1,5}$ && "$PORT_ARG" -ge 1 && "$PORT_ARG" -le 65535 ]]; then
  stop "--port takes a number, e.g. --port 8788"
fi
PORT="${PORT_ARG:-8787}"
if [[ -n "$SRC" && "$SRC" != /* && -e "$START/$SRC" ]]; then
  SRC="$START/$SRC"
fi
SHOW="${SRC:-<move file>}"  # for the lines that say what to type next

# --- 1. Is this PC ready? -----------------------------------------------------------------------------------
echo "Checking this PC ($(hostname 2>/dev/null || echo "this computer"))..."
if [[ "$(id -u)" == 0 ]]; then
  stop "Don't run this as root or with sudo." \
       "Run it as the person who'll look after Next Box on this PC: $S/move-in.sh ${SRC:---check}"
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
if ! pick_python; then
  echo
  python_help
  exit 1
fi
AWAY=""
if [[ -f "$DATA/MOVED-AWAY" ]]; then
  AWAY=yes
  if [[ -z "$CHECK" && -z "$REPLACE$NEWER" ]]; then
    stop "Next Box moved away from this PC before. There are two ways to bring it back:" \
         "- Changed your mind before using the new PC? Then: $S/move-out.sh --undo" \
         "  (it turns Next Box on here again, with the lists it had on moving day)." \
         "- Moving back with a newer move file (made on the other PC with ~/.local/share/nextbox/app/scripts/move-out.sh)? Then:" \
         "  $S/move-in.sh $SHOW --replace" \
         "  (this PC's old lists are kept in $DATA/backups/)."
  fi
fi

# A Next Box answering on this PC's port? (asks /version on 127.0.0.1 only)
answers_like_nextbox() {
  "$PY" - "$PORT" <<'PY'
import json, sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(f"http://127.0.0.1:{sys.argv[1]}/version", timeout=2) as r:
        sys.exit(0 if "version" in json.loads(r.read(4096) or b"{}") else 1)
except Exception:
    sys.exit(1)
PY
}
# Is anything at all listening on a port? (tries to take it for a moment, the way Next Box would)
port_taken() {
  "$PY" - "${1:-$PORT}" <<'PY'
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
# Port taken by another program: name it (and its service), and offer another port instead of stopping it.
port_problem() {
  local who="" prog pid unit sysunit how free=8788
  while port_taken "$free" && [[ $free -lt 8899 ]]; do free=$((free + 1)); done
  if command -v ss >/dev/null; then
    who="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'users:(("[^"]*",pid=[0-9]*' | head -1 || true)"
    if [[ -z "$who" ]]; then  # another user's program: only root may see its name
      echo "Checking which program has port $PORT (it may ask for this PC's password)..."
      who="$(sudo ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'users:(("[^"]*",pid=[0-9]*' | head -1 || true)"
    fi
  fi
  if [[ -n "$who" ]]; then
    prog="${who#users:((\"}"
    prog="${prog%%\"*}"
    pid="${who##*pid=}"
    unit="$(ps -o uunit= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    sysunit="$(ps -o unit= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [[ -n "$unit" && "$unit" != "-" && "$unit" == *.service ]]; then
      how="It's your service $unit. If you don't need it: systemctl --user disable --now $unit"
    elif [[ "$sysunit" == *.service && "$sysunit" != user@* ]]; then
      how="It's the system service $sysunit. If you don't need it: sudo systemctl disable --now $sysunit"
    else
      how="If you don't need it, close that program (or: kill $pid), then run this again."
    fi
    stop "Port $PORT, which Next Box uses, is taken by $prog (process $pid)." \
         "$how" \
         "If you don't know what $prog is, don't stop it. Run Next Box on another port instead:" \
         "  $S/move-in.sh $SHOW --port $free"
  fi
  stop "Port $PORT, which Next Box uses, is taken by another program." \
       "See which one with: sudo ss -ltnp 'sport = :$PORT'" \
       "If you don't know what it is, don't stop it. Run Next Box on another port instead:" \
       "  $S/move-in.sh $SHOW --port $free"
}

RUNNING=""
if systemctl --user is-active --quiet nextbox.service 2>/dev/null || answers_like_nextbox; then
  RUNNING=yes  # its port is Next Box's own: it's stopped below, once the move file has been checked
elif port_taken; then
  port_problem
fi

if [[ -n "$CHECK" ]]; then
  echo
  if ZONE="$("$PY" -I nextbox/movecheck.py --zone)"; then
    echo "✓ Time zone: ${ZONE:-unknown}. The old PC must use the same (move-in.sh checks it against the file)."
  else
    echo "• This PC's clock is set to $ZONE. If you don't live in UTC, set your time zone now, e.g.:"
    echo "    sudo timedatectl set-timezone America/New_York     (your own: timedatectl list-timezones)"
  fi
  if ! "$PY" -c 'import urllib.request; urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=15)' \
      >/dev/null 2>&1; then
    stop "This PC can't reach pypi.org, where Next Box's Python packages come from." \
         "Check its internet connection, then run this again."
  fi
  echo "✓ Linux with systemd, $("$PY" -V), port $PORT${RUNNING:+ (used by the Next Box already here)}, internet."
  if [[ -n "$AWAY" ]]; then
    echo "• Next Box moved away from this PC before. To move it back here: $S/move-in.sh <move file> --replace"
  fi
  echo "This PC is ready for Next Box. Nothing was changed."
  exit 0
fi

# --- 2. The move file -----------------------------------------------------------------------------------------
if [[ -f "$SRC" ]]; then
  FILE="$(cd "$(dirname "$SRC")" && pwd -P)/$(basename "$SRC")"
  chmod 600 "$FILE" 2>/dev/null || true  # a copy from a USB stick is often readable by everyone
elif [[ "$SRC" == *:* ]]; then
  command -v scp >/dev/null || stop "scp isn't installed on this PC." \
    "Ubuntu/Debian: sudo apt install -y openssh-client" "Fedora: sudo dnf install -y openssh-clients" \
    "Then run this again."
  echo "Copying the move file from the old PC (it asks for the old PC's password)."
  echo "If it asks \"Are you sure you want to continue connecting\", type yes."
  if ! scp "$SRC" "$HOME/"; then
    stop "Couldn't copy it. Check the address, and that the old PC has SSH (there: sudo apt install -y openssh-server)." \
         "Or copy the file with a USB stick and run: $S/move-in.sh ~/<the file's name>"
  fi
  FILE="$HOME/$(basename "${SRC#*:}")"
  [[ -f "$FILE" ]] || stop "The copy didn't arrive at $FILE." "Copy it with a USB stick, then: $S/move-in.sh ~/<the file's name>"
  chmod 600 "$FILE"
  SHOW="$FILE"
else
  stop "Can't find $SRC." "Give the move file's path, e.g.: $S/move-in.sh ~/nextbox-move-2026-10-27.nbmove"
fi

# Before anything is installed (seconds, with the system's own Python): the file, the time zone, what's here.
set +e
"$PY" -I nextbox/movecheck.py "$FILE" "$DATA" $REPLACE $NEWER
CODE=$?
set -e
# Already in place from an earlier run: done if Next Box runs; otherwise that run stopped before the install.
DONE=""
already_here() {
  if systemctl --user is-active --quiet nextbox.service 2>/dev/null; then
    echo "Nothing was changed."
    exit 0
  fi
  DONE=yes
  echo "The move file is already in place here. Finishing the setup..."
}
case $CODE in
  0) ;;
  3) stop "Nothing was changed. Get the newest code as it says above, then run this again." ;;
  4) stop "Copy the move file again (from the old PC, or from the USB stick), then run this again." ;;
  6) already_here ;;
  7) stop "Only if that Next Box was just a test copy: --replace puts its lists aside (in $DATA/backups/):" \
          "$S/move-in.sh $SHOW --replace" ;;
  *) stop "Nothing was changed. Fix what it says above, then run this again." ;;
esac

# --- 3. Python packages, then the move ------------------------------------------------------------------------
TURNED_OFF=""
PROBLEMS=""
if [[ -z "$DONE" ]]; then
  echo "Setting up Next Box's Python packages (a few minutes)..."
  install_packages || stop "Nothing was moved yet. Fix what it says above, then run this again."
  echo
  set +e
  .venv/bin/nextbox move info "$FILE"
  CODE=$?
  set -e
  case $CODE in
    0) ;;
    3) stop "Nothing was changed. Get the newest code as it says above, then run this again." ;;
    *) stop "Copy the move file again (from the old PC, or from the USB stick), then run this again." ;;
  esac
  if [[ -n "$AWAY" ]]; then  # what move-out.sh turned off here: it's turned back on after the move
    TURNED_OFF="$(.venv/bin/nextbox move marker show --turned-off 2>/dev/null || true)"
  fi
  if [[ -n "$RUNNING" ]]; then
    echo "Stopping the Next Box that's on this PC now (what's in it is set aside in $DATA/backups/)..."
    systemctl --user stop nextbox.service 2>/dev/null || true
    sleep 1
    if answers_like_nextbox; then
      stop "A Next Box is still answering on port $PORT (started by hand in a terminal?)." \
           "Stop it there (Ctrl+C), then run this again."
    fi
    port_taken && port_problem
  fi
  echo
  LOG="$(mktemp)"
  trap 'rm -f "$LOG"' EXIT
  PORT_OPT=()
  [[ -n "$PORT_ARG" ]] && PORT_OPT=(--port "$PORT_ARG")
  set +e
  PYTHONUNBUFFERED=1 .venv/bin/nextbox move unpack "$FILE" $REPLACE $NEWER ${PORT_OPT[@]+"${PORT_OPT[@]}"} \
    --no-next-steps 2>&1 | tee "$LOG"
  CODE=${PIPESTATUS[0]}
  set -e
  if [[ $CODE == 6 ]]; then
    echo "The move file is already in place here. Finishing the setup..."
  elif [[ $CODE != 0 ]]; then
    if [[ -n "$RUNNING" ]]; then
      systemctl --user start nextbox.service 2>/dev/null || true
      echo "The Next Box that was on this PC is on again."
    fi
    stop "Nothing was changed by this run. Fix what it says above, then run this again."
  fi
  # Anything marked ✗ above, with its fix, is repeated at the very end (the install's output pushes it away).
  PROBLEMS="$(awk '/✗/ {show=1; print; next} show && /^    / {print; next} {show=0}' "$LOG")"
fi

# --- 4. Install: the service, starting at boot (keeps the moved .env) ------------------------------------------
echo
if ! scripts/install.sh; then
  stop "Your lists and accounts are in place, but the install didn't finish." \
       "Do what it says above."
fi
if [[ " $TURNED_OFF " == *" cloudflared "* ]]; then
  echo "Turning the internet tunnel back on here (it may ask for this PC's password)."
  sudo systemctl enable --now cloudflared || echo "Please run: sudo systemctl enable --now cloudflared"
fi
echo
.venv/bin/nextbox doctor || true

# --- 5. What's left ---------------------------------------------------------------------------------------------
echo
echo "Next Box has moved to this PC. What's left:"
TUNNEL_HERE=()
[[ " $TURNED_OFF " == *" cloudflared "* ]] && TUNNEL_HERE=(--tunnel-here)
.venv/bin/nextbox move next-steps ${TUNNEL_HERE[@]+"${TUNNEL_HERE[@]}"} | sed 's/^/  /'
if [[ -n "$PROBLEMS" ]]; then
  echo "  Still to fix (from above):"
  echo "$PROBLEMS" | sed 's/^/  /'
fi
if [[ " $TURNED_OFF " != *" cloudflared "* ]] && grep -q '^NEXTBOX_PUBLIC_URL=.' .env 2>/dev/null; then
  echo
  echo "The internet address: in your browser open dash.cloudflare.com → Zero Trust → Networks → Tunnels,"
  echo "click your tunnel → Configure, and keep the page with its install command (Debian) open."
  read -r -p "Move the tunnel to this PC now? [y/N] " ANSWER || ANSWER=""
  if [[ "$ANSWER" == [yY]* ]]; then
    scripts/go-online.sh || echo "When that's done, check it again with: $S/go-online.sh"
  else
    echo "Later, with that page open: $S/go-online.sh"
  fi
fi
if [[ -n "${XDG_CURRENT_DESKTOP:-}${DISPLAY:-}${WAYLAND_DISPLAY:-}" && ! -f "$HOME/.config/autostart/nextbox-tray.desktop" ]]; then
  echo
  echo "The status square in the top bar: $S/install-tray.sh"
fi
echo
echo "Keep the move file ($FILE) and the old PC as they are for a week. Then, when everything works here:"
echo "  1. on the old PC: ~/.local/share/nextbox/app/scripts/move-out.sh --forget"
echo "  2. here, and on the USB stick if you used one: rm ~/nextbox-move-*.nbmove"
