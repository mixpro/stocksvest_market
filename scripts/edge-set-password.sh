#!/usr/bin/env bash
# Create or change the Authelia login on the VPS. Run it yourself in a terminal: it asks for the password,
# hashes it here on your machine (argon2, with Authelia's own tool in Docker) and sends only the hash.
#
#   scripts/edge-set-password.sh [username]      # default username: saeed
set -euo pipefail

VPS="${VPS:-root@65.21.180.156}"
DIR=/opt/stocksvest-market/edge
USERNAME="${1:-saeed}"
AUTHELIA_IMAGE=authelia/authelia:4.39.27

read -rp "Display name [Saeed]: " NAME; NAME="${NAME:-Saeed}"
read -rp "Email (shown in Authelia; one-time codes still go to notifications.txt): " EMAIL
read -rsp "Password: " PW; echo
read -rsp "Password again: " PW2; echo
[ "$PW" = "$PW2" ] || { echo "passwords differ"; exit 1; }
[ "${#PW}" -ge 12 ] || { echo "use at least 12 characters"; exit 1; }

HASH="$(docker run --rm "$AUTHELIA_IMAGE" authelia crypto hash generate argon2 --password "$PW" | sed -n 's/^Digest: //p')"
unset PW PW2
[ -n "$HASH" ] || { echo "hashing failed"; exit 1; }

ssh "$VPS" "mkdir -p $DIR/authelia && umask 077 && cat > $DIR/authelia/users.yml" <<EOF
# Written by scripts/edge-set-password.sh. Only the password's argon2 hash is stored.
users:
  $USERNAME:
    displayname: "$NAME"
    email: "$EMAIL"
    password: "$HASH"
    groups: [admins]
EOF
echo "saved user '$USERNAME' on $VPS (Authelia reloads users.yml by itself)"
