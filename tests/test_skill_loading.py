"""Lazy skill loading through native SDKs with synthetic credentials and HTTP."""
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import json
import sys
import unittest
import httpx
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from config import Config
from model import Model, ModelFailure
from nearby_places import instructions as skill_body
from storage import BudgetExceeded
from test_clients import Allowance


class TrackedAllowance(Allowance):
    def __init__(self, reject_second=False):
        super().__init__()
        self.reservations, self.charges = [], []
        self.reject_second = reject_second

    def reserve(self, amount, budget):
        self.reservations.append(amount)
        if self.reject_second and len(self.reservations) == 2:
            raise BudgetExceeded()
        super().reserve(amount, budget)

    def reconcile(self, reserved, actual):
        self.charges.append(actual)
        super().reconcile(reserved, actual)


class SkillLoadingTests(unittest.TestCase):
    def model(self, provider, handler, allowance=None):
        http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(http.close)
        client = OpenAI(api_key='synthetic-key', base_url='https://provider.example/v1/',
                        http_client=http, max_retries=0)
        config = Config('synthetic', 'synthetic', frozenset({-1}), provider=provider,
                        model='gpt-6-luna' if provider == 'openai' else 'glm-4.7',
                        zai_key='synthetic', api_min_interval_seconds=0)
        return Model(config, allowance or TrackedAllowance(), client)

    def response(self, provider, *, call=None, text='Hello', searched=False):
        if provider == 'openai':
            output = []
            if searched:
                output.extend([{'type': 'web_search_call', 'id': 'ws_fake', 'status': 'completed',
                    'action': {'type': 'search', 'query': 'public cafe'}},
                    {'type': 'message', 'id': 'msg_source', 'role': 'assistant', 'status': 'completed',
                     'content': [{'type': 'output_text', 'text': 'Evidence', 'annotations': [
                        {'type': 'url_citation', 'url': 'https://example.com/cafe', 'title': 'Cafe',
                         'start_index': 0, 'end_index': 8}]}]}])
            if call is not None:
                output.append({'type': 'function_call', 'id': 'fc_fake', 'call_id': 'call_fake',
                    'name': call[0], 'arguments': call[1], 'status': 'completed'})
            else:
                output.append({'type': 'message', 'id': 'msg_fake', 'role': 'assistant',
                    'status': 'completed', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]})
            return httpx.Response(200, json={'id': 'resp_fake', 'object': 'response', 'created_at': 1,
                'model': 'gpt-6-luna', 'status': 'completed', 'output': output,
                'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}})
        message = {'role': 'assistant', 'content': None if call is not None else text}
        if call is not None:
            message.update(tool_calls=[{'id': 'call_fake', 'type': 'function',
                'function': {'name': call[0], 'arguments': call[1]}}], reasoning_content='Synthetic reasoning')
        return httpx.Response(200, json={'id': 'chat_fake', 'object': 'chat.completion', 'created': 1,
            'model': 'glm-4.7', 'choices': [{'index': 0, 'finish_reason': 'tool_calls' if call else 'stop',
                                         'message': message}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110},
            'web_search': [{'title': 'Cafe', 'link': 'https://example.com/cafe'}] if searched else []})

    def test_unrelated_reply_never_reads_or_sends_skill_body(self):
        for provider in ('openai', 'zai'):
            with self.subTest(provider=provider):
                bodies = []
                def handler(request):
                    bodies.append(json.loads(request.content))
                    return self.response(provider)
                model = self.model(provider, handler)
                with patch('model.place_instructions') as load:
                    self.assertEqual(model.generate('Cat', [{'role': 'user', 'content': 'Hi'}], allow_search=True), 'Hello')
                load.assert_not_called()
                self.assertEqual(len(bodies), 1)
                self.assertNotIn(skill_body(), json.dumps(bodies, ensure_ascii=False))
                self.assertIn('load_skill', json.dumps(bodies))

    def test_format_retry_after_skill_does_not_reload_tools_or_lose_sources(self):
        for provider in ('openai', 'zai'):
            with self.subTest(provider=provider):
                bodies = []
                def handler(request):
                    bodies.append(json.loads(request.content))
                    if len(bodies) == 1:
                        return self.response(provider, call=('load_skill', '{"name":"nearby-places"}'))
                    if len(bodies) == 2:
                        return self.response(provider, text='Malformed conversational answer', searched=True)
                    return self.response(provider, text='{"respond":true,"reply":"Cafe"}')
                answer = self.model(provider, handler).decide_reply('Cat', [], allow_search=True)
                self.assertIn('Cafe', answer)
                self.assertIn('https://example.com/cafe', answer)
                self.assertEqual(len(bodies), 3)
                self.assertNotIn('tools', bodies[2])
                self.assertNotIn('No search tool is available', json.dumps(bodies[2]))

    def test_native_tool_roundtrip_is_billed_and_preserves_json_format(self):
        for provider in ('openai', 'zai'):
            with self.subTest(provider=provider):
                bodies = []
                def handler(request):
                    bodies.append(json.loads(request.content))
                    return self.response(provider, call=('load_skill', '{"name":"nearby-places"}')) if len(bodies) == 1 else self.response(provider, text='{"respond":true,"reply":"Cafe"}', searched=provider == 'zai')
                model = self.model(provider, handler)
                history = [{'role': 'user', 'content': 'Find a nearby cafe'}]
                with patch('model.place_instructions', wraps=skill_body) as load:
                    self.assertIn('Cafe', model.decide_reply('Cat', history, allow_search=True))
                load.assert_called_once_with()
                self.assertEqual(len(bodies), 2)
                self.assertEqual(len(model.store.reservations), 2)
                self.assertEqual(len(model.store.charges), 2)
                self.assertEqual(model.store.total, sum(model.store.charges))
                self.assertEqual(history, [{'role': 'user', 'content': 'Find a nearby cafe'}])
                if provider == 'openai':
                    self.assertNotIn(skill_body(), bodies[0]['instructions'])
                    result = bodies[1]['input'][-1]
                    self.assertEqual(result['type'], 'function_call_output')
                    self.assertEqual(result['output'], skill_body())
                    self.assertEqual(bodies[0]['text'], bodies[1]['text'])
                    self.assertFalse(bodies[1]['store'])
                else:
                    self.assertNotIn(skill_body(), bodies[0]['messages'][0]['content'])
                    result = bodies[1]['messages'][-1]
                    self.assertEqual(result['role'], 'tool')
                    self.assertEqual(result['content'], skill_body())
                    self.assertEqual(bodies[1]['messages'][-2]['reasoning_content'], 'Synthetic reasoning')
                    self.assertEqual(bodies[0]['response_format'], bodies[1]['response_format'])
                self.assertNotIn('load_skill', json.dumps(bodies[1]['tools']))

    def test_invalid_calls_never_read_files_or_continue(self):
        calls = [('run_shell', '{}'), ('load_skill', '{"name":"../../.env"}'),
                 ('load_skill', '{"name":"nearby-places","path":".env"}'),
                 ('load_skill', '{"name":"nearby-places","name":"nearby-places"}'),
                 ('load_skill', 'not-json'), ('load_skill', '[' * 2000)]
        for provider in ('openai', 'zai'):
            for call in calls:
                with self.subTest(provider=provider, call=call[0]):
                    requests = []
                    def handler(request):
                        requests.append(request)
                        return self.response(provider, call=call)
                    model = self.model(provider, handler)
                    with patch('model.place_instructions') as load:
                        with self.assertRaises(ModelFailure): model.generate('Cat', [], allow_search=True)
                    load.assert_not_called()
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(len(model.store.charges), 1)

    def test_continuation_respects_budget_and_context_limits(self):
        for provider in ('openai', 'zai'):
            for limited in ('budget', 'context', 'missing'):
                with self.subTest(provider=provider, limited=limited):
                    requests = []
                    def handler(request):
                        requests.append(request)
                        return self.response(provider, call=('load_skill', '{"name":"nearby-places"}'))
                    allowance = TrackedAllowance(reject_second=limited == 'budget')
                    model = self.model(provider, handler, allowance)
                    if limited == 'context': model.config = replace(model.config, max_tool_chars=4500)
                    with patch('model.place_instructions', side_effect=OSError('synthetic-secret') if limited == 'missing' else None,
                               wraps=skill_body if limited != 'missing' else None):
                        with self.assertRaises(BudgetExceeded if limited == 'budget' else ModelFailure) as caught:
                            model.generate('Cat', [], allow_search=True)
                    # An oversized tool result ends research: one tools-disabled final answer
                    # follows (and fails here only because the mock keeps requesting tools).
                    expected = 2 if limited == 'context' else 1
                    self.assertEqual(len(requests), expected)
                    self.assertEqual(len(allowance.charges), expected)
                    if limited == 'context':
                        self.assertNotIn('tools', json.loads(requests[-1].content))
                    self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_full_tool_context_ends_research_with_an_answer_instead_of_failing(self):
        for provider in ('openai', 'zai'):
            with self.subTest(provider=provider):
                requests = []
                def handler(request):
                    requests.append(json.loads(request.content))
                    if len(requests) == 1:
                        return self.response(provider, call=('load_skill', '{"name":"nearby-places"}'), searched=True)
                    return self.response(provider, text='Answer from gathered evidence')
                model = self.model(provider, handler)
                model.config = replace(model.config, max_tool_chars=4500)
                with patch('model.place_instructions', wraps=skill_body):
                    self.assertIn('Answer from gathered evidence', model.generate('Cat', [], allow_search=True))
                self.assertEqual(len(requests), 2)
                self.assertNotIn('tools', requests[1])
                self.assertIn('context_budget', json.dumps(requests[1], ensure_ascii=False))

    def test_long_history_does_not_count_against_tool_budget(self):
        for provider in ('openai', 'zai'):
            with self.subTest(provider=provider):
                requests = []
                def handler(request):
                    requests.append(request)
                    if len(requests) == 1:
                        return self.response(provider, call=('load_skill', '{"name":"nearby-places"}'), searched=True)
                    return self.response(provider, text='Cafe')
                model = self.model(provider, handler)
                # History near the conversation limit plus the full skill body still fits.
                history = [{'role': 'user', 'content': 'x' * 15000}]
                with patch('model.place_instructions', wraps=skill_body):
                    self.assertIn('Cafe', model.generate('Cat', history, allow_search=True))
                self.assertEqual(len(requests), 2)

    def test_search_before_skill_does_not_enable_another_search(self):
        for provider in ('openai', 'zai'):
            with self.subTest(provider=provider):
                bodies = []
                def handler(request):
                    bodies.append(json.loads(request.content))
                    return self.response(provider, call=('load_skill', '{"name":"nearby-places"}'), searched=True) if len(bodies) == 1 else self.response(provider, text='Cafe')
                answer = self.model(provider, handler).generate('Cat', [], allow_search=True, require_search=True)
                self.assertIn('https://example.com/cafe', answer)
                if provider == 'openai':
                    self.assertEqual([tool.get('name') for tool in bodies[1]['tools']], ['read_yandex_reviews'])
                else:
                    self.assertIn('search_web', json.dumps(bodies[1]['tools']))

    def test_repeated_skill_calls_reuse_body_until_model_round_limit(self):
        for provider in ('openai', 'zai'):
            with self.subTest(provider=provider):
                requests = []
                def handler(request):
                    requests.append(request)
                    if len(requests) <= 5:
                        return self.response(provider, call=('load_skill', '{"name":"nearby-places"}'), searched=True)
                    body = json.loads(request.content)
                    self.assertNotIn('tools', body)
                    return self.response(provider, text='Final answer')
                model = self.model(provider, handler)
                model.config = replace(model.config, max_input_chars=100000)
                with patch('model.place_instructions', wraps=skill_body) as load:
                    answer = model.generate('Cat', [], allow_search=True)
                    self.assertIn('Final answer', answer)
                load.assert_called_once_with()
                self.assertEqual(len(requests), 6)
                self.assertEqual(len(model.store.charges), 6)
