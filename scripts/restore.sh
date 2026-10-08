#!/bin/sh
# Explicit operator action: never called by startup or development tests.
set -eu
umask 077
if [ "$#" -ne 2 ]; then
  printf 'Usage: %s BACKUP.dump CONSERVATIVE_LIFETIME_SPEND_USD\n' "$0" >&2
  exit 2
fi
restore_file=$1
restore_floor=$2
case "$restore_floor" in
  ''|*[!0-9.]*|.*|*.*.*) printf 'Supply a nonnegative decimal spending floor.\n' >&2; exit 2 ;;
esac
test -f "$restore_file"
docker compose stop bot
# Preserve any more recent local spending before replacing the database.
previous_spend=$(docker compose exec -T db psql -U postgres -d roleplay -At -v ON_ERROR_STOP=1 -c 'SELECT spent FROM runtime WHERE id=1')
docker compose exec -T db pg_restore -U postgres -d roleplay --single-transaction --exit-on-error --clean --if-exists < "$restore_file"
docker compose exec -T db psql -U postgres -d roleplay -v ON_ERROR_STOP=1 -v floor="$restore_floor" -v previous="$previous_spend" <<'SQL'
BEGIN;
-- Old archives may predate the ledger. Startup migrations will adopt them.
CREATE TABLE IF NOT EXISTS spending_ledger (
 id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL,
 amount NUMERIC(14,8) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE spending_ledger OWNER TO roleplay;
ALTER SEQUENCE spending_ledger_id_seq OWNER TO roleplay;
INSERT INTO spending_ledger(kind,amount) SELECT 'legacy_balance',spent FROM runtime WHERE id=1
 AND NOT EXISTS (SELECT 1 FROM spending_ledger);
INSERT INTO spending_ledger(kind,amount)
 SELECT 'restore_floor',GREATEST(spent,:'floor'::numeric,:'previous'::numeric)-spent FROM runtime WHERE id=1;
UPDATE runtime SET spent=GREATEST(spent,:'floor'::numeric,:'previous'::numeric) WHERE id=1;
-- Never replay replies or drafts from an older point in time.
DELETE FROM jobs;
DELETE FROM personality_drafts;
ALTER TABLE runtime ADD COLUMN IF NOT EXISTS drop_pending_on_start BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE runtime SET drop_pending_on_start=TRUE WHERE id=1;
COMMIT;
SQL
printf 'Restore complete. Bot remains stopped. Review the spending floor before restarting; startup will discard pending Telegram updates.\n'
