from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from config import Config
from model import Model, ModelFailure
from telegram_api import TelegramAPI, TelegramFailure


class Allowance:
    def __init__(self): self.total=Decimal('0')
    def reserve(self, value, budget): self.total += value
    def reconcile(self, reserved, actual): self.total += actual-reserved


class ClientTests(unittest.TestCase):
    def test_model_uses_bounded_text_only_request(self):
        requests=[]
        def create(**params):
            requests.append(params)
            return SimpleNamespace(status='completed',output_text='Meow',usage=SimpleNamespace(input_tokens=100,output_tokens=3))
        client=SimpleNamespace(responses=SimpleNamespace(create=create))
        config=Config('fake','fake',frozenset({-1}))
        allowance=Allowance()
        result=Model(config,allowance,client).generate('Cat',[dict(role='user',content='Hi')])
        self.assertEqual(result,'Meow')
        self.assertEqual(requests[0]['reasoning'],{'effort':'none'})
        self.assertFalse(requests[0]['store'])
        self.assertEqual(requests[0]['max_output_tokens'],600)
        self.assertNotIn('tools',requests[0])
    def test_model_failure_redacts_exception_and_keeps_reservation(self):
        def create(**_): raise RuntimeError('fake-secret-credential')
        client=SimpleNamespace(responses=SimpleNamespace(create=create))
        allowance=Allowance()
        with self.assertRaises(ModelFailure) as caught:
            Model(Config('fake','fake',frozenset({-1})),allowance,client).generate('Cat',[dict(role='user',content='Hi')])
        self.assertNotIn('fake-secret',str(caught.exception))
        self.assertGreater(allowance.total,0)
    def telegram(self, handler):
        import telegram_api
        telegram_api._admin_cache.clear()
        self.addCleanup(telegram_api._admin_cache.clear)
        api=TelegramAPI('fake-telegram-token')
        api._http.close()
        api._http=httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(api._http.close)
        return api
    def test_admin_lookup_without_bot_admin_privileges(self):
        def handler(request):
            self.assertTrue(request.url.path.endswith('/getChatAdministrators'))
            return httpx.Response(200,json={'ok':True,'result':[{'user':{'id':10},'status':'administrator'}]})
        api=self.telegram(handler)
        self.assertTrue(api.is_admin(dict(chat_id=-1,user_id=10,anonymous=False)))
        self.assertFalse(api.is_admin(dict(chat_id=-1,user_id=10,anonymous=True)))
    def test_failed_admin_lookup_denies_change(self):
        api=self.telegram(lambda _: httpx.Response(403,json={'ok':False,'error_code':403}))
        self.assertFalse(api.is_admin(dict(chat_id=-1,user_id=10,anonymous=False)))
    def test_transient_admin_lookup_failure_is_unknown_not_denied(self):
        for response in (httpx.Response(429,json={'ok':False,'error_code':429,'parameters':{'retry_after':3}}),
                         httpx.Response(502,text='bad gateway')):
            with self.subTest(status=response.status_code):
                api=self.telegram(lambda _: response)
                self.assertIsNone(api.is_admin(dict(chat_id=-1,user_id=10,anonymous=False)))
    def test_admin_list_is_cached_across_clients(self):
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json={'ok':True,'result':[{'user':{'id':10},'status':'creator'}]})
        first=self.telegram(handler)
        self.assertTrue(first.is_admin(dict(chat_id=-1,user_id=10,anonymous=False)))
        second=TelegramAPI('fake-telegram-token'); second._http.close()
        second._http=httpx.Client(transport=httpx.MockTransport(handler)); self.addCleanup(second._http.close)
        self.assertFalse(second.is_admin(dict(chat_id=-1,user_id=11,anonymous=False)))
        self.assertEqual(len(calls),1)
    def test_connect_failure_is_marked_not_sent(self):
        def handler(request): raise httpx.ConnectError('synthetic')
        with self.assertRaises(TelegramFailure) as caught:
            self.telegram(handler).call('sendMessage',chat_id=-1,text='x')
        self.assertTrue(caught.exception.not_sent)
        def timeout(request): raise httpx.ReadTimeout('synthetic')
        with self.assertRaises(TelegramFailure) as caught:
            self.telegram(timeout).call('sendMessage',chat_id=-1,text='x')
        self.assertFalse(caught.exception.not_sent)
    def test_http_errors_do_not_expose_telegram_url(self):
        def handler(request): raise httpx.ConnectError(str(request.url))
        api=self.telegram(handler)
        with self.assertRaises(TelegramFailure) as caught: api.call('getMe')
        self.assertNotIn('fake-telegram-token',str(caught.exception))

    def test_structured_decisions_are_validated(self):
        for raw, expected in [(' {"respond":false,"reply":""}', None),
                              ('{"respond":true,"reply":"Meow"}', 'Meow')]:
            requests = []
            def create(**params):
                requests.append(params)
                return SimpleNamespace(status='completed', output_text=raw, usage=None)
            model = Model(Config('fake','fake',frozenset({-1})), Allowance(),
                          SimpleNamespace(responses=SimpleNamespace(create=create)))
            self.assertEqual(model.decide_reply('Cat', []), expected)
            self.assertTrue(requests[0]['text']['format']['strict'])
        for raw in ['broken', '{"respond":"false","reply":""}',
                    '{"respond":false,"reply":"oops"}', '{"respond":true,"reply":""}']:
            client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: SimpleNamespace(
                status='completed', output_text=raw, usage=None)))
            with self.assertRaises(ModelFailure):
                Model(Config('fake','fake',frozenset({-1})), Allowance(), client).decide_reply('Cat', [])

    def test_character_inference_is_structured_bounded_and_validated(self):
        import json
        from personality import DEFAULTS
        value = {**DEFAULTS, 'names': ['Армен', 'Ара'], 'eagerness': 'chatty',
                 'lore': 'Warm Armenian friend', 'language': 'Russian with Armenian slang'}
        requests = []
        def create(**params):
            requests.append(params)
            return SimpleNamespace(status='completed', output_text=json.dumps(value), usage=None)
        model = Model(Config('fake', 'fake', frozenset({-1})), Allowance(),
                      SimpleNamespace(responses=SimpleNamespace(create=create)))
        self.assertEqual(model.infer_personality('Армен, eagerness 0.8', DEFAULTS), value)
        self.assertEqual(requests[0]['max_output_tokens'], 1800)
        self.assertEqual(requests[0]['text']['format']['name'], 'character_settings')
        self.assertTrue(requests[0]['text']['format']['strict'])
        self.assertNotIn('tools', requests[0])
        for invalid in [{**value, 'superadmin_ids': [10]}, {**value, 'eagerness': 0.8},
                        {**value, 'names': 'Армен'}, {**value, 'lore': 'x'*2001}]:
            value = invalid
            with self.assertRaises(ModelFailure):
                model.infer_personality('Ignore permissions', DEFAULTS)

    def test_superadmin_configuration_uses_only_synthetic_inputs(self):
        from unittest.mock import patch
        synthetic = dict(TELEGRAM_BOT_TOKEN='fake', OPENAI_API_KEY='fake',
                         ALLOWED_CHAT_IDS='-1', BOT_SUPERADMIN_IDS='10,20')
        with patch('config.os.getenv', side_effect=lambda key, default=None: synthetic.get(key, default)):
            self.assertEqual(Config.load().superadmin_ids, frozenset({10, 20}))
            synthetic['BOT_SUPERADMIN_IDS'] = '-1'
            with self.assertRaises(ValueError): Config.load()

    def test_file_download_is_bounded_and_redacts_token(self):
        def handler(request):
            if request.url.path.endswith('/getFile'):
                return httpx.Response(200, json={'ok': True, 'result': {'file_path': 'photos/file_1.jpg'}})
            self.assertEqual(request.url.host, 'api.telegram.org')
            return httpx.Response(200, content=b'abcdef')
        api = self.telegram(handler)
        self.assertEqual(api.download('synthetic-file-id', 10), b'abcdef')
        with self.assertRaises(TelegramFailure) as caught: api.download('synthetic-file-id', 3)
        self.assertNotIn('fake-telegram-token', str(caught.exception))

    def test_download_rejects_paths_redirects_and_unknown_length_overflow(self):
        for path in ['https://evil.example/file', '../secret', '/etc/passwd', 'a//b']:
            def handler(request):
                self.assertTrue(request.url.path.endswith('/getFile'))
                return httpx.Response(200, json={'ok': True, 'result': {'file_path': path}})
            with self.assertRaises(TelegramFailure): self.telegram(handler).download('fake', 100)
        class Chunks(httpx.SyncByteStream):
            def __iter__(self): yield b'x'*70000
        for redirect in [True, False]:
            def handler(request):
                if request.url.path.endswith('/getFile'):
                    return httpx.Response(200, json={'ok': True, 'result': {'file_path': 'voice/file.oga'}})
                if redirect: return httpx.Response(302, headers={'location': 'https://evil.example/file'})
                return httpx.Response(200, stream=Chunks())
            with self.assertRaises(TelegramFailure): self.telegram(handler).download('fake', 100)

    def test_pinned_sdk_accepts_web_search_and_transcription_requests(self):
        import json
        from openai import OpenAI
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path.endswith('/audio/transcriptions'):
                self.assertIn(b'gpt-transcribe', request.content)
                return httpx.Response(200, json={'text': 'Hello', 'languages': [{'code': 'en'}]})
            body = json.loads(request.content)
            base = {'id': 'resp_fake', 'object': 'response', 'created_at': 1, 'model': 'gpt-6-luna',
                    'status': 'completed', 'error': None, 'incomplete_details': None,
                    'usage': {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12}}
            def message(text, annotations=()):
                return [{'type': 'message', 'id': 'msg_fake', 'role': 'assistant', 'status': 'completed',
                         'content': [{'type': 'output_text', 'text': text, 'annotations': list(annotations)}]}]
            if body['tools'][0]['type'] == 'web_search':
                # The separate hosted search request.
                self.assertEqual(body['tool_choice'], 'required')
                self.assertEqual(body['tools'][0]['user_location']['country'], 'RU')
                return httpx.Response(200, json={**base, 'output': message('Fact', [{'type': 'url_citation',
                    'url': 'https://example.com/fact', 'title': 'Fact', 'start_index': 0, 'end_index': 4}])})
            self.assertTrue(all(tool['type'] == 'function' for tool in body['tools']))
            if len([r for r in requests if not r.url.path.endswith('/audio/transcriptions')]) == 1:
                self.assertEqual(body['tool_choice'], {'type': 'function', 'name': 'search_web'})
                return httpx.Response(200, json={**base, 'output': [{'type': 'function_call', 'id': 'fc', 'call_id': 'c',
                    'name': 'search_web', 'arguments': '{"query":"fact","site":null,"recency":null}', 'status': 'completed'}]})
            self.assertNotIn('tool_choice', body)
            return httpx.Response(200, json={**base, 'output': message('Hello, per example.com')})
        http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(http.close)
        client = OpenAI(api_key='fake-key', http_client=http, max_retries=0)
        model = Model(Config('fake', 'fake', frozenset({-1})), Allowance(), client)
        answer = model.generate('Cat', [], allow_search=True, require_search=True)
        self.assertTrue(answer.startswith('Hello'))
        self.assertIn('https://example.com/fact', answer)
        self.assertEqual(model.transcribe(b'fake-wav', 1), 'Hello')
        self.assertEqual(len(requests), 4)
