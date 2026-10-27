# Sourced by scripts/install.sh and scripts/move-in.sh (not run by itself).
# Next Box needs Python 3.11 or newer: SQLAlchemy 2.1 and websockets 17 in constraints.txt need it.
#   pick_python       sets PY to the system's python3 when it's new enough and can make a venv, otherwise to the
#                     newest python3.14/3.13/3.12/3.11 that can. Returns 1 (and sets PY_PROBLEM) when none can.
#   python_help       says plainly what to install (after pick_python failed).
#   install_packages  makes .venv and installs Next Box's packages into it; on failure says why, returns 1.

pick_python() {
  PY=""
  PY_PROBLEM=old
  PY_NAME=""
  local candidate
  for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null || continue
    if "$candidate" -c 'import venv, ensurepip' 2>/dev/null; then
      PY="$(command -v "$candidate")"
      PY_PROBLEM=""
      return 0
    fi
    if [[ -z "$PY_NAME" ]]; then
      PY_NAME="$candidate"  # new enough, but it can't make a venv yet
      PY_PROBLEM=venv
    fi
  done
  return 1
}

python_help() {
  local have
  have="$(python3 -V 2>&1 || echo "no Python")"
  if [[ "$PY_PROBLEM" == venv ]]; then
    echo "Python on this PC can't make Next Box's own package folder (a venv) yet. Install that part:"
    echo "  Ubuntu/Debian: sudo apt install -y $PY_NAME-venv"
    echo "  Fedora:        sudo dnf install -y python3"
    echo "Then run this again."
  else
    echo "Next Box needs Python 3.11 or newer, and this PC has: $have"
    echo "  Ubuntu 22.04 (its Python 3.10 is too old): sudo apt install -y python3.11 python3.11-venv"
    echo "  Ubuntu 24.04, Debian 12 and newer already have a new enough Python: sudo apt install -y python3-venv"
    echo "  Ubuntu 20.04 and Debian 11 are too old for Next Box: upgrade to Ubuntu 24.04 or Debian 12."
    echo "  Fedora: sudo dnf install -y python3      Arch: sudo pacman -S python"
    echo "Then run this again."
  fi
}

# Why pip failed, in plain words: an old Python, no internet, or something else (pip's last lines are shown).
explain_pip() {
  local log="$1"
  echo
  tail -n 4 "$log" | sed 's/^/  pip: /'
  if grep -qiE "Requires-Python|requires a different Python|No matching distribution found for (sqlalchemy|websockets)" "$log"; then
    echo "Next Box's packages need a newer Python than this PC's $("$PY" -V 2>&1)."
    PY_PROBLEM=old
    python_help
  elif ! "$PY" -c 'import urllib.request; urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=15)' >/dev/null 2>&1; then
    echo "This PC can't reach pypi.org, where Python packages come from. Check its internet connection,"
    echo "then run this again."
  else
    echo "pip couldn't install Next Box's packages (its last lines are above). Run this again; if it fails the"
    echo "same way, send those lines to whoever helps you with Next Box."
  fi
}

install_packages() {
  local log
  log="$(mktemp)"
  # An older .venv made with a Python that's too old can't be reused: start it again.
  if [[ -x .venv/bin/python ]] && ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    "$PY" -m venv --clear .venv
  fi
  if ! "$PY" -m venv .venv 2>"$log" || ! .venv/bin/pip install -q --upgrade pip setuptools 2>>"$log"; then
    explain_pip "$log"
    rm -f "$log"
    return 1
  fi
  # constraints.txt pins every package to the versions this release was tested with.
  # heic: iPhone HEIC photos on Snap. Optional: if it can't be installed here, everything else still is.
  if ! .venv/bin/pip install -q -c constraints.txt -e '.[dev,postgres,heic]' 2>"$log"; then
    echo "Couldn't add iPhone (HEIC) photos, so installing without them: people send JPEGs instead."
    if ! .venv/bin/pip install -q -c constraints.txt -e '.[dev,postgres]' 2>"$log"; then
      explain_pip "$log"
      rm -f "$log"
      return 1
    fi
  fi
  rm -f "$log"
}
