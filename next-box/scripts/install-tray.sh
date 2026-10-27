#!/usr/bin/env bash
# The Next Box status square in the top bar: green all good, yellow online but needs attention,
# red offline. Safe to run again. It updates itself when Next Box is updated.
#   scripts/install-tray.sh            add it and start it now
#   scripts/install-tray.sh --remove   take it away again
# Never reads or prints .env.
set -euo pipefail

LINK="$HOME/.local/share/nextbox/app"   # made by install.sh; it follows the app if the folder moves
TRAY="$LINK/tray/nextbox_tray.py"
CACHE="$HOME/.cache/nextbox-tray"
PIDFILE="$CACHE/tray.pid"
LOG="$CACHE/tray.log"
AUTOSTART="$HOME/.config/autostart/nextbox-tray.desktop"
PY=/usr/bin/python3   # the system Python: PyGObject comes from the system's packages, not .venv

# The running status icon's process id, from its pid file (only if it really is the status icon).
running_pid() {
  local pid cmd
  [[ -f "$PIDFILE" ]] || return 1
  pid="$(tr -cd '0-9' < "$PIDFILE" 2>/dev/null || true)"
  [[ -n "$pid" && -r "/proc/$pid/cmdline" ]] || return 1
  cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ "$cmd" == *nextbox_tray.py* ]] || return 1
  echo "$pid"
}

case "${1:-}" in
  "") ;;
  --remove)
    rm -f "$AUTOSTART"
    if PID="$(running_pid)"; then
      kill "$PID" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$PID" 2>/dev/null || break
        sleep 0.3
      done
      kill -0 "$PID" 2>/dev/null && kill -KILL "$PID" 2>/dev/null || true  # still there: it's stuck
    fi
    rm -rf "$CACHE"
    echo "Removed the Next Box status icon. Next Box itself keeps running."
    echo "(The packages it used stay installed. Add it back any time with: scripts/install-tray.sh)"
    exit 0
    ;;
  *)
    echo "Usage: scripts/install-tray.sh [--remove]"
    exit 2
    ;;
esac

# Moved to another PC (scripts/move-out.sh)? Next Box stays off here, so a square would only ever be red.
if [[ -x "$LINK/.venv/bin/nextbox" ]] && "$LINK/.venv/bin/nextbox" move marker show >/dev/null 2>&1; then
  echo "Next Box was moved to another PC, so there's no status square for it here."
  echo "Add it on the new PC (scripts/install-tray.sh there). To bring Next Box back here: scripts/move-out.sh --undo"
  exit 0
fi

if [[ -z "${XDG_CURRENT_DESKTOP:-}" && -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo "This isn't a desktop session, so there's no top bar to put the status icon in."
  echo "Run this in a terminal on the computer's desktop."
  exit 0
fi

if [[ ! -f "$TRAY" ]]; then
  echo "Next Box isn't installed at $LINK yet (or it's an older version)."
  echo "Run scripts/install.sh (or ~/.local/share/nextbox/app/scripts/update.sh) first, then this again."
  exit 1
fi
if [[ ! -x "$PY" ]]; then
  echo "The status icon needs the system's python3 at $PY. Install it, then run this again."
  exit 1
fi

# What the icon needs: PyGObject with GTK 3 and an AppIndicator library (and notify-send for alerts).
can_run() {
  "$PY" - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
except ValueError:
    gi.require_version("AppIndicator3", "0.1")
PY
}
if ! can_run || ! command -v notify-send >/dev/null; then
  if command -v apt-get >/dev/null && command -v dpkg-query >/dev/null; then
    NEED=()
    for pkg in python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 libnotify-bin; do
      if [[ "$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null || true)" != "install ok installed" ]]; then
        NEED+=("$pkg")
      fi
    done
    if (( ${#NEED[@]} )); then
      echo "Installing what the status icon needs: ${NEED[*]} (may ask for your password)"
      if ! sudo apt-get install -y "${NEED[@]}"; then
        sudo apt-get update
        sudo apt-get install -y "${NEED[@]}"
      fi
    fi
  fi
  if ! can_run; then
    echo "The status icon needs PyGObject, GTK 3 and the Ayatana AppIndicator library for $PY."
    echo "  Ubuntu/Debian: sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 libnotify-bin"
    echo "  Fedora:        sudo dnf install python3-gobject gtk3 libayatana-appindicator-gtk3 libnotify"
    echo "  Arch:          sudo pacman -S python-gobject gtk3 libayatana-appindicator libnotify"
    echo "Then run this again."
    exit 1
  fi
  command -v notify-send >/dev/null || echo "(No notify-send here, so there won't be pop-up alerts; the square still works.)"
fi

# GNOME shows these icons through its AppIndicator extension (built into Ubuntu, but it can be off).
if command -v gnome-extensions >/dev/null; then
  ENABLED="$(gnome-extensions list --enabled 2>/dev/null || true)"
  if ! grep -qi appindicator <<<"$ENABLED"; then
    if gnome-extensions enable ubuntu-appindicators@ubuntu.com 2>/dev/null \
        || gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com 2>/dev/null; then
      echo "Turned on GNOME's AppIndicator extension, which shows the square in the top bar."
    else
      echo "GNOME's AppIndicator extension is off or missing, so the square won't show in the top bar yet. To fix it:"
      echo "  sudo apt install gnome-shell-extension-appindicator"
      echo "  then log out and back in, and run: gnome-extensions enable ubuntu-appindicators@ubuntu.com"
      echo "  (or open the Extensions app and switch on \"Ubuntu AppIndicators\")"
    fi
  fi
  unset ENABLED
elif [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]]; then
  echo "Can't check GNOME's AppIndicator extension here: if the square doesn't show, open the Extensions"
  echo "app and switch on \"Ubuntu AppIndicators\" (sudo apt install gnome-shell-extension-appindicator)."
fi

# Start at every login. The path goes through the app link, so it keeps working if the folder moves.
mkdir -p "$(dirname "$AUTOSTART")"
cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Next Box status
Comment=A square in the top bar: green all good, yellow needs attention, red offline
Exec=$PY "$TRAY"
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF
echo "The status icon starts by itself when you log in ($AUTOSTART)."

mkdir -p "$CACHE"
chmod 700 "$CACHE"
if PID="$(running_pid)"; then
  echo "The status icon is already running (it picks up updates by itself)."
  exit 0
fi
nohup setsid "$PY" "$TRAY" </dev/null >"$LOG" 2>&1 &
for _ in 1 2 3 4 5 6 7 8 9 10; do
  running_pid >/dev/null && break
  sleep 0.5
done
sleep 1  # still there a moment later, not just started and stopped
if running_pid >/dev/null; then
  echo "Started: look for the square in the top bar (grey while it checks, then green, yellow or red)."
  echo "Click it for what needs fixing. Take it away with: scripts/install-tray.sh --remove"
else
  echo "The status icon didn't start. See why in $LOG"
  exit 1
fi
