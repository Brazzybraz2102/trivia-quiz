#!/usr/bin/env bash
# One-shot desktop install: venv, tests, systemd user service, linger.
# Safe to re-run. Never prints secrets.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip setuptools
# constraints.txt pins every package to the versions this release was tested with.
.venv/bin/pip install -q -c constraints.txt -e '.[dev]'
.venv/bin/python -m pytest -q

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from .env.example. Fill it in, then re-run this script."
  exit 0
fi
chmod 600 .env

# systemd doesn't like spaces in paths ("custom projects"), so point it at a symlink.
LINK="$HOME/.local/share/nextbox/app"
mkdir -p "$(dirname "$LINK")"
ln -sfn "$HERE" "$LINK"

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/nextbox.service" <<EOF
[Unit]
Description=Next Box label printer server (LAN only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.local/share/nextbox/app
Environment=NEXTBOX_ENV_FILE=%h/.local/share/nextbox/app/.env
ExecStart=%h/.local/share/nextbox/app/.venv/bin/nextbox serve
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=%h/.local/share/nextbox

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now nextbox.service
systemctl --user restart nextbox.service
if ! loginctl show-user "$USER" -p Linger | grep -q yes; then
  echo "Enabling linger so Next Box starts at boot without a login (may ask for your password)."
  sudo loginctl enable-linger "$USER"
fi
sleep 2
systemctl --user --no-pager status nextbox.service | head -5
IP="$(hostname -I | awk '{print $1}')"
echo
echo "Web app: http://$IP:8787/app"
if .venv/bin/nextbox user list | grep -q '(no accounts)'; then
  echo "No sign-in accounts yet. Create yours with: .venv/bin/nextbox user add <name>"
fi
