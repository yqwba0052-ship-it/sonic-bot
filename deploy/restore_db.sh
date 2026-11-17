#!/usr/bin/env bash
# Restore Sonic's database from one backup. Run as root, only when the owner decides to.
#   restore_db.sh /opt/sonic/backups/sonic-YYYYmmdd-HHMMSS.db.gz
# Steps: check the backup (sha256 + integrity) -> stop the bot -> keep the current database
# beside it as sonic.db.before-restore-<time> (nothing is deleted) -> put the backup in place
# -> start the bot (it resumes by itself when "resume after restart" is on).
set -euo pipefail
BACKUP="${1:?give the backup file}"
DB=/opt/sonic/app/server/sonic.db
PY=/opt/sonic/venv/bin/python
if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root." >&2
  exit 1
fi
sudo -u sonic "$PY" /opt/sonic/app/deploy/backup_db.py --verify "$BACKUP"
STAMP=$(date -u +%Y%m%d-%H%M%S)
TMP=$(mktemp -p /opt/sonic restore-XXXXXX.db)
gunzip -c "$BACKUP" > "$TMP"
chown sonic:sonic "$TMP"
chmod 600 "$TMP"
systemctl stop sonic
cp -p "$DB" "$DB.before-restore-$STAMP"
mv "$TMP" "$DB"
systemctl start sonic
echo "Restored $BACKUP. The previous database is kept as $DB.before-restore-$STAMP"
