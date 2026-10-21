#!/usr/bin/env bash
# Deploy StocksVest to tinkergw (home server). Nothing is built there and no source code is copied:
#   1. run the tests here
#   2. build the image for tinkergw's processor (linux/amd64), tagged with the git commit
#   3. load it on tinkergw over SSH, copy the run files (compose, Grafana provisioning, backup script)
#   4. restart with the new tag, check health, install the nightly backup
#
#   scripts/deploy-tinkergw.sh                 # HOST=saeed@tinkergw.local; SKIP_TESTS=1 to skip step 1
#   STOCKSVEST_TAG=<older tag> scripts/deploy-tinkergw.sh --no-build   # roll back to an image already there
set -euo pipefail

HOST="${HOST:-saeed@tinkergw.local}"
DIR=stocksvest
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BUILD=1
[ "${1:-}" = "--no-build" ] && BUILD=0

if [ "$BUILD" = 1 ]; then
  TAG="$(git rev-parse --short HEAD)"
  [ -z "$(git status --porcelain -- src requirements.txt Dockerfile)" ] || TAG="$TAG-dirty"
  if [ "${SKIP_TESTS:-0}" != 1 ]; then
    echo "== tests"
    .venv/bin/python -m pytest -q
  fi
  echo "== building stocksvest:$TAG for linux/amd64"
  docker buildx build --platform linux/amd64 -t "stocksvest:$TAG" --load .
  echo "== loading the image on $HOST"
  docker save "stocksvest:$TAG" | gzip | ssh "$HOST" docker load
else
  TAG="${STOCKSVEST_TAG:?set STOCKSVEST_TAG to an image already on tinkergw}"
fi

echo "== run files"
ssh "$HOST" "mkdir -p ~/$DIR/data ~/$DIR/grafana ~/$DIR/backups"
scp -q deploy/tinkergw/docker-compose.yml deploy/tinkergw/backup.sh "$HOST:$DIR/"
scp -q -r grafana/provisioning grafana/dashboards "$HOST:$DIR/grafana/"

ssh "$HOST" TAG="$TAG" DIR="$DIR" bash -s <<'REMOTE'
set -euo pipefail
cd ~/"$DIR"
chmod +x backup.sh
[ -s .env ] || { echo "!! ~/$DIR/.env is missing: copy it from the Mac first (see deploy/README.md)"; exit 1; }
if grep -q '^STOCKSVEST_TAG=' .env; then
  sed -i "s/^STOCKSVEST_TAG=.*/STOCKSVEST_TAG=$TAG/" .env
else
  printf '\nSTOCKSVEST_TAG=%s\n' "$TAG" >> .env
fi

echo "== starting stocksvest:$TAG"
docker compose up -d --remove-orphans

echo "== health"
check() {
  for i in $(seq 1 30); do curl -fs -o /dev/null "$2" && { echo "  $1 ok"; return 0; }; sleep 2; done
  echo "  $1 NOT healthy ($2)"; docker compose logs --tail 30 "$1"; return 1
}
check market http://127.0.0.1:8000/health
check app http://100.126.3.52:8100/health
check grafana http://100.126.3.52:3000/api/health

LINE="15 3 * * * $HOME/$DIR/backup.sh >> $HOME/$DIR/backups/backup.log 2>&1"
if ! crontab -l 2>/dev/null | grep -qF "$DIR/backup.sh"; then
  { crontab -l 2>/dev/null || true; echo "$LINE"; } | crontab -   # no crontab yet: start one
  echo "== nightly backup installed (03:15)"
fi
docker image ls stocksvest --format '  kept image: {{.Tag}} ({{.CreatedSince}})' | head -5
REMOTE
echo "== deployed stocksvest:$TAG"
