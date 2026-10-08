from decimal import Decimal
from dataclasses import replace
import unittest
from test_service import MemoryStore, FakeTelegram, FakeModel, payload
from config import Config
from service import Service


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
