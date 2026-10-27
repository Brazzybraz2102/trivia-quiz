#!/usr/bin/env bash
# Put Next Box on the internet at https://nextbox.braznarkel.com through a Cloudflare Tunnel.
# Before running: create the tunnel in the Cloudflare dashboard (README "Online at ...").
# It asks you to paste the install command Cloudflare shows; the secret in it is never printed.
# It ends by checking that the public address really reaches this Next Box.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
PUBLIC="${1:-https://nextbox.braznarkel.com}"
PORT="$(sed -n 's/^NEXTBOX_PORT=\([0-9][0-9]*\)$/\1/p' .env 2>/dev/null | tail -1 || true)"
PORT="${PORT:-8787}"

if ! command -v cloudflared >/dev/null; then
  if ! command -v apt-get >/dev/null; then
    echo "This PC has no apt (so it isn't Ubuntu or Debian), and Cloudflare's tunnel program isn't installed."
    echo "Install cloudflared for your Linux from https://pkg.cloudflare.com (Fedora: their .rpm),"
    echo "then run this again: scripts/go-online.sh"
    exit 1
  fi
  echo "Installing Cloudflare's tunnel program (may ask for your computer password)..."
  sudo mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
  sudo apt-get update -qq || true
  sudo apt-get install -y cloudflared >/dev/null || true
  if ! command -v cloudflared >/dev/null; then
    echo "Couldn't install cloudflared. Check this PC's internet connection, then run this again."
    exit 1
  fi
fi

# The tunnel a token belongs to (its "t" field). Tokens come in through the environment, never as arguments,
# and nothing of them is printed.
tunnel_of() {
  python3 - <<'PY'
import base64, json, os
token = os.environ.get("NB_TOKEN", "").strip()
try:
    print(json.loads(base64.b64decode(token + "=" * (-len(token) % 4)))["t"])
except Exception:
    print("")
PY
}
running_token() {  # the token the running cloudflared service was installed with (its unit file is readable)
  systemctl cat cloudflared 2>/dev/null | grep -oE -- '--token[ =]+eyJ[A-Za-z0-9_+/=-]{40,}' | head -1 \
    | grep -oE 'eyJ[A-Za-z0-9_+/=-]{40,}' || true
}

ask_token() {
  echo
  echo "Paste the command Cloudflare showed you (the one starting with sudo cloudflared service install),"
  echo "then press Enter. Nothing will show while you paste; that's on purpose."
  read -rs PASTED || PASTED=""
  echo
  TOKEN="$(printf '%s' "$PASTED" | grep -oE 'eyJ[A-Za-z0-9_+/=-]{40,}' | head -1 || true)"
  unset PASTED
}

OTHER=""
if systemctl is-active --quiet cloudflared 2>/dev/null; then
  echo "A Cloudflare tunnel is already running on this PC. To check it's Next Box's (and not one for"
  echo "something else), paste Next Box's install command too, or just press Enter to skip that."
  ask_token
  if [[ -n "$TOKEN" ]]; then
    MINE="$(NB_TOKEN="$TOKEN" tunnel_of)"
    THERE="$(NB_TOKEN="$(running_token)" tunnel_of)"
    if [[ -n "$MINE" && -n "$THERE" && "$MINE" != "$THERE" ]]; then
      OTHER=yes  # this PC already runs another tunnel; one PC runs one tunnel service
    elif [[ -n "$MINE" && "$MINE" == "$THERE" ]]; then
      echo "It's Next Box's tunnel."
    fi
  fi
  unset TOKEN
else
  ask_token
  if [[ -z "$TOKEN" ]]; then
    echo "That didn't contain a tunnel token. Copy the whole command again and re-run this script."
    exit 1
  fi
  if ! sudo cloudflared service install "$TOKEN" >/dev/null; then
    unset TOKEN
    echo "Couldn't install the tunnel. Run this again; if it fails the same way, check the command you copied."
    exit 1
  fi
  unset TOKEN
  echo "Tunnel installed. It starts by itself whenever this computer starts."
fi

# A tunnel that also carries something else: move-out.sh must leave it on when Next Box moves away later.
if [[ -n "$OTHER" ]] && ! grep -q '^NEXTBOX_TUNNEL=shared$' .env; then
  sed -i '/^NEXTBOX_TUNNEL=/d' .env
  echo "NEXTBOX_TUNNEL=shared" >> .env
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

# Does the public address reach this Next Box? (asks its /version; a minute at most for the tunnel to connect)
echo "Checking $PUBLIC from the internet (up to a minute)..."
OK=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if curl -fsS --max-time 8 "$PUBLIC/version" 2>/dev/null | grep -q '"version"'; then
    OK=yes
    break
  fi
  sleep 5
done
echo
if [[ -n "$OK" ]]; then
  echo "Done: $PUBLIC reaches Next Box on this PC. Open $PUBLIC/app/ on your phone."
  exit 0
fi
HOST="${PUBLIC#*://}"
HOST="${HOST%%/*}"
if [[ -n "$OTHER" ]]; then
  echo "This PC already runs another Cloudflare tunnel (for something else), and a PC runs only one."
  echo "So Next Box's address moves onto that one, in the dashboard (Zero Trust → Networks → Tunnels):"
  echo "  1. Next Box's old tunnel → Public Hostname: delete $HOST. If Cloudflare then says the name is"
  echo "     still taken, delete its leftover record under your domain's DNS → Records too."
  echo "  2. This PC's tunnel → Public Hostname → Add: $HOST → HTTP → localhost:$PORT"
  echo "  3. Check: curl $PUBLIC/version"
else
  echo "$PUBLIC doesn't reach Next Box here yet. In the dashboard, open your tunnel → Public Hostname and"
  echo "check it says: $HOST → HTTP → localhost:$PORT. Then check again with: curl $PUBLIC/version"
fi
exit 1
