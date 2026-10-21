#!/usr/bin/env bash
# Nightly backup of the two databases (run by cron on tinkergw, installed by scripts/deploy-tinkergw.sh).
# Uses SQLite's online backup inside the app container, so it is safe while the services run.
# Keeps 14 days in ~/stocksvest/backups/YYYY-MM-DD/.
set -euo pipefail
cd "$(dirname "$0")"

DAY="$(date +%F)"
mkdir -p "backups/$DAY"
docker exec -i stocksvest-app python - "$DAY" <<'PY'
import sqlite3, sys
day = sys.argv[1]
for name in ("app.db", "market.db"):
    src = sqlite3.connect(f"/data/{name}")
    dst = sqlite3.connect(f"/data/backup-tmp-{name}")
    src.backup(dst)
    dst.close()
    src.close()
PY
for name in app.db market.db; do
  mv "data/backup-tmp-$name" "backups/$DAY/$name"
done
gzip -f "backups/$DAY/market.db"
find backups -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -r {} +
echo "$(date -Is) backup ok: backups/$DAY"
