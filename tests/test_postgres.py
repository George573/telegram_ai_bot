"""Run only against an isolated synthetic-credential test database."""
from pathlib import Path
from decimal import Decimal
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import psycopg
from psycopg.rows import dict_row
from storage import Store, BudgetExceeded

# Explicit opt-in marker supplied only by the isolated test runner, never private env.
ENABLED = Path('/tmp/enable-isolated-postgres-tests').exists()


@unittest.skipUnless(ENABLED, 'Isolated PostgreSQL integration runner required')
class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.conn=psycopg.connect('host=db dbname=roleplay user=roleplay password=test-only-password',autocommit=True,row_factory=dict_row)
        self.store=Store(50,self.conn)
        self.conn.execute('TRUNCATE messages, chats, jobs CASCADE')
        self.conn.execute('UPDATE runtime SET next_update=0,spent=0')
    def tearDown(self): self.conn.close()
    def test_atomic_checkpoint_and_deduplication(self):
        self.store.enqueue(10,dict(chat_id=-1))
        self.store.enqueue(10,dict(chat_id=-1))
        self.assertEqual(self.store.checkpoint(),11)
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM jobs').fetchone()['n'],1)

    def test_lower_update_id_after_week_of_inactivity(self):
        self.store.enqueue(10000, {'chat_id': -1})
        self.conn.execute("UPDATE runtime SET last_update_at=now()-interval '8 days'")
        self.assertEqual(self.store.checkpoint(), 0)
        self.store.enqueue(12, {'chat_id': -2})
        self.assertEqual(self.store.checkpoint(), 13)
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM jobs').fetchone()['n'], 2)

    def test_topics_isolate_messages_summary_and_cooldown(self):
        for topic in (7, 8):
            with self.store.memory_scope(topic):
                self.store.state(-1)
                self.store.append(-1, 'user', f'Topic {topic}')
        with self.store.memory_scope(7):
            row = self.store.messages(-1)[0]
            with self.store.transaction():
                self.store.save_summary(-1, 'Topic seven summary', row['id'])
            self.store.mark_reply(-1)
            self.assertTrue(self.store.cooling_down(-1, 30))
        with self.store.memory_scope(8):
            self.assertEqual(self.store.state(-1)['summary'], '')
            self.assertEqual(self.store.messages(-1)[0]['content'], 'Topic 8')
            self.assertFalse(self.store.cooling_down(-1, 30))
    def test_state_and_queued_work_survive_reconnect(self):
        self.store.state(-1)
        self.store.setting(-1,'persona','Persistent cat')
        self.store.setting(-1,'personality',dict(names=['Mittens'], eagerness='quiet', lore='Royal cat'))
        self.store.append(-1,'user','Remember this')
        self.store.enqueue(7,dict(chat_id=-1))
        self.conn.close()
        self.conn=psycopg.connect('host=db dbname=roleplay user=roleplay password=test-only-password',autocommit=True,row_factory=dict_row)
        self.store=Store(50,self.conn)
        self.assertEqual(self.store.state(-1)['persona'],'Persistent cat')
        self.assertEqual(self.store.state(-1)['personality']['names'], ['Mittens'])
        self.assertEqual(self.store.messages(-1)[0]['content'],'Remember this')
        self.assertEqual(self.store.next_job()['update_id'],7)
    def test_summary_transaction_rolls_back(self):
        self.store.state(-1); self.store.append(-1,'user','original')
        row=self.store.messages(-1)[0]
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.save_summary(-1,'summary',row['id'])
                raise RuntimeError('synthetic failure')
        self.assertEqual(self.store.state(-1)['summary'],'')
        self.assertEqual(self.store.messages(-1)[0]['content'],'original')
    def test_allowance_persists_and_rejects_overspend(self):
        self.store.reserve(Decimal('9'),Decimal('10'))
        with self.assertRaises(BudgetExceeded): self.store.reserve(Decimal('2'),Decimal('10'))
        self.assertEqual(self.store.spent(),Decimal('9'))

    def test_spending_ledger_matches_reservations_and_reconciliation(self):
        before = self.conn.execute('SELECT COALESCE(sum(amount),0) AS total FROM spending_ledger').fetchone()['total']
        self.store.reserve(Decimal('2'), Decimal('10'))
        self.store.reconcile(Decimal('2'), Decimal('0.5'))
        after = self.conn.execute('SELECT sum(amount) AS total FROM spending_ledger').fetchone()['total']
        self.assertEqual(after-before, Decimal('0.5'))

    def test_per_chat_budget_is_atomic_with_global_budget(self):
        self.store.state(-1)
        self.store.state(-2)
        with self.store.spending_scope(-1, Decimal('2')):
            self.store.reserve(Decimal('2'), Decimal('10'))
            with self.assertRaises(BudgetExceeded):
                self.store.reserve(Decimal('1'), Decimal('10'))
        self.assertEqual(self.store.spent(), Decimal('2'))
        with self.store.spending_scope(-2, Decimal('2')):
            self.store.reserve(Decimal('1'), Decimal('10'))
        self.assertEqual(self.store.spent(), Decimal('3'))

    def test_durable_admission_is_idempotent_and_limits_media(self):
        self.store.state(-1)
        for update in range(1, 4):
            self.store.enqueue(update, {'chat_id': -1, 'user_id': 10, 'media': {'kind': 'photo'}})
        job = self.store.next_job()
        self.assertTrue(self.store.admit(job, media_limit=1))
        self.assertTrue(self.store.admit(self.store.next_job(), media_limit=1))
        self.store.finish(1)
        self.assertFalse(self.store.admit(self.store.next_job(), media_limit=1))

    def test_silent_participation_checks_start_cooldown(self):
        self.store.state(-1)
        self.store.mark_check(-1)
        self.assertTrue(self.store.cooling_down(-1, 10))
    def test_application_role_is_not_administrator(self):
        role=self.conn.execute('SELECT rolsuper,rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()
        self.assertFalse(role['rolsuper'])
        self.assertFalse(role['rolcreaterole'])
    def test_second_instance_rejected(self):
        other=psycopg.connect('host=db dbname=roleplay user=roleplay password=test-only-password',autocommit=True,row_factory=dict_row)
        try:
            with self.assertRaises(RuntimeError): Store(50,other)
        finally: other.close()

    def test_personality_migration_preserves_existing_chats(self):
        from storage import MIGRATIONS
        self.conn.execute('CREATE SCHEMA migration_fixture')
        try:
            self.conn.execute('SET search_path TO migration_fixture')
            self.conn.execute(MIGRATIONS[0])
            self.conn.execute('CREATE TABLE schema_version(version INTEGER PRIMARY KEY)')
            self.conn.execute('INSERT INTO schema_version VALUES(1)')
            self.conn.execute("INSERT INTO chats(id,history_limit,summary) VALUES(-1,50,'Legacy summary')")
            self.conn.execute("INSERT INTO messages(chat_id,role,content) VALUES(-1,'user','Existing memory')")
            upgraded = Store(50, self.conn)
            self.assertEqual(upgraded.state(-1)['personality'], {})
            self.assertEqual(upgraded.state(-1)['summary'], 'Legacy summary')
            self.assertEqual(upgraded.messages(-1)[0]['content'], 'Existing memory')
        finally:
            self.conn.execute('SET search_path TO public')
            self.conn.execute('DROP SCHEMA migration_fixture CASCADE')

    def test_expiry_clears_old_summary_and_messages(self):
        self.store.state(-1)
        self.store.append(-1, 'user', 'Old data', sender_id=10, message_id=20)
        self.conn.execute("UPDATE messages SET created_at=now()-interval '31 days'")
        self.conn.execute("UPDATE conversations SET summary='Old summary',summary_started_at=now()-interval '31 days'")
        self.assertEqual(self.store.state(-1)['summary'], '')
        self.assertFalse(self.store.messages(-1))

    def test_forget_user_clears_derived_data_but_preserves_other_senders(self):
        self.store.state(-1)
        self.store.append(-1, 'user', 'Delete me', sender_id=10, message_id=20)
        self.store.append(-1, 'user', 'Keep me', sender_id=11, message_id=21)
        self.store.save_summary(-1, 'Derived data', 0)
        self.store.enqueue(1, {'chat_id': -1, 'user_id': 10})
        self.store.forget_user(-1, 10, 2)
        self.assertEqual([r['content'] for r in self.store.messages(-1)], ['Keep me'])
        self.assertEqual(self.store.state(-1)['summary'], '')
        self.assertIsNone(self.store.next_job())

    def test_drafts_persist_scope_and_expire_without_changing_character(self):
        from personality import DEFAULTS
        self.store.state(-1); self.store.state(-2)
        self.store.save_draft(-1, 10, {**DEFAULTS, 'lore': 'Proposed character'})
        self.assertIsNone(self.store.draft(-1, 20))
        self.assertIsNone(self.store.draft(-2, 10))
        self.conn.close()
        self.conn = psycopg.connect('host=db dbname=roleplay user=roleplay password=test-only-password',
                                   autocommit=True, row_factory=dict_row)
        self.store = Store(50, self.conn)
        self.assertEqual(self.store.draft(-1, 10)['settings']['lore'], 'Proposed character')
        self.assertEqual(self.store.state(-1)['personality'], {})
        self.conn.execute("UPDATE personality_drafts SET expires_at=now()-interval '1 second'")
        self.assertIsNone(self.store.draft(-1, 10))
        self.assertEqual(self.store.state(-1)['personality'], {})

    def test_converted_media_and_feature_settings_survive_restart(self):
        self.store.state(-1)
        self.store.setting(-1, 'web_enabled', False)
        self.store.setting(-1, 'images_enabled', False)
        self.store.enqueue(5, {'chat_id': -1, 'media': {'file_id': 'synthetic-id'}, 'text': ''})
        self.store.converted(5, {'chat_id': -1, 'media': None, 'text': '[Photo description] A sofa'})
        self.conn.close()
        self.conn = psycopg.connect('host=db dbname=roleplay user=roleplay password=test-only-password',
                                   autocommit=True, row_factory=dict_row)
        self.store = Store(50, self.conn)
        self.assertFalse(self.store.state(-1)['web_enabled'])
        self.assertFalse(self.store.state(-1)['images_enabled'])
        self.assertIsNone(self.store.next_job()['payload']['media'])
        self.assertNotIn('file_id', str(self.store.next_job()['payload']))

    def test_newer_schema_is_rejected_without_changing_state(self):
        from storage import MIGRATIONS
        newer = len(MIGRATIONS) + 1
        self.conn.execute('INSERT INTO schema_version VALUES(%s)', (newer,))
        try:
            with self.assertRaises(RuntimeError):
                Store(50, self.conn)
        finally:
            self.conn.execute('DELETE FROM schema_version WHERE version=%s', (newer,))

    def test_failed_migration_does_not_record_version(self):
        from unittest.mock import patch
        from storage import MIGRATIONS, migrate
        version = len(MIGRATIONS) + 1
        with patch('storage.MIGRATIONS', MIGRATIONS + ('SELECT deliberately_missing_function()',)):
            with self.assertRaises(psycopg.Error):
                migrate(self.conn)
        self.assertIsNone(self.conn.execute('SELECT version FROM schema_version WHERE version=%s', (version,)).fetchone())
