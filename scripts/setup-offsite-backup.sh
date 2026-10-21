#!/usr/bin/env bash
# One-time setup of the off-site backup: tinkergw pushes its nightly backup to the VPS.
#   - a dedicated SSH key on tinkergw (~/.ssh/stocksvest_backup, used for nothing else)
#   - on the VPS that key may only WRITE into /opt/stocksvest-market/backups (rrsync -wo: no shell, no reading, no deleting)
#   - the VPS thins out old copies every day (deploy/edge/prune-backups.py)
# Safe to run again: every step checks first.
set -euo pipefail

HOST="${HOST:-saeed@tinkergw.local}"
VPS="${VPS:-root@65.21.180.156}"
VPS_ADDR="${VPS##*@}"
DEST=/opt/stocksvest-market/backups
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "== key on tinkergw"
ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
[ -f ~/.ssh/stocksvest_backup ] || ssh-keygen -q -t ed25519 -N "" -C "stocksvest-backup@tinkergw" -f ~/.ssh/stocksvest_backup
REMOTE
PUB="$(ssh "$HOST" cat .ssh/stocksvest_backup.pub)"

echo "== VPS: backup folders, restricted key, daily cleanup"
ssh "$VPS" "mkdir -p $DEST"
scp -q "$ROOT/deploy/edge/prune-backups.py" "$VPS:/opt/stocksvest-market/prune-backups.py"
ssh "$VPS" "PUB='$PUB' DEST='$DEST' bash -s" <<'REMOTE'
set -euo pipefail
mkdir -p "$DEST/app" "$DEST/market" && chmod 700 "$DEST"
chmod +x /opt/stocksvest-market/prune-backups.py
LINE="command=\"/usr/bin/rrsync -wo $DEST\",restrict $PUB"
grep -qF "$PUB" /root/.ssh/authorized_keys || { echo "$LINE" >> /root/.ssh/authorized_keys; echo "  restricted key added"; }
CRON="30 5 * * * /opt/stocksvest-market/prune-backups.py >> $DEST/prune.log 2>&1"
crontab -l 2>/dev/null | grep -qF prune-backups.py || { { crontab -l 2>/dev/null || true; echo "$CRON"; } | crontab -; echo "  daily cleanup installed (05:30)"; }
REMOTE

echo "== tinkergw trusts the VPS host key"
ssh "$HOST" "ssh-keygen -F $VPS_ADDR >/dev/null || ssh-keyscan -t ed25519 $VPS_ADDR 2>/dev/null >> ~/.ssh/known_hosts"

echo "== the key can write backups and nothing else"
ssh "$HOST" "ssh -i ~/.ssh/stocksvest_backup -o BatchMode=yes $VPS 'ls /' 2>&1 | head -2 || true"
echo "== done: the nightly backup now also goes to $VPS:$DEST"
