"""Bounded Z.ai retries using mocked HTTP responses and clocks only."""
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch
from types import SimpleNamespace
import sys
import unittest
import httpx
from openai import OpenAI
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from config import Config
from model import Model, ModelFailure, estimate_cost
from providers import provider_error_code, zai_retry_delay
from storage import BudgetExceeded
from test_clients import Allowance


class RetryTests(unittest.TestCase):
    def model(self, responses, model='glm-4.7-flash'):
        calls = []
        def handler(request):
            calls.append(request)
            response = responses[min(len(calls) - 1, len(responses) - 1)]
            return response
        http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(http.close)
        client = OpenAI(api_key='synthetic-key', base_url='https://api.z.ai/api/paas/v4/',
                        max_retries=0, http_client=http)
        config = Config('synthetic', '', frozenset({-1}), provider='zai',
                        zai_key='synthetic-key', model=model, api_min_interval_seconds=0)
        store = Allowance()
        return Model(config, store, client), store, calls

    def success(self):
        return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion',
            'created': 1, 'model': 'glm-4.7-flash', 'choices': [{'index': 0,
            'message': {'role': 'assistant', 'content': 'Hello'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}})

    def failure(self, code='1302', status=429, headers=None):
        return httpx.Response(status, headers=headers,
            json={'error': {'code': code, 'message': 'synthetic-secret private-response'}})

    def test_temporary_limits_retry_and_preserve_paid_failed_reservations(self):
        model, store, calls = self.model([self.failure(), self.failure('1305'), self.success()],
                                          model='glm-4.7-flashx')
        with patch('model.time.sleep') as sleep, patch('model.random.uniform', return_value=0), patch('model.event') as log:
            self.assertEqual(model.generate('Cat', []), 'Hello')
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        reservation = estimate_cost('glm-4.7-flashx', 'Cat', [], 600, 'zai')
        self.assertEqual(store.total, reservation * 2 + Decimal('0.00001275'))
        retries = [call for call in log.call_args_list if call.args[0] == 'ai_request_retry']
        self.assertEqual([call.kwargs['provider_code'] for call in retries], [1302, 1305])
        self.assertNotIn('synthetic-secret', str(log.call_args_list))

    def test_retry_after_header_is_respected(self):
        model, _, calls = self.model([self.failure(headers={'Retry-After': '7'}), self.success()])
        with patch('model.time.sleep') as sleep, patch('model.random.uniform', return_value=0):
            self.assertEqual(model.generate('Cat', []), 'Hello')
        sleep.assert_called_once_with(7)
        self.assertEqual(len(calls), 2)

    def test_retry_exhaustion_is_bounded_and_redacted(self):
        model, _, calls = self.model([self.failure()])
        with patch('model.time.sleep') as sleep, patch('model.random.uniform', return_value=0), patch('model.event') as log:
            with self.assertRaises(ModelFailure) as caught: model.generate('Cat', [])
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn('synthetic-secret', str(caught.exception))
        self.assertEqual(log.call_args_list[-1].kwargs['provider_code'], 1302)

    def test_account_quota_unknown_code_and_non_429_errors_are_not_retried(self):
        for code, status in [('1113', 429), ('1308', 429), ('1311', 429), ('1310', 429),
                             ('synthetic-secret', 429), ('9999', 429), ('1000', 401), ('1200', 500)]:
            with self.subTest(code=code, status=status):
                model, _, calls = self.model([self.failure(code, status)])
                with patch('model.time.sleep') as sleep:
                    with self.assertRaises(ModelFailure): model.generate('Cat', [])
                sleep.assert_not_called()
                self.assertEqual(len(calls), 1)

    def test_long_retry_after_is_not_shortened(self):
        model, _, calls = self.model([self.failure(headers={'Retry-After': '120'})])
        with patch('model.time.sleep') as sleep:
            with self.assertRaises(ModelFailure): model.generate('Cat', [])
        sleep.assert_not_called()
        self.assertEqual(len(calls), 1)

    def test_retry_cannot_bypass_spending_allowance(self):
        model, store, calls = self.model([self.failure(), self.success()], model='glm-4.7-flashx')
        reserve = store.reserve
        count = 0
        def bounded(amount, budget):
            nonlocal count
            count += 1
            if count > 1: raise BudgetExceeded()
            reserve(amount, budget)
        store.reserve = bounded
        with patch('model.time.sleep') as sleep:
            with self.assertRaises(BudgetExceeded): model.generate('Cat', [])
        sleep.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertGreater(store.total, 0)

    def test_transcription_and_vision_use_same_bounded_retry_policy(self):
        for operation in ['image', 'transcription']:
            response = httpx.Response(200, json={'text': 'Hello'}) if operation == 'transcription' else self.success()
            model, store, calls = self.model([self.failure(), response])
            with patch('model.time.sleep'), patch('model.random.uniform', return_value=0):
                text = model.transcribe(b'synthetic-wav', 1) if operation == 'transcription' else model.describe_image(b'\xff\xd8\xffsynthetic')
            self.assertEqual(text, 'Hello')
            self.assertEqual(len(calls), 2)
            self.assertGreater(store.total, 0)

    def test_error_code_allowlist_and_malformed_retry_headers(self):
        for code in ['synthetic-secret', '9999', True, {}, 1302.0]:
            self.assertIsNone(provider_error_code(SimpleNamespace(body={'error': {'code': code}})))
        self.assertEqual(provider_error_code(SimpleNamespace(body={'error': {'code': '1302'}})), 1302)
        for value in ['NaN', 'inf', '-2', 'synthetic-secret']:
            error = SimpleNamespace(status_code=429, body={}, response=SimpleNamespace(headers={'retry-after': value}))
            self.assertEqual(zai_retry_delay(error, 0), 2)
        error = SimpleNamespace(status_code=429, body={}, response=SimpleNamespace(headers={'retry-after-ms': '2500'}))
        self.assertEqual(zai_retry_delay(error, 0), 2.5)
