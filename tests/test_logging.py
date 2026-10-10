"""Verify operational logs without touching real credentials or networks."""
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import json
import hashlib
import logging
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from observability import LOGGER, configure_logging, event, skill_event
from config import Config
from model import Model, ModelFailure
from storage import BudgetExceeded
from test_clients import Allowance
from skill_registry import instructions as _load_skill_body
def skill_body(name='nearby-places'): return _load_skill_body(name)
from providers import Generation, SearchOutcome, ToolCall
from test_skill_loading import EVIDENCE, cards


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

    def test_review_reader_logs_only_metadata_and_safe_failure_reason(self):
        model = self.model(None)
        url = 'https://yandex.ru/maps/org/private_name/123/reviews/'
        for status, reason in [('ok', None), ('unavailable', 'captcha')]:
            result = {'status': status, 'reason': reason, 'returned_count': 1,
                      'reviews': [{'text': 'private review text'}], 'company': {'name': 'private cafe'}, 'url': url}
            with patch('model.read_reviews', return_value=result):
                self.assertEqual(model._read_yandex_reviews(url), result)
        logs = [record for record in self.records() if record['event'].startswith('reviews_read_')]
        self.assertEqual([r['event'] for r in logs], ['reviews_read_started', 'reviews_read_completed',
                                                    'reviews_read_started', 'reviews_read_failed'])
        self.assertEqual(logs[0]['reviews_request_id'], logs[1]['reviews_request_id'])
        self.assertEqual(logs[1]['review_count'], 1)
        self.assertEqual(logs[3]['reason'], 'captcha')
        self.assertNotIn('private', self.output.getvalue())

    def model(self, create, store=None, provider='openai'):
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        return Model(Config('synthetic-telegram-secret', 'synthetic-openai-secret', frozenset({-1}),
                            provider=provider, model='gpt-6-luna' if provider == 'openai' else 'glm-4.7',
                            zai_key='synthetic-zai-secret'),
                     store or Allowance(), client)

    def test_skill_logs_validated_request_and_exact_local_return_for_both_providers(self):
        for provider in ('openai', 'zai'):
            with self.subTest(provider=provider):
                self.output.seek(0)
                self.output.truncate(0)
                model = self.model(None, provider=provider)
                call = ToolCall('synthetic-secret-call-id', 'load_skill',
                                '{ "name" : "nearby-places" }')
                search = ToolCall('search', 'search_web', '{"query":"public cafe"}')
                responses = [Generation('', True, tool_calls=(call,)), Generation('', True, tool_calls=(search,)),
                             Generation(cards('private-answer'), True)]
                with patch.object(model, '_generation_attempt', side_effect=responses) as generate, \
                        patch.object(model, '_search_web', return_value=(EVIDENCE, True)):
                    model.generate('private-prompt', [{'role': 'user', 'content': 'private-message'}],
                                   allow_search=True)
                requested, returned = [r for r in self.records() if r['event'].startswith('skill_')]
                self.assertEqual(requested['event'], 'skill_requested')
                self.assertEqual(returned['event'], 'skill_returned')
                self.assertEqual(requested['requested'], {'name': 'nearby-places'})
                self.assertEqual(requested['tool'], 'load_skill')
                self.assertEqual(requested['provider'], provider)
                self.assertEqual(requested['skill_request_id'], returned['skill_request_id'])
                self.assertRegex(requested['skill_request_id'], r'^[0-9a-f]{32}$')
                self.assertEqual(returned['returned'], {'name': 'nearby-places',
                    'sha256': hashlib.sha256(skill_body().encode('utf-8')).hexdigest()})
                self.assertEqual(returned['returned_chars'], len(skill_body()))
                continuation = generate.call_args_list[1].kwargs['continuation'][-1]
                self.assertEqual(returned['returned']['sha256'], hashlib.sha256(
                    continuation['output' if provider == 'openai' else 'content'].encode('utf-8')).hexdigest())
                self.assertNotIn('content', returned['returned'])
                self.assertNotIn(skill_body(), self.output.getvalue())
                for secret in ('synthetic-secret-call-id', 'private-prompt', 'private-message',
                               'private-answer', 'synthetic-openai-secret', 'synthetic-zai-secret'):
                    self.assertNotIn(secret, self.output.getvalue())

    def test_failed_skill_load_logs_request_and_safe_failure(self):
        model = self.model(None)
        response = Generation('', True, tool_calls=(ToolCall('call', 'load_skill',
                                                            '{"name":"nearby-places"}'),))
        with patch.object(model, '_generation_attempt', return_value=response), \
                patch('model.skill_instructions', side_effect=OSError('synthetic-secret')):
            with self.assertRaises(ModelFailure):
                model.generate('private-prompt', [], allow_search=True)
        requested, failed = self.records()
        self.assertEqual([requested['event'], failed['event']], ['skill_requested', 'skill_failed'])
        self.assertEqual(requested['skill_request_id'], failed['skill_request_id'])
        self.assertEqual(failed['reason'], 'skill_loading_failed')
        self.assertNotIn('returned', failed)
        self.assertNotIn('synthetic-secret', self.output.getvalue())

    def test_rejected_skill_arguments_are_not_logged_or_loaded(self):
        model = self.model(None)
        for arguments in ('{"name":"synthetic-secret"}', 'synthetic-secret',
                          '{"name":"nearby-places","token":"synthetic-secret"}'):
            response = Generation('', True, tool_calls=(ToolCall('call', 'load_skill', arguments),))
            with patch.object(model, '_generation_attempt', return_value=response), \
                    patch('model.skill_instructions') as load:
                with self.assertRaises(ModelFailure):
                    model.generate('private-prompt', [], allow_search=True)
                load.assert_not_called()
        self.assertEqual(len(self.records()), 3)
        for record in self.records():
            self.assertEqual(record['event'], 'skill_failed')
            self.assertEqual(record['reason'], 'invalid_skill_call')
            self.assertNotIn('requested', record)
        self.assertNotIn('synthetic-secret', self.output.getvalue())

    def test_no_skill_request_emits_no_skill_events(self):
        model = self.model(lambda **_: SimpleNamespace(status='completed', output_text='Hello',
                                                       usage=None))
        model.generate('private-prompt', [], allow_search=True)
        self.assertFalse(any(record['event'].startswith('skill_') for record in self.records()))

    def test_general_logger_cannot_emit_arbitrary_skill_content(self):
        event('skill_returned', returned={'content': 'synthetic-secret'}, content='synthetic-secret')
        skill_event('requested', request_id='0' * 32, provider='synthetic-secret',
                    model='synthetic-secret', name='nearby-places', content='synthetic-secret')
        skill_event('returned', request_id='synthetic-secret', provider='openai', model='gpt-6-luna',
                    name='nearby-places', content='synthetic-secret')
        skill_event('requested', request_id='0' * 32, provider='openai', model='gpt-6-luna',
                    name='synthetic-secret')
        self.assertEqual(len(self.records()), 2)
        self.assertNotIn('synthetic-secret', self.output.getvalue())

    def test_return_logs_only_metadata_even_if_body_contains_sensitive_text(self):
        skill_event('returned', request_id='0' * 32, provider='openai', model='gpt-6-luna',
                    name='nearby-places', content='Секретный текст synthetic-secret\nprivate-message')
        record, = self.records()
        self.assertEqual(record['returned']['name'], 'nearby-places')
        self.assertEqual(set(record['returned']), {'name', 'sha256'})
        for text in ('Секретный', 'synthetic-secret', 'private-message'):
            self.assertNotIn(text, self.output.getvalue())

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
        self.assertFalse(records[0]['web_search_enabled'])
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
        self.assertEqual(self.records()[-1]['event'], 'ai_json_repair_exhausted')
        failure = [record for record in self.records() if record['event'] == 'ai_output_invalid'][-1]
        self.assertEqual(failure['reason'], 'invalid_json')
        self.assertIn('Начало ответа не является допустимым JSON-значением',
                      failure['validation_detail'])
        self.assertEqual(failure['error_line'], 1)
        self.assertEqual(failure['error_column'], 1)
        self.assertEqual(failure['output_chars'], len('private-invalid-json'))
        self.assertNotIn('private-invalid-json', self.output.getvalue())

    def test_json_failure_details_are_fixed_and_do_not_echo_model_output(self):
        cases = [('{"respond":true,\n"reply" "synthetic-secret"}', 'expected_colon', 'двоеточие'),
                 ('{"respond":true,"reply":"synthetic-secret"} trailing', 'extra_data', 'лишний текст'),
                 ('{"respond":true,"reply":"synthetic-secret}', 'unterminated_string', 'не закрыта')]
        for text, label, detail in cases:
            with self.subTest(parser=label):
                model = self.model(lambda **_: SimpleNamespace(status='completed', output_text=text, usage=None))
                with self.assertRaises(ModelFailure):
                    model.decide_reply('private-prompt', [])
                record = [record for record in self.records() if record['event'] == 'ai_output_invalid'][-1]
                self.assertEqual(record['parser_error'], label)
                self.assertIn(detail, record['validation_detail'])
                self.assertGreaterEqual(record['error_line'], 1)
                self.assertGreaterEqual(record['error_column'], 1)
        self.assertNotIn('synthetic-secret', self.output.getvalue())
        self.assertNotIn('private-prompt', self.output.getvalue())

    def test_schema_failure_detail_and_untrusted_detail_field(self):
        event('ai_output_invalid', validation_error='missing_fields', validation_detail='synthetic-secret')
        self.assertEqual(self.records()[-1]['validation_detail'], 'Отсутствует обязательное поле respond или reply.')
        self.assertNotIn('synthetic-secret', self.output.getvalue())

    def test_repair_events_explain_roundtrip_without_answer_or_feedback_text(self):
        outputs = iter(['private-first-answer', '{"respond":true}', '{"respond":true,"reply":"private-fixed-answer"}'])
        model = self.model(lambda **_: SimpleNamespace(status='completed', output_text=next(outputs), usage=None))
        self.assertEqual(model.decide_reply('private-prompt', []), 'private-fixed-answer')
        repair = [record for record in self.records() if record['event'].startswith('ai_json_repair_')]
        self.assertEqual([record['event'] for record in repair], ['ai_json_repair_started', 'ai_json_repair_failed',
                                                                 'ai_json_repair_started', 'ai_json_repair_completed'])
        self.assertEqual(len({record['json_request_id'] for record in repair}), 1)
        self.assertTrue(repair[0]['previous_answer_included'])
        self.assertTrue(repair[0]['error_feedback_included'])
        self.assertEqual(repair[0]['max_repairs'], 3)
        self.assertEqual(repair[-1]['repair_attempt'], 2)
        self.assertEqual(repair[-1]['format_mode'], 'json_schema')
        self.assertEqual(repair[-1]['schema_name'], 'reply_decision')
        for text in ('private-first-answer', 'private-fixed-answer', 'private-prompt', 'JSON validation feedback'):
            self.assertNotIn(text, self.output.getvalue())

    def test_search_logs_metadata_without_query_or_result_content(self):
        model = self.model(None, provider='zai')
        responses = [Generation('', True, tool_calls=(ToolCall('call', 'search_web',
                                                               '{"query":"private-query"}'),)),
                     Generation('private-answer', True)]
        results = [{'title': 'private-title', 'url': 'https://example.com/private-url',
                    'content': 'private-evidence', 'publish_date': ''}]
        with patch.object(model, '_generation_attempt', side_effect=responses), \
                patch.object(model.provider, 'search', return_value=SearchOutcome(results)):
            model.generate('private-prompt', [], allow_search=True)
        started, completed = [record for record in self.records() if record['event'].startswith('web_search_')]
        self.assertEqual(started['event'], 'web_search_started')
        self.assertEqual(completed['event'], 'web_search_completed')
        self.assertEqual(started['search_request_id'], completed['search_request_id'])
        self.assertEqual(completed['result_count'], 1)
        self.assertEqual(started['query_chars'], len('private-query'))
        for text in ('private-query', 'private-answer', 'private-title', 'private-url', 'private-evidence', 'private-prompt'):
            self.assertNotIn(text, self.output.getvalue())

    def test_incomplete_response_logs_finish_reason_usage_and_length_without_text(self):
        from providers import ZaiProvider
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_:
            SimpleNamespace(choices=[SimpleNamespace(finish_reason='length',
                message=SimpleNamespace(content='private-truncated-output'))],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=600)))))
        model = self.model(None, provider='zai')
        model.provider = ZaiProvider(model.config, client)
        with self.assertRaises(ModelFailure):
            model.generate('private-prompt', [])
        failure = self.records()[-1]
        self.assertEqual(failure['finish_reason'], 'length')
        self.assertEqual(failure['output_tokens'], 600)
        self.assertEqual(failure['output_chars'], len('private-truncated-output'))
        self.assertNotIn('private-truncated-output', self.output.getvalue())

    def test_tool_rejection_logs_exact_safe_category(self):
        cases = [((ToolCall('call', 'web_search', '{"query":"private-query"}'),), 'unknown_tool', 'web_search'),
                 ((ToolCall('call', 'synthetic-secret-tool', '{}'),), 'unknown_tool', 'other'),
                 ((ToolCall('', 'load_skill', '{}'),), 'missing_call_id', 'load_skill'),
                 ((ToolCall('call', 'load_skill', 'private-invalid-json'),), 'malformed_arguments', 'load_skill'),
                 ((ToolCall('call', 'load_skill', '{"name":"synthetic-secret"}'),), 'invalid_arguments', 'load_skill'),
                 ((ToolCall('call1', 'load_skill', '{}'), ToolCall('call2', 'search_web', '{}')), 'invalid_arguments', None)]
        for calls, category, name in cases:
            with self.subTest(category=category):
                model = self.model(None)
                with patch.object(model, '_generation_attempt', return_value=Generation('', True, tool_calls=calls)):
                    with self.assertRaises(ModelFailure):
                        model.generate('private-prompt', [], allow_search=True)
                record = self.records()[-1]
                self.assertEqual(record['tool_error'], category)
                self.assertEqual(record['tool_call_count'], len(calls))
                if name:
                    self.assertEqual(record['requested_tool'], name)
        for text in ('synthetic-secret', 'private-query', 'private-invalid-json', 'private-prompt'):
            self.assertNotIn(text, self.output.getvalue())
