#!/bin/sh
set -eu

COMPOSE_FILE=/opt/student-execution-os/current/deploy/docker-compose.nginx.yml
ENV_FILE=/etc/student-execution-os/student-execution-os.env
BACKUP_DIR=/var/backups/student-execution-os
STAMP=$(date -u +%Y-%m-%dT%H%M%SZ)
BACKUP=/backups/student-execution-os-$STAMP.db

umask 077
mkdir -p "$BACKUP_DIR"
docker compose -p student-execution-os -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T api \
  python -m student_execution_os backup \
  --database /data/student-execution-os.db \
  --output "$BACKUP"

find "$BACKUP_DIR" -type f \
  \( -name 'student-execution-os-*.db' -o -name 'student-execution-os-*.db.manifest.json' \) \
  -mtime +13 -delete
