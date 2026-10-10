#!/bin/sh
set -eu
umask 077
mkdir -p backups
filename="backups/database-$(date -u +%Y%m%dT%H%M%SZ).dump"
# Write to a temporary name so a failed dump never looks like a valid backup.
partial="$filename.partial"
trap 'rm -f "$partial"' EXIT INT TERM
docker compose exec -T db pg_dump -U postgres -d roleplay -Fc > "$partial"
# Confirm the archive is readable before publishing it.
docker compose exec -T db pg_restore -l < "$partial" > /dev/null
mv "$partial" "$filename"
trap - EXIT INT TERM
printf 'Backup saved to %s\n' "$filename"
