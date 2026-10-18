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
from skill_registry import instructions as _load_skill_body
def skill_body(name='nearby-places'): return _load_skill_body(name)
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


EVIDENCE = [{'title': 'Cafe', 'url': 'https://example.com/cafe', 'content': 'Evidence', 'publish_date': ''}]
SEARCH_CALL = ('search_web', '{"query":"public cafe","site":null,"recency":null}')


def cards(name='Cafe', link='https://example.com/cafe'):
    """A valid place_recommendations answer."""
    return json.dumps({'intro': 'Here you go', 'unconfirmed': [], 'outro': None, 'items': [
        {'name': name, 'address': 'Synthetic st., 1', 'rating': None, 'when': None,
         'text': 'Matches the request', 'links': [link]}]})


class SkillLoadingTests(unittest.TestCase):
    def model(self, provider, handler, allowance=None):
        http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(http.close)
        client = OpenAI(api_key='synthetic-key', base_url='https://provider.example/v1/',
                        http_client=http, max_retries=0)
        config = Config('synthetic', 'synthetic', frozenset({-1}), provider=provider,
                        model='gpt-6-luna' if provider == 'openai' else 'glm-4.7',
                        zai_key='synthetic', api_min_interval_seconds=0)
        model = Model(config, allowance or TrackedAllowance(), client)
        # Search itself is covered in test_search_typing; here it only supplies evidence.
        model._search_web = lambda query, site=None, recency=None: (EVIDENCE, True)
        return model

    def response(self, provider, *, call=None, text='Hello', searched=False):
        """searched=True with a call also requests search_web in the same batch."""
        calls = ([call] if call is not None else []) + ([SEARCH_CALL] if searched and call is not None else [])
        if provider == 'openai':
            output = []
            if calls:
                for index, (name, arguments) in enumerate(calls):
                    output.append({'type': 'function_call', 'id': f'fc_fake{index}', 'call_id': f'call_fake{index}',
                        'name': name, 'arguments': arguments, 'status': 'completed'})
            else:
                output.append({'type': 'message', 'id': 'msg_fake', 'role': 'assistant',
                    'status': 'completed', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]})
            return httpx.Response(200, json={'id': 'resp_fake', 'object': 'response', 'created_at': 1,
                'model': 'gpt-6-luna', 'status': 'completed', 'output': output,
                'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}})
        message = {'role': 'assistant', 'content': None if call is not None else text}
        if call is not None:
            message.update(tool_calls=[{'id': f'call_fake{index}', 'type': 'function',
                'function': {'name': name, 'arguments': arguments}} for index, (name, arguments) in enumerate(calls)],
                reasoning_content='Synthetic reasoning')
        return httpx.Response(200, json={'id': 'chat_fake', 'object': 'chat.completion', 'created': 1,
            'model': 'glm-4.7', 'choices': [{'index': 0, 'finish_reason': 'tool_calls' if call else 'stop',
                                         'message': message}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}})

    def test_unrelated_reply_never_reads_or_sends_skill_body(self):
        for provider in ('openai', 'zai'):
            with self.subTest(provider=provider):
                bodies = []
                def handler(request):
                    bodies.append(json.loads(request.content))
                    return self.response(provider)
                model = self.model(provider, handler)
                with patch('model.skill_instructions') as load:
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
                        return self.response(provider, call=('load_skill', '{"name":"nearby-places"}'), searched=True)
                    if len(bodies) == 2:
                        return self.response(provider, text='Malformed conversational answer')
                    return self.response(provider, text='{"respond":true,"reply":"Cafe, see example.com"}')
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
                    return self.response(provider, call=('load_skill', '{"name":"nearby-places"}'), searched=True) if len(bodies) == 1 else self.response(provider, text='{"respond":true,"reply":"Cafe"}')
                model = self.model(provider, handler)
                history = [{'role': 'user', 'content': 'Find a nearby cafe'}]
                with patch('model.skill_instructions', wraps=skill_body) as load:
                    self.assertIn('Cafe', model.decide_reply('Cat', history, allow_search=True))
                load.assert_called_once_with('nearby-places')
                self.assertEqual(len(bodies), 2)
                self.assertEqual(len(model.store.reservations), 2)
                self.assertEqual(len(model.store.charges), 2)
                self.assertEqual(model.store.total, sum(model.store.charges))
                self.assertEqual(history, [{'role': 'user', 'content': 'Find a nearby cafe'}])
                if provider == 'openai':
                    self.assertNotIn(skill_body(), bodies[0]['instructions'])
                    result = bodies[1]['input'][-2]
                    self.assertEqual(result['type'], 'function_call_output')
                    self.assertEqual(result['output'], skill_body())
                    self.assertEqual(bodies[0]['text'], bodies[1]['text'])
                    self.assertFalse(bodies[1]['store'])
                else:
                    self.assertNotIn(skill_body(), bodies[0]['messages'][0]['content'])
                    result = bodies[1]['messages'][-2]
                    self.assertEqual(result['role'], 'tool')
                    self.assertEqual(result['content'], skill_body())
                    self.assertEqual(bodies[1]['messages'][-3]['reasoning_content'], 'Synthetic reasoning')
                    # Both rounds offer tools, so neither uses JSON mode; the schema stays in the instructions.
                    self.assertNotIn('response_format', bodies[0])
                    self.assertNotIn('response_format', bodies[1])
                    self.assertIn('"respond"', bodies[1]['messages'][0]['content'])
                # load_skill stays offered for the other skill; this one is never re-read (load called once).
                self.assertIn('load_skill', json.dumps(bodies[1]['tools']))

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
                    with patch('model.skill_instructions') as load:
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
                    with patch('model.skill_instructions', side_effect=OSError('synthetic-secret') if limited == 'missing' else None,
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
                    return self.response(provider, text=cards('Answer from gathered evidence'))
                model = self.model(provider, handler)
                model.config = replace(model.config, max_tool_chars=4500)
                with patch('model.skill_instructions', wraps=skill_body):
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
                    return self.response(provider, text=cards())
                model = self.model(provider, handler)
                # History near the conversation limit plus the full skill body still fits.
                history = [{'role': 'user', 'content': 'x' * 15000}]
                with patch('model.skill_instructions', wraps=skill_body):
                    self.assertIn('Cafe', model.generate('Cat', history, allow_search=True))
                self.assertEqual(len(requests), 2)

    def test_search_before_skill_does_not_enable_another_search(self):
        for provider in ('openai', 'zai'):
            with self.subTest(provider=provider):
                bodies = []
                def handler(request):
                    bodies.append(json.loads(request.content))
                    return self.response(provider, call=('load_skill', '{"name":"nearby-places"}'), searched=True) if len(bodies) == 1 else self.response(provider, text=cards())
                answer = self.model(provider, handler).generate('Cat', [], allow_search=True, require_search=True)
                self.assertIn('https://example.com/cafe', answer)
                # Both providers keep the same local tools after a search; no hosted search exists.
                names = [tool.get('name') or tool['function']['name'] for tool in bodies[1]['tools']]
                self.assertIn('search_web', names)
                self.assertNotIn('web_search', json.dumps([tool.get('type') for tool in bodies[1]['tools']]))

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
                    return self.response(provider, text=cards('Final answer'))
                model = self.model(provider, handler)
                model.config = replace(model.config, max_input_chars=100000)
                with patch('model.skill_instructions', wraps=skill_body) as load:
                    answer = model.generate('Cat', [], allow_search=True)
                    self.assertIn('Final answer', answer)
                load.assert_called_once_with('nearby-places')
                self.assertEqual(len(requests), 6)
                self.assertEqual(len(model.store.charges), 6)

    def test_zai_research_after_skill_offers_tools_without_json_mode(self):
        # Z.ai JSON mode plus "return only JSON" made glm-5.3-flash skip every tool and write cards at once.
        bodies = []
        def handler(request):
            bodies.append(json.loads(request.content))
            if len(bodies) == 1:
                return self.response('zai', call=('load_skill', '{"name":"nearby-places"}'))
            if len(bodies) == 2:
                return self.response('zai', call=SEARCH_CALL)
            return self.response('zai', text=cards())
        model = self.model('zai', handler)
        model.config = replace(model.config, model='glm-5.3-flash')
        answer = model.generate('Cat', [{'role': 'user', 'content': 'Find a cafe'}], allow_search=True)
        self.assertIn('**Cafe**', answer)
        self.assertEqual(len(bodies), 3)
        for body in bodies[1:]:
            self.assertIn('tools', body)
            self.assertNotIn('response_format', body)
            self.assertNotIn('Format requirement', json.dumps(body['messages'], ensure_ascii=False))
            # The model still sees the card shape it must use for the final answer.
            self.assertIn('"unconfirmed"', body['messages'][0]['content'])
