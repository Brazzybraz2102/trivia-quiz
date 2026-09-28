#!/usr/bin/env bash
# One-shot desktop install: venv, tests, systemd user service, linger.
# Safe to re-run. Never prints secrets.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e '.[dev]'
.venv/bin/python -m pytest -q

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from .env.example. Fill it in, then re-run this script."
  exit 0
fi
chmod 600 .env

# systemd doesn't like spaces in paths ("custom projects"), so point it at a symlink.
LINK="$HOME/.local/share/ticket/app"
mkdir -p "$(dirname "$LINK")"
ln -sfn "$HERE" "$LINK"

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/ticket.service" <<EOF
[Unit]
Description=ticket label printer server (LAN only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.local/share/ticket/app
Environment=TICKET_ENV_FILE=%h/.local/share/ticket/app/.env
ExecStart=%h/.local/share/ticket/app/.venv/bin/ticket serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now ticket.service
systemctl --user restart ticket.service
if ! loginctl show-user "$USER" -p Linger | grep -q yes; then
  echo "Enabling linger so ticket starts at boot without a login (may ask for your password)."
  sudo loginctl enable-linger "$USER"
fi
sleep 2
systemctl --user --no-pager status ticket.service | head -5
IP="$(hostname -I | awk '{print $1}')"
echo
echo "Web app: http://$IP:8787/app"
