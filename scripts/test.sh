#!/bin/sh
set -eu
mkdir -p work
cat > work/test.env <<'ENV'
# Synthetic environment; no credentials from private .env.
ENV
cat > work/compose.test.yaml <<'YAML'
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
  tests:
    build:
      context: ..
      dockerfile: Dockerfile.test
    depends_on:
      db:
        condition: service_healthy
    networks: [isolated]
    command: [sh, -c, "touch /tmp/enable-isolated-postgres-tests && python -B -m unittest discover -s tests -v"]
networks:
  # No route out: an accidental real network call fails instead of leaving the machine.
  isolated:
    internal: true
YAML
cleanup() {
  docker compose --env-file work/test.env -f work/compose.test.yaml -p roleplay-isolated-test down
}
trap cleanup EXIT INT TERM
docker build -t telegram-roleplay:dev .
docker compose --env-file work/test.env -f work/compose.test.yaml -p roleplay-isolated-test up --build --abort-on-container-exit --exit-code-from tests
# Real Chromium against local HTML under the production hardening of the tools service.
docker build -t telegram-roleplay-tools:dev -f tools/Dockerfile .
docker run --rm --network none --read-only --tmpfs /tmp:size=256m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges:true --shm-size 64m \
  -v "$PWD/tests/browser:/app/browser_tests:ro,Z" telegram-roleplay-tools:dev \
  python -B -m unittest discover -s /app/browser_tests -v
# Backup/restore scripts against a disposable database.
./scripts/test-restore.sh
