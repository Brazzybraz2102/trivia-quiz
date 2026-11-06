#!/usr/bin/env bash
# Where does Next Box stand on this PC? One short report, safe to paste into a chat: it never shows a key, a
# token or a .env value (only versions, yes/no, service states, addresses, days and the tunnel's id).
#   ~/next-box/scripts/status.sh                        (the new PC, before or after the move)
#   ~/.local/share/nextbox/app/scripts/status.sh        (any PC where Next Box is or was installed)
# With a .venv it runs `nextbox move status`; without one (the code only) it prints the same report with the
# system's python3. Nothing is changed, nothing is printed on paper.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$HERE"
MASK='s/eyJ[A-Za-z0-9_+\/=.-]{20,}/<token>/g'  # belt and braces: a token never reaches the screen
if [[ -x .venv/bin/nextbox ]]; then
  if OUT="$(.venv/bin/nextbox move status 2>/dev/null)"; then
    printf '%s\n' "$OUT" | sed -E "$MASK"
    exit 0
  fi
  echo "(.venv/bin/nextbox couldn't run here, so this is the plain report from the system's python3)"
fi
for PY in python3 python3.14 python3.13 python3.12 python3.11; do
  if command -v "$PY" >/dev/null 2>&1; then
    "$PY" -I nextbox/movecheck.py --status "$HERE" 2>&1 | sed -E "$MASK"
    exit "${PIPESTATUS[0]}"
  fi
done
echo "This PC has no python3, so Next Box isn't installed here (Ubuntu/Debian: sudo apt install -y python3)."
exit 1
