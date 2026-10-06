#!/bin/sh
set -eu
umask 077
mkdir -p backups
filename="backups/database-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T db pg_dump -U postgres -d roleplay -Fc > "$filename"
printf 'Backup saved to %s\n' "$filename"
