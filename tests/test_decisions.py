"""Decision repair is bounded, schema-validated and content-free in logs."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
import json
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from config import Config
from model import Model, ModelFailure, ModelText, parse_decision, DecisionValidationError
from storage import BudgetExceeded
from test_clients import Allowance
from test_service import MemoryStore, FakeTelegram, payload
from service import Service


class DecisionTests(unittest.TestCase):
    def model(self, outputs, provider='zai', store=None):
        calls = []
        def create(**params):
            calls.append(params)
            text = outputs[min(len(calls) - 1, len(outputs) - 1)]
            if isinstance(text, Exception): raise text
            if provider == 'zai':
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
                    message=SimpleNamespace(content=text))], usage=None)
            return SimpleNamespace(status='completed', output_text=text, usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                                 responses=SimpleNamespace(create=create))
        config = Config('synthetic', 'synthetic', frozenset({-1}), provider=provider,
                        zai_key='synthetic', model='glm-4.7-flashx' if provider == 'zai' else 'gpt-6-luna',
                        api_min_interval_seconds=0)
        return Model(config, store or Allowance(), client), calls

    def test_invalid_then_valid_decision_is_corrected_once_on_both_providers(self):
        for provider in ['zai', 'openai']:
            model, calls = self.model(['{"answer":"private-rejected-answer"}',
                                      '{"respond":true,"reply":"Hello"}'], provider)
            with patch('model.event') as log:
                self.assertEqual(model.decide_reply('Cat', []), 'Hello')
            self.assertEqual(len(calls), 2)
            instructions = calls[1]['messages'][0]['content'] if provider == 'zai' else calls[1]['instructions']
            sent = calls[1]['messages'] if provider == 'zai' else calls[1]['input']
            self.assertIn('Include both required keys', sent[-1]['content'])
            self.assertEqual(sent[-2], {'role': 'assistant', 'content': '{"answer":"private-rejected-answer"}'})
            self.assertIn('missing_fields', sent[-1]['content'])
            self.assertIn('{"respond":false,"reply":""}', instructions)
            self.assertNotIn('private-rejected-answer', instructions)
            self.assertNotIn('private-rejected-answer', str(log.call_args_list))
            invalid = [c for c in log.call_args_list if c.args[0] == 'ai_output_invalid']
            self.assertEqual(invalid[0].kwargs['validation_error'], 'missing_fields')
            self.assertEqual(model.store.total > 0, True)

    def test_valid_silence_needs_no_retry_and_repaired_silence_is_allowed(self):
        for outputs, count in [(['{"respond":false,"reply":""}'], 1),
                               (['{"respond":false,"reply":"wrong"}', '{"respond":false,"reply":""}'], 2)]:
            model, calls = self.model(outputs)
            self.assertIsNone(model.decide_reply('Cat', []))
            self.assertEqual(len(calls), count)

    def test_each_validation_category_is_specific_and_no_invalid_coercion(self):
        cases = [('private-invalid-json', 'malformed_json'),
                 ('[]', 'not_object'), ('{"respond":true}', 'missing_fields'),
                 ('{"respond":true,"reply":"x","admin":true}', 'extra_fields'),
                 ('{"respond":"true","reply":"x"}', 'wrong_types'),
                 ('{"respond":true,"reply":42}', 'wrong_types'),
                 ('{"respond":true,"reply":" "}', 'reply_required'),
                 ('{"respond":false,"reply":"x"}', 'silent_reply_nonempty'),
                 ('{"respond":true,"reply":"' + 'x' * 3501 + '"}', 'reply_too_long'),
                 ('{"respond":false,"respond":true,"reply":"x"}', 'duplicate_fields')]
        for text, category in cases:
            with self.subTest(category=category):
                with self.assertRaises(DecisionValidationError) as caught: parse_decision(text)
                self.assertEqual(str(caught.exception), category)
                model, calls = self.model([text])
                with patch('model.event') as log:
                    with self.assertRaises(ModelFailure): model.decide_reply('Cat', [])
                self.assertEqual(len(calls), 4)
                invalid = [c for c in log.call_args_list if c.args[0] == 'ai_output_invalid']
                self.assertEqual(len(invalid), 4)
                self.assertTrue(all(c.kwargs['validation_error'] == category for c in invalid))

    def test_budget_rejection_blocks_corrective_call(self):
        store = Allowance()
        reserve = store.reserve
        count = 0
        def bounded(amount, budget):
            nonlocal count
            count += 1
            if count == 2: raise BudgetExceeded()
            reserve(amount, budget)
        store.reserve = bounded
        model, calls = self.model(['bad', '{"respond":true,"reply":"Hello"}'], store=store)
        with self.assertRaises(BudgetExceeded): model.decide_reply('Cat', [])
        self.assertEqual(len(calls), 1)
        self.assertGreater(store.total, 0)

    def test_repair_respects_call_pacing(self):
        model, calls = self.model(['bad', '{"respond":true,"reply":"Hello"}'])
        model.config = replace(model.config, api_min_interval_seconds=2)
        with patch('model.time.monotonic', return_value=100), patch('model.time.sleep') as sleep:
            self.assertEqual(model.decide_reply('Cat', []), 'Hello')
        sleep.assert_called_once_with(2)

    def test_repair_disables_tools_and_preserves_existing_search_sources(self):
        model, _ = self.model([])
        with patch.object(model, 'generate', side_effect=[ModelText('bad', [('private', 'https://example.com')]),
                            ModelText('{"respond":true,"reply":"Hello"}', [])]) as generate:
            self.assertIn('https://example.com', model.decide_reply('Cat', [], allow_search=True))
        self.assertTrue(generate.call_args_list[0].kwargs['allow_search'])
        self.assertFalse(generate.call_args_list[1].kwargs['allow_search'])
        self.assertIn('formatting repair only', generate.call_args_list[1].args[0])
        self.assertNotIn('No search tool is available', generate.call_args_list[1].args[0])
        self.assertIn('Do not ask the user to run /search', generate.call_args_list[1].args[0])

    def test_network_failure_does_not_trigger_json_repair(self):
        model, calls = self.model([RuntimeError('synthetic-secret')])
        with self.assertRaises(ModelFailure): model.decide_reply('Cat', [])
        self.assertEqual(len(calls), 1)

    def test_failed_repair_in_all_mode_sends_nothing(self):
        model, calls = self.model(['bad'])
        store = MemoryStore()
        store.chat['mode'] = 'all'
        service = Service(model.config, store, FakeTelegram(), model)
        message = payload('general chatter')
        message['directed'] = False
        store.enqueue(1, message)
        service.prepare(store.next_job())
        service.generate(store.next_job())
        self.assertIsNone(store.next_job())
        self.assertEqual(service.telegram.sent, [])
        self.assertEqual(len(calls), 4)

    def test_third_repair_succeeds_and_receives_latest_answer_and_error(self):
        outputs = ['private-first', '{"respond":true}', '{"respond":"true","reply":"private-third"}',
                   '{"respond":true,"reply":"Fixed"}']
        history = [{'role': 'user', 'content': 'Original request'}]
        for provider in ('openai', 'zai'):
            with self.subTest(provider=provider):
                model, calls = self.model(outputs, provider)
                with patch('model.event') as log:
                    self.assertEqual(model.decide_reply('Cat', history), 'Fixed')
                self.assertEqual(len(calls), 4)
                for index, category in enumerate(('malformed_json', 'missing_fields', 'wrong_types'), 1):
                    messages = calls[index]['input' if provider == 'openai' else 'messages']
                    self.assertEqual(messages[-2], {'role': 'assistant', 'content': outputs[index - 1]})
                    feedback = json.loads(messages[-1]['content'].split('\n', 1)[1])
                    self.assertEqual(feedback['validation_error'], category)
                    self.assertIn('Repair your previous answer', feedback['instruction'])
                    self.assertEqual(len(messages), 3 if provider == 'openai' else 4)
                    if index == 1:
                        self.assertEqual(feedback['error_position'], 0)
                        self.assertEqual(feedback['error_line'], 1)
                started = [call.kwargs for call in log.call_args_list if call.args[0] == 'ai_json_repair_started']
                self.assertEqual([item['repair_attempt'] for item in started], [1, 2, 3])
                completed = [call.kwargs for call in log.call_args_list if call.args[0] == 'ai_json_repair_completed']
                self.assertEqual(completed[0]['repair_attempt'], 3)
                self.assertEqual(len({item['json_request_id'] for item in started + completed}), 1)
                self.assertNotIn('private-first', str(log.call_args_list))
                self.assertNotIn('private-third', str(log.call_args_list))
        self.assertEqual(history, [{'role': 'user', 'content': 'Original request'}])

    def test_character_settings_use_same_repair_and_validation(self):
        from personality import DEFAULTS
        outputs = ['private-malformed', json.dumps({**DEFAULTS, 'eagerness': 'invalid'}), json.dumps(DEFAULTS)]
        for provider in ('openai', 'zai'):
            with self.subTest(provider=provider):
                model, calls = self.model(outputs, provider)
                with patch('model.event') as log:
                    self.assertEqual(model.infer_personality('Cat', DEFAULTS), DEFAULTS)
                self.assertEqual(len(calls), 3)
                repair = [call.kwargs for call in log.call_args_list if call.args[0] == 'ai_json_repair_started']
                self.assertEqual([item['validation_error'] for item in repair], ['malformed_json', 'invalid_settings'])
                self.assertTrue(all(item['schema_name'] == 'character_settings' for item in repair))

    def test_request_failure_during_repair_is_logged_and_stops(self):
        model, calls = self.model(['bad', RuntimeError('synthetic-secret')])
        with patch('model.event') as log:
            with self.assertRaises(ModelFailure):
                model.decide_reply('Cat', [])
        self.assertEqual(len(calls), 2)
        failed = [call.kwargs for call in log.call_args_list if call.args[0] == 'ai_json_repair_failed']
        self.assertEqual(failed[-1]['reason'], 'request_failed')
        self.assertNotIn('synthetic-secret', str(log.call_args_list))
        self.assertNotIn('Respond only with a short conversational reply', calls[0]['messages'][0]['content'])

    def test_only_whole_json_fences_and_leading_bom_are_normalized(self):
        valid = '{"respond":true,"reply":"Hello"}'
        for raw in ['```json\n' + valid + '\n```', '```\n' + valid + '\n```', '\ufeff' + valid]:
            self.assertEqual(parse_decision(raw), (True, 'Hello'))
            model, calls = self.model([raw])
            self.assertEqual(model.decide_reply('Cat', []), 'Hello')
            self.assertEqual(len(calls), 1)
        for raw in ['Explanation\n' + valid, valid + '\nExplanation',
                    '```json\n' + valid + '\n```\nExtra text',
                    '<think>Reasoning</think>' + valid,
                    '```json\n{"respond":"true","reply":"Hello"}\n```']:
            with self.assertRaises(DecisionValidationError): parse_decision(raw)

    def test_parser_diagnostics_do_not_expose_rejected_output(self):
        model, _ = self.model(['{"respond": true, "reply": "synthetic-private"} trailing-private'])
        with patch('model.event') as log:
            with self.assertRaises(ModelFailure): model.decide_reply('Cat', [])
        invalid = [c for c in log.call_args_list if c.args[0] == 'ai_output_invalid']
        self.assertEqual(invalid[0].kwargs['parser_error'], 'extra_data')
        self.assertEqual(invalid[0].kwargs['format_hint'], 'object')
        self.assertIsInstance(invalid[0].kwargs['error_position'], int)
        self.assertNotIn('synthetic-private', str(log.call_args_list))
        self.assertNotIn('trailing-private', str(log.call_args_list))
