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
is_decimal() {
  case "$1" in
    ''|*[!0-9.]*|.*|*.*.*) return 1 ;;
  esac
}
is_decimal "$restore_floor" || { printf 'Supply a nonnegative decimal spending floor.\n' >&2; exit 2; }
test -f "$restore_file"
docker compose stop bot
query() {
  docker compose exec -T db psql -U postgres -d roleplay -At -v ON_ERROR_STOP=1 -c "$1"
}
# Preserve any more recent local spending before replacing the database.
# A fresh volume has no tables yet (the bot creates them on first start).
previous_spend=0
chat_spend=''
if [ "$(query "SELECT to_regclass('public.runtime') IS NOT NULL")" = t ]; then
  previous_spend=$(query 'SELECT COALESCE((SELECT spent FROM runtime WHERE id=1), 0)')
fi
if [ "$(query "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='chats' AND column_name='spent')")" = t ]; then
  chat_spend=$(query "SELECT COALESCE(string_agg(id || ':' || spent, ',' ORDER BY id), '') FROM chats")
fi
is_decimal "$previous_spend" || { printf 'Unexpected current spending value; nothing was changed.\n' >&2; exit 1; }
case "$chat_spend" in
  *[!0-9:.,-]*) printf 'Unexpected per-chat spending values; nothing was changed.\n' >&2; exit 1 ;;
esac
# Runs in the same transaction as the schema replacement: any failure rolls back everything.
post_sql=$(cat <<'SQL'
SET search_path TO public;
-- Old archives may predate the ledger. Startup migrations will adopt them.
CREATE TABLE IF NOT EXISTS spending_ledger (
 id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL,
 amount NUMERIC(14,8) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO spending_ledger(kind,amount) SELECT 'legacy_balance',spent FROM runtime WHERE id=1
 AND NOT EXISTS (SELECT 1 FROM spending_ledger);
INSERT INTO spending_ledger(kind,amount)
 SELECT 'restore_floor',GREATEST(spent,:'floor'::numeric,:'previous'::numeric)-spent FROM runtime WHERE id=1;
UPDATE runtime SET spent=GREATEST(spent,:'floor'::numeric,:'previous'::numeric) WHERE id=1;
-- Per-chat allowances keep the larger of archived and pre-restore spend for each chat.
ALTER TABLE chats ADD COLUMN IF NOT EXISTS spent NUMERIC(14,8) NOT NULL DEFAULT 0;
UPDATE chats c SET spent=GREATEST(c.spent, p.spent)
 FROM (SELECT split_part(item,':',1)::bigint AS id, split_part(item,':',2)::numeric AS spent
       FROM unnest(string_to_array(:'chat_spend', ',')) AS item) p WHERE c.id=p.id;
-- Never replay replies or drafts from an older point in time.
DELETE FROM jobs;
DO $$ BEGIN
  IF to_regclass('public.personality_drafts') IS NOT NULL THEN DELETE FROM personality_drafts; END IF;
END $$;
ALTER TABLE runtime ADD COLUMN IF NOT EXISTS drop_pending_on_start BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE runtime SET drop_pending_on_start=TRUE WHERE id=1;
SQL
)
if ! docker compose exec -T db sh -c '
  set -eu
  umask 077
  restore_sql=$(mktemp /tmp/roleplay-restore.XXXXXX)
  trap '\''rm -f "$restore_sql"'\'' EXIT INT TERM
  {
    printf "DROP SCHEMA public CASCADE;\nCREATE SCHEMA public AUTHORIZATION roleplay;\n"
    pg_restore --exit-on-error --no-owner --role=roleplay -f -
    printf "%s\n" "$4"
  } > "$restore_sql" 2>/dev/null
  psql -U postgres -d roleplay --single-transaction -v ON_ERROR_STOP=1 \
    -v floor="$1" -v previous="$2" -v chat_spend="$3" -f "$restore_sql" >/dev/null 2>&1
' restore "$restore_floor" "$previous_spend" "$chat_spend" "$post_sql" < "$restore_file"; then
  printf 'Restore failed; the bot remains stopped. Nothing was changed: the replacement is one transaction.\n' >&2
  exit 1
fi
printf 'Restore complete. Bot remains stopped. Review the spending floor before restarting; startup will discard pending Telegram updates.\n'
