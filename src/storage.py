from contextlib import contextmanager
from decimal import Decimal
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# Versions 1–4 adopt the legacy additive schema. New versions run exactly
# once, in order, in the same transaction as their version record.
MIGRATIONS = (
    """
CREATE TABLE IF NOT EXISTS chats (
 id BIGINT PRIMARY KEY, persona TEXT NOT NULL DEFAULT 'You are a playful family cat.',
 mode TEXT NOT NULL DEFAULT 'directed', paused BOOLEAN NOT NULL DEFAULT FALSE,
 history_limit INTEGER NOT NULL, summary TEXT NOT NULL DEFAULT '');
ALTER TABLE chats ADD COLUMN IF NOT EXISTS last_reply TIMESTAMPTZ;
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
""",
    """
ALTER TABLE chats ADD COLUMN IF NOT EXISTS personality JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE TABLE IF NOT EXISTS personality_drafts (
 chat_id BIGINT NOT NULL REFERENCES chats(id), user_id BIGINT NOT NULL,
 settings JSONB, expires_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY(chat_id,user_id));
""",
    """
ALTER TABLE chats ADD COLUMN IF NOT EXISTS images_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS voice_enabled BOOLEAN NOT NULL DEFAULT TRUE;
""",
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS web_enabled BOOLEAN NOT NULL DEFAULT FALSE;",
    """
ALTER TABLE chats ALTER COLUMN web_enabled SET DEFAULT FALSE;
ALTER TABLE runtime ADD COLUMN IF NOT EXISTS last_update_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS delivery_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now();
""",
    """
CREATE TABLE conversations (
 chat_id BIGINT NOT NULL REFERENCES chats(id), thread_id BIGINT NOT NULL DEFAULT 0,
 summary TEXT NOT NULL DEFAULT '', last_reply TIMESTAMPTZ,
 PRIMARY KEY(chat_id,thread_id));
INSERT INTO conversations(chat_id,summary,last_reply) SELECT id,summary,last_reply FROM chats;
UPDATE chats SET summary='',last_reply=NULL;
ALTER TABLE messages ADD COLUMN thread_id BIGINT NOT NULL DEFAULT 0;
CREATE INDEX messages_topic_idx ON messages(chat_id,thread_id,id);
""",
    """
ALTER TABLE messages ADD COLUMN sender_id BIGINT;
ALTER TABLE messages ADD COLUMN telegram_message_id BIGINT;
ALTER TABLE messages ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE conversations ADD COLUMN summary_started_at TIMESTAMPTZ;
UPDATE conversations SET summary_started_at=now() WHERE summary<>'';
CREATE INDEX messages_sender_idx ON messages(chat_id,sender_id);
""",
    """
CREATE TABLE IF NOT EXISTS spending_ledger (
 id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL,
 amount NUMERIC(14,8) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO spending_ledger(kind,amount) SELECT 'legacy_balance',spent FROM runtime WHERE id=1
 AND NOT EXISTS (SELECT 1 FROM spending_ledger);
ALTER TABLE runtime ADD COLUMN IF NOT EXISTS drop_pending_on_start BOOLEAN NOT NULL DEFAULT FALSE;
""",
    """
ALTER TABLE chats ADD COLUMN spent NUMERIC(14,8) NOT NULL DEFAULT 0;
ALTER TABLE conversations ADD COLUMN last_check TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN admitted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE spending_ledger ADD COLUMN chat_id BIGINT;
CREATE TABLE rate_limits (
 chat_id BIGINT NOT NULL, subject_id BIGINT NOT NULL, kind TEXT NOT NULL,
 tokens NUMERIC NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(chat_id,subject_id,kind));
""",
)


def migrate(conn):
    with conn.transaction():
        conn.execute('CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)')
        versions = [row['version'] for row in conn.execute('SELECT version FROM schema_version ORDER BY version').fetchall()]
        if versions and (versions != list(range(1, max(versions) + 1)) or max(versions) > len(MIGRATIONS)):
            raise RuntimeError('Unsupported database schema version')
        current = max(versions, default=0)
        for version, sql in enumerate(MIGRATIONS, 1):
            if version > current:
                conn.execute(sql)
                conn.execute('INSERT INTO schema_version(version) VALUES(%s)', (version,))



