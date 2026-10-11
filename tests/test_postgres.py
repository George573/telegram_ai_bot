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
        self.conn.execute('TRUNCATE messages, chats, jobs, rate_limits, prompts CASCADE')
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

    def test_reducing_history_limit_compacts_every_topic_before_setting(self):
        from test_service import payload, FakeTelegram, FakeModel
        from config import Config
        from service import Service
        for topic in (7, 8):
            with self.store.memory_scope(topic):
                self.store.state(-1)
                for _ in range(15):
                    self.store.append(-1, 'user', 'Synthetic conversation')
        service = Service(Config('synthetic', 'synthetic', frozenset({-1})), self.store, FakeTelegram(admin=True), FakeModel())
        service.accept(1, payload('/historylimit 10'))
        service.process_one()
        self.assertEqual(self.store.state(-1)['history_limit'], 10)
        for topic in (7, 8):
            with self.store.memory_scope(topic):
                self.assertLessEqual(len(self.store.messages(-1)), 10)
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

    def test_concurrent_workers_cannot_overspend_global_budget(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        for chat in (-1, -2):
            self.store.state(chat)
        barrier = Barrier(2)
        def reserve(chat):
            with psycopg.connect('host=db dbname=roleplay user=roleplay password=test-only-password', autocommit=True, row_factory=dict_row) as conn:
                worker = Store(50, conn, worker=True)
                with worker.spending_scope(chat, Decimal('1')):
                    barrier.wait(timeout=2)
                    try:
                        worker.reserve(Decimal('0.7'), Decimal('1'))
                        return True
                    except BudgetExceeded:
                        return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(reserve, [-1, -2]))
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertEqual(self.store.spent(), Decimal('0.7'))

    def test_deferred_delivery_survives_reconnect_and_allows_other_chat(self):
        self.store.enqueue(1, {'chat_id': -1})
        self.store.enqueue(2, {'chat_id': -1})
        self.store.enqueue(3, {'chat_id': -2})
        self.store.phase(1, 'generated', 'Synthetic answer', True)
        self.store.retry_delivery(self.store.next_job(), 300)
        self.conn.close()
        self.conn = psycopg.connect('host=db dbname=roleplay user=roleplay password=test-only-password', autocommit=True, row_factory=dict_row)
        self.store = Store(50, self.conn)
        self.assertEqual(self.store.next_job()['update_id'], 3)
        self.assertIsNone(self.store.next_job(-1))
        row = self.store.job(1)
        self.assertEqual(row['delivery_attempts'], 1)
        remaining = self.conn.execute('SELECT EXTRACT(EPOCH FROM (next_attempt_at-now())) AS seconds FROM jobs WHERE update_id=1').fetchone()['seconds']
        self.assertGreater(remaining, 290)

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

    def test_job_lock_excludes_a_second_connection(self):
        other = psycopg.connect('host=db dbname=roleplay user=roleplay password=test-only-password', autocommit=True, row_factory=dict_row)
        worker = Store(50, other, worker=True)
        try:
            with self.store.job_lock(100) as acquired:
                self.assertTrue(acquired)
                with worker.job_lock(100) as second:
                    self.assertFalse(second)
            with worker.job_lock(100) as acquired:
                self.assertTrue(acquired)
        finally:
            other.close()

    def test_help_and_pause_are_delivered_while_older_ai_job_is_prepared(self):
        from test_service import payload, FakeTelegram, FakeModel
        from config import Config
        from service import Service
        telegram = FakeTelegram(admin=True)
        service = Service(Config('synthetic', 'synthetic', frozenset({-1})), self.store, telegram, FakeModel())
        service.accept(1, payload())
        service.process_one()
        self.assertEqual(self.store.job(1)['phase'], 'prepared')
        service.accept(2, payload('/help'))
        self.assertTrue(telegram.sent)
        service.accept(3, payload('/pause'))
        self.assertTrue(self.store.state(-1)['paused'])
        self.assertIsNone(self.store.job(3))
        service.process_one()
        self.assertIsNone(self.store.job(1))
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

    def test_maintenance_expires_idle_groups_but_excludes_active_workers(self):
        for chat in (-1, -2):
            self.store.state(chat)
            self.store.append(chat, 'user', 'Old data')
        self.conn.execute("UPDATE messages SET created_at=now()-interval '31 days'")
        self.store.maintenance([-1])
        self.assertTrue(self.store.messages(-1))
        self.assertFalse(self.store.messages(-2))
        self.store.maintenance()
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

    def test_forget_user_keeps_bot_replies_but_clears_legacy_user_rows(self):
        self.store.state(-1)
        self.store.append(-1, 'user', 'Legacy unknown sender')
        self.store.append(-1, 'assistant', 'Bot reply')
        self.store.append(-1, 'user', 'Delete me', sender_id=10)
        self.store.forget_user(-1, 10, 2)
        self.assertEqual([r['content'] for r in self.store.messages(-1)], ['Bot reply'])

    def test_throttle_bucket_limits_then_refills(self):
        self.store.state(-1)
        self.assertTrue(self.store.throttle(-1, 10, 'forgetme', 2, 3600))
        self.assertTrue(self.store.throttle(-1, 10, 'forgetme', 2, 3600))
        self.assertFalse(self.store.throttle(-1, 10, 'forgetme', 2, 3600))
        self.assertTrue(self.store.throttle(-1, 11, 'forgetme', 2, 3600))
        self.conn.execute("UPDATE rate_limits SET updated_at=now()-interval '1 hour' WHERE subject_id=10")
        self.assertTrue(self.store.throttle(-1, 10, 'forgetme', 2, 3600))

    def test_incidental_admission_has_separate_group_bucket(self):
        self.store.state(-1)
        for update_id in range(1, 4):
            self.store.enqueue(update_id, {'chat_id': -1, 'user_id': update_id, 'directed': False})
        jobs = [self.store.job(update_id) for update_id in range(1, 4)]
        self.assertTrue(self.store.admit(jobs[0], user_limit=5, chat_limit=1))
        self.assertFalse(self.store.admit(jobs[1], user_limit=5, chat_limit=1))
        self.store.enqueue(4, {'chat_id': -1, 'user_id': 4, 'directed': True})
        self.assertTrue(self.store.admit(self.store.job(4), user_limit=5, chat_limit=1))

    def test_admission_does_not_touch_other_chats_rate_rows(self):
        self.store.state(-1); self.store.state(-2)
        self.conn.execute("INSERT INTO rate_limits(chat_id,subject_id,kind,tokens,updated_at) VALUES(-2,0,'requests',1,now()-interval '2 hours')")
        self.store.enqueue(1, {'chat_id': -1, 'user_id': 10, 'directed': True})
        self.assertTrue(self.store.admit(self.store.job(1)))
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM rate_limits WHERE chat_id=-2').fetchone()['n'], 1)
        self.store.maintenance()
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM rate_limits WHERE chat_id=-2').fetchone()['n'], 0)

    def test_record_failure_counts_and_delays_retry(self):
        self.store.enqueue(1, {'chat_id': -1})
        self.assertEqual(self.store.record_failure(1), 1)
        self.assertIsNone(self.store.next_job(-1))
        self.assertEqual(self.store.record_failure(1), 2)
        self.assertEqual(self.store.record_failure(999), 0)

    def test_next_job_only_expires_its_own_chat(self):
        self.store.enqueue(1, {'chat_id': -1}); self.store.enqueue(2, {'chat_id': -2})
        self.conn.execute("UPDATE jobs SET created_at=now()-interval '2 days'")
        self.assertIsNone(self.store.next_job(-1))
        self.assertIsNotNone(self.store.job(2))

    def test_prompts_are_single_use_scoped_and_expire(self):
        self.store.state(-1)
        self.store.save_prompt(-1, 500, 10, '/historylimit')
        self.store.save_prompt(-1, 501, 10, 'skill', 'city-events')
        self.assertEqual(self.store.prompt(-1, 500), {'user_id': 10, 'command': '/historylimit', 'skill': None})
        self.assertEqual(self.store.prompt(-1, 501)['skill'], 'city-events')
        self.assertIsNone(self.store.prompt(-2, 500))
        self.store.delete_prompt(-1, 500)
        self.assertIsNone(self.store.prompt(-1, 500))
        self.conn.execute("UPDATE prompts SET expires_at=now()-interval '1 minute' WHERE message_id=501")
        self.assertIsNone(self.store.prompt(-1, 501))
        self.store.maintenance()
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM prompts').fetchone()['n'], 0)

    def test_forgetme_removes_the_users_prompts(self):
        self.store.state(-1)
        self.store.save_prompt(-1, 500, 10, '/chat')
        self.store.save_prompt(-1, 501, 11, '/chat')
        self.store.forget_user(-1, 10, 2)
        self.assertIsNone(self.store.prompt(-1, 500))
        self.assertIsNotNone(self.store.prompt(-1, 501))

    def test_job_buttons_prompt_and_status_message_round_trip(self):
        self.store.enqueue(1, {'chat_id': -1})
        markup = [[['✅ Да', 'v1:confirm:yes'], ['✖️ Нет', 'v1:confirm:no']]]
        self.store.phase(1, 'generated', 'Точно?', False, markup=markup, reply_prompt='confirm:/forgetme')
        self.store.set_status_message(1, 777)
        job = self.store.job(1)
        self.assertEqual((job['markup'], job['reply_prompt'], job['status_message_id']),
                         (markup, 'confirm:/forgetme', 777))
        self.store.phase(1, 'sending', 'Точно?', False)
        self.assertIsNone(self.store.job(1)['markup'])

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
