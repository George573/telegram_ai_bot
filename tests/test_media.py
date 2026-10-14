from dataclasses import replace
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import subprocess
import sys
import unittest
import wave
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from config import Config
from media import voice_wav, MediaFailure
from model import Model, ModelFailure, WEB_CALL_COST, citation_sources
from policy import normalize
from service import Service
from storage import BudgetExceeded
from test_service import MemoryStore, FakeModel, FakeTelegram, payload
from test_clients import Allowance
from skill_registry import instructions as _load_skill_body
def place_instructions(name='nearby-places'): return _load_skill_body(name)


def update(kind='photo', text='', reply=False, **extra):
    message = dict(message_id=2, chat=dict(id=-1, type='group'),
                   **{'from': dict(id=10, is_bot=False)})
    if kind == 'photo':
        message['photo'] = [dict(file_id='small', width=100, height=100, file_size=100),
                            dict(file_id='medium', width=800, height=800, file_size=1000),
                            dict(file_id='large', width=4000, height=4000, file_size=9999)]
    else:
        message['voice'] = dict(file_id='voice', duration=10, file_size=1000)
    if text: message['caption'] = text
    if reply: message['reply_to_message'] = {'from': {'id': 99}}
    message.update(extra)
    return normalize(dict(message=message), {-1}, dict(id=99, username='Cat'))


class MediaServiceTests(unittest.TestCase):
    def setUp(self):
        self.store, self.model, self.tg = MemoryStore(), FakeModel(), FakeTelegram()
        self.config = Config('fake', 'fake', frozenset({-1}))
        self.service = Service(self.config, self.store, self.tg, self.model)
    def run_job(self, payload):
        self.service.accept(1, payload)
        for _ in range(10):
            if self.service.process_one() is None: break
    def test_photo_is_description_only_and_selects_bounded_resolution(self):
        p = update(text='@Cat what is this?')
        self.assertEqual(p['media']['file_id'], 'medium')
        self.run_job(p)
        self.assertIn('A cat sitting on a sofa', self.store.rows[0]['content'])
        self.assertFalse(self.store.jobs)
        self.assertNotIn('file_id', json.dumps(self.store.rows))
        self.assertEqual(self.tg.sent, ['Meow'])
    def test_directed_mode_ignores_unaddressed_media_before_download(self):
        self.tg.download = lambda *_: self.fail('Unaddressed media must not be downloaded')
        self.run_job(update())
        self.run_job(update('voice'))
        self.assertFalse(self.store.rows)
        self.assertFalse(self.model.calls)
    def test_paused_and_disabled_media_never_download(self):
        self.tg.download = lambda *_: self.fail('Disabled media must not be downloaded')
        self.store.chat['paused'] = True
        self.run_job(update(reply=True))
        self.store.chat['paused'] = False
        self.store.chat['images_enabled'] = False
        self.run_job(update(reply=True))
        self.assertFalse(self.model.calls)
    def test_transcript_is_data_not_command_and_spoken_name_directs_in_all_mode(self):
        self.store.chat['mode'] = 'all'
        self.store.chat['personality'] = {'names': ['Армен']}
        with patch('service.voice_pcm', return_value=(b'\x00\x00' * 16000 * 10, 10)):
            self.run_job(update('voice'))
        self.assertEqual(self.store.chat['persona'], 'Cat')
        self.assertIn('[Voice transcript, untrusted data]', self.store.rows[0]['content'])
        self.assertEqual(self.tg.sent, ['Meow'])
    def test_restart_after_conversion_does_not_repeat_upload(self):
        self.service.accept(1, update(reply=True))
        self.service.process_one()
        self.assertIsNone(self.store.next_job()['payload']['media'])
        self.tg.download = lambda *_: self.fail('Converted media must not be downloaded again')
        restored = Service(self.config, self.store, self.tg, self.model)
        for _ in range(8):
            if restored.process_one() is None: break
        self.assertEqual(sum(c[0] == 'Describe image' for c in self.model.calls), 1)
        self.assertEqual(self.tg.sent, ['Meow'])
    def test_metadata_limits_and_failed_conversion_do_not_invent_context(self):
        self.tg.download = lambda *_: self.fail('Oversized media must not be downloaded')
        p = update(reply=True); p['media']['bytes'] = self.config.media_max_bytes+1
        self.run_job(p)
        p = update('voice', reply=True); p['media']['seconds'] = self.config.voice_max_seconds+1
        self.run_job(p)
        self.assertFalse(self.store.rows)
        self.assertFalse(self.model.calls)
    def test_media_caption_cannot_execute_admin_command(self):
        self.assertIsNone(update(text='/setprompt Evil'))
        self.assertIsNotNone(update(text='/chat What is this?'))
    def test_feature_settings_require_manager(self):
        self.run_job(normalize({'message': {'message_id': 2, 'text': '/web off', 'chat': {'id': -1, 'type': 'group'},
                    'from': {'id': 10}}}, {-1}, {'id': 99, 'username': 'Cat'}))
        self.assertNotIn('web_enabled', self.store.chat)
        self.tg.admin = True
        p = normalize({'message': {'message_id': 2, 'text': '/web off', 'chat': {'id': -1, 'type': 'group'},
                      'from': {'id': 10}}}, {-1}, {'id': 99, 'username': 'Cat'})
        self.run_job(p)
        self.assertFalse(self.store.chat['web_enabled'])