class Store:
    def __init__(self, default_limit, conn=None, retention_days=30):
        self.default_limit = default_limit
        self.retention_days = retention_days
        self.thread_id = 0
        self.budget_chat = None
        self.chat_budget = None
        self.conn = conn or psycopg.connect(autocommit=True, row_factory=dict_row)
        if not self.conn.execute('SELECT pg_try_advisory_lock(781235689)').fetchone()['pg_try_advisory_lock']:
            raise RuntimeError('Another bot instance is already using this database')
        migrate(self.conn)

    @contextmanager
    def transaction(self):
        with self.conn.transaction():
            yield

    @contextmanager
    def memory_scope(self, thread_id):
        previous = self.thread_id
        self.thread_id = thread_id or 0
        try:
            yield
        finally:
            self.thread_id = previous

    @contextmanager
    def spending_scope(self, chat_id, budget):
        previous = self.budget_chat, self.chat_budget
        self.budget_chat, self.chat_budget = chat_id, budget
        try:
            yield
        finally:
            self.budget_chat, self.chat_budget = previous

    def admit(self, job, user_limit=6, chat_limit=30, media_limit=3):
        if job.get('admitted'):
            return True
        payload = job['payload']
        rules = [(0, 'requests', chat_limit), (payload.get('user_id') or -1, 'requests', user_limit)]
        if payload.get('media'):
            rules += [(0, 'media', media_limit * 4), (payload.get('user_id') or -1, 'media', media_limit)]
        try:
            with self.transaction():
                self.conn.execute("DELETE FROM rate_limits WHERE updated_at < now()-interval '1 hour'")
                for subject, kind, capacity in rules:
                    self.conn.execute('''INSERT INTO rate_limits(chat_id,subject_id,kind,tokens)
                        VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''', (payload['chat_id'],subject,kind,capacity))
                    allowed = self.conn.execute('''UPDATE rate_limits SET
                        tokens=LEAST(%s,tokens+EXTRACT(EPOCH FROM (now()-updated_at))*%s/60)-1,updated_at=now()
                        WHERE chat_id=%s AND subject_id=%s AND kind=%s
                        AND LEAST(%s,tokens+EXTRACT(EPOCH FROM (now()-updated_at))*%s/60)>=1
                        RETURNING tokens''', (capacity,capacity,payload['chat_id'],subject,kind,capacity,capacity)).fetchone()
                    if allowed is None:
                        raise RateLimited()
                self.conn.execute('UPDATE jobs SET admitted=TRUE WHERE update_id=%s', (job['update_id'],))
            return True
        except RateLimited:
            return False

    def mark_check(self, chat_id):
        self.conn.execute('UPDATE conversations SET last_check=now() WHERE chat_id=%s AND thread_id=%s', (chat_id,self.thread_id))

    def state(self, chat_id):
        self.conn.execute('INSERT INTO chats(id,history_limit) VALUES(%s,%s) ON CONFLICT DO NOTHING', (chat_id, self.default_limit))
        self.conn.execute('INSERT INTO conversations(chat_id,thread_id) VALUES(%s,%s) ON CONFLICT DO NOTHING', (chat_id,self.thread_id))
        self.expire_memory(chat_id)
        return self.conn.execute('''SELECT c.*,t.summary,t.last_reply FROM chats c JOIN conversations t ON t.chat_id=c.id
            WHERE c.id=%s AND t.thread_id=%s''', (chat_id,self.thread_id)).fetchone()

    def messages(self, chat_id):
        return self.conn.execute('SELECT id,role,content FROM messages WHERE chat_id=%s AND thread_id=%s ORDER BY id', (chat_id,self.thread_id)).fetchall()

    def append(self, chat_id, role, content, sender_id=None, message_id=None):
        self.conn.execute('''INSERT INTO messages(chat_id,thread_id,role,content,sender_id,telegram_message_id)
            VALUES(%s,%s,%s,%s,%s,%s)''', (chat_id,self.thread_id,role,content,sender_id,message_id))

    def expire_memory(self, chat_id):
        if self.retention_days:
            with self.transaction():
                self.conn.execute("DELETE FROM messages WHERE chat_id=%s AND created_at < now()-%s * interval '1 day'", (chat_id,self.retention_days))
                self.conn.execute("UPDATE conversations SET summary='',summary_started_at=NULL WHERE chat_id=%s AND summary_started_at < now()-%s * interval '1 day'", (chat_id,self.retention_days))

    def forget_user(self, chat_id, user_id, update_id):
        with self.transaction():
            self.conn.execute('DELETE FROM messages WHERE chat_id=%s AND sender_id=%s', (chat_id,user_id))
            # Legacy history has no trustworthy sender metadata. Clear it too;
            # summaries cannot safely subtract an individual speaker.
            self.conn.execute('DELETE FROM messages WHERE chat_id=%s AND sender_id IS NULL', (chat_id,))
            self.conn.execute("UPDATE conversations SET summary='',summary_started_at=NULL WHERE chat_id=%s", (chat_id,))
            self.delete_drafts(chat_id, user_id)
            self.conn.execute("DELETE FROM jobs WHERE payload->>'chat_id'=%s AND update_id<>%s AND (payload->>'user_id'=%s OR phase<>'queued')", (str(chat_id),update_id,str(user_id)))

    def clear(self, chat_id):
        self.conn.execute('DELETE FROM messages WHERE chat_id=%s', (chat_id,))
        self.conn.execute("UPDATE chats SET summary='' WHERE id=%s", (chat_id,))
        self.conn.execute("UPDATE conversations SET summary='',summary_started_at=NULL WHERE chat_id=%s", (chat_id,))

    def setting(self, chat_id, field, value):
        if field not in {'persona', 'mode', 'paused', 'history_limit', 'personality', 'images_enabled', 'voice_enabled', 'web_enabled'}:
            raise ValueError('Invalid setting')
        if field == 'personality':
            value = Jsonb(value)
        self.conn.execute(f'UPDATE chats SET {field}=%s WHERE id=%s', (value,chat_id))

    def save_summary(self, chat_id, summary, through_id):
        self.conn.execute('''UPDATE conversations SET summary=%s,
            summary_started_at=COALESCE(summary_started_at,
                (SELECT min(created_at) FROM messages WHERE chat_id=%s AND thread_id=%s AND id<=%s),now())
            WHERE chat_id=%s AND thread_id=%s''', (summary,chat_id,self.thread_id,through_id,chat_id,self.thread_id))
        self.conn.execute('DELETE FROM messages WHERE chat_id=%s AND thread_id=%s AND id<=%s', (chat_id,self.thread_id,through_id))

    def checkpoint(self):
        # After a week without updates Telegram can choose a lower random ID.
        # Its remote queue expires after 24 hours, so the old cursor is no
        # longer useful. Keep durable local jobs; reset only the polling cursor.
        self.conn.execute("UPDATE runtime SET next_update=0 WHERE id=1 AND last_update_at < now()-interval '7 days'")
        return self.conn.execute('SELECT next_update FROM runtime WHERE id=1').fetchone()['next_update']

    def discard_backlog_required(self):
        return self.conn.execute('SELECT drop_pending_on_start FROM runtime WHERE id=1').fetchone()['drop_pending_on_start']

    def backlog_discarded(self):
        self.conn.execute('UPDATE runtime SET drop_pending_on_start=FALSE,next_update=0,last_update_at=now() WHERE id=1')

    def enqueue(self, update_id, payload):
        with self.transaction():
            if update_id < self.checkpoint():
                return
            if payload:
                self.conn.execute('INSERT INTO jobs(update_id,payload) VALUES(%s,%s) ON CONFLICT DO NOTHING', (update_id,Jsonb(payload)))
            self.conn.execute('UPDATE runtime SET next_update=%s,last_update_at=now() WHERE id=1', (update_id + 1,))

    def next_job(self):
        self.conn.execute("DELETE FROM jobs WHERE created_at < now()-interval '24 hours'")
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
        with self.transaction():
            row = self.conn.execute('UPDATE runtime SET spent=spent+%s WHERE id=1 AND spent+%s<=%s RETURNING spent', (amount,amount,budget)).fetchone()
            if row is None:
                raise BudgetExceeded()
            if self.budget_chat is not None:
                chat = self.conn.execute('UPDATE chats SET spent=spent+%s WHERE id=%s AND spent+%s<=%s RETURNING spent', (amount,self.budget_chat,amount,self.chat_budget)).fetchone()
                if chat is None:
                    raise BudgetExceeded()
            self.conn.execute("INSERT INTO spending_ledger(kind,amount,chat_id) VALUES('reservation',%s,%s)", (amount,self.budget_chat))

    def reconcile(self, reserved, actual):
        with self.transaction():
            self.conn.execute('UPDATE runtime SET spent=spent-%s+%s WHERE id=1', (reserved,actual))
            if self.budget_chat is not None:
                self.conn.execute('UPDATE chats SET spent=spent-%s+%s WHERE id=%s', (reserved,actual,self.budget_chat))
            self.conn.execute("INSERT INTO spending_ledger(kind,amount,chat_id) VALUES('reconciliation',%s,%s)", (actual-reserved,self.budget_chat))

    def cooling_down(self, chat_id, seconds):
        return self.conn.execute("SELECT COALESCE(GREATEST(last_reply,last_check) > now() - %s * interval '1 second', FALSE) AS active FROM conversations WHERE chat_id=%s AND thread_id=%s", (seconds,chat_id,self.thread_id)).fetchone()['active']

    def mark_reply(self, chat_id):
        self.conn.execute('UPDATE conversations SET last_reply=now() WHERE chat_id=%s AND thread_id=%s', (chat_id,self.thread_id))

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


class RateLimited(Exception):
    pass
