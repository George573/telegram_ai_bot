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
    command: [sh, -c, "touch /tmp/enable-isolated-postgres-tests && python -B -m unittest discover -s tests -v"]
YAML
cleanup() {
  docker compose --env-file work/test.env -f work/compose.test.yaml -p roleplay-isolated-test down
}
trap cleanup EXIT INT TERM
docker build -t telegram-roleplay:dev .
docker compose --env-file work/test.env -f work/compose.test.yaml -p roleplay-isolated-test up --build --abort-on-container-exit --exit-code-from tests
