#!/usr/bin/env bash
# Install (or refresh) the daily database backup on the cloud server. Run as root.
# Safe to run again. It never stops the bot and never touches the live database.
set -euo pipefail
APP_DIR=/opt/sonic/app
BACKUP_DIR=/opt/sonic/backups
if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root." >&2
  exit 1
fi
install -d -m 700 -o sonic -g sonic "$BACKUP_DIR"
install -m 644 "$APP_DIR/deploy/sonic-backup.service" /etc/systemd/system/sonic-backup.service
install -m 644 "$APP_DIR/deploy/sonic-backup.timer" /etc/systemd/system/sonic-backup.timer
systemctl daemon-reload
systemctl enable --now sonic-backup.timer
echo "Backup timer installed: $(systemctl show sonic-backup.timer -p NextElapseUSecRealtime --value)"
echo "Run one now with: systemctl start sonic-backup.service; then cat $BACKUP_DIR/last_backup.json"
