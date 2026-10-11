"""Crash safety, throttling, admin uncertainty, delivery retry and topic scoping. Synthetic only."""
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bot import ChatScheduler, accept_update, MAX_UPDATE_FAILURES
from config import Config
from policy import normalize
from service import Service
from telegram_api import TelegramFailure
from test_service import MemoryStore, FakeTelegram, FakeModel, payload


class Executor:
    def __init__(self, error=None): self.error = error
    def submit(self, work, chat):
        future = Future()
        future.set_exception(self.error) if self.error else future.set_result(None)
        return future


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tg, self.model = MemoryStore(), FakeTelegram(), FakeModel()
        self.config = Config('synthetic', 'synthetic', frozenset({-1}))
        self.service = Service(self.config, self.store, self.tg, self.model)

    def run_all(self):
        for _ in range(8):
            if self.service.process_one() is None:
                break

    def test_worker_exception_is_logged_not_raised_into_poller(self):
        scheduler = ChatScheduler(Executor(RuntimeError('synthetic-db-error')), 2, lambda _: None)
        with patch('bot.event') as log:
            scheduler.tick([-1])
            scheduler.tick([-1])  # Collects the failed future without raising.
        self.assertEqual(log.call_args.args[0], 'worker_failed')
        self.assertNotIn('synthetic', str(log.call_args_list))

    def test_unexpected_job_exception_is_contained_and_job_dropped(self):
        def broken(*_, **__): raise RuntimeError('synthetic-bug')
        self.model.generate = broken
        self.service.accept(1, payload())
        with patch('service.event') as log:
            self.run_all()
        self.assertFalse(self.store.jobs)
        failed = [call for call in log.call_args_list if call.args[0] == 'job_failed']
        self.assertEqual(failed[-1].kwargs['reason'], 'gave_up')

    def test_job_failures_back_off_before_giving_up(self):
        counts = []
        def record(update_id):
            counts.append(update_id)
            return len(counts)
        self.store.record_failure = record
        def broken(*_, **__): raise RuntimeError('synthetic-bug')
        self.model.generate = broken
        self.service.accept(1, payload())
        self.service.process_one()  # queued -> prepared
        self.service.process_one()
        self.assertTrue(self.store.jobs, 'first failure schedules a retry')
        self.service.process_one(); self.service.process_one()
        self.assertFalse(self.store.jobs)
        self.assertEqual(len(counts), 3)

    def test_update_that_keeps_failing_is_skipped_after_retries(self):
        skipped = []
        store = type('Store', (), {'enqueue': lambda self, update_id, payload: skipped.append((update_id, payload))})()
        class Broken:
            def accept(self, *_): raise RuntimeError('synthetic')
        failures = {}
        update = {'update_id': 5, 'message': {}}
        with patch('bot.event'):
            for _ in range(MAX_UPDATE_FAILURES - 1):
                self.assertFalse(accept_update(Broken(), store, self.config, {'id': 1, 'username': 'b'}, update, failures))
            self.assertTrue(accept_update(Broken(), store, self.config, {'id': 1, 'username': 'b'}, update, failures))
        self.assertEqual(skipped, [(5, None)])

    def test_button_presses_are_parsed_as_callbacks(self):
        seen = []
        class Recorder:
            def accept(self, update_id, payload): seen.append(payload)
        update = {'update_id': 6, 'callback_query': {'id': 'cb', 'data': 'v1:help:skills', 'from': {'id': 10, 'is_bot': False},
                  'message': {'message_id': 9, 'chat': {'id': -1, 'type': 'group'}, 'from': {'id': 1, 'is_bot': True}}}}
        self.assertTrue(accept_update(Recorder(), None, self.config, {'id': 1, 'username': 'b'}, update, {}))
        self.assertEqual((seen[0]['command'], seen[0]['argument'], seen[0]['callback_id']), ('/help', 'skills', 'cb'))

    def test_unknown_admin_status_keeps_draft_and_asks_for_retry(self):
        self.store.save_draft(-1, 10, {'names': ['Cat']})
        self.tg.is_admin = lambda _: None
        self.service.accept(1, payload('/context all'))
        self.run_all()
        self.assertIn('Не удалось проверить права администратора', self.tg.sent[-1])
        self.assertIsNotNone(self.store.draft(-1, 10))
        self.assertEqual(self.store.chat['mode'], 'directed')

    def test_free_commands_are_throttled_silently(self):
        allowed = iter([True, False])
        self.store.throttle = lambda *args: next(allowed)
        for update in (1, 2):
            self.service.accept(update, payload('/whoami'))
            self.run_all()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertFalse(self.store.jobs)

    def test_repeated_forgetme_is_limited(self):
        def throttle(chat, user, kind, capacity, period):
            return kind != 'forgetme'
        self.store.throttle = throttle
        forgotten = []
        self.store.forget_user = lambda *args: forgotten.append(args)
        self.service.accept(1, payload('/forgetme'))
        self.run_all()
        self.assertFalse(forgotten)
        self.assertIn('выполнялась недавно', self.tg.sent[-1])

    def test_unsent_delivery_is_retried_but_ambiguous_failure_is_not(self):
        for error, retried in ((TelegramFailure(not_sent=True), True), (TelegramFailure(), False)):
            with self.subTest(retried=retried):
                self.setUp()
                def send(*_): raise error
                self.tg.send = send
                self.service.accept(1, payload())
                for _ in range(3):
                    self.service.process_one()
                self.assertEqual(bool(self.store.jobs), retried)
                if retried:
                    self.assertEqual(self.store.jobs[0]['delivery_attempts'], 1)
                    self.assertGreaterEqual(self.store.jobs[0]['retry_seconds'], 1)

    def test_ignored_chatter_does_not_consume_admission(self):
        admitted = []
        self.store.admit = lambda job, *args: admitted.append(job) or True
        self.service.accept(1, payload())
        self.store.chat['paused'] = True  # Paused after queueing, before preparation.
        self.run_all()
        self.assertFalse(self.store.jobs)
        self.assertFalse(admitted)
        self.assertFalse(self.model.calls)


