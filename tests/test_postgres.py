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
        self.store.state(-1)
        self.store.append(-1, 'user', 'Existing memory')
        self.conn.execute('ALTER TABLE chats DROP COLUMN personality')
        self.store = Store(50, self.conn)
        self.assertEqual(self.store.state(-1)['personality'], {})
        self.assertEqual(self.store.messages(-1)[0]['content'], 'Existing memory')

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
