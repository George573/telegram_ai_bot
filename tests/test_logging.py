"""Verify operational logs without touching real credentials or networks."""
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import json
import logging
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from observability import LOGGER, configure_logging, event
from config import Config
from model import Model, ModelFailure
from storage import BudgetExceeded
from test_clients import Allowance


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.output = StringIO()
        self.disabled = logging.root.manager.disable
        self.saved = [(logger, logger.handlers[:], logger.level, logger.propagate)
                      for logger in [logging.getLogger(), LOGGER, logging.getLogger('openai'),
                                     logging.getLogger('httpx'), logging.getLogger('httpcore')]]
        configure_logging(self.output)

    def tearDown(self):
        for logger, handlers, level, propagate in self.saved:
            logger.handlers, logger.propagate = handlers, propagate
            logger.setLevel(level)
        logging.disable(self.disabled)

    def records(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def model(self, create, store=None):
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        return Model(Config('synthetic-telegram-secret', 'synthetic-openai-secret', frozenset({-1})),
                     store or Allowance(), client)

    def test_event_fields_are_allowlisted_and_json_lines(self):
        event('bot_started', provider='openai', model='gpt-6-luna', budget_usd=Decimal('5'),
              api_key='synthetic-secret', payload={'content': 'private-message'},
              exception=RuntimeError('synthetic-secret'))
        event('unknown-secret-event', token='synthetic-secret')
        event('ai_request_failed', provider='synthetic-secret', model='private-message',
              status_code='synthetic-secret', validation_error='synthetic-secret',
              duration_ms=-1, charged_usd=Decimal('NaN'))
        records = self.records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['budget_usd'], '5')
        self.assertIn('time', records[0])
        self.assertEqual(set(records[1]), {'time', 'level', 'event'})
        self.assertNotIn('synthetic-secret', self.output.getvalue())
        self.assertNotIn('private-message', self.output.getvalue())

    def test_sdk_debug_and_arbitrary_tracebacks_never_reach_output(self):
        for name in ['openai', 'openai._base_client', 'httpx', 'httpcore.connection']:
            logging.getLogger(name).error('https://host/bot/synthetic-secret private-message')
        try:
            raise RuntimeError('synthetic-secret')
        except RuntimeError:
            LOGGER.exception('private-message')
        self.assertEqual(self.output.getvalue(), '')
        configure_logging(self.output)
        event('bot_stopped', reason='shutdown')
        self.assertEqual(len(self.records()), 1)

    def test_success_records_usage_timing_and_cost_without_content(self):
        model = self.model(lambda **_: SimpleNamespace(status='completed', output_text='private-answer',
                      usage=SimpleNamespace(input_tokens=100, output_tokens=10)))
        self.assertEqual(model.generate('private-prompt', [{'role': 'user', 'content': 'private-message'}]), 'private-answer')
        records = self.records()
        self.assertEqual([r['event'] for r in records], ['ai_request_started', 'ai_request_completed'])
        completed = records[1]
        self.assertEqual(completed['input_tokens'], 100)
        self.assertEqual(completed['output_tokens'], 10)
        self.assertEqual(Decimal(completed['charged_usd']), Decimal('0.0000175'))
        self.assertGreaterEqual(completed['duration_ms'], 0)
        for text in ['private-prompt', 'private-message', 'private-answer', 'synthetic-telegram-secret', 'synthetic-openai-secret']:
            self.assertNotIn(text, self.output.getvalue())

    def test_http_failure_records_status_without_raw_exception(self):
        class SyntheticHTTPError(Exception): status_code = 401
        def create(**_): raise SyntheticHTTPError('synthetic-secret raw-response')
        with self.assertRaises(ModelFailure): self.model(create).generate('private-message', [])
        failure = self.records()[-1]
        self.assertEqual(failure['event'], 'ai_request_failed')
        self.assertEqual(failure['status_code'], 401)
        self.assertEqual(failure['level'], 'WARNING')
        self.assertNotIn('synthetic-secret', self.output.getvalue())
        self.assertNotIn('raw-response', self.output.getvalue())

    def test_budget_rejection_is_logged_without_calling_provider(self):
        store = Allowance()
        def reserve(*_): raise BudgetExceeded()
        store.reserve = reserve
        model = self.model(lambda **_: self.fail('Budget rejection must not call provider'), store)
        with self.assertRaises(BudgetExceeded): model.generate('private-message', [])
        self.assertEqual([r['event'] for r in self.records()], ['budget_exceeded'])

    def test_invalid_model_json_records_only_failure_category(self):
        model = self.model(lambda **_: SimpleNamespace(status='completed',
                                                      output_text='private-invalid-json', usage=None))
        with self.assertRaises(ModelFailure): model.decide_reply('private-prompt', [])
        self.assertEqual(self.records()[-1]['event'], 'ai_output_invalid')
        self.assertEqual(self.records()[-1]['reason'], 'invalid_json')
        self.assertNotIn('private-invalid-json', self.output.getvalue())
