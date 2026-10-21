#!/usr/bin/env bash
# Install the Brevo SMTP credentials on the servers. Run it yourself in a terminal: it asks for the login and the
# SMTP key here and writes them straight to the servers (never into git):
#   VPS       /opt/stocksvest-market/edge/secrets/smtp_password + edge/.env (Authelia's one-time codes)
#   tinkergw  ~/stocksvest/.env SMTP_* (alerts from the app)
#
#   scripts/set-smtp-key.sh
set -euo pipefail

VPS="${VPS:-root@65.21.180.156}"
HOST="${HOST:-saeed@tinkergw.local}"
EDGE=/opt/stocksvest-market/edge
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587

read -rp "SMTP login (Brevo → SMTP & API → SMTP, e.g. xxxx@smtp-brevo.com): " LOGIN
read -rsp "SMTP key: " KEY; echo
[ -n "$LOGIN" ] && [ -n "$KEY" ] || { echo "login and key are required"; exit 1; }

echo "== VPS (Authelia)"
printf '%s' "$KEY" | ssh "$VPS" "umask 077 && mkdir -p $EDGE/secrets && cat > $EDGE/secrets/smtp_password"
ssh "$VPS" "umask 077 && cd $EDGE && touch .env && grep -v '^AUTHELIA_NOTIFIER_SMTP_USERNAME=' .env > .env.new; \
  echo 'AUTHELIA_NOTIFIER_SMTP_USERNAME=$LOGIN' >> .env.new && mv .env.new .env"

echo "== tinkergw (alerts)"
{ printf 'SMTP_HOST=%s\nSMTP_PORT=%s\nSMTP_USERNAME=%s\nSMTP_PASSWORD=%s\n' "$SMTP_HOST" "$SMTP_PORT" "$LOGIN" "$KEY"; } |
  ssh "$HOST" "cd ~/stocksvest && umask 077 && grep -vE '^SMTP_(HOST|PORT|USERNAME|PASSWORD)=' .env > .env.new; cat >> .env.new && mv .env.new .env"
unset KEY
echo "saved. Next: scripts/deploy-edge.sh (Authelia sends its codes by email from now on)"
