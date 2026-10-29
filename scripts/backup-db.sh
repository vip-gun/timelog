#!/usr/bin/env sh
# Dump the PostgreSQL database to backups/timelog-YYYYmmdd-HHMM.sql.gz
set -eu
cd "$(dirname "$0")/.."
mkdir -p backups
FILE="backups/timelog-$(date +%Y%m%d-%H%M).sql.gz"
docker compose exec -T db pg_dump -U timelog -d timelog | gzip > "$FILE"
echo "Backup written to $FILE"
