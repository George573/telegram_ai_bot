"""Provider contracts tested with synthetic keys and mocked network calls only."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import sys
import unittest
import httpx
from openai import OpenAI
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from config import Config
from providers import THINKING_ALLOWANCE
from model import Model, ModelFailure, estimate_cost
from storage import BudgetExceeded
from test_clients import Allowance
from test_service import MemoryStore, FakeTelegram, FakeModel, payload
from service import Service


class ProviderTests(unittest.TestCase):
    def test_glm5_selection_requests_and_budget_accounting(self):
        inputs = {'TELEGRAM_BOT_TOKEN': 'synthetic', 'ALLOWED_CHAT_IDS': '-1',
                  'MODEL_PROVIDER': 'zai', 'ZAI_API_KEY': 'synthetic-zai'}
        for name, input_rate, output_rate in [('glm-5', Decimal('1.00'), Decimal('3.20')),
                                               ('glm-5.1', Decimal('1.40'), Decimal('4.40')),
                                               ('glm-5.3-flash', Decimal('0.15'), Decimal('0.50'))]:
            with self.subTest(model=name):
                inputs['ZAI_MODEL'] = name
                with patch('config.os.getenv', side_effect=lambda key, default=None: inputs.get(key, default)):
                    self.assertEqual(Config.load().model, name)
                expected_reservation = (Decimal(2053) * input_rate * Decimal('1.25')
                                        + Decimal(600) * output_rate) / 1000000
                self.assertEqual(estimate_cost(name, 'Cat', [], 600, 'zai'), expected_reservation)
                model, store, calls = self.model(model=name)
                self.assertEqual(model.generate('Cat', []), 'Hello')
                self.assertEqual(calls[0]['model'], name)
                if name == 'glm-5.3-flash':
                    # Reasoning shares max_tokens with the answer, so it gets its own allowance.
                    self.assertEqual(calls[0]['extra_body'], {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'low'})
                    self.assertEqual(calls[0]['max_tokens'], 600 + THINKING_ALLOWANCE)
                else:
                    self.assertEqual(calls[0]['extra_body'], {'thinking': {'type': 'disabled'}})
                    self.assertEqual(calls[0]['max_tokens'], 600)
                self.assertEqual(store.total, (100 * input_rate * Decimal('1.25')
                                              + 10 * output_rate) / 1000000)

    def config(self, model='glm-4.7-flash'):
        return Config('synthetic-telegram', '', frozenset({-1}),
                      provider='zai', zai_key='synthetic-zai', model=model, zai_vision_model='glm-4.6v-flashx')

    def model(self, text='Hello', finish='stop', usage=True, model='glm-4.7-flash'):
        calls = []
        def create(**params):
            calls.append(params)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
                message=SimpleNamespace(content=text))],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10) if usage else None)
        store = Allowance()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return Model(self.config(model), store, client), store, calls

    def test_provider_selection_requires_only_selected_key(self):
        inputs = {'TELEGRAM_BOT_TOKEN': 'synthetic', 'ALLOWED_CHAT_IDS': '-1',
                  'MODEL_PROVIDER': 'zai', 'ZAI_API_KEY': 'synthetic-zai'}
        def getenv(key, default=None):
            self.assertNotEqual(key, 'OPENAI_API_KEY')
            return inputs.get(key, default)
        with patch('config.os.getenv', side_effect=getenv):
            config = Config.load()
            self.assertEqual(config.provider, 'zai')
            self.assertEqual(config.model, 'glm-4.7')
            self.assertEqual(config.openai_key, '')
            for bad in ['gpt-6-luna', 'unknown']:
                inputs['ZAI_MODEL'] = bad
                with self.assertRaises(ValueError): Config.load()
            inputs.pop('ZAI_MODEL')
            inputs.pop('ZAI_API_KEY')
            inputs['OPENAI_API_KEY'] = 'synthetic-other'
            with self.assertRaises(ValueError): Config.load()
            inputs['MODEL_PROVIDER'] = 'unknown'
            with self.assertRaises(ValueError): Config.load()

    def test_default_openai_selection_is_backward_compatible(self):
        inputs = {'TELEGRAM_BOT_TOKEN': 'synthetic', 'ALLOWED_CHAT_IDS': '-1',
                  'OPENAI_API_KEY': 'synthetic-openai'}
        with patch('config.os.getenv', side_effect=lambda key, default=None: inputs.get(key, default)):
            config = Config.load()
        self.assertEqual((config.provider, config.model), ('openai', 'gpt-6-luna'))
        self.assertEqual(config.zai_key, '')

    def test_factory_uses_standard_zai_endpoint_and_selected_key(self):
        with patch('providers.OpenAI') as sdk:
            Model(self.config(), Allowance())
        sdk.assert_called_once_with(api_key='synthetic-zai',
            base_url='https://api.z.ai/api/paas/v4/', max_retries=0, timeout=45)

    def test_sdk_serializes_zai_native_request_and_usage(self):
        def handler(request):
            self.assertEqual(str(request.url), 'https://api.z.ai/api/paas/v4/chat/completions')
            self.assertEqual(request.headers['authorization'], 'Bearer synthetic-zai')
            body = json.loads(request.content)
            self.assertEqual(body['thinking'], {'type': 'disabled'})
            self.assertEqual(body['max_tokens'], 600)
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            self.assertEqual(body['messages'][0]['role'], 'system')
            self.assertIn('"required": ["respond", "reply"]', body['messages'][0]['content'])
            self.assertNotIn('store', body)
            self.assertNotIn('tools', body)
            return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion',
                'created': 1, 'model': 'glm-4.7-flashx', 'choices': [{'index': 0,
                'finish_reason': 'stop', 'message': {'role': 'assistant',
                'content': '{"respond":true,"reply":"Hello"}'}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}})
        http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(http.close)
        client = OpenAI(api_key='synthetic-zai', base_url='https://api.z.ai/api/paas/v4/',
                        http_client=http, max_retries=0)
        allowance = Allowance()
        model = Model(self.config('glm-4.7-flashx'), allowance, client)
        self.assertEqual(model.decide_reply('Cat', [], allow_search=False), 'Hello')
        self.assertEqual(allowance.total, Decimal('0.00001275'))

    def test_glm53_structured_request_serialization_and_validation(self):
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(body['model'], 'glm-5.3-flash')
            self.assertEqual(body['thinking'], {'type': 'enabled'})
            self.assertEqual(body['reasoning_effort'], 'low')
            self.assertEqual(body['max_tokens'], 600 + THINKING_ALLOWANCE)
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            self.assertEqual(body['messages'][-1]['role'], 'system')
            self.assertIn('not the schema itself', body['messages'][-1]['content'])
            if len(requests) == 1:
                self.assertEqual(body['messages'][-2]['content'], 'Hello, Cat!')
            else:
                self.assertEqual(body['messages'][-3], {'role': 'assistant', 'content': 'plain reply'})
                self.assertIn('expected_value', body['messages'][-2]['content'])
            content = 'plain reply' if len(requests) == 1 else '{"respond":true,"reply":"Hello"}'
            return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion',
                'created': 1, 'model': 'glm-5.3-flash', 'choices': [{'index': 0,
                'finish_reason': 'stop', 'message': {'role': 'assistant',
                    'content': content, 'reasoning_content': 'Synthetic reasoning, never a decision'}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}})
        http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(http.close)
        client = OpenAI(api_key='synthetic-zai', base_url='https://api.z.ai/api/paas/v4/',
                        http_client=http, max_retries=0)
        config = replace(self.config('glm-5.3-flash'), api_min_interval_seconds=0)
        model = Model(config, Allowance(), client)
        history = [{'role': 'user', 'content': 'Hello, Cat!'}]
        self.assertEqual(model.decide_reply('Stay in character', history, allow_search=True), 'Hello')
        self.assertEqual(len(requests), 2)
        self.assertIn('tools', requests[0])
        self.assertNotIn('tools', requests[1])
        self.assertEqual(history, [{'role': 'user', 'content': 'Hello, Cat!'}])

    def test_text_summary_and_missing_usage_keep_bounded_reservations(self):
        model, store, calls = self.model(usage=False, model='glm-4.7-flashx')
        self.assertEqual(model.generate('Summarize', [], output_tokens=1800, max_chars=3), 'Hel')
        self.assertEqual(calls[0]['max_tokens'], 1800)
        self.assertGreater(store.total, 0)
        model, store, calls = self.model()
        self.assertEqual(model.generate('Cat', [], allow_search=False), 'Hello')
        self.assertEqual(store.total, 0)
        self.assertNotIn('tools', calls[0])

    def test_json_decisions_and_personality_still_use_local_validation(self):
        from personality import DEFAULTS
        for text, expected in [('{"respond":false,"reply":""}', None),
                               ('{"respond":true,"reply":"Meow"}', 'Meow')]:
            model, _, calls = self.model(text)
            self.assertEqual(model.decide_reply('Cat', []), expected)
            self.assertEqual(calls[0]['response_format'], {'type': 'json_object'})
        for text in ['broken', '{"respond":true,"reply":""}',
                     '{"respond":false,"reply":"bad"}', '{"respond":true,"reply":"Hi","admin":true}']:
            with self.assertRaises(ModelFailure): self.model(text)[0].decide_reply('Cat', [])
        model, _, _ = self.model(json.dumps(DEFAULTS))
        self.assertEqual(model.infer_personality('Cat', DEFAULTS), DEFAULTS)
        model, _, _ = self.model(json.dumps({**DEFAULTS, 'admin': True}))
        with self.assertRaises(ModelFailure): model.infer_personality('Cat', DEFAULTS)

    def test_incomplete_empty_and_failed_responses_are_safe(self):
        for text, finish in [('Hello', 'length'), ('Hello', 'tool_calls'), ('', 'stop')]:
            with self.assertRaises(ModelFailure): self.model(text, finish)[0].generate('Cat', [])
        def fail(**params): raise RuntimeError('synthetic-secret')
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail)))
        allowance = Allowance()
        model = Model(self.config('glm-4.7-flashx'), allowance, client)
        with self.assertRaises(ModelFailure) as caught: model.generate('Cat', [])
        self.assertNotIn('synthetic-secret', str(caught.exception))
        self.assertGreater(allowance.total, 0)

    def test_invalid_or_oversized_media_never_calls_or_reserves(self):
        model, store, calls = self.model()
        with self.assertRaises(ModelFailure): model.describe_image(b'not-jpeg')
        with self.assertRaises(ModelFailure): model.transcribe(b'synthetic', 31)
        self.assertEqual(calls, [])
        self.assertEqual(store.total, 0)

    def test_budget_and_schema_context_limits_prevent_requests(self):
        model, store, calls = self.model(model='glm-4.7-flashx')
        store.reserve = lambda *_: (_ for _ in ()).throw(BudgetExceeded())
        with self.assertRaises(BudgetExceeded): model.generate('Cat', [])
        self.assertEqual(calls, [])
        model, _, calls = self.model()
        model.config = replace(model.config, max_input_chars=10)
        with self.assertRaises(ModelFailure): model.decide_reply('Cat', [])
        self.assertEqual(calls, [])

    def test_service_reports_and_allows_provider_features(self):
        store, telegram = MemoryStore(), FakeTelegram(admin=True)
        service = Service(self.config(), store, telegram, FakeModel())
        status = service.status(-1)
        self.assertIn('Провайдер: zai', status)
        self.assertIn('Голосовые до 120 с', status)  # Longer than Z.ai's 30 s: audio is split into pieces.
        self.assertIn('Фото: вкл · Голосовые: вкл · Поиск: выкл', status)
        for feature in ['images', 'voice', 'web']:
            store.enqueue(1, payload('/' + feature + ' on'))
            service.command(store.next_job())
            self.assertTrue(store.chat[feature + '_enabled'])
            store.finish(1)

    def test_service_rejects_audio_beyond_voice_max_seconds_before_download(self):
        for kind in ['voice', 'video_note']:
            store, telegram = MemoryStore(), FakeTelegram()
            telegram.download = lambda *_: self.fail('Unsupported media must not download')
            model = FakeModel()
            service = Service(self.config(), store, telegram, model)
            message = payload()
            message['media'] = {'kind': kind, 'file_id': 'synthetic', 'bytes': 10, 'seconds': 121}
            store.enqueue(1, message)
            service.prepare(store.next_job())
            self.assertEqual(store.next_job()['phase'], 'generated')
            self.assertEqual(model.calls, [])
            self.assertEqual(store.rows, [])

    def test_service_zai_replies_use_same_internal_api_without_search(self):
        store, telegram = MemoryStore(), FakeTelegram()
        store.chat['web_enabled'] = False
        model, allowance, calls = self.model()
        service = Service(self.config(), store, telegram, model)
        store.enqueue(1, payload())
        service.prepare(store.next_job())
        service.generate(store.next_job())
        self.assertEqual(store.next_job()['answer'], 'Hello')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('tools', calls[0])
        self.assertEqual(allowance.total, 0)

    def test_service_zai_natural_lookup_offers_function_search(self):
        store, telegram = MemoryStore(), FakeTelegram()
        store.chat['web_enabled'] = True
        model, allowance, calls = self.model()
        service = Service(self.config(), store, telegram, model)
        service.accept(1, payload('/chat find current exhibitions'))
        for _ in range(3): service.process_one()
        self.assertEqual({tool['type'] for tool in calls[0]['tools']}, {'function'})
        self.assertIn('search_web', [tool['function']['name'] for tool in calls[0]['tools']])
        self.assertEqual(telegram.sent[-1], 'Hello')
        self.assertEqual(store.rows[-1]['role'], 'assistant')

    def test_zai_vision_sdk_request_uses_photo_model_and_its_prices(self):
        import base64
        photo = b'\xff\xd8\xffsynthetic'
        def handler(request):
            self.assertEqual(request.url.host, 'api.z.ai')
            body = json.loads(request.content)
            self.assertEqual(body['model'], 'glm-4.6v-flashx')
            self.assertEqual(body['max_tokens'], 600)
            self.assertEqual(body['thinking'], {'type': 'disabled'})
            self.assertNotIn('response_format', body)
            part = body['messages'][-1]['content'][0]
            self.assertEqual(part['type'], 'image_url')
            self.assertEqual(part['image_url']['url'], 'data:image/jpeg;base64,' + base64.b64encode(photo).decode())
            return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion',
                'created': 1, 'model': body['model'], 'choices': [{'index': 0,
                'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'A cat'}}],
                'usage': {'prompt_tokens': 1000, 'completion_tokens': 10, 'total_tokens': 1010}})
        http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(http.close)
        client = OpenAI(api_key='synthetic-zai', base_url='https://api.z.ai/api/paas/v4/',
                        http_client=http, max_retries=0)
        store = Allowance()
        model = Model(self.config(), store, client)
        self.assertEqual(model.describe_image(photo), 'A cat')
        # The free text rate must never be used to price a paid image call.
        self.assertEqual(store.total, Decimal('0.000054'))

    def test_vision_missing_usage_failure_and_budget_keep_paid_reservation(self):
        photo = b'\xff\xd8\xffsynthetic'
        model, store, calls = self.model(usage=False)
        self.assertEqual(model.describe_image(photo), 'Hello')
        self.assertEqual(calls[0]['model'], 'glm-4.6v-flashx')
        self.assertGreater(store.total, Decimal('0.001'))
        model, store, calls = self.model(finish='length', usage=False)
        with self.assertRaises(ModelFailure): model.describe_image(photo)
        self.assertGreater(store.total, 0)
        model, store, calls = self.model()
        store.reserve = lambda *_: (_ for _ in ()).throw(BudgetExceeded())
        with self.assertRaises(BudgetExceeded): model.describe_image(photo)
        self.assertEqual(calls, [])
        model, store, calls = self.model()
        with self.assertRaises(ModelFailure): model.describe_image(b'\xff\xd8\xff' + b'x' * (5 * 1024 * 1024))
        self.assertEqual(calls, [])

    def test_zai_transcription_sdk_request_and_measured_duration_charge(self):
        def handler(request):
            self.assertEqual(str(request.url), 'https://api.z.ai/api/paas/v4/audio/transcriptions')
            self.assertIn(b'glm-asr-2512', request.content)
            self.assertIn(b'filename="voice.wav"', request.content)
            self.assertIn(b'audio/wav', request.content)
            self.assertNotIn(b'gpt-transcribe', request.content)
            self.assertNotIn(b'response_format', request.content)
            return httpx.Response(200, json={'id': 'synthetic', 'created': 1,
                                           'model': 'glm-asr-2512', 'text': 'Hello'})
        http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(http.close)
        client = OpenAI(api_key='synthetic-zai', base_url='https://api.z.ai/api/paas/v4/',
                        http_client=http, max_retries=0)
        store = Allowance()
        model = Model(self.config(), store, client)
        self.assertEqual(model.transcribe(b'synthetic-wav', 30), 'Hello')
        self.assertEqual(store.total, Decimal('31') * Decimal('0.0024') * Decimal('1.25') / 60)

    def test_zai_transcription_failures_are_redacted_and_reservation_retained(self):
        for text in ['', 'x' * 8001, None]:
            def create(**params):
                if text is None: raise RuntimeError('synthetic-secret')
                return SimpleNamespace(text=text)
            client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
            store = Allowance()
            model = Model(self.config(), store, client)
            with self.assertRaises(ModelFailure) as caught: model.transcribe(b'synthetic', 10)
            self.assertNotIn('synthetic-secret', str(caught.exception))
            self.assertGreater(store.total, 0)

    def test_vision_configuration_validation_and_alternative_rates(self):
        inputs = {'TELEGRAM_BOT_TOKEN': 'synthetic', 'ALLOWED_CHAT_IDS': '-1',
                  'MODEL_PROVIDER': 'zai', 'ZAI_API_KEY': 'synthetic-zai'}
        with patch('config.os.getenv', side_effect=lambda key, default=None: inputs.get(key, default)):
            self.assertEqual(Config.load().zai_vision_model, 'glm-4.6v')
            for vision in ['glm-4.6v', 'glm-4.6v-flash']:
                inputs['ZAI_VISION_MODEL'] = vision
                self.assertEqual(Config.load().zai_vision_model, vision)
            inputs['ZAI_VISION_MODEL'] = 'glm-4.7-flash'
            with self.assertRaises(ValueError): Config.load()
        model, store, calls = self.model()
        model.provider.vision_model = 'glm-4.6v'
        self.assertEqual(model.describe_image(b'\xff\xd8\xffsynthetic'), 'Hello')
        self.assertEqual(calls[0]['model'], 'glm-4.6v')
        self.assertEqual(store.total, Decimal('0.0000465'))

    def test_service_zai_photo_becomes_text_in_memory(self):
        store, telegram = MemoryStore(), FakeTelegram()
        model, allowance, calls = self.model('A cat')
        service = Service(self.config(), store, telegram, model)
        message = payload()
        message['media'] = {'kind': 'photo', 'file_id': 'synthetic', 'bytes': 10}
        store.enqueue(1, message)
        service.prepare(store.next_job())
        converted = store.next_job()['payload']
        self.assertIsNone(converted['media'])
        self.assertIn('A cat', converted['text'])
        self.assertEqual(calls[0]['model'], 'glm-4.6v-flashx')
        service.prepare(store.next_job())
        self.assertIn('A cat', store.rows[0]['content'])

    def test_long_zai_voice_is_split_into_pieces_under_the_30_second_limit(self):
        store, telegram = MemoryStore(), FakeTelegram()
        model = FakeModel()
        pieces = []
        model.transcribe = lambda audio, seconds: pieces.extend(audio) or 'Длинное сообщение'
        service = Service(self.config(), store, telegram, model)
        message = payload()
        message['media'] = {'kind': 'voice', 'file_id': 'synthetic', 'bytes': 10, 'seconds': 70}
        store.enqueue(1, message)
        with patch('service.voice_pcm', return_value=(b'\x01\x00' * 16000 * 70, 70)) as decode:
            service.prepare(store.next_job())
        self.assertEqual(decode.call_args.args[1], 120)
        self.assertEqual(len(pieces), 3)
        self.assertTrue(all(seconds <= 28 for _, seconds in pieces))
        self.assertAlmostEqual(sum(seconds for _, seconds in pieces), 70, places=3)
        self.assertIn('Voice transcript', store.next_job()['payload']['text'])

    def test_paid_text_model_uses_native_api_and_correct_costs(self):
        model, store, calls = self.model(model='glm-4.7')
        self.assertEqual(model.generate('Cat', []), 'Hello')
        self.assertEqual(calls[0]['model'], 'glm-4.7')
        self.assertEqual(calls[0]['extra_body'], {'thinking': {'type': 'disabled'}})
        self.assertEqual(store.total, Decimal('0.000097'))

    def test_paid_defaults_and_explicit_old_models_remain_selectable(self):
        inputs = {'TELEGRAM_BOT_TOKEN': 'synthetic', 'ALLOWED_CHAT_IDS': '-1',
                  'MODEL_PROVIDER': 'zai', 'ZAI_API_KEY': 'synthetic-zai'}
        with patch('config.os.getenv', side_effect=lambda key, default=None: inputs.get(key, default)):
            config = Config.load()
            self.assertEqual((config.model, config.zai_vision_model), ('glm-4.7', 'glm-4.6v'))
            self.assertEqual(config.openai_key, '')
            inputs['ZAI_MODEL'] = 'glm-4.7-flash'
            inputs['ZAI_VISION_MODEL'] = 'glm-4.6v-flashx'
            config = Config.load()
            self.assertEqual((config.model, config.zai_vision_model), ('glm-4.7-flash', 'glm-4.6v-flashx'))


class ThinkingBudgetTests(unittest.TestCase):
    def test_thinking_model_reply_has_room_and_reservation_covers_it(self):
        helper = ProviderTests()
        model, store, calls = helper.model(model='glm-5.3-flash')
        reserved = []
        store.reserve = lambda amount, budget: reserved.append(amount)
        model.generate('Cat', [])
        self.assertEqual(calls[0]['max_tokens'], 600 + THINKING_ALLOWANCE)
        self.assertEqual(calls[0]['extra_body']['reasoning_effort'], 'low')
        self.assertEqual(reserved[0], estimate_cost('glm-5.3-flash', calls[0]['messages'][0]['content'], [],
                                                    600 + THINKING_ALLOWANCE, 'zai'))
