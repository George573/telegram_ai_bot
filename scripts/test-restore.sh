#!/bin/sh
# Round-trip backup.sh/restore.sh against a disposable database. Never touches the real project.
set -eu
mkdir -p work
cat > work/compose.restore.yaml <<'YAML'
services:
  db:
    build:
      context: ..
      dockerfile: Dockerfile.db
    environment:
      POSTGRES_DB: roleplay
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: test-admin-password
      BOT_DB_PASSWORD: test-only-password
    tmpfs: [/var/lib/postgresql/data]
    networks: [isolated]
    healthcheck:
      test: [CMD-SHELL, "pg_isready -U postgres -d roleplay"]
      interval: 2s
      timeout: 2s
      retries: 30
  bot:
    # Stand-in so the scripts can stop "bot"; nothing real runs.
    image: postgres:17-bookworm
    command: [sleep, infinity]
    networks: [isolated]
networks:
  isolated:
    internal: true
YAML
COMPOSE_FILE=work/compose.restore.yaml
COMPOSE_PROJECT_NAME=roleplay-restore-test
export COMPOSE_FILE COMPOSE_PROJECT_NAME
cleanup() { docker compose down -v >/dev/null 2>&1 || true; rm -rf work/restore-backups; }
trap cleanup EXIT INT TERM
docker compose up -d --wait db bot >/dev/null
sql() { docker compose exec -T db psql -U postgres -d roleplay -At -v ON_ERROR_STOP=1 -c "$1"; }
check() {
  actual=$(sql "$2")
  if [ "$actual" != "$3" ]; then printf 'FAIL %s: expected %s, got %s\n' "$1" "$3" "$actual" >&2; exit 1; fi
  printf 'ok   %s\n' "$1"
}
# An old-format archive (schema v1: no ledger, no per-chat spend, no personality_drafts).
sql "SET ROLE roleplay;
CREATE TABLE chats (id BIGINT PRIMARY KEY, persona TEXT NOT NULL DEFAULT 'Cat', mode TEXT NOT NULL DEFAULT 'directed',
  paused BOOLEAN NOT NULL DEFAULT FALSE, history_limit INTEGER NOT NULL, summary TEXT NOT NULL DEFAULT '');
CREATE TABLE messages (id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL REFERENCES chats(id), role TEXT NOT NULL, content TEXT NOT NULL);
CREATE TABLE runtime (id INTEGER PRIMARY KEY CHECK(id=1), next_update BIGINT NOT NULL DEFAULT 0, spent NUMERIC(14,8) NOT NULL DEFAULT 0);
CREATE TABLE jobs (update_id BIGINT PRIMARY KEY, payload JSONB NOT NULL, phase TEXT NOT NULL DEFAULT 'queued', answer TEXT,
  assistant BOOLEAN NOT NULL DEFAULT FALSE);
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version VALUES (1);
INSERT INTO runtime(id,spent) VALUES (1,2.0);
INSERT INTO chats(id,history_limit) VALUES (-1,50),(-2,50);
INSERT INTO messages(chat_id,role,content) VALUES (-1,'user','archived');
INSERT INTO jobs(update_id,payload) VALUES (7,'{\"chat_id\":-1}');" >/dev/null
mkdir -p work/restore-backups
(cd work/restore-backups && COMPOSE_FILE=../compose.restore.yaml ../../scripts/backup.sh) >/dev/null
dump=$(ls work/restore-backups/backups/*.dump)
test -z "$(ls work/restore-backups/backups/*.partial 2>/dev/null)"
printf 'ok   backup published without a partial file\n'

# 1. Fresh database (no tables yet): previously aborted before restoring.
sql 'DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION roleplay;' >/dev/null
./scripts/restore.sh "$dump" 1.5 >/dev/null
check 'fresh restore keeps archived spend above floor' 'SELECT spent FROM runtime' '2.00000000'
check 'fresh restore clears jobs' 'SELECT count(*) FROM jobs' '0'
check 'fresh restore discards pending updates on start' 'SELECT drop_pending_on_start FROM runtime' 't'
check 'archived messages restored' 'SELECT content FROM messages' 'archived'

# 2. Newer local spend survives, per chat rather than copied to every chat.
sql 'UPDATE runtime SET spent=5; UPDATE chats SET spent=0.7 WHERE id=-1;' >/dev/null
./scripts/restore.sh "$dump" 1 >/dev/null
check 'global spend keeps newer local value' 'SELECT spent FROM runtime' '5.00000000'
check 'chat keeps its own newer spend' 'SELECT spent FROM chats WHERE id=-1' '0.70000000'
check 'other chat is not raised to global spend' 'SELECT spent FROM chats WHERE id=-2' '0.00000000'

# 3. A failed restore changes nothing.
sql 'UPDATE runtime SET spent=6;' >/dev/null
printf 'not a dump' > work/restore-backups/broken.dump
if ./scripts/restore.sh work/restore-backups/broken.dump 1 >/dev/null 2>&1; then
  printf 'FAIL broken archive was accepted\n' >&2; exit 1
fi
check 'failed restore leaves database unchanged' 'SELECT spent FROM runtime' '6.00000000'
printf 'Restore round-trip tests passed.\n'
