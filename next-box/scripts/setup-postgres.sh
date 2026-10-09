#!/usr/bin/env bash
# Move Next Box from its SQLite file to PostgreSQL on this computer, so you can learn the
# database real products use. Safe to re-run. Never prints the database password.
#   - installs PostgreSQL (free) and starts it at boot
#   - makes a "nextbox" login plus two databases: nextbox (real) and nextbox_practice
#   - copies everything from the SQLite file, then points .env's DATABASE_URL at PostgreSQL
# The SQLite file is left where it is, untouched, as a backup.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
[[ -f .env ]] || { echo "Run scripts/install.sh first."; exit 1; }
if grep -q '^DATABASE_URL=' .env; then
  echo "Next Box already uses PostgreSQL (DATABASE_URL is in .env). Nothing to do."
  exit 0
fi

echo "Installing PostgreSQL (this may ask for your computer password)..."
sudo apt-get install -y postgresql postgresql-client >/dev/null
sudo systemctl enable --now postgresql

PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
# The password goes in on stdin, never on a command line other people could see.
sudo -u postgres psql -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nextbox') THEN
    CREATE ROLE nextbox LOGIN PASSWORD '$PW';
  ELSE
    ALTER ROLE nextbox LOGIN PASSWORD '$PW';
  END IF;
END \$\$;
SQL
for DB in nextbox nextbox_practice; do
  if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB'" | grep -q 1; then
    sudo -u postgres createdb -O nextbox "$DB"
  fi
done
URL="postgresql://nextbox:$PW@127.0.0.1:5432/nextbox"

.venv/bin/pip install -q -c constraints.txt -e '.[dev,postgres]'
DATA="$(.venv/bin/python -c 'from nextbox.config import load_settings; print(load_settings().data_dir)')"
if [[ -f "$DATA/nextbox.db" ]]; then
  echo "Copying your accounts, lists and history from $DATA/nextbox.db ..."
  printf '%s' "$URL" | .venv/bin/nextbox db copy-to -
fi

echo "DATABASE_URL=$URL" >> .env
chmod 600 .env
unset PW URL
systemctl --user restart nextbox.service 2>/dev/null || true
echo
echo "Done. Next Box now uses PostgreSQL. Your old SQLite file stays in $DATA as a backup."
echo "Look inside:  psql -h 127.0.0.1 -U nextbox nextbox   (password: in DATABASE_URL in .env)"
echo "Practice:     .venv/bin/nextbox db demo   then see DATABASE.md, lesson 13"
.venv/bin/nextbox doctor || true
