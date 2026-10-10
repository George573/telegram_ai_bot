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
from providers import SearchOutcome
from test_skill_loading import cards


class SearchTests(unittest.TestCase):
    def tool_response(self, name, arguments, call_id='call_search'):
        return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion',
            'model': 'glm-4.7', 'created': 1, 'choices': [{'index': 0, 'finish_reason': 'tool_calls',
            'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}]}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}})

    def test_explicit_search_roundtrip_after_skill_returns_real_evidence_and_charges(self):
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append((request.url.path, body))
            if request.url.path.endswith('/web_search'):
                self.assertEqual(body, {'search_engine': 'search-prime', 'search_query': 'кафе Щёлковская', 'count': 5})
                return httpx.Response(200, json={'search_result': [
                    {'title': 'Cafe', 'link': 'https://example.com/cafe', 'content': 'Evidence from public page'},
                    {'title': 'Unsafe', 'link': 'http://127.0.0.1/private', 'content': 'private'},
                    {'title': 'Duplicate', 'link': 'https://example.com/cafe', 'content': 'duplicate'}]})
            if len(requests) == 1:
                return self.tool_response('load_skill', '{"name":"nearby-places"}', 'call_skill')
            if len(requests) == 2:
                self.assertIn('search_web', json.dumps(body['tools']))
                return self.tool_response('search_web', '{"query":"кафе Щёлковская"}')
            tool_results = [message for message in body['messages'] if message['role'] == 'tool']
            self.assertEqual([message['tool_call_id'] for message in tool_results], ['call_skill', 'call_search'])
            evidence = json.loads(tool_results[-1]['content'])
            self.assertEqual(evidence['result_count'], 1)
            self.assertEqual(evidence['results'][0]['content'], 'Evidence from public page')
            self.assertNotIn('private', tool_results[-1]['content'])
            return self.response(text=cards('Found Cafe'))
        model = self.model(handler)
        with patch('model.event') as log:
            answer = model.generate('Find nearby cafes', [{'role': 'user', 'content': 'кафе Щёлковская'}], allow_search=True)
        self.assertIn('https://example.com/cafe', answer)
        self.assertEqual(len(requests), 4)
        # Three generations (no per-round hosted search fee) plus one search_web fee.
        self.assertEqual(model.store.total, Decimal('0.010291'))
        events = [call for call in log.call_args_list if call.args[0].startswith('web_search_')]
        self.assertEqual([call.args[0] for call in events], ['web_search_started', 'web_search_completed'])
        self.assertEqual(events[-1].kwargs['result_count'], 1)
        self.assertNotIn('Щёлковская', str(log.call_args_list))
        self.assertNotIn('Evidence from public page', str(log.call_args_list))

    def test_required_search_refusal_is_retried_then_rejected(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return self.response(text='Run /search yourself')
        with patch('model.event') as log:
            with self.assertRaises(ModelFailure):
                self.model(handler).generate('Lookup', [], allow_search=True, require_search=True)
        self.assertEqual(len(requests), 4)
        self.assertIn('Call search_web', requests[-1]['messages'][0]['content'])
        self.assertTrue(any(call.kwargs.get('reason') == 'search_not_executed' for call in log.call_args_list))

    def test_explicit_search_budget_rejection_does_not_call_search_endpoint(self):
        store = Allowance()
        reservations = []
        def reserve(amount, budget):
            reservations.append(amount)
            if len(reservations) == 2:
                raise BudgetExceeded()
        store.reserve = reserve
        requests = []
        def handler(request):
            requests.append(request.url.path)
            return self.tool_response('search_web', '{"query":"public query"}')
        with self.assertRaises(BudgetExceeded):
            self.model(handler, store).generate('Lookup', [], allow_search=True)
        self.assertEqual(len(requests), 1)
        self.assertFalse(any(path.endswith('/web_search') for path in requests))

    def test_empty_search_results_are_returned_as_empty_evidence(self):
        requests = []
        def handler(request):
            requests.append(request.url.path)
            if request.url.path.endswith('/web_search'):
                return httpx.Response(200, json={'search_result': []})
            if len(requests) == 1:
                return self.tool_response('search_web', '{"query":"public query"}')
            body = json.loads(request.content)
            self.assertEqual(json.loads(body['messages'][-1]['content'])['result_count'], 0)
            return self.response(text='No usable results')
        self.assertEqual(self.model(handler).generate('Lookup', [], allow_search=True, require_search=True), 'No usable results')
        self.assertEqual(len(requests), 3)

    def test_invalid_search_arguments_never_call_search_endpoint(self):
        for arguments in ('{"query":""}', '{"query":"public","query":"secret"}',
                          '{"query":"public","url":"http://127.0.0.1"}', '{"query":42}'):
            with self.subTest(arguments=arguments):
                requests = []
                def handler(request):
                    requests.append(request.url.path)
                    return self.tool_response('search_web', arguments)
                with self.assertRaises(ModelFailure):
                    self.model(handler).generate('Lookup', [], allow_search=True)
                self.assertEqual(len(requests), 1)

    def test_search_failure_hides_response_body_and_stops(self):
        def handler(request):
            if request.url.path.endswith('/web_search'):
                return httpx.Response(401, json={'error': {'message': 'synthetic-secret'}})
            return self.tool_response('search_web', '{"query":"public query"}')
        with patch('model.event') as log:
            with self.assertRaises(ModelFailure) as error:
                self.model(handler).generate('Lookup', [], allow_search=True)
        self.assertNotIn('synthetic-secret', str(error.exception))
        self.assertNotIn('synthetic-secret', str(log.call_args_list))
        failed = [call for call in log.call_args_list if call.args[0] == 'web_search_failed']
        self.assertEqual(failed[0].kwargs['status_code'], 401)

    def test_searches_across_rounds_have_no_two_call_limit(self):
        model = self.model(lambda _: self.fail('Mocked generation and search only'))
        from providers import Generation, ToolCall
        responses = [Generation('', True, tool_calls=(
            ToolCall(f'call{i}', 'search_web', '{"query":"public query"}'),)) for i in range(4)]
        responses.append(Generation('Done', True))
        with patch.object(model, '_generation_attempt', side_effect=responses), \
                patch.object(model.provider, 'search', return_value=SearchOutcome([])) as search:
            self.assertEqual(model.generate('Lookup', [], allow_search=True), 'Done')
        self.assertEqual(search.call_count, 4)

    def test_last_model_round_drains_batch_then_requests_tools_disabled_final_answer(self):
        # The final batch has 8 calls: 4 run, 4 get a short too_many_calls result.
        model = self.model(lambda _: self.fail('Mocked generation and search only'))
        from providers import Generation, ToolCall
        responses = [Generation('', True, tool_calls=(
            ToolCall(f'call{i}', 'search_web', '{"query":"public query"}'),)) for i in range(4)]
        responses.append(Generation('', True, tool_calls=tuple(
            ToolCall(f'last{i}', 'search_web', '{"query":"public query"}') for i in range(8))))
        responses.append(Generation('Done', True))
        with patch.object(model, '_generation_attempt', side_effect=responses) as generate, \
                patch.object(model.provider, 'search', return_value=SearchOutcome([])) as search:
            self.assertEqual(model.generate('Lookup', [], allow_search=True), 'Done')
        self.assertEqual(generate.call_count, 6)
        self.assertEqual(search.call_count, 8)
        rejected = [item for item in generate.call_args.kwargs['continuation'] if 'too_many_calls' in str(item)]
        self.assertEqual(len(rejected), 4)
        final = generate.call_args
        self.assertFalse(final.args[4])
        self.assertFalse(final.args[6])
        self.assertFalse(final.kwargs['skill_enabled'])
        self.assertEqual(len(final.kwargs['continuation']), 12)

    def test_final_answer_keeps_json_mode_and_repairs_without_research(self):
        generations = []
        searches = []
        def handler(request):
            body = json.loads(request.content)
            if request.url.path.endswith('/web_search'):
                searches.append(body)
                return httpx.Response(200, json={'search_result': [
                    {'title': 'Cafe', 'link': 'https://example.com/cafe', 'content': 'Public evidence'}]})
            generations.append(body)
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            if len(generations) <= 5:
                return self.tool_response('search_web', '{"query":"public cafes"}', f'call{len(generations)}')
            self.assertNotIn('tools', body)
            if len(generations) == 6:
                tools = [m for m in body['messages'] if m['role'] == 'tool']
                self.assertEqual([m['tool_call_id'] for m in tools], [f'call{i}' for i in range(1, 6)])
                self.assertTrue(all('Public evidence' in m['content'] for m in tools))
                return self.response(text='Invalid plain answer')
            self.assertEqual(len(generations), 7)
            self.assertTrue(any(m['role'] == 'assistant' and m['content'] == 'Invalid plain answer'
                                for m in body['messages']))
            self.assertTrue(any('JSON validation feedback' in str(m['content']) for m in body['messages']))
            return self.response(text='{"respond":true,"reply":"Cafe found"}')
        with patch('model.event') as log:
            decision = self.model(handler).decide_reply('Lookup', [], allow_search=True)
        self.assertIn('Cafe found', decision)
        self.assertIn('https://example.com/cafe', decision)
        self.assertEqual(len(searches), 5)
        names = [call.args[0] for call in log.call_args_list]
        self.assertIn('ai_final_answer_started', names)
        self.assertIn('ai_final_answer_completed', names)
        self.assertIn('ai_json_repair_completed', names)

    def test_final_answer_budget_failure_stops_without_research_retry(self):
        model = self.model(lambda _: self.fail('Mocked generation and search only'))
        from providers import Generation, ToolCall
        responses = [Generation('', True, tool_calls=(
            ToolCall(f'call{i}', 'search_web', '{"query":"public query"}'),)) for i in range(5)]
        responses.append(BudgetExceeded())
        with patch.object(model, '_generation_attempt', side_effect=responses) as generate, \
                patch.object(model.provider, 'search', return_value=SearchOutcome([])) as search, patch('model.event') as log:
            with self.assertRaises(BudgetExceeded):
                model.generate('Lookup', [], allow_search=True)
        self.assertEqual(generate.call_count, 6)
        self.assertEqual(search.call_count, 5)
        self.assertEqual(log.call_args.args[0], 'ai_final_answer_failed')

    def test_eight_search_calls_answer_every_id_but_run_only_four(self):
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append((request.url.path, body))
            if request.url.path.endswith('/web_search'):
                return httpx.Response(200, json={'search_result': [
                    {'title': 'Cafe', 'link': 'https://example.com/cafe', 'content': 'Public evidence'}]})
            if len(requests) == 1:
                return self.tool_response('load_skill', '{"name":"nearby-places"}', 'call_skill')
            if len(requests) == 2:
                document = self.tool_response('search_web', '{"query":"cafes"}', 'search1').json()
                for i in range(2, 9):
                    document['choices'][0]['message']['tool_calls'].append({'id': f'search{i}', 'type': 'function',
                        'function': {'name': 'search_web', 'arguments': '{"query":"exhibitions"}'}})
                return httpx.Response(200, json=document)
            tools = [message for message in body['messages'] if message['role'] == 'tool']
            self.assertEqual([message['tool_call_id'] for message in tools], ['call_skill'] + [f'search{i}' for i in range(1, 9)])
            assistants = [message for message in body['messages'] if message['role'] == 'assistant']
            self.assertEqual(len(assistants), 2)
            self.assertEqual(len(assistants[-1]['tool_calls']), 8)
            return self.response(text=cards('Found Cafe'))
        with patch('model.event') as log:
            answer = self.model(handler).generate('Lookup', [], allow_search=True)
        self.assertIn('https://example.com/cafe', answer)
        # Skill, batch, four searches, answer: calls 5-8 are rejected without searching.
        self.assertEqual(len(requests), 7)
        self.assertEqual(len([path for path, _ in requests if path.endswith('/web_search')]), 4)
        batches = [call for call in log.call_args_list if call.args[0] == 'tool_batch_received']
        self.assertEqual(batches[0].kwargs['tool_call_count'], 8)

    def test_batch_load_and_search_preserves_both_tool_results(self):
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append(request.url.path)
            if request.url.path.endswith('/web_search'):
                return httpx.Response(200, json={'search_result': []})
            if len(requests) == 1:
                document = self.tool_response('load_skill', '{"name":"nearby-places"}', 'skill').json()
                document['choices'][0]['message']['tool_calls'].append({'id': 'search', 'type': 'function',
                    'function': {'name': 'search_web', 'arguments': '{"query":"public cafes"}'}})
                return httpx.Response(200, json=document)
            tools = [message for message in body['messages'] if message['role'] == 'tool']
            self.assertEqual([message['tool_call_id'] for message in tools], ['skill', 'search'])
            return self.response(text=cards('No results'))
        self.assertIn('No results', self.model(handler).generate('Lookup', [], allow_search=True))
        self.assertEqual(len(requests), 3)

    def test_batch_is_fully_validated_before_any_search_or_skill_read(self):
        from providers import Generation, ToolCall
        cases = [((ToolCall('id1', 'search_web', '{"query":"public"}'),
                   ToolCall('id2', 'run_shell', '{}')), 'unknown_tool'),
                 ((ToolCall('id', 'load_skill', '{"name":"nearby-places"}'),
                   ToolCall('id', 'search_web', '{"query":"public"}')), 'duplicate_call_id'),
                 ((ToolCall('id1', 'load_skill', '{"name":"nearby-places"}'),
                   ToolCall('id2', 'search_web', '{"query":42}')), 'invalid_arguments')]
        for calls, error in cases:
            with self.subTest(error=error):
                model = self.model(lambda _: self.fail('No HTTP allowed'))
                with patch.object(model, '_generation_attempt', return_value=Generation('', True, tool_calls=calls)), \
                        patch.object(model.provider, 'search') as search, patch('model.skill_instructions') as load, \
                        patch('model.event') as log:
                    with self.assertRaises(ModelFailure):
                        model.generate('Lookup', [], allow_search=True)
                    search.assert_not_called()
                    load.assert_not_called()
                failures = [call for call in log.call_args_list if call.args[0] == 'skill_failed']
                self.assertEqual(failures[-1].kwargs['tool_error'], error)

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

    def test_only_local_function_tools_are_offered(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual({tool['type'] for tool in body['tools']}, {'function'})
            names = [tool['function']['name'] for tool in body['tools']]
            self.assertEqual(names, ['load_skill', 'search_web', 'read_yandex_reviews',
                                     'read_afisha_listing', 'read_afisha_event'])
            return self.response()
        self.assertEqual(self.model(handler).generate('Cat', [], allow_search=True), 'Hello')

    def test_structured_search_maps_site_and_recency_to_zai_filters_and_keeps_safe_results(self):
        bodies = []
        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            return httpx.Response(200, json={'search_result': [
                {'title': 'Fact', 'link': 'https://afisha.yandex.ru/moscow/theatre/x'},
                {'title': 'Unsafe', 'link': 'http://127.0.0.1/private'},
                {'title': 'Credential', 'link': 'https://secret@example.com'},
                {'title': 'Duplicate', 'link': 'https://afisha.yandex.ru/moscow/theatre/x'}]})
        model = self.model(handler)
        outcome = model.provider.search('спектакль', 'afisha.yandex.ru', 'week')
        self.assertEqual(bodies[0], {'search_engine': 'search-prime', 'search_query': 'спектакль', 'count': 5,
                                     'search_domain_filter': 'afisha.yandex.ru', 'search_recency_filter': 'oneWeek'})
        self.assertEqual([r['url'] for r in outcome.results], ['https://afisha.yandex.ru/moscow/theatre/x'])
        self.assertTrue(outcome.filters_applied)
        model.provider.search('кафе', 'yandex.ru/maps', None)
        self.assertEqual(bodies[1]['search_query'], 'кафе Яндекс Карты')
        self.assertEqual(bodies[1]['search_domain_filter'], 'yandex.ru')

    def test_rejected_zai_filters_fall_back_to_a_site_query(self):
        bodies = []
        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            if 'search_domain_filter' in body:
                return httpx.Response(400, json={'error': {'code': '1214', 'message': 'synthetic'}})
            return httpx.Response(200, json={'search_result': []})
        outcome = self.model(handler).provider.search('концерт', 'afisha.yandex.ru', None)
        self.assertEqual(bodies[1], {'search_engine': 'search-prime', 'count': 5,
                                     'search_query': 'site:afisha.yandex.ru концерт'})
        self.assertFalse(outcome.filters_applied)

    def test_search_call_passes_structured_arguments_and_echoes_them(self):
        model = self.model(lambda _: self.fail('Mocked generation and search only'))
        from providers import Generation, ToolCall
        call = ToolCall('c', 'search_web', '{"query":"джаз","site":"afisha.yandex.ru","recency":"month"}')
        with patch.object(model, '_generation_attempt', side_effect=[Generation('', True, tool_calls=(call,)),
                Generation('Done', True)]) as generate, \
                patch.object(model.provider, 'search', return_value=SearchOutcome([])) as search:
            model.generate('Lookup', [], allow_search=True)
        self.assertEqual(search.call_args.args, ('джаз', 'afisha.yandex.ru', 'month'))
        result = json.loads(generate.call_args.kwargs['continuation'][-1]['content'])
        self.assertEqual((result['site'], result['recency']), ('afisha.yandex.ru', 'month'))
        for bad in ('{"query":"x","site":"evil.example"}', '{"query":"x","recency":"decade"}'):
            with self.subTest(arguments=bad):
                bad_call = ToolCall('c', 'search_web', bad)
                with patch.object(model, '_generation_attempt', return_value=Generation('', True, tool_calls=(bad_call,))), \
                        patch.object(model.provider, 'search') as search, self.assertRaises(ModelFailure):
                    model.generate('Lookup', [], allow_search=True)
                search.assert_not_called()

    def test_disabled_search_sends_no_tool_or_fee(self):
        def handler(request):
            self.assertNotIn('tools', json.loads(request.content))
            return self.response()
        model = self.model(handler)
        self.assertEqual(model.generate('Cat', [], allow_search=False), 'Hello')
        self.assertEqual(model.store.total, Decimal('0.000097'))

    def test_enabled_search_without_a_search_call_charges_no_tool_fee(self):
        model = self.model(lambda _: self.response())
        self.assertEqual(model.generate('Cat', [], allow_search=True), 'Hello')
        self.assertEqual(model.store.total, Decimal('0.000097'))

    def test_search_budget_rejection_never_calls_network(self):
        store = Allowance()
        store.reserve = lambda *_: (_ for _ in ()).throw(BudgetExceeded())
        model = self.model(lambda _: self.fail('No request allowed'), store)
        with self.assertRaises(BudgetExceeded): model.generate('Cat', [], allow_search=True)

    def test_search_works_with_json_decisions_and_safe_sources(self):
        generations = []
        def handler(request):
            from skill_registry import instructions as _load_skill_body
            body = json.loads(request.content)
            if request.url.path.endswith('/web_search'):
                return httpx.Response(200, json={'search_result': [
                    {'title': 'Fact', 'link': 'https://example.com/fact'}, {'title': 'Bad', 'link': 'http://10.0.0.1/'}]})
            generations.append(body)
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            self.assertNotIn(_load_skill_body('nearby-places'), body['messages'][0]['content'])
            if len(generations) == 1:
                self.assertEqual(body['tools'][0]['function']['name'], 'load_skill')
                return self.tool_response('search_web', '{"query":"public fact","site":null,"recency":null}')
            return self.response(text='{"respond":true,"reply":"Fact"}')
        answer = self.model(handler).decide_reply('Cat', [], allow_search=True)
        self.assertIn('https://example.com/fact', answer)
        self.assertNotIn('10.0.0.1', answer)


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
