"""Pacing contract tested with synthetic credentials and a virtual clock."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from config import Config
from model import Model
from test_clients import Allowance


class PacingTests(unittest.TestCase):
    def model(self, interval=2):
        config = Config('synthetic', '', frozenset({-1}), provider='zai',
                        zai_key='synthetic', model='glm-4.7-flash', api_min_interval_seconds=interval)
        return Model(config, Allowance(), SimpleNamespace())

    def test_gap_is_shared_across_text_image_and_transcription(self):
        model = self.model()
        clock = [100.0]
        starts, waits = [], []
        def sleep(delay): waits.append(delay); clock[0] += delay
        def call(): starts.append(clock[0]); clock[0] += 0.5; return 'ok'
        with patch('model.time.monotonic', side_effect=lambda: clock[0]), patch('model.time.sleep', side_effect=sleep):
            for operation in ['text', 'image', 'transcription']:
                self.assertEqual(model.request(call, Decimal('0'), 'glm-4.7-flash', operation), 'ok')
        self.assertEqual(starts, [100.0, 102.5, 105.0])
        self.assertEqual(waits, [2, 2])

    def test_elapsed_work_and_zero_interval_need_no_delay(self):
        model = self.model()
        model.last_request_finished = 100
        with patch('model.time.monotonic', return_value=110), patch('model.time.sleep') as sleep:
            model.paced_call(lambda: None, 'glm-4.7-flash', 'text')
        sleep.assert_not_called()
        model = self.model(0)
        model.last_request_finished = 100
        with patch('model.time.monotonic', return_value=100), patch('model.time.sleep') as sleep:
            model.paced_call(lambda: None, 'glm-4.7-flash', 'text')
        sleep.assert_not_called()

    def test_failed_call_counts_and_retry_backoff_satisfies_gap(self):
        model = self.model()
        clock = [100.0]
        attempts, waits = [], []
        class Limit(Exception):
            status_code = 429
            body = {'error': {'code': '1302'}}
        def call():
            attempts.append(clock[0])
            clock[0] += 0.5
            if len(attempts) == 1: raise Limit()
            return 'ok'
        def sleep(delay): waits.append(delay); clock[0] += delay
        with patch('model.time.monotonic', side_effect=lambda: clock[0]), patch('model.time.sleep', side_effect=sleep), patch('model.random.uniform', return_value=0):
            self.assertEqual(model.request(call, Decimal('0'), 'glm-4.7-flash', 'text'), 'ok')
        self.assertEqual(attempts, [100.0, 102.5])
        self.assertEqual(waits, [2])

    def test_fractional_gap_after_failure(self):
        model = self.model(0.5)
        clock = [100.0]
        def fail(): raise RuntimeError('synthetic-secret')
        with patch('model.time.monotonic', side_effect=lambda: clock[0]), patch('model.time.sleep') as sleep:
            with self.assertRaises(RuntimeError): model.paced_call(fail, 'glm-4.7-flash', 'text')
            clock[0] += 0.1
            model.paced_call(lambda: None, 'glm-4.7-flash', 'text')
        self.assertAlmostEqual(sleep.call_args.args[0], 0.4)

    def test_configuration_defaults_and_rejects_invalid_intervals(self):
        inputs = {'TELEGRAM_BOT_TOKEN': 'synthetic', 'OPENAI_API_KEY': 'synthetic', 'ALLOWED_CHAT_IDS': '-1'}
        with patch('config.os.getenv', side_effect=lambda key, default=None: inputs.get(key, default)):
            self.assertEqual(Config.load().api_min_interval_seconds, 0.25)
            for valid in ['0', '0.5', '30']:
                inputs['API_MIN_INTERVAL_SECONDS'] = valid
                self.assertEqual(Config.load().api_min_interval_seconds, float(valid))
            for invalid in ['NaN', 'inf', '-1', '31', 'bad']:
                inputs['API_MIN_INTERVAL_SECONDS'] = invalid
                with self.assertRaises(ValueError): Config.load()
