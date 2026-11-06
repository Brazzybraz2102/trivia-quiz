#!/usr/bin/env bash
# Put Next Box on the internet at https://nextbox.braznarkel.com through a Cloudflare Tunnel.
#   ~/next-box/scripts/go-online.sh      (or ~/.local/share/nextbox/app/scripts/go-online.sh)
# Before running: the tunnel exists in the Cloudflare dashboard (README "Online at ...") and its page is open at
# Overview → Replicas → + Add a replica → Debian, 64-bit: the install command there carries the tunnel's token.
# It asks you to paste that command. The token is never shown or kept anywhere but the service itself
# (cloudflared writes it to /etc/cloudflared, readable by root only; sudo's own log line holds the command too),
# so never paste it into a chat, a note or the terminal itself: only into this script's hidden prompt.
# A tunnel service already on this PC: Next Box's own (also one left from before, or one with an older token)
# is replaced with the pasted command; a tunnel for something else is asked about.
# It ends by checking that the public address really reaches this Next Box, and what to do if not.
set -euo pipefail
OLD_STTY=""  # the terminal's settings while a paste is read (put back on any exit)
trap '[[ -n "${OLD_STTY:-}" ]] && stty "$OLD_STTY" 2>/dev/null; exit 130' INT TERM
HERE="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$HERE"
# How hints name this folder's scripts, typed from the home folder (quoted, so a space in a folder name pastes).
if [[ "$HERE" == "$HOME"/* ]]; then printf -v S '~/%q/scripts' "${HERE#"$HOME"/}"; else printf -v S '%q/scripts' "$HERE"; fi
PUBLIC="${1:-https://nextbox.braznarkel.com}"
HOST="${PUBLIC#*://}"
HOST="${HOST%%/*}"
PORT="$(sed -n 's/^NEXTBOX_PORT=\([0-9][0-9]*\)$/\1/p' .env 2>/dev/null | tail -1 || true)"
PORT="${PORT:-8787}"
VERSION="$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml 2>/dev/null | head -1 || true)"
THIS_PC="$(hostname 2>/dev/null || echo "this PC")"
MASK='s/eyJ[A-Za-z0-9_+\/=.-]{20,}/<token>/g'  # anything cloudflared says is shown with tokens blanked
DASH="Zero Trust → Networks → Tunnels (on newer dashboards: Networking → Tunnels) → your tunnel"

if ! command -v cloudflared >/dev/null; then
  if ! command -v apt-get >/dev/null; then
    echo "This PC has no apt (so it isn't Ubuntu or Debian), and Cloudflare's tunnel program isn't installed."
    echo "Install cloudflared for your Linux from https://pkg.cloudflare.com (Fedora: their .rpm),"
    echo "then run this again: $S/go-online.sh"
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
unit_exists() {  # a cloudflared service file on this PC (running or not; also one left from an earlier try)
  systemctl cat cloudflared >/dev/null 2>&1  # systemd's own answer (a plain file check would bypass the tests' stand-in)
}
# The tunnel the service here was installed for: THERE gets its id (or "" when it can't be told) and UNIT_TOKEN
# its token, if readable (never printed). cloudflared before 2026.7 put the token on the service's ExecStart line;
# newer ones keep it in a file only root can read; the service's log names the tunnel too ("tunnelID=").
unit_tunnel() {
  local unit path
  THERE=""
  UNIT_TOKEN=""
  unit="$(systemctl cat cloudflared 2>/dev/null || true)"
  UNIT_TOKEN="$(grep -oE -- '--token[ =]+eyJ[A-Za-z0-9_+/=.-]{40,}' <<<"$unit" | head -1 \
    | grep -oE 'eyJ[A-Za-z0-9_+/=.-]{40,}' || true)"
  if [[ -z "$UNIT_TOKEN" ]]; then
    path="$(grep -oE -- '--token-file[ =]+[^ ]+' <<<"$unit" | head -1 | sed -E 's/^--token-file[ =]+//' || true)"
    if [[ -n "$path" ]]; then
      echo "(Reading which tunnel the service here runs may ask for your password.)"
      UNIT_TOKEN="$(sudo cat "$path" 2>/dev/null | tr -d '[:space:]' || true)"
    fi
  fi
  if [[ -n "$UNIT_TOKEN" ]]; then
    THERE="$(NB_TOKEN="$UNIT_TOKEN" tunnel_of)"
  fi
  if [[ -z "$THERE" ]]; then
    THERE="$(journalctl -u cloudflared -n 300 --no-pager -o cat 2>/dev/null | grep -oE 'tunnelID=[0-9a-f-]{36}' \
      | tail -1 | cut -d= -f2 || true)"
  fi
}

ask_token() {
  echo
  echo "In the dashboard, under + Add a replica (Debian, 64-bit), copy the command that ends with the long"
  echo "token: it starts with sudo cloudflared service install. Paste it here. Nothing shows while you paste;"
  echo "that's on purpose. The script reads the paste by itself, so you don't need to press Enter."
  # The paste is read byte by byte, until it stops coming (a second of quiet). A paste is one block however
  # many lines and blank lines it has, and nothing of it is left for the shell: a line-by-line read stopped at
  # a blank line, and a time limit on a line handed the token's line to the shell (on screen, in the history).
  local all="" c line
  TOKEN=""
  if [[ -t 0 ]]; then
    OLD_STTY="$(stty -g 2>/dev/null || true)"
    stty -icanon -echo min 1 time 0 2>/dev/null || true
    IFS= read -rs -N 1 c || c=""
    all="$c"
    while [[ -n "$c" ]] && IFS= read -rs -t 1 -N 1 c; do
      all+="$c"
    done
    stty "$OLD_STTY" 2>/dev/null || true
    OLD_STTY=""
    TOKEN="$(printf '%s' "$all" | grep -oE 'eyJ[A-Za-z0-9_+/=.-]{40,}' | head -1 || true)"
  else  # not a terminal (a script feeds it): line by line until the token, so later answers stay unread
    local eof=""
    while [[ -z "$TOKEN" && -z "$eof" ]]; do
      IFS= read -rs line || eof=yes  # at the end, a last line without a newline still arrives in $line
      TOKEN="$(printf '%s' "$line" | grep -oE 'eyJ[A-Za-z0-9_+/=.-]{40,}' | head -1 || true)"
    done
  fi
  echo
  unset all c line
}

remove_unit() {  # the tunnel service on this PC goes (cloudflared's own uninstall: unit, timer and token file)
  local out
  if ! out="$(sudo cloudflared service uninstall 2>&1)"; then
    sed -E "$MASK" <<<"$out" | tail -n 3 | sed 's/^/  /'
  fi
  if unit_exists; then
    echo "Couldn't remove the tunnel service that's on this PC. Run this, then run this script again:"
    echo "  sudo cloudflared service uninstall"
    exit 1
  fi
}

install_tunnel() {  # installs the pasted token's tunnel as the service; on failure, cloudflared's own words
  local err
  err="$(mktemp)"
  if sudo cloudflared service install "$TOKEN" >/dev/null 2>"$err"; then
    rm -f "$err"
    return 0
  fi
  echo "Couldn't install the tunnel. cloudflared said:"
  sed -E "$MASK" "$err" | tail -n 3 | sed 's/^/  /'
  if grep -q "already installed" "$err"; then
    echo "A tunnel service is still on this PC. Remove it, then run this again:"
    echo "  sudo cloudflared service uninstall"
  else
    echo "Copy the whole command from the dashboard again ($DASH → + Add a replica → Debian), then run this again."
  fi
  rm -f "$err"
  return 1
}

# cloudflared started by hand in a terminal ("cloudflared tunnel run ...") would be a second connector: close it.
if ! systemctl is-active --quiet cloudflared 2>/dev/null && PIDS="$(pgrep -x cloudflared 2>/dev/null)" && [[ -n "$PIDS" ]]; then
  echo "cloudflared is running on this PC by hand (process $(tr '\n' ' ' <<<"$PIDS")), not as a service: a tunnel"
  echo "started in a terminal. Close that terminal (Ctrl+C there) first, then run this again."
  exit 1
fi

OTHER=""
INSTALLED=""
THERE=""
UNIT_TOKEN=""
if systemctl is-active --quiet cloudflared 2>/dev/null; then
  echo "A Cloudflare tunnel is already running on this PC. To check it's Next Box's (and not one for"
  echo "something else), paste Next Box's install command too, or just press Enter to skip that."
  ask_token
  if [[ -n "$TOKEN" ]]; then
    MINE="$(NB_TOKEN="$TOKEN" tunnel_of)"
    unit_tunnel
    if [[ -n "$MINE" && -n "$THERE" && "$MINE" != "$THERE" ]]; then
      OTHER=yes  # this PC already runs another tunnel; one PC runs one tunnel service
      echo "The tunnel here is another one (for something else). One PC runs one tunnel service, so Next Box's"
      echo "address goes onto that one (how: at the end)."
    elif [[ -n "$MINE" && "$MINE" == "$THERE" && -n "$UNIT_TOKEN" && "$UNIT_TOKEN" == "$TOKEN" ]]; then
      echo "It's Next Box's tunnel, with this same token. Nothing to install."
    elif [[ -n "$MINE" && "$MINE" == "$THERE" ]]; then
      echo "It's Next Box's tunnel. Reinstalling it with the command you pasted, so it runs with the newest token"
      echo "(after a Rotate token in the dashboard, the old token can't connect any more). It may ask for your password."
      remove_unit
      install_tunnel || { unset TOKEN UNIT_TOKEN; exit 1; }
      INSTALLED=yes
    elif grep -q '^NEXTBOX_TUNNEL=shared$' .env 2>/dev/null; then
      OTHER=yes  # go-online.sh put Next Box on a tunnel that was already here for something else
      echo "The tunnel here is shared with something else (NEXTBOX_TUNNEL=shared in .env), so it stays as it is."
    else
      echo "Couldn't tell which tunnel the service here runs (no token to read, and its log doesn't say)."
      read -r -p "Replace it with Next Box's tunnel, from the command you pasted? Say n only if this PC runs another website through Cloudflare. [Y/n] " ANSWER || ANSWER=""
      if [[ "$ANSWER" == [nN]* ]]; then
        OTHER=yes
      else
        echo "Replacing it (it may ask for your password)."
        remove_unit
        install_tunnel || { unset TOKEN UNIT_TOKEN; exit 1; }
        INSTALLED=yes
      fi
    fi
  fi
  unset TOKEN UNIT_TOKEN
else
  ask_token
  if [[ -z "$TOKEN" ]]; then
    echo "That didn't contain a tunnel token. In the dashboard ($DASH → + Add a replica → Debian) the page"
    echo "shows two commands: the one that installs the program (apt), and below it the one that starts with"
    echo "sudo cloudflared service install and ends with the long token. Copy that one, and run this again."
    exit 1
  fi
  if unit_exists; then  # left from an earlier try (Mike's server), or a tunnel for something else that's off
    MINE="$(NB_TOKEN="$TOKEN" tunnel_of)"
    unit_tunnel
    if [[ -n "$THERE" && -n "$MINE" && "$THERE" != "$MINE" ]]; then
      echo "A tunnel service for another tunnel (not Next Box's) is on this PC, but it isn't running."
      read -r -p "Replace it with Next Box's? Say n only if that tunnel carries another website from this PC. [y/N] " ANSWER || ANSWER=""
      if [[ "$ANSWER" != [yY]* ]]; then
        echo "Nothing was changed. To put Next Box on that tunnel instead: sudo systemctl enable --now cloudflared,"
        echo "then run this again (it then says how to add $HOST to it in the dashboard)."
        unset TOKEN UNIT_TOKEN
        exit 1
      fi
      echo "Replacing it (it may ask for your password)."
    elif [[ -n "$THERE" ]]; then
      echo "A tunnel service from before is on this PC but isn't running (it's Next Box's tunnel)."
      echo "Replacing it with the command you pasted (it may ask for your password)."
    else
      echo "A tunnel service from before is on this PC but isn't running (can't tell whose: no token to read)."
      echo "Replacing it with the command you pasted (it may ask for your password)."
    fi
    remove_unit
  fi
  install_tunnel || { unset TOKEN UNIT_TOKEN; exit 1; }
  unset TOKEN UNIT_TOKEN
  INSTALLED=yes
fi

if [[ -n "$INSTALLED" ]]; then
  echo "Tunnel installed. It starts by itself whenever this computer starts. Waiting for it to connect..."
  UP=""
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if systemctl is-active --quiet cloudflared 2>/dev/null; then
      UP=yes
      break
    fi
    sleep 2
  done
  if [[ -z "$UP" ]]; then
    echo "The tunnel service was installed but isn't running yet. See why with (safe to paste; no token in it):"
    echo "  journalctl -u cloudflared -n 20 --no-pager"
  fi
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
systemctl --user restart nextbox.service 2>/dev/null \
  || echo "Next Box's service isn't set up on this PC yet: run $S/install.sh, then check with: curl $PUBLIC/version"

# Does the public address reach this Next Box? (asks its /version; a minute at most for the tunnel to connect)
echo "Checking $PUBLIC from the internet (up to a minute)..."
BODY="$(mktemp)"
trap 'rm -f "$BODY"; [[ -n "${OLD_STTY:-}" ]] && stty "$OLD_STTY" 2>/dev/null; true' EXIT
OK=""
CODE=000
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  CODE="$(curl -s -o "$BODY" -w '%{http_code}' --max-time 8 "$PUBLIC/version" 2>/dev/null)" || CODE=000
  [[ "$CODE" =~ ^[0-9]{3}$ ]] || CODE=000
  if [[ "$CODE" == 200 ]] && grep -q '"version"' "$BODY"; then
    OK=yes
    break
  fi
  sleep 5
done
echo
REPLICAS="In the tunnel's page (Overview), Replicas must list only $THIS_PC. If the old PC is there too, run on it: sudo systemctl disable --now cloudflared"
if [[ -n "$OK" ]]; then
  GOT="$(grep -oE '"version": *"[^"]+"' "$BODY" | head -1 | sed -E 's/.*"([^"]+)"$/\1/' || true)"
  if [[ -n "$VERSION" && -n "$GOT" && "$GOT" != "$VERSION" ]]; then
    echo "$PUBLIC answers, but with Next Box $GOT, not this PC's $VERSION: another PC (the old one?) is still on"
    echo "the tunnel. On it: sudo systemctl disable --now cloudflared. Then check again: curl $PUBLIC/version"
    echo "$REPLICAS"
    exit 1
  fi
  echo "Done: $PUBLIC reaches Next Box on this PC. Open $PUBLIC/app/ on your phone."
  echo "$REPLICAS"
  exit 0
fi
if [[ -n "$OTHER" ]]; then
  echo "This PC already runs another Cloudflare tunnel (for something else), and a PC runs only one."
  echo "So Next Box's address moves onto that one, in the dashboard ($DASH):"
  echo "  1. Next Box's old tunnel → Routes (Published application routes; it used to be called Public Hostname):"
  echo "     delete $HOST. If Cloudflare then says the name is still taken, delete its leftover record under"
  echo "     your domain's DNS → Records too."
  echo "  2. This PC's tunnel → Routes → Add route → Published application: $HOST → HTTP → localhost:$PORT"
  echo "  3. Check: curl $PUBLIC/version"
elif [[ "$CODE" == 502 ]]; then
  echo "The tunnel is up, but Cloudflare reached a PC where Next Box isn't running: another PC (the old one?)"
  echo "still runs this tunnel, or Next Box here is off (check: curl http://127.0.0.1:$PORT/version)."
  echo "On the old PC: sudo systemctl disable --now cloudflared. Then check again: curl $PUBLIC/version"
elif [[ "$CODE" == 530 ]]; then
  echo "No PC is connected to the tunnel. See why here with (safe to paste; no token in it):"
  echo "  journalctl -u cloudflared -n 20 --no-pager"
  echo "Does it say Unauthorized? Then the token was rotated in the dashboard: run this again with a fresh"
  echo "command from + Add a replica."
elif [[ "$CODE" == 000 ]]; then
  echo "This PC can't reach $HOST right now (no internet, or no DNS). Check its connection, then:"
  echo "  curl $PUBLIC/version"
else
  echo "$PUBLIC doesn't reach Next Box here yet (it answered $CODE). In the dashboard open $DASH →"
  echo "Routes (Published application routes; it used to be called Public Hostname) and check it says:"
  echo "  $HOST → HTTP → localhost:$PORT"
  echo "Then check again with: curl $PUBLIC/version"
fi
echo "$REPLICAS"
exit 1
