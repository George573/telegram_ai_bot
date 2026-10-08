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
