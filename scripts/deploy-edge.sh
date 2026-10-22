#!/usr/bin/env bash
# Deploy the VPS edge: Authelia + the Caddy site blocks for stocksvest.hardansaeed.com and auth.hardansaeed.com.
# Copies only run files (no source code), creates the secrets once, validates everything before Caddy reloads.
#
#   scripts/deploy-edge.sh            # VPS=root@65.21.180.156 by default
set -euo pipefail

VPS="${VPS:-root@65.21.180.156}"
DIR=/opt/stocksvest-market/edge
HERE="$(cd "$(dirname "$0")/.." && pwd)/deploy/edge"
AUTHELIA_IMAGE=authelia/authelia:4.39.27

echo "== copying run files to $VPS:$DIR"
ssh "$VPS" "mkdir -p $DIR/authelia/data $DIR/secrets $DIR/redis && chmod 700 $DIR/secrets $DIR/redis"
# Fingerprint of what Authelia reads at startup (file hashes only), to restart it only when it changed.
fingerprint='sha256sum authelia/configuration.yml docker-compose.yml 2>/dev/null | cut -d" " -f1 | tr -d "\n"'
OLD_SUM="$(ssh "$VPS" "cd $DIR && $fingerprint")"
scp -q "$HERE/docker-compose.yml" "$HERE/stocksvest.caddy" "$VPS:$DIR/"
scp -q "$HERE/watch.py" "$VPS:$DIR/../watch.py"
scp -q "$HERE/authelia/configuration.yml" "$VPS:$DIR/authelia/"

ssh "$VPS" DIR="$DIR" IMAGE="$AUTHELIA_IMAGE" OLD_SUM="$OLD_SUM" bash -s <<'REMOTE'
set -euo pipefail
cd "$DIR"

echo "== secrets (created once, kept on this server only)"
for name in jwt session storage; do
  if [ ! -s "secrets/$name" ]; then
    openssl rand -hex 64 > "secrets/$name" && echo "  created secrets/$name"
  fi
done
chmod 600 secrets/*

if [ ! -s authelia/users.yml ]; then
  echo "!! authelia/users.yml is missing: run scripts/edge-set-password.sh first"; exit 1
fi
if [ ! -s secrets/smtp_password ] || ! grep -q '^AUTHELIA_NOTIFIER_SMTP_USERNAME=' .env 2>/dev/null; then
  echo "!! the SMTP login is missing: run scripts/set-smtp-key.sh first"; exit 1
fi

echo "== validating the Authelia configuration"
docker run --rm -v "$DIR/authelia:/config" -v "$DIR/secrets:/secrets:ro" \
  -e AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE=/secrets/jwt \
  -e AUTHELIA_SESSION_SECRET_FILE=/secrets/session \
  -e AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE=/secrets/storage \
  -e AUTHELIA_NOTIFIER_SMTP_PASSWORD_FILE=/secrets/smtp_password --env-file .env \
  "$IMAGE" authelia validate-config --config /config/configuration.yml

echo "== starting Authelia and Redis"
docker compose up -d
NEW_SUM="$(sha256sum authelia/configuration.yml docker-compose.yml | cut -d" " -f1 | tr -d "\n")"
if [ "$NEW_SUM" != "$OLD_SUM" ]; then  # Authelia reads its configuration only at startup
  docker compose restart authelia
  echo "  configuration changed: Authelia restarted (logins are kept in Redis)"
else
  echo "  configuration unchanged: Authelia not restarted"
fi
for i in $(seq 1 30); do
  curl -fs -o /dev/null http://127.0.0.1:9091/api/health && break
  sleep 2
done
curl -fs http://127.0.0.1:9091/api/health && echo

echo "== Caddy: import the site blocks (the existing sites stay as they are)"
LINE="import $DIR/stocksvest.caddy"
if ! grep -qxF "$LINE" /etc/caddy/Caddyfile; then
  cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S)"
  printf '\n%s\n' "$LINE" >> /etc/caddy/Caddyfile
  echo "  added: $LINE (backup saved next to the Caddyfile)"
fi
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy

echo "== watcher: tinkergw reachable, nightly backup arrived (every 5 minutes)"
chmod +x ../watch.py
grep -q '^ALERT_EMAIL_TO=' .env || echo "!! set ALERT_EMAIL_TO in $DIR/.env to receive the watcher's emails"
CRON="*/5 * * * * $DIR/../watch.py >> $DIR/../watch.log 2>&1"
crontab -l 2>/dev/null | grep -qF watch.py || { { crontab -l 2>/dev/null || true; echo "$CRON"; } | crontab -; echo "  installed"; }
echo "== done"
REMOTE
