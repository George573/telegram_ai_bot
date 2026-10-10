from decimal import Decimal
from dataclasses import replace
import unittest
from test_service import MemoryStore, FakeTelegram, FakeModel, payload
from config import Config
from service import Service
from telegram_api import TelegramFailure


class ReviewFixTests(unittest.TestCase):
    def setup_service(self, **config):
        self.store, self.telegram, self.model = MemoryStore(), FakeTelegram(), FakeModel()
        self.config = replace(Config('synthetic', 'synthetic', frozenset({-1})), **config)
        return Service(self.config, self.store, self.telegram, self.model)

    def test_revoked_chat_jobs_are_cancelled_in_every_phase(self):
        for phase in ('queued', 'prepared', 'generated', 'sending'):
            with self.subTest(phase=phase):
                service = self.setup_service(chats=frozenset({-2}))
                self.store.enqueue(1, payload())
                self.store.phase(1, phase, 'Synthetic answer', True)
                service.process_one()
                self.assertFalse(self.store.jobs)
                self.assertFalse(self.model.calls)
                self.assertFalse(self.telegram.sent)

    def test_accept_checks_allowlist_even_without_normalization(self):
        service = self.setup_service(chats=frozenset({-2}))
        service.accept(1, payload())
        self.assertFalse(self.store.jobs)

    def test_delivery_honors_long_wait_and_bounds_retries(self):
        service = self.setup_service()
        self.store.enqueue(1, payload())
        self.store.phase(1, 'generated', 'Synthetic answer', True)
        def limited(*_):
            raise TelegramFailure(429, 300)
        self.telegram.send = limited
        self.assertEqual(service.process_one(), 0)
        self.assertEqual(self.store.jobs[0]['retry_seconds'], 300)
        for _ in range(5):
            service.process_one()
        self.assertFalse(self.store.jobs)

    def test_search_receives_only_explicit_query(self):
        service = self.setup_service()
        self.store.chat['web_enabled'] = True
        self.store.chat['summary'] = 'Private summary'
        self.store.append(-1, 'user', 'Private history')
        calls = []
        def generate(instructions, history, **options):
            calls.append((instructions, history, options))
            return 'Public answer'
        self.model.generate = generate
        service.accept(1, payload('/search weather in Paris'))
        service.process_one()
        self.assertEqual(calls[0][1], [{'role': 'user', 'content': 'weather in Paris'}])
        self.assertTrue(calls[0][2]['allow_search'])
        self.assertTrue(calls[0][2]['require_search'])
        self.assertNotIn('Private', str(calls))

    def test_normal_reply_enables_search_and_retains_answer_for_followups(self):
        service = self.setup_service()
        self.store.chat['web_enabled'] = True
        calls = []
        self.model.generate = lambda instructions, history, **kw: calls.append((instructions, kw)) or 'Answer https://example.com/fact'
        service.accept(1, payload())
        service.process_one()
        service.process_one()
        self.assertTrue(calls[-1][1]['allow_search'])
        self.assertIn('search is enabled', calls[-1][0])
        self.assertIn('Do not ask the user to send /search', calls[-1][0])
        service.process_one()
        self.assertEqual(self.store.rows[-1]['role'], 'assistant')
        self.assertIn('https://example.com/fact', self.store.rows[-1]['content'])

    def test_incidental_decision_has_no_tools_and_silence_costs_no_research(self):
        service = self.setup_service()
        self.store.chat.update(web_enabled=True, mode='all')
        calls, generated = [], []
        self.model.decide_reply = lambda instructions, history, **kw: calls.append(kw) or None
        self.model.generate = lambda instructions, history, **kw: generated.append(kw) or 'Summary'
        service.accept(1, payload('hello'))
        service.process_one()
        service.process_one()
        self.assertFalse(calls[-1]['allow_search'])
        self.assertFalse([kw for kw in generated if kw.get('allow_search')])
        self.assertFalse(self.store.jobs)

    def test_incidental_reply_researches_only_after_deciding_to_join(self):
        service = self.setup_service()
        self.store.chat.update(web_enabled=True, mode='all')
        decided, generated = [], []
        self.model.decide_reply = lambda instructions, history, **kw: decided.append(kw) or 'Draft'
        self.model.generate = lambda instructions, history, **kw: generated.append((instructions, kw)) or 'Researched'
        service.accept(1, payload('anyone know a cafe nearby?'))
        service.process_one()
        service.process_one()
        self.assertFalse(decided[-1]['allow_search'])
        self.assertTrue(generated[-1][1]['allow_search'])
        self.assertIn('decided to join', generated[-1][0])
        self.assertEqual(self.store.jobs[0]['answer'], 'Researched')

    def test_normal_reply_explains_disabled_search(self):
        service = self.setup_service()
        calls = []
        self.model.generate = lambda instructions, history, **kw: calls.append((instructions, kw)) or 'Answer'
        service.accept(1, payload())
        service.process_one()
        service.process_one()
        self.assertFalse(calls[-1][1]['allow_search'])
        self.assertIn('search is disabled', calls[-1][0])
        self.assertIn('/web on', calls[-1][0])
        self.assertIn('Never claim to have searched', calls[-1][0])

    def test_status_shows_automatic_search(self):
        service = self.setup_service()
        self.store.chat['web_enabled'] = True
        self.assertIn('web: True (automatic in conversation)', service.status(-1))

    def test_pause_takes_effect_before_previous_reply_finishes(self):
        service = self.setup_service()
        self.telegram.admin = True
        service.accept(1, payload())
        service.process_one()
        def generating(*args, **kwargs):
            service.accept(2, payload('/pause'))
            return 'Late reply'
        self.model.generate = generating
        service.process_one()
        self.assertTrue(self.store.chat['paused'])
        self.assertNotIn('Late reply', [job['answer'] for job in self.store.jobs])
        self.assertFalse(self.telegram.sent)
