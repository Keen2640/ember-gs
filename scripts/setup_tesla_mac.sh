#!/usr/bin/env bash
# One-time Tesla setup on your own Mac (steps 0-2 of docs/TESLA.md).
#
#   1. installs ember-gs into a local virtualenv
#   2. asks for the Tesla Client ID and Client Secret (secret input is hidden)
#      and stores them in ~/.ember/.env with owner-only permissions
#   3. generates the app's P-256 key pair: private key stays in ~/.ember,
#      public key is written into pages/ for publishing
#
# Run from the repo root:   bash scripts/setup_tesla_mac.sh
# Safe to re-run: existing credentials and keys are kept unless you say otherwise.
set -euo pipefail

cd "$(dirname "$0")/.."
EMBER_HOME="${EMBER_HOME:-$HOME/.ember}"
PUB="pages/.well-known/appspecific/com.tesla.3p.public-key.pem"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# --- 1. Python environment ------------------------------------------------------
say "1/3  Installing ember-gs"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10+ not found. Install it with:  brew install python@3.12   then re-run this script."
  exit 1
fi
[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -e ".[dev]"
echo "installed: $(ember --help >/dev/null && echo ember CLI ok)"

# --- 2. Credentials ---------------------------------------------------------------
say "2/3  Tesla app credentials (developer.tesla.com -> your app -> Credentials)"
mkdir -p "$EMBER_HOME" && chmod 700 "$EMBER_HOME"
ENV_FILE="$EMBER_HOME/.env"
write_env=1
if [ -f "$ENV_FILE" ] && grep -q '^TESLA_CLIENT_SECRET=.' "$ENV_FILE"; then
  read -r -p "Credentials already saved in $ENV_FILE. Replace them? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || write_env=0
fi
if [ "$write_env" = 1 ]; then
  read -r -p "Client ID: " CID
  read -r -s -p "Client Secret (hidden while you type): " CSEC; echo
  if [ -z "$CID" ] || [ -z "$CSEC" ]; then echo "both values are required"; exit 1; fi
  umask 077
  {
    echo "TESLA_CLIENT_ID=$CID"
    echo "TESLA_CLIENT_SECRET=$CSEC"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  unset CSEC
  echo "saved to $ENV_FILE (readable only by you)"
fi

# --- 3. Key pair --------------------------------------------------------------------
say "3/3  App key pair"
if [ -f "$EMBER_HOME/tesla-private-key.pem" ]; then
  echo "private key already exists at $EMBER_HOME/tesla-private-key.pem (keeping it)"
  mkdir -p "$(dirname "$PUB")"
  openssl ec -in "$EMBER_HOME/tesla-private-key.pem" -pubout -out "$PUB" 2>/dev/null
else
  ember tesla keygen --out "$PUB"
fi

say "Done. Your PUBLIC key (safe to share; paste it to Claude):"
cat "$PUB"
cat <<EOF

Next:
  * Create an empty public GitHub repo named exactly  Keen2640.github.io  and tell Claude.
  * In new terminals, load your credentials with:
        source .venv/bin/activate && set -a && source ~/.ember/.env && set +a
  * Never share ~/.ember/.env or ~/.ember/tesla-private-key.pem.
EOF
