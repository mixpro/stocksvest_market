#!/usr/bin/env bash
# Deploy to tinkergw (home server). Only run files are copied: docker-compose.yml (and later .env and images).
# Step 1 of the move: the whoami dummy app on the Tailscale address, reached through the VPS relay.
#
#   scripts/deploy-tinkergw.sh          # HOST=saeed@tinkergw.local by default
set -euo pipefail

HOST="${HOST:-saeed@tinkergw.local}"
DIR=stocksvest
HERE="$(cd "$(dirname "$0")/.." && pwd)/deploy/tinkergw"

echo "== copying run files to $HOST:~/$DIR"
ssh "$HOST" "mkdir -p ~/$DIR"
scp -q "$HERE/docker-compose.yml" "$HOST:$DIR/"

echo "== starting"
ssh "$HOST" "cd ~/$DIR && docker compose up -d && docker compose ps"
