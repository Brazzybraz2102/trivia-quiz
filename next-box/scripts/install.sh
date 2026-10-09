#!/usr/bin/env bash
# One-shot desktop install: venv, tests, systemd user service, linger.
# Safe to re-run. Never prints secrets.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip setuptools
# constraints.txt pins every package to the versions this release was tested with.
.venv/bin/pip install -q -c constraints.txt -e '.[dev,postgres]'
.venv/bin/python -m pytest -q

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example."
fi
chmod 600 .env
# The server won't start without NEXTBOX_KEY, so make one if it's missing (never shown).
if ! grep -q '^NEXTBOX_KEY=.\+' .env; then
  KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  if grep -q '^NEXTBOX_KEY=' .env; then
    sed -i "s|^NEXTBOX_KEY=.*|NEXTBOX_KEY=$KEY|" .env
  else
    echo "NEXTBOX_KEY=$KEY" >> .env
  fi
  unset KEY
  echo "Made a NEXTBOX_KEY for you (saved in .env)."
fi
# Still the example printer address: preview only until a real one is set, so nothing hangs.
if grep -q '^PRINTER_IP=192.168.1.50$' .env && ! grep -q '^NEXTBOX_DRY_RUN=' .env; then
  echo "NEXTBOX_DRY_RUN=1" >> .env
  echo "No printer address yet, so Next Box makes previews only. Set PRINTER_IP in .env and delete"
  echo "the NEXTBOX_DRY_RUN=1 line when your printer is ready (or add printers in Admin → Printers)."
fi

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
if ! systemctl --user is-active --quiet nextbox.service; then
  echo "The service didn't start. See why with: journalctl --user -u nextbox -n 20 --no-pager"
fi
echo "On this computer: http://localhost:8787/app"
echo "On your phone (same Wi-Fi): http://$IP:8787/app"
if command -v ufw >/dev/null && sudo -n ufw status 2>/dev/null | grep -q "Status: active"; then
  echo "Your firewall is on. To let phones in: sudo ufw allow from 192.168.0.0/16 to any port 8787 proto tcp"
fi
echo "Check everything any time with: .venv/bin/nextbox doctor"
if .venv/bin/nextbox user list | grep -q '(no accounts)'; then
  echo "No sign-in accounts yet. Create yours with: .venv/bin/nextbox user add <name>"
fi
