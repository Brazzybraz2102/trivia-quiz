#!/usr/bin/env bash
# Starts Next Box at a hosting company. The container runs this (Dockerfile CMD; Render: render.yaml).
# It never prints a setting's value: when something is missing it names the setting, nothing more.
set -euo pipefail
cd "$(dirname "$0")/../.."

# Listen on every interface: the host's own proxy is what reaches this container.
export NEXTBOX_HOST="${NEXTBOX_HOST:-0.0.0.0}"
# The host says which port to listen on through PORT (Render: 10000). NEXTBOX_PORT wins when both are set.
if [[ -z "${NEXTBOX_PORT:-}" && -n "${PORT:-}" ]]; then
  export NEXTBOX_PORT="$PORT"
fi

# Hosted mode (NEXTBOX_HOSTED=1, the Dockerfile's default): no database here, no lasting disk, so these
# three have to come from the host's environment. Refuse plainly instead of starting and failing later.
hosted() {
  local v="${NEXTBOX_HOSTED:-}"
  [[ "${v,,}" == 1 || "${v,,}" == true || "${v,,}" == yes || "${v,,}" == on ]]
}
if hosted; then
  missing=0
  if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "DATABASE_URL is not set: Next Box needs the PostgreSQL database's connection string (the Blueprint fills it in from the database)." >&2
    missing=1
  fi
  if [[ -z "${NEXTBOX_KEY:-}" ]]; then
    echo "NEXTBOX_KEY is not set: the shared key the web app and scripts send (the Blueprint generates one)." >&2
    missing=1
  fi
  if [[ -z "${NEXTBOX_SECRET_KEY:-}" ]]; then
    echo "NEXTBOX_SECRET_KEY is not set: the key that locks people's saved to-do app connections (your secret.key's content, or 32+ random characters)." >&2
    missing=1
  fi
  if (( missing )); then
    echo "Add it in Render: your service, Environment. Then deploy again." >&2
    exit 1
  fi
fi

exec python -m nextbox serve
