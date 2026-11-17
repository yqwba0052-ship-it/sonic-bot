#!/usr/bin/env bash
# Prove the newest backup can be restored, in a separate temporary folder. Run as root.
# The live database and the bot are never touched.
set -euo pipefail
BACKUP_DIR=/opt/sonic/backups
LATEST=$(ls -1 "$BACKUP_DIR"/sonic-*.db.gz | sort | tail -1)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
chown sonic:sonic "$WORK"
echo "Backup: $LATEST"
sudo -u sonic /opt/sonic/venv/bin/python /opt/sonic/app/deploy/backup_db.py --verify "$LATEST"
gunzip -c "$LATEST" > "$WORK/sonic.db"
chown sonic:sonic "$WORK/sonic.db"
# open it with Sonic's own storage code, as the server would at start
sudo -u sonic bash -c "cd /opt/sonic/app/server && /opt/sonic/venv/bin/python -c \"
from sonic.store import Store
s = Store('$WORK/sonic.db')
print('restored store opens: trades', len(s.trades(100000)), 'events', len(s.events(100000)), 'last_scan', bool(s.get('last_scan')))
\""
echo "Restore test passed (live database untouched)."
