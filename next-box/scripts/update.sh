#!/usr/bin/env bash
# Get the newest Next Box and reinstall it. Keeps your settings (.env), accounts and lists.
#   ~/.local/share/nextbox/app/scripts/update.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$HERE"
if ! command -v rsync >/dev/null; then  # Ubuntu/Debian, Fedora or Arch
  if command -v apt-get >/dev/null; then sudo apt-get install -y rsync >/dev/null
  elif command -v dnf >/dev/null; then sudo dnf install -y rsync >/dev/null
  elif command -v pacman >/dev/null; then sudo pacman -S --noconfirm rsync >/dev/null
  else echo "Please install rsync, then run this again."; exit 1; fi
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Downloading the newest Next Box..."
git clone -q --depth 1 -b claude/ticket-todoist-printer-nldz8a https://github.com/brazzybraz2102/trivia-quiz.git "$TMP/src"
rsync -a --delete --exclude .env --exclude .venv "$TMP/src/next-box/" "$HERE/"
echo "Installing (your .env, accounts, lists and journal are kept)..."
scripts/install.sh
