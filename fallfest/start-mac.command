#!/bin/bash
# Double-click to start the Fall Fest Party Pack on a Mac.
cd "$(dirname "$0")"
if ! command -v node >/dev/null 2>&1; then
  echo "Node.js isn't installed. Get the LTS version from https://nodejs.org, then double-click this file again."
  read -r -p "Press Enter to close."
  exit 1
fi
(sleep 1; open "http://localhost:${PORT:-3000}/host") &
node server.js