class TopicTests(unittest.TestCase):
    def update(self, **message):
        return dict(update_id=1, message=dict(message_id=2, text='hi @Cat', chat=dict(id=-1, type='supergroup'),
                                              **{'from': dict(id=10, is_bot=False)}, **message))

    def test_reply_threads_in_ordinary_supergroups_are_not_topics(self):
        result = normalize(self.update(message_thread_id=55), {-1}, dict(id=99, username='Cat'))
        self.assertIsNone(result['thread_id'])

    def test_forum_topics_keep_their_thread(self):
        result = normalize(self.update(message_thread_id=55, is_topic_message=True), {-1}, dict(id=99, username='Cat'))
        self.assertEqual(result['thread_id'], 55)


class ConfigTests(unittest.TestCase):
    def load(self, **env):
        base = {'TELEGRAM_BOT_TOKEN': 'synthetic', 'OPENAI_API_KEY': 'synthetic', 'ALLOWED_CHAT_IDS': '-1'}
        with patch.dict('os.environ', {**base, **env}, clear=True):
            return Config.load()

    def test_context_limits_and_tools_url_are_configurable_and_validated(self):
        config = self.load(MAX_INPUT_CHARS='60000', MAX_TOOL_CHARS='20000', TOOLS_URL='http://tools:8080')
        self.assertEqual((config.max_input_chars, config.max_tool_chars, config.tools_url), (60000, 20000, 'http://tools:8080'))
        for env in ({'MAX_INPUT_CHARS': '10'}, {'MAX_TOOL_CHARS': '1'},
                    {'TOOLS_URL': 'https://evil.example/'}, {'TOOLS_URL': 'http://user:pw@tools'}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                self.load(**env)


if __name__ == '__main__':
    unittest.main()
