#!/usr/bin/env bash
# Put Next Box on the internet at https://nextbox.braznarkel.com through a Cloudflare Tunnel.
# Before running: create the tunnel in the Cloudflare dashboard (README "Online at ...").
# It asks you to paste the install command Cloudflare shows; the secret in it is never printed.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
PUBLIC="${1:-https://nextbox.braznarkel.com}"

if ! command -v cloudflared >/dev/null; then
  echo "Installing Cloudflare's tunnel program (may ask for your computer password)..."
  sudo mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y cloudflared >/dev/null
fi

if systemctl is-active --quiet cloudflared 2>/dev/null; then
  echo "The tunnel is already installed and running."
else
  echo
  echo "Paste the command Cloudflare showed you (the one starting with sudo cloudflared service install),"
  echo "then press Enter. Nothing will show while you paste; that's on purpose."
  read -rs PASTED
  echo
  TOKEN="$(printf '%s' "$PASTED" | grep -oE 'eyJ[A-Za-z0-9_+/=-]{40,}' | head -1 || true)"
  unset PASTED
  if [[ -z "$TOKEN" ]]; then
    echo "That didn't contain a tunnel token. Copy the whole command again and re-run this script."
    exit 1
  fi
  sudo cloudflared service install "$TOKEN" >/dev/null
  unset TOKEN
  echo "Tunnel installed. It starts by itself whenever this computer starts."
fi

if grep -q '^NEXTBOX_PUBLIC_URL=' .env; then
  sed -i "s|^NEXTBOX_PUBLIC_URL=.*|NEXTBOX_PUBLIC_URL=$PUBLIC|" .env
else
  echo "NEXTBOX_PUBLIC_URL=$PUBLIC" >> .env
fi
# Cloudflare now carries the traffic, so the Privacy and About pages name it (a name already there is kept).
if grep -q '^NEXTBOX_HOSTING=.\+' .env; then
  :
elif grep -q '^NEXTBOX_HOSTING=' .env; then
  sed -i "s|^NEXTBOX_HOSTING=.*|NEXTBOX_HOSTING=Cloudflare|" .env
else
  echo "NEXTBOX_HOSTING=Cloudflare" >> .env
fi
systemctl --user restart nextbox.service
sleep 3
echo
echo "Done. On your phone, turn Wi-Fi OFF and open:  $PUBLIC/app/"
echo "If you see the Next Box sign-in page, you're online."
