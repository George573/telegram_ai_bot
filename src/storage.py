from contextlib import contextmanager
from decimal import Decimal
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA = '''
CREATE TABLE IF NOT EXISTS chats (
 id BIGINT PRIMARY KEY, persona TEXT NOT NULL DEFAULT 'You are a playful family cat.',
 mode TEXT NOT NULL DEFAULT 'directed', paused BOOLEAN NOT NULL DEFAULT FALSE,
 history_limit INTEGER NOT NULL, summary TEXT NOT NULL DEFAULT '');
ALTER TABLE chats ADD COLUMN IF NOT EXISTS last_reply TIMESTAMPTZ;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS personality JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE TABLE IF NOT EXISTS messages (
 id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL REFERENCES chats(id),
 role TEXT NOT NULL, content TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_chat_idx ON messages(chat_id,id);
CREATE TABLE IF NOT EXISTS runtime (
 id INTEGER PRIMARY KEY CHECK(id=1), next_update BIGINT NOT NULL DEFAULT 0,
 spent NUMERIC(14,8) NOT NULL DEFAULT 0);
INSERT INTO runtime(id) VALUES(1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS jobs (
 update_id BIGINT PRIMARY KEY, payload JSONB NOT NULL,
 phase TEXT NOT NULL DEFAULT 'queued', answer TEXT, assistant BOOLEAN NOT NULL DEFAULT FALSE);
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version VALUES(1) ON CONFLICT DO NOTHING;
INSERT INTO schema_version VALUES(2) ON CONFLICT DO NOTHING;
'''


class Store:
    def __init__(self, default_limit, conn=None):
        self.default_limit = default_limit
        self.conn = conn or psycopg.connect(autocommit=True, row_factory=dict_row)
        if not self.conn.execute('SELECT pg_try_advisory_lock(781235689)').fetchone()['pg_try_advisory_lock']:
            raise RuntimeError('Another bot instance is already using this database')
        with self.conn.transaction():
            self.conn.execute(SCHEMA)

    @contextmanager
    def transaction(self):
        with self.conn.transaction():
            yield

    def state(self, chat_id):
        self.conn.execute('INSERT INTO chats(id,history_limit) VALUES(%s,%s) ON CONFLICT DO NOTHING', (chat_id, self.default_limit))
        return self.conn.execute('SELECT * FROM chats WHERE id=%s', (chat_id,)).fetchone()

    def messages(self, chat_id):
        return self.conn.execute('SELECT id,role,content FROM messages WHERE chat_id=%s ORDER BY id', (chat_id,)).fetchall()

    def append(self, chat_id, role, content):
        self.conn.execute('INSERT INTO messages(chat_id,role,content) VALUES(%s,%s,%s)', (chat_id, role, content))

    def clear(self, chat_id):
        self.conn.execute('DELETE FROM messages WHERE chat_id=%s', (chat_id,))
        self.conn.execute("UPDATE chats SET summary='' WHERE id=%s", (chat_id,))

    def setting(self, chat_id, field, value):
        if field not in {'persona', 'mode', 'paused', 'history_limit', 'personality'}:
            raise ValueError('Invalid setting')
        if field == 'personality':
            value = Jsonb(value)
        self.conn.execute(f'UPDATE chats SET {field}=%s WHERE id=%s', (value,chat_id))

    def save_summary(self, chat_id, summary, through_id):
        self.conn.execute('UPDATE chats SET summary=%s WHERE id=%s', (summary,chat_id))
        self.conn.execute('DELETE FROM messages WHERE chat_id=%s AND id<=%s', (chat_id,through_id))

    def checkpoint(self):
        return self.conn.execute('SELECT next_update FROM runtime WHERE id=1').fetchone()['next_update']

    def enqueue(self, update_id, payload):
        with self.transaction():
            if update_id < self.checkpoint():
                return
            if payload:
                self.conn.execute('INSERT INTO jobs(update_id,payload) VALUES(%s,%s) ON CONFLICT DO NOTHING', (update_id,Jsonb(payload)))
            self.conn.execute('UPDATE runtime SET next_update=%s WHERE id=1', (update_id + 1,))

    def next_job(self):
        return self.conn.execute('SELECT * FROM jobs ORDER BY update_id LIMIT 1').fetchone()

    def phase(self, update_id, phase, answer=None, assistant=False):
        self.conn.execute('UPDATE jobs SET phase=%s,answer=%s,assistant=%s WHERE update_id=%s', (phase,answer,assistant,update_id))

    def finish(self, update_id):
        self.conn.execute('DELETE FROM jobs WHERE update_id=%s', (update_id,))

    def spent(self):
        return self.conn.execute('SELECT spent FROM runtime WHERE id=1').fetchone()['spent']

    def reserve(self, amount, budget):
        row = self.conn.execute('UPDATE runtime SET spent=spent+%s WHERE id=1 AND spent+%s<=%s RETURNING spent', (amount,amount,budget)).fetchone()
        if row is None:
            raise BudgetExceeded()

    def reconcile(self, reserved, actual):
        self.conn.execute('UPDATE runtime SET spent=GREATEST(0,spent-%s+%s) WHERE id=1', (reserved,actual))

    def cooling_down(self, chat_id, seconds):
        return self.conn.execute("SELECT COALESCE(last_reply > now() - %s * interval '1 second', FALSE) AS active FROM chats WHERE id=%s", (seconds,chat_id)).fetchone()['active']

    def mark_reply(self, chat_id):
        self.conn.execute('UPDATE chats SET last_reply=now() WHERE id=%s', (chat_id,))


class BudgetExceeded(Exception):
    pass
