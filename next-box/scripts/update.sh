#!/usr/bin/env bash
# Get the newest Next Box and reinstall it. Keeps your settings (.env), accounts and lists.
#   cd ~/next-box && scripts/update.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
command -v rsync >/dev/null || sudo apt-get install -y rsync >/dev/null
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Downloading the newest Next Box..."
git clone -q --depth 1 -b claude/ticket-todoist-printer-nldz8a https://github.com/brazzybraz2102/trivia-quiz.git "$TMP/src"
rsync -a --delete --exclude .env --exclude .venv "$TMP/src/next-box/" "$HERE/"
echo "Installing..."
scripts/install.sh
