"""Search and typing use mocked transport; no live API calls."""
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
import json
import sys
import unittest
import httpx
from openai import OpenAI
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from config import Config
from model import Model, ModelFailure
from telegram_api import TelegramAPI
from service import Service
from storage import BudgetExceeded
from test_clients import Allowance
from test_service import MemoryStore, FakeTelegram, FakeModel, payload


class SearchTests(unittest.TestCase):
    def model(self, handler, store=None):
        http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(http.close)
        client = OpenAI(api_key='synthetic', base_url='https://api.z.ai/api/paas/v4/',
                        http_client=http, max_retries=0)
        config = Config('synthetic', '', frozenset({-1}), provider='zai',
                        model='glm-4.7', zai_key='synthetic', api_min_interval_seconds=0)
        return Model(config, store or Allowance(), client)

    def response(self, results=None, text='Hello'):
        return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion',
            'model': 'glm-4.7', 'created': 1, 'choices': [{'index': 0, 'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': text}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110},
            'web_search': results or []})

    def test_native_search_request_citations_and_conservative_tool_charge(self):
        def handler(request):
            body = json.loads(request.content)
            tool = body['tools'][0]
            self.assertEqual(tool['type'], 'web_search')
            self.assertEqual(tool['web_search']['search_engine'], 'search-prime')
            self.assertTrue(tool['web_search']['enable'])
            self.assertEqual(tool['web_search']['count'], 3)
            return self.response([{'title': 'Fact', 'link': 'https://example.com/fact'},
                                  {'title': 'Unsafe', 'link': 'http://127.0.0.1/private'},
                                  {'title': 'Credential', 'link': 'https://secret@example.com'},
                                  {'title': 'Duplicate', 'link': 'https://example.com/fact'}])
        model = self.model(handler)
        answer = model.generate('Cat', [], allow_search=True)
        self.assertIn('https://example.com/fact', answer)
        self.assertNotIn('127.0.0.1', answer)
        self.assertNotIn('secret@', answer)
        self.assertEqual(answer.count('https://example.com/fact'), 1)
        self.assertEqual(model.store.total, Decimal('0.010097'))

    def test_disabled_search_sends_no_tool_or_fee(self):
        def handler(request):
            self.assertNotIn('tools', json.loads(request.content))
            return self.response()
        model = self.model(handler)
        self.assertEqual(model.generate('Cat', [], allow_search=False), 'Hello')
        self.assertEqual(model.store.total, Decimal('0.000097'))

    def test_enabled_search_without_results_keeps_one_tool_fee_estimate(self):
        model = self.model(lambda _: self.response())
        self.assertEqual(model.generate('Cat', [], allow_search=True), 'Hello')
        self.assertEqual(model.store.total, Decimal('0.010097'))

    def test_search_budget_rejection_never_calls_network(self):
        store = Allowance()
        store.reserve = lambda *_: (_ for _ in ()).throw(BudgetExceeded())
        model = self.model(lambda _: self.fail('No request allowed'), store)
        with self.assertRaises(BudgetExceeded): model.generate('Cat', [], allow_search=True)

    def test_search_works_with_json_decisions_and_safe_sources(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            self.assertEqual(body['tools'][0]['type'], 'web_search')
            return self.response([{'title': 'Fact', 'link': 'https://example.com/fact'}],
                                 '{"respond":true,"reply":"Fact"}')
        self.assertIn('https://example.com/fact', self.model(handler).decide_reply('Cat', [], allow_search=True))


class TypingTests(unittest.TestCase):
    def telegram(self, handler):
        api = TelegramAPI('synthetic-secret')
        api._http.close()
        api._http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(api._http.close)
        return api

    def test_typing_request_uses_topic_and_contains_no_reply_content(self):
        def handler(request):
            self.assertTrue(request.url.path.endswith('/sendChatAction'))
            self.assertEqual(json.loads(request.content), {'chat_id': -1, 'action': 'typing', 'message_thread_id': 7})
            return httpx.Response(200, json={'ok': True, 'result': True})
        self.assertTrue(self.telegram(handler).send_typing({'chat_id': -1, 'thread_id': 7}))

    def test_typing_failures_are_redacted_and_nonfatal(self):
        def handler(request): raise httpx.ConnectError('synthetic-secret')
        api = self.telegram(handler)
        with patch('telegram_api.event') as log:
            self.assertFalse(api.send_typing({'chat_id': -1}))
        self.assertNotIn('synthetic-secret', str(log.call_args_list))

    def test_worker_refreshes_every_four_seconds_and_stops_without_waiting(self):
        api = self.telegram(lambda _: self.fail('No real request expected'))
        stopped = MagicMock()
        stopped.is_set.return_value = False
        stopped.wait.side_effect = [False, True]
        with patch('telegram_api.threading.Event', return_value=stopped), patch('telegram_api.threading.Thread') as thread, patch.object(api, 'send_typing', return_value=True) as send:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs['target']()
            with api.typing({'chat_id': -1}): pass
        self.assertEqual(send.call_count, 2)
        self.assertEqual([call.args for call in stopped.wait.call_args_list], [(4,), (4,)])
        stopped.set.assert_called_once()
        thread.return_value.join.assert_called_once_with(timeout=0)

    def test_worker_start_failure_does_not_block_model(self):
        api = self.telegram(lambda _: self.fail('No network expected'))
        with patch('telegram_api.threading.Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('synthetic-secret')
            with api.typing({'chat_id': -1}): self.assertTrue(True)

    def test_service_typing_covers_generation_and_cleans_up_on_failure(self):
        for fails in [False, True]:
            active, transitions = [], []
            telegram = FakeTelegram()
            @contextmanager
            def typing(job):
                transitions.append('start'); active.append(True)
                try: yield
                finally: active.pop(); transitions.append('stop')
            telegram.typing = typing
            model = FakeModel(fail=fails)
            original = model.generate
            def generate(*args, **kwargs):
                self.assertTrue(active)
                return original(*args, **kwargs)
            model.generate = generate
            config = Config('synthetic', 'synthetic', frozenset({-1}))
            store = MemoryStore()
            service = Service(config, store, telegram, model)
            store.enqueue(1, payload())
            service.prepare(store.next_job())
            service.generate(store.next_job())
            self.assertEqual(transitions, ['start', 'stop'])
            self.assertEqual(active, [])
