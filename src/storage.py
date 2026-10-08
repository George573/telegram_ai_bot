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
ALTER TABLE chats ADD COLUMN IF NOT EXISTS images_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS voice_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS web_enabled BOOLEAN NOT NULL DEFAULT TRUE;
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
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS delivery_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE TABLE IF NOT EXISTS personality_drafts (
 chat_id BIGINT NOT NULL REFERENCES chats(id), user_id BIGINT NOT NULL,
 settings JSONB, expires_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version VALUES(1) ON CONFLICT DO NOTHING;
INSERT INTO schema_version VALUES(2) ON CONFLICT DO NOTHING;
INSERT INTO schema_version VALUES(3) ON CONFLICT DO NOTHING;
INSERT INTO schema_version VALUES(4) ON CONFLICT DO NOTHING;
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
        if field not in {'persona', 'mode', 'paused', 'history_limit', 'personality', 'images_enabled', 'voice_enabled', 'web_enabled'}:
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
        return self.conn.execute('''SELECT j.* FROM jobs j
            WHERE j.next_attempt_at <= now() AND NOT EXISTS (
                SELECT 1 FROM jobs older WHERE older.payload->>'chat_id'=j.payload->>'chat_id'
                AND older.update_id < j.update_id)
            ORDER BY j.update_id LIMIT 1''').fetchone()

    def retry_delivery(self, job, seconds):
        # Rejection is known, so scheduling another attempt is safe. Unknown
        # sends retain the separate sending recovery behavior.
        self.conn.execute('''UPDATE jobs SET phase='generated',
            delivery_attempts=delivery_attempts+1,
            next_attempt_at=now()+%s * interval '1 second' WHERE update_id=%s''',
            (max(1, seconds), job['update_id']))

    def phase(self, update_id, phase, answer=None, assistant=False):
        self.conn.execute('UPDATE jobs SET phase=%s,answer=%s,assistant=%s WHERE update_id=%s', (phase,answer,assistant,update_id))

    def converted(self, update_id, payload):
        self.conn.execute("UPDATE jobs SET payload=%s,phase='queued' WHERE update_id=%s", (Jsonb(payload), update_id))

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

    def draft(self, chat_id, user_id):
        self.conn.execute('DELETE FROM personality_drafts WHERE expires_at <= now()')
        return self.conn.execute('SELECT settings FROM personality_drafts WHERE chat_id=%s AND user_id=%s',
                                 (chat_id, user_id)).fetchone()

    def save_draft(self, chat_id, user_id, settings=None):
        self.conn.execute("INSERT INTO personality_drafts(chat_id,user_id,settings,expires_at) "
                          "VALUES(%s,%s,%s,now()+interval '30 minutes') ON CONFLICT(chat_id,user_id) "
                          "DO UPDATE SET settings=EXCLUDED.settings,expires_at=EXCLUDED.expires_at",
                          (chat_id, user_id, Jsonb(settings) if settings is not None else None))

    def delete_drafts(self, chat_id, user_id=None):
        if user_id is None:
            self.conn.execute('DELETE FROM personality_drafts WHERE chat_id=%s', (chat_id,))
        else:
            self.conn.execute('DELETE FROM personality_drafts WHERE chat_id=%s AND user_id=%s', (chat_id, user_id))


class BudgetExceeded(Exception):
    pass
