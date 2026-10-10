"""Job correlation, per-reply summaries and fallback events. Synthetic data only."""
from decimal import Decimal
from io import StringIO
import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from config import Config
from model import ModelFailure
from observability import LOGGER, configure_logging, event, job_context
from providers import OpenAIProvider, ZaiProvider
from service import Service
from storage import BudgetExceeded
from test_service import MemoryStore, FakeTelegram, FakeModel, payload


class PaidModel(FakeModel):
    """Emits the same request events a real Model would."""
    def __init__(self, failure=None):
        super().__init__()
        self.failure = failure

    def generate(self, instructions, history, **kwargs):
        event('ai_request_started', provider='zai', model='glm-4.7', operation='text', reserved_usd=Decimal('0.02'))
        if self.failure:
            event('ai_request_failed', provider='zai', model='glm-4.7', operation='text', reason='request_failed')
            raise self.failure
        event('ai_request_completed', provider='zai', model='glm-4.7', operation='text', input_tokens=100,
              output_tokens=10, reasoning_tokens=40, charged_usd=Decimal('0.001'))
        event('web_search_completed', provider='zai', model='glm-4.7', result_count=3, charged_usd=Decimal('0.01'))
        return 'private-answer'


class JobLoggingTests(unittest.TestCase):
    def setUp(self):
        self.output = StringIO()
        self.saved = LOGGER.handlers[:], LOGGER.level
        configure_logging(self.output)

    def tearDown(self):
        LOGGER.handlers, level = self.saved
        LOGGER.setLevel(level)

    def records(self, name=None):
        records = [json.loads(line) for line in self.output.getvalue().splitlines()]
        return [r for r in records if name is None or r['event'] == name]

    def run_job(self, model, text='hello @Cat', **chat):
        store, telegram = MemoryStore(), FakeTelegram()
        store.chat.update(chat)
        service = Service(Config('synthetic', 'synthetic', frozenset({-1})), store, telegram, model)
        service.accept(7, payload(text))
        for _ in range(4):
            if service.process_one() is None:
                break
        return telegram

    def test_events_carry_the_job_and_summary_totals_the_reply(self):
        telegram = self.run_job(PaidModel())
        self.assertEqual(telegram.sent, ['private-answer'])
        for record in self.records('ai_request_completed') + self.records('web_search_completed'):
            self.assertEqual(record['update_id'], 7)
        summary, = self.records('reply_finished')
        self.assertEqual(summary['outcome'], 'delivered')
        self.assertEqual((summary['model_requests'], summary['searches'], summary['reasoning_tokens']), (1, 1, 40))
        self.assertEqual(summary['spent_usd'], '0.011')
        self.assertTrue(summary['directed'])
        self.assertNotIn('private-answer', self.output.getvalue())

    def test_failed_reply_logs_fallback_and_counts_kept_reservation(self):
        for failure, reason in ((ModelFailure(), 'model_failure'), (BudgetExceeded(), 'budget')):
            with self.subTest(reason=reason):
                self.output.seek(0); self.output.truncate()
                self.run_job(PaidModel(failure))
                fallback, = self.records('reply_fallback')
                self.assertEqual((fallback['fallback'], fallback['update_id'], fallback['level']), (reason, 7, 'WARNING'))
                summary, = self.records('reply_finished')
                self.assertEqual((summary['outcome'], summary['fallback']), ('fallback_delivered', reason))
                self.assertEqual((summary['failed_requests'], summary['spent_usd']), (1, '0.02'))

    def test_dropped_turns_report_why(self):
        cases = [({'paused': True}, 'hello @Cat', None), ({'mode': 'all'}, 'just chatting', 'silent')]
        for chat, text, outcome in cases:
            with self.subTest(outcome=outcome):
                self.output.seek(0); self.output.truncate()
                self.run_job(PaidModel(), text, **chat)
                summaries = self.records('reply_finished')
                if outcome is None:
                    self.assertEqual(summaries, [])  # Paused turns are filtered before queueing.
                else:
                    self.assertEqual(summaries[0]['outcome'], outcome)
                    self.assertFalse(summaries[0]['directed'])

    def test_skill_audit_events_join_the_job_and_count(self):
        from observability import skill_event, job_finished
        with job_context(11):
            skill_event('returned', request_id='0' * 32, provider='zai', model='glm-4.7', name='city-events', content='x')
        job_finished(11, 'delivered')
        returned, = self.records('skill_returned')
        self.assertEqual(returned['update_id'], 11)
        self.assertEqual(self.records('reply_finished')[0]['skills_loaded'], 1)

    def test_job_context_is_per_thread(self):
        seen = []
        with job_context(9):
            event('typing_failed', level=logging.WARNING)
            worker = threading.Thread(target=lambda: event('typing_failed', level=logging.WARNING))
            worker.start(); worker.join()
        event('typing_failed', level=logging.WARNING)
        self.assertEqual([r.get('update_id') for r in self.records('typing_failed')], [9, None, None])


class ReasoningTokenTests(unittest.TestCase):
    def test_both_providers_report_reasoning_tokens(self):
        config = Config('synthetic', 'synthetic', frozenset({-1}), provider='zai', zai_key='synthetic', model='glm-5.3-flash')
        zai_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: SimpleNamespace(
            choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='Hi'))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=900,
                                  completion_tokens_details=SimpleNamespace(reasoning_tokens=850))))))
        result = ZaiProvider(config, zai_client).generate(model='glm-5.3-flash', instructions='x', history=[], output_tokens=10)
        self.assertEqual(result.reasoning_tokens, 850)
        openai_client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: SimpleNamespace(
            output_text='Hi', status='completed', output=[], incomplete_details=None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=20, output_tokens_details={'reasoning_tokens': 5}))))
        result = OpenAIProvider(config, openai_client).generate(model='gpt-6-luna', instructions='x', history=[], output_tokens=10)
        self.assertEqual(result.reasoning_tokens, 5)
        missing = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: SimpleNamespace(
            output_text='Hi', status='completed', output=[], incomplete_details=None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=20))))
        self.assertIsNone(OpenAIProvider(config, missing).generate(model='gpt-6-luna', instructions='x', history=[],
                                                                   output_tokens=10).reasoning_tokens)


class ToolsLogTests(unittest.TestCase):
    def test_tools_service_logs_match_bot_shape(self):
        try:
            import server
        except ImportError as error:
            self.skipTest(f'tools server import unavailable: {error}')
        output = StringIO()
        with contextlib.redirect_stdout(output):
            server.log('afisha_event_read', 'WARNING', status='unavailable', reason='busy', url=None)
        record = json.loads(output.getvalue())
        self.assertEqual({k: record[k] for k in ('level', 'service', 'event', 'reason')},
                         {'level': 'WARNING', 'service': 'tools', 'event': 'afisha_event_read', 'reason': 'busy'})
        self.assertIn('time', record)
        self.assertNotIn('url', record)


if __name__ == '__main__':
    unittest.main()