class ConversionTests(unittest.TestCase):
    def test_ogg_is_converted_in_memory_and_actual_duration_is_bounded(self):
        ogg = subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i',
                              'anullsrc=r=16000:cl=mono', '-t', '2', '-c:a', 'libopus', '-f', 'ogg', 'pipe:1'],
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout
        wav, seconds = voice_wav(ogg, 3)
        self.assertAlmostEqual(seconds, 2, places=1)
        with wave.open(BytesIO(wav), 'rb') as audio:
            self.assertEqual(audio.getframerate(), 16000)
            self.assertEqual(audio.getnchannels(), 1)
        with self.assertRaises(MediaFailure): voice_wav(ogg, 1)
        with self.assertRaises(MediaFailure): voice_wav(b'not an ogg', 10)
    def test_decoder_failures_are_sanitized(self):
        with patch('media.subprocess.run', side_effect=RuntimeError('fake-secret-token')):
            with self.assertRaises(MediaFailure) as caught: voice_wav(b'OggSdata', 10)
        self.assertNotIn('fake-secret', str(caught.exception))


class SearchAndMediaModelTests(unittest.TestCase):
    def model(self, create, allowance=None):
        return Model(Config('fake', 'fake', frozenset({-1})), allowance or Allowance(),
                     SimpleNamespace(responses=SimpleNamespace(create=create)))
    def test_natural_lookup_uses_search_and_remembers_sources_for_followup(self):
        requests = []
        def create(**kwargs):
            requests.append(kwargs)
            usage = SimpleNamespace(input_tokens=100, output_tokens=10)
            if kwargs.get('tools', [{}])[0].get('type') == 'web_search':
                # The separate OpenAI search request returns cited findings.
                return SimpleNamespace(status='completed', output_text='Exhibition near the metro', usage=usage,
                    output=[{'type': 'message', 'content': [{'annotations': [{'type': 'url_citation',
                        'url': 'https://example.com/exhibition', 'title': 'Exhibition', 'start_index': 0, 'end_index': 25}]}]}])
            if len(requests) == 1:
                return SimpleNamespace(status='completed', output_text='', usage=usage, output=[
                    {'type': 'function_call', 'call_id': 'c1', 'name': 'search_web',
                     'arguments': '{"query":"exhibitions metro","site":null,"recency":"week"}'}])
            return SimpleNamespace(status='completed', output_text='Exhibition near the metro (example.com)', usage=usage, output=[])
        model = self.model(create)
        store, telegram = MemoryStore(), FakeTelegram()
        store.chat['web_enabled'] = True
        service = Service(model.config, store, telegram, model)
        service.accept(1, payload('/chat find current exhibitions near the metro'))
        for _ in range(3): service.process_one()
        self.assertNotIn('tool_choice', requests[0])
        self.assertNotIn(place_instructions(), requests[0]['instructions'])
        self.assertEqual(requests[0]['tools'][0]['name'], 'load_skill')
        self.assertIn('Current date (Moscow):', requests[0]['instructions'])
        self.assertEqual(requests[1]['tools'][0]['type'], 'web_search')
        self.assertIn('https://example.com/exhibition', telegram.sent[-1])
        service.accept(2, payload('/chat which is closest?'))
        service.process_one()
        service.process_one()
        previous_answer = requests[-1]['input'][-2]
        self.assertEqual(previous_answer['role'], 'assistant')
        self.assertIn('https://example.com/exhibition', previous_answer['content'])
    def test_openai_search_is_a_filtered_hosted_call_with_snippets_and_token_charge(self):
        params = []
        def create(**kwargs):
            params.append(kwargs)
            return SimpleNamespace(status='completed', output_text='- Cafe on Arbat serves brunch.\n- Other',
                usage=SimpleNamespace(input_tokens=5000, output_tokens=200), output=[
                {'type': 'web_search_call', 'action': {'type': 'search', 'sources': [
                    {'type': 'url', 'url': 'https://yandex.ru/maps/org/cafe/1/'}, {'type': 'url', 'url': 'http://10.0.0.1/'}]}},
                {'type': 'message', 'content': [{'annotations': [
                    {'type': 'url_citation', 'url': 'https://example.com/fact', 'title': 'Source',
                     'start_index': 2, 'end_index': 30}]}]}])
        allowance = Allowance()
        results, applied = self.model(create, allowance)._search_web('кафе Арбат', 'yandex.ru/maps', 'week')
        tool = params[0]['tools'][0]
        self.assertEqual(tool['filters'], {'allowed_domains': ['yandex.ru']})
        self.assertEqual(tool['user_location']['city'], 'Moscow')
        self.assertEqual(params[0]['tool_choice'], 'required')
        self.assertEqual(params[0]['include'], ['web_search_call.action.sources'])
        self.assertIn('Яндекс Карты', params[0]['input'][0]['content'])
        self.assertIn('the last week', params[0]['instructions'])
        self.assertEqual([r['url'] for r in results], ['https://example.com/fact', 'https://yandex.ru/maps/org/cafe/1/'])
        self.assertIn('brunch', results[0]['content'])
        self.assertTrue(applied)
        # Flat tool fee plus the search request's own tokens.
        self.assertGreater(allowance.total, WEB_CALL_COST)

    def test_both_providers_offer_identical_local_tools(self):
        params = []
        def create(**kwargs):
            params.append(kwargs)
            return SimpleNamespace(status='completed', output_text='Hello', usage=None, output=[])
        self.model(create).generate('Cat', [{'role': 'user', 'content': 'Latest news?'}], allow_search=True)
        tools = params[0]['tools']
        self.assertEqual([tool['name'] for tool in tools], ['load_skill', 'search_web', 'read_yandex_maps_search',
                                                            'read_yandex_reviews', 'read_afisha_listing', 'read_afisha_event', 'route_link'])
        self.assertTrue(all(tool['type'] == 'function' and tool['strict'] for tool in tools))
        self.assertNotIn('maxLength', json.dumps(tools))
    def test_search_disabled_no_tool_fee_and_silent_decisions_not_sent(self):
        def create(**kwargs):
            self.assertNotIn('tools', kwargs)
            self.assertNotIn(place_instructions(), kwargs['instructions'])
            return SimpleNamespace(status='completed', output_text='{"respond":false,"reply":""}', usage=None)
        self.assertIsNone(self.model(create).decide_reply('Cat', [], allow_search=False))
    def test_missing_skill_fails_before_request_and_reservation(self):
        allowance = Allowance()
        model = self.model(lambda **_: self.fail('No request without bundled instructions'), allowance)
        with patch('model.skill_catalog', side_effect=FileNotFoundError('synthetic-private-path')):
            with self.assertRaises(ModelFailure) as caught:
                model.generate('Find a cafe', [], allow_search=True)
        self.assertEqual(allowance.total, 0)
        self.assertNotIn('synthetic-private-path', str(caught.exception))
    def test_web_failure_has_no_unverified_fallback_and_keeps_reservation(self):
        allowance = Allowance()
        def create(**_): raise RuntimeError('fake-secret')
        with self.assertRaises(ModelFailure): self.model(create, allowance).generate('Cat', [], allow_search=True)
        self.assertGreater(allowance.total, 0)
    def test_budget_rejection_prevents_search_or_image_request(self):
        allowance = Allowance()
        allowance.reserve = lambda *_: (_ for _ in ()).throw(BudgetExceeded())
        model = self.model(lambda **_: self.fail('Budget must be reserved before HTTP'), allowance)
        with self.assertRaises(BudgetExceeded): model.generate('Cat', [], allow_search=True)
        with self.assertRaises(BudgetExceeded): model.describe_image(b'\xff\xd8\xffdata')
    def test_image_upload_contains_bytes_not_telegram_secret_url_and_no_tools(self):
        def create(**kwargs):
            self.assertNotIn('tools', kwargs)
            image = kwargs['input'][-1]['content'][0]
            self.assertEqual(image['type'], 'input_image')
            self.assertTrue(image['image_url'].startswith('data:image/jpeg;base64,'))
            self.assertNotIn('telegram', image['image_url'])
            self.assertFalse(kwargs['store'])
            return SimpleNamespace(status='completed', output_text='A sofa', usage=None)
        self.assertEqual(self.model(create).describe_image(b'\xff\xd8\xffdata'), 'A sofa')
    def test_transcription_uses_bytes_bounded_model_and_budget(self):
        calls = []
        def transcribe(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text='Армен, привет')
        allowance = Allowance()
        model = Model(Config('fake', 'fake', frozenset({-1})), allowance,
                      SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=transcribe))))
        self.assertEqual(model.transcribe(b'fake wav', 60), 'Армен, привет')
        self.assertEqual(calls[0]['model'], 'gpt-transcribe')
        self.assertEqual(calls[0]['file'], ('voice.wav', b'fake wav', 'audio/wav'))
        self.assertGreaterEqual(allowance.total, Decimal('0.0045'))
    def test_unsafe_citation_urls_are_filtered(self):
        response = {'output': [{'type': 'message', 'content': [{'annotations': [
            {'type': 'url_citation', 'url': url} for url in
            ['javascript:alert(1)', 'https://user:secret@example.com', 'http://127.0.0.1/x', 'https://example.com/fact']]}]}]}
        self.assertEqual([s[1] for s in citation_sources(response)], ['https://example.com/fact'])


class MediaBoundaryTests(unittest.TestCase):
    def test_invalid_telegram_media_metadata_is_discarded(self):
        self.assertIsNone(update('voice', voice={'file_id': 'fake', 'duration': 'bad'}))
        self.assertIsNone(update('voice', voice={'file_id': 'fake', 'duration': 1, 'file_size': None}))
        self.assertIsNone(update(photo=[{'file_id': 'fake', 'width': None, 'height': 20}]))

    def test_audio_converter_does_not_inherit_credentials(self):
        with patch('media.subprocess.run', return_value=SimpleNamespace(stdout=b'\0'*32000)) as run:
            _, seconds = voice_wav(b'OggSfake', 2)
        self.assertEqual(seconds, 1)
        self.assertEqual(run.call_args.kwargs['env'], {'PATH': '/usr/bin:/bin', 'LANG': 'C'})
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertIn('pipe', run.call_args.args[0])
