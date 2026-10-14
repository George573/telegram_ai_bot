"""Polls and scheduled messages: local validation, model self-correction and delivery. No live requests."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import json
import sys
import unittest
import httpx
from openai import OpenAI
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from chat_actions import ChatActions, deliver_due, MOSCOW
from config import Config
from model import Model, ModelFailure
from telegram_api import TelegramAPI, TelegramFailure
from test_clients import Allowance
from test_service import MemoryStore, FakeTelegram, FakeModel, payload
from service import Service

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)  # 15:00 in Moscow


def poll(**overrides):
    value = {'question': 'Куда идём?', 'options': ['Пицца', 'Суши'], 'type': 'regular',
             'allows_multiple_answers': False, 'is_anonymous': False, 'correct_option_index': None,
             'explanation': None, 'send_at': None, 'delay_minutes': None}
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False)


class ScheduleStore:
    """In-memory stand-in for the scheduled_messages methods of Store."""
    def __init__(self):
        self.rows, self.next_id = [], 1

    def schedule(self, chat_id, thread_id, kind, content, send_at, *, created_by, reply_to, source_update,
                 fingerprint, status='pending', max_pending=25):
        for row in self.rows:
            if (row['source_update'], row['fingerprint']) == (source_update, fingerprint):
                return row, False
        if status == 'pending' and sum(r['status'] == 'pending' and r['chat_id'] == chat_id for r in self.rows) >= max_pending:
            return None, False
        row = dict(id=self.next_id, chat_id=chat_id, thread_id=thread_id, kind=kind, content=content,
                   send_at=send_at, status=status, created_by=created_by, reply_to=reply_to,
                   source_update=source_update, fingerprint=fingerprint, attempts=0, message_id=None)
        self.next_id += 1
        self.rows.append(row)
        return row, True

    def find(self, item_id):
        return next((row for row in self.rows if row['id'] == item_id), None)

    def scheduled(self, chat_id, limit=25):
        return sorted((r for r in self.rows if r['chat_id'] == chat_id and r['status'] == 'pending'),
                      key=lambda r: r['send_at'])[:limit]

    def scheduled_item(self, chat_id, item_id):
        row = self.find(item_id)
        return row if row and row['chat_id'] == chat_id and row['status'] == 'pending' else None

    def cancel_scheduled(self, chat_id, item_id):
        row = self.scheduled_item(chat_id, item_id)
        if row:
            row['status'] = 'cancelled'
        return row is not None

    def due_scheduled(self, limit=10):
        return [r for r in self.rows if r['status'] == 'pending' and r['send_at'] <= NOW][:limit]

    def mark_scheduled(self, item_id, status, message_id=None):
        row = self.find(item_id)
        row['status'] = status
        row['message_id'] = message_id or row['message_id']

    def retry_scheduled(self, item_id, seconds):
        row = self.find(item_id)
        row.update(status='pending', attempts=row['attempts'] + 1, send_at=NOW + timedelta(seconds=seconds))

    def delete_scheduled(self, item_id):
        self.rows.remove(self.find(item_id))


class PollTelegram:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.polls, self.texts = [], []

    def send_poll(self, chat_id, content, thread_id=None, reply_to=None):
        if self.failures:
            raise self.failures.pop(0)
        self.polls.append((chat_id, content, thread_id, reply_to))
        return {'message_id': 700 + len(self.polls)}

    def send_text(self, chat_id, text, thread_id=None, reply_to=None):
        if self.failures:
            raise self.failures.pop(0)
        self.texts.append((chat_id, text, thread_id, reply_to))
        return {'message_id': 800 + len(self.texts)}


class ChatActionTests(unittest.TestCase):
    def setUp(self):
        self.store, self.telegram = ScheduleStore(), PollTelegram()

    def actions(self, user_id=10, admin=False, update_id=1):
        request = {'chat_id': -1, 'thread_id': None, 'user_id': user_id, 'message_id': 2}
        return ChatActions(self.store, self.telegram, request, update_id, can_manage=lambda: admin, clock=lambda: NOW)

    def call(self, name, arguments, **options):
        return json.loads(self.actions(**options).call(name, arguments))

    def test_poll_is_posted_once_even_if_the_job_is_retried(self):
        result = self.call('send_poll', poll())
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['posted'])
        self.assertEqual(self.telegram.polls, [(-1, {'question': 'Куда идём?', 'options': ['Пицца', 'Суши'],
            'type': 'regular', 'is_anonymous': False, 'allows_multiple_answers': False}, None, 2)])
        # A crashed job regenerates the same call: no second poll.
        again = self.call('send_poll', poll())
        self.assertEqual(again['status'], 'ok')
        self.assertEqual(len(self.telegram.polls), 1)
        self.assertEqual(self.store.rows[0]['status'], 'sent')

    def test_poll_mistakes_name_the_field_and_ask_for_a_corrected_call(self):
        cases = [
            (poll(options=['Только один']), 'options', '2-10'),
            (poll(options=['Да', 'да']), 'options', 'distinct'),
            (poll(question=''), 'question', 'empty'),
            (poll(question='x' * 301), 'question', '301 characters'),
            (poll(type='quiz'), 'correct_option_index', '0 to 1'),
            (poll(type='quiz', correct_option_index=1, allows_multiple_answers=True), 'allows_multiple_answers', 'one right answer'),
            (poll(correct_option_index=0), 'correct_option_index', 'quizzes only'),
            (poll(send_at='2026-10-12T09:00', delay_minutes=5), 'send_at', 'only one'),
        ]
        for arguments, field, hint in cases:
            with self.subTest(field=field, hint=hint):
                result = self.call('send_poll', arguments)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['error'], 'invalid_arguments')
                self.assertEqual(result['field'], field)
                self.assertIn(hint, result['message'])
                self.assertIn('call send_poll again', result['next_step'])
        self.assertEqual(self.telegram.polls, [])
        self.assertEqual(self.store.rows, [])

    def test_malformed_or_incomplete_arguments_are_explained(self):
        result = self.call('schedule_message', '{"text": "hi", ')
        self.assertEqual(result['error'], 'malformed_arguments')
        self.assertIn('valid JSON', result['message'])
        result = self.call('schedule_message', '{"text": "hi", "when": "soon"}')
        self.assertEqual(result['error'], 'invalid_arguments')
        self.assertIn('Missing: delay_minutes, send_at', result['message'])
        self.assertIn('Unknown: when', result['message'])

    def test_telegram_rejection_is_returned_so_the_model_can_fix_it(self):
        self.telegram.failures = [TelegramFailure(400, description='Bad Request: poll question length must not exceed 255')]
        result = self.call('send_poll', poll())
        self.assertEqual(result['error'], 'telegram_rejected')
        self.assertIn('must not exceed 255', result['message'])
        self.assertIn('call send_poll again', result['next_step'])
        # The claim is released, so the corrected (or same) poll can be posted.
        self.assertEqual(self.store.rows, [])
        self.assertEqual(self.call('send_poll', poll())['status'], 'ok')

    def test_uncertain_poll_delivery_is_never_repeated(self):
        self.telegram.failures = [TelegramFailure(502)]
        result = self.call('send_poll', poll())
        self.assertEqual(result['error'], 'delivery_uncertain')
        self.assertIn('Do not call this tool again', result['next_step'])
        self.assertEqual(self.store.rows[0]['status'], 'unknown')
        self.assertEqual(self.call('send_poll', poll())['status'], 'ok')
        self.assertEqual(self.telegram.polls, [])

    def test_quiz_and_scheduled_poll(self):
        result = self.call('send_poll', poll(type='quiz', correct_option_index=1, explanation='Суши вкуснее',
                                             delay_minutes=90))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['send_at'], '2026-10-11 16:30 (Moscow)')
        row = self.store.rows[0]
        self.assertEqual((row['kind'], row['status']), ('poll', 'pending'))
        self.assertEqual(row['content']['correct_option_id'], 1)
        self.assertEqual(self.telegram.polls, [])

    def test_schedule_message_times(self):
        ok = self.call('schedule_message', json.dumps({'text': 'Напоминаю про билеты', 'send_at': '2026-10-12T09:00',
                                                       'delay_minutes': None}))
        self.assertEqual(ok, {'status': 'ok', 'scheduled_id': 1, 'send_at': '2026-10-12 09:00 (Moscow)'})
        self.assertEqual(self.store.rows[0]['send_at'], datetime(2026, 10, 12, 9, 0, tzinfo=MOSCOW))
        utc = self.call('schedule_message', json.dumps({'text': 'B', 'send_at': '2026-10-11T13:00Z', 'delay_minutes': None}))
        self.assertEqual(utc['send_at'], '2026-10-11 16:00 (Moscow)')
        past = self.call('schedule_message', json.dumps({'text': 'Hi', 'send_at': '2026-10-11T14:00', 'delay_minutes': None}))
        self.assertEqual(past['field'], 'send_at')
        self.assertIn('it is now 2026-10-11 15:00 (Moscow)', past['message'])
        self.assertIn('tomorrow', past['message'])
        for arguments, field in [({'text': 'Hi', 'send_at': None, 'delay_minutes': None}, 'send_at'),
                                 ({'text': 'Hi', 'send_at': 'завтра в 9', 'delay_minutes': None}, 'send_at'),
                                 ({'text': 'Hi', 'send_at': '2026-13-40T09:00', 'delay_minutes': None}, 'send_at'),
                                 ({'text': 'Hi', 'send_at': '2027-10-11T09:00', 'delay_minutes': None}, 'send_at'),
                                 ({'text': 'Hi', 'send_at': None, 'delay_minutes': 0}, 'delay_minutes'),
                                 ({'text': 'Hi', 'send_at': None, 'delay_minutes': 1.5}, 'delay_minutes'),
                                 ({'text': ' ', 'send_at': None, 'delay_minutes': 5}, 'text')]:
            with self.subTest(arguments=arguments):
                result = self.call('schedule_message', json.dumps(arguments, ensure_ascii=False))
                self.assertEqual((result['status'], result['field']), ('error', field))
        self.assertEqual(len(self.store.rows), 2)

    def test_pending_limit_and_per_request_limit(self):
        for index in range(25):
            self.store.schedule(-1, 0, 'text', {'text': str(index)}, NOW + timedelta(hours=1), created_by=1,
                                reply_to=None, source_update=50, fingerprint=str(index))
        result = self.call('schedule_message', json.dumps({'text': 'Hi', 'send_at': None, 'delay_minutes': 5}))
        self.assertEqual(result['error'], 'pending_limit')
        self.assertIn('Do not call this tool again', result['next_step'])
        self.store.rows.clear()
        actions = self.actions()
        statuses = [json.loads(actions.call('schedule_message', json.dumps(
            {'text': f'Hi {n}', 'send_at': None, 'delay_minutes': 5})))['status'] for n in range(6)]
        self.assertEqual(statuses, ['ok'] * 5 + ['error'])

    def test_list_and_cancel_permissions(self):
        self.call('schedule_message', json.dumps({'text': 'Мой', 'send_at': None, 'delay_minutes': 10}), user_id=10)
        self.call('schedule_message', json.dumps({'text': 'Чужой', 'send_at': None, 'delay_minutes': 5}), user_id=20)
        listing = self.call('list_scheduled_messages', '{}', user_id=10)
        self.assertEqual([(item['preview'], item['created_by_requester']) for item in listing['scheduled']],
                         [('Чужой', False), ('Мой', True)])
        denied = self.call('cancel_scheduled_message', '{"id": 2}', user_id=10)
        self.assertEqual(denied['error'], 'not_allowed')
        self.assertEqual(self.call('cancel_scheduled_message', '{"id": 1}', user_id=10)['status'], 'ok')
        self.assertEqual(self.call('cancel_scheduled_message', '{"id": 2}', user_id=10, admin=True)['status'], 'ok')
        missing = self.call('cancel_scheduled_message', '{"id": 2}', user_id=10, admin=True)
        self.assertEqual(missing['field'], 'id')
        self.assertIn('list_scheduled_messages', missing['message'])

    def test_results_and_logs_never_contain_content_in_events(self):
        with patch('chat_actions.event') as log:
            self.call('schedule_message', json.dumps({'text': 'Секретный текст', 'send_at': None, 'delay_minutes': 5}))
            self.call('send_poll', poll(options=['Одна опция']))
        self.assertEqual([c.args[0] for c in log.call_args_list], ['action_completed', 'action_failed'])
        self.assertNotIn('Секретный', str(log.call_args_list))
        self.assertNotIn('Одна опция', str(log.call_args_list))

    def test_deliver_due_sends_retries_and_gives_up(self):
        def add(kind, content, chat_id=-1):
            return self.store.schedule(chat_id, 0, kind, content, NOW - timedelta(seconds=1), created_by=10,
                                       reply_to=2, source_update=len(self.store.rows), fingerprint='f')[0]
        text, quiz, revoked = add('text', {'text': 'Пора!'}), add('poll', {'question': 'Q', 'options': ['a', 'b']}), \
            add('text', {'text': 'x'}, chat_id=-5)
        self.telegram.failures = [TelegramFailure(429, retry_after=7)]
        with patch('chat_actions.event') as log:
            self.assertEqual(deliver_due(self.store, self.telegram, frozenset({-1})), 3)
        self.assertEqual((text['status'], text['attempts'], text['send_at']), ('pending', 1, NOW + timedelta(seconds=7)))
        self.assertEqual((quiz['status'], quiz['message_id']), ('sent', 701))
        self.assertEqual(revoked['status'], 'revoked')
        self.assertEqual([c.args[0] for c in log.call_args_list], ['scheduled_failed', 'scheduled_delivered'])
        text['send_at'] = NOW
        deliver_due(self.store, self.telegram, frozenset({-1}))
        self.assertEqual(self.telegram.texts, [(-1, 'Пора!', None, 2)])
        later = add('text', {'text': 'y'})
        self.telegram.failures = [TelegramFailure(403)]
        deliver_due(self.store, self.telegram, frozenset({-1}))
        self.assertEqual(later['status'], 'failed')


class ModelActionLoopTests(unittest.TestCase):
    def tool_response(self, name, arguments, call_id):
        return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion', 'model': 'glm-4.7',
            'created': 1, 'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}]}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}})

    def text_response(self, text):
        return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion', 'model': 'glm-4.7',
            'created': 1, 'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}})

    def model(self, handler):
        http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(http.close)
        client = OpenAI(api_key='synthetic', base_url='https://api.z.ai/api/paas/v4/', http_client=http, max_retries=0)
        config = Config('synthetic', '', frozenset({-1}), provider='zai', model='glm-4.7', zai_key='synthetic',
                        api_min_interval_seconds=0)
        return Model(config, Allowance(), client)

    def test_model_corrects_a_rejected_poll_and_then_answers(self):
        store, telegram = ScheduleStore(), PollTelegram()
        actions = ChatActions(store, telegram, {'chat_id': -1, 'user_id': 10, 'message_id': 2}, 1, clock=lambda: NOW)
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                names = [tool['function']['name'] for tool in body['tools']]
                self.assertEqual(names, ['send_poll', 'schedule_message', 'list_scheduled_messages',
                                         'cancel_scheduled_message'])
                self.assertIn('Current Moscow date and time: 2026-10-11', body['messages'][0]['content'])
                return self.tool_response('send_poll', poll(options=['Пицца']), 'call_1')
            if len(requests) == 2:
                feedback = json.loads(body['messages'][-1]['content'])
                self.assertEqual((feedback['status'], feedback['field']), ('error', 'options'))
                return self.tool_response('send_poll', poll(options=['Пицца', 'Суши', 'Бургеры']), 'call_2')
            feedback = json.loads(body['messages'][-1]['content'])
            self.assertEqual(feedback['status'], 'ok')
            return self.text_response('Готово, голосуйте!')
        with patch('chat_actions.datetime') as clock:
            clock.now.return_value = NOW.astimezone(MOSCOW)
            clock.fromisoformat = datetime.fromisoformat
            answer = self.model(handler).generate('Persona', [{'role': 'user', 'content': 'Сделай опрос'}],
                                                  actions=actions)
        self.assertEqual(answer, 'Готово, голосуйте!')
        self.assertEqual(len(requests), 3)
        self.assertEqual(telegram.polls[0][1]['options'], ['Пицца', 'Суши', 'Бургеры'])

    def test_malformed_action_json_is_returned_to_the_model_not_a_failure(self):
        actions = ChatActions(ScheduleStore(), PollTelegram(), {'chat_id': -1, 'user_id': 10, 'message_id': 2}, 1,
                              clock=lambda: NOW)
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return self.tool_response('schedule_message', '{"text": "hi"', 'call_1')
            self.assertEqual(json.loads(requests[-1]['messages'][-1]['content'])['error'], 'malformed_arguments')
            return self.text_response('Не получилось, уточните время.')
        self.assertEqual(self.model(handler).generate('Persona', [], actions=actions), 'Не получилось, уточните время.')

    def test_action_tools_are_not_callable_without_actions(self):
        def handler(request):
            self.assertNotIn('tools', json.loads(request.content))
            return self.tool_response('send_poll', poll(), 'call_1')
        with self.assertRaises(ModelFailure):
            self.model(handler).generate('Persona', [])

    def test_request_log_says_whether_actions_were_offered(self):
        actions = ChatActions(ScheduleStore(), PollTelegram(), {'chat_id': -1, 'user_id': 10, 'message_id': 2}, 1)
        model = self.model(lambda request: self.text_response('Привет'))
        with patch('model.event') as log:
            model.generate('Persona', [], actions=actions)
            model.generate('Persona', [])
        started = [c.kwargs['actions_enabled'] for c in log.call_args_list if c.args[0] == 'ai_request_started']
        self.assertEqual(started, [True, False])

    def test_web_tools_and_action_tools_are_offered_together(self):
        actions = ChatActions(ScheduleStore(), PollTelegram(), {'chat_id': -1, 'user_id': 10, 'message_id': 2}, 1)
        def handler(request):
            names = [tool['function']['name'] for tool in json.loads(request.content)['tools']]
            self.assertEqual(names[-4:], ['send_poll', 'schedule_message', 'list_scheduled_messages',
                                          'cancel_scheduled_message'])
            self.assertIn('search_web', names)
            return self.text_response('Привет')
        self.model(handler).generate('Persona', [], allow_search=True, actions=actions)


class TelegramPollTests(unittest.TestCase):
    def api(self, handler):
        api = TelegramAPI('123:synthetic')
        api._http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(api._http.close)
        return api

    def test_send_poll_parameters_and_error_description(self):
        bodies = []
        def handler(request):
            bodies.append(json.loads(request.content))
            if len(bodies) == 1:
                return httpx.Response(200, json={'ok': True, 'result': {'message_id': 9}})
            return httpx.Response(400, json={'ok': False, 'error_code': 400, 'description': 'Bad Request: POLL_ANSWERS_INVALID'})
        api = self.api(handler)
        quiz = {'question': 'Q?', 'options': ['a', 'b'], 'type': 'quiz', 'is_anonymous': True,
                'allows_multiple_answers': False, 'correct_option_id': 1, 'explanation': 'because'}
        self.assertEqual(api.send_poll(-1, quiz, thread_id=5, reply_to=2), {'message_id': 9})
        self.assertEqual(bodies[0], {'chat_id': -1, 'message_thread_id': 5,
            'reply_parameters': {'message_id': 2, 'allow_sending_without_reply': True}, 'question': 'Q?',
            'options': [{'text': 'a'}, {'text': 'b'}], 'type': 'quiz', 'is_anonymous': True,
            'correct_option_id': 1, 'explanation': 'because'})
        with self.assertRaises(TelegramFailure) as caught:
            api.send_poll(-1, {**quiz, 'type': 'regular'})
        self.assertEqual(caught.exception.description, 'Bad Request: POLL_ANSWERS_INVALID')
        self.assertNotIn('123:synthetic', str(caught.exception))
        self.assertEqual(bodies[1]['allows_multiple_answers'], False)
        self.assertNotIn('correct_option_id', bodies[1])


class ServiceActionTests(unittest.TestCase):
    class ActionStore(MemoryStore, ScheduleStore):
        def __init__(self):
            MemoryStore.__init__(self)
            ScheduleStore.__init__(self)

    class ActionTelegram(FakeTelegram, PollTelegram):
        def __init__(self):
            FakeTelegram.__init__(self)
            PollTelegram.__init__(self)

    class RecordingModel(FakeModel):
        def generate(self, instructions, history, **kwargs):
            self.actions = kwargs.get('actions')
            return super().generate(instructions, history)

    def test_joined_all_mode_replies_get_actions_but_the_decision_does_not(self):
        class JoiningModel(self.RecordingModel):
            def decide_reply(self, instructions, history, **kwargs):
                self.decision_kwargs = kwargs
                return 'Давайте!'
        store, telegram, model = self.ActionStore(), self.ActionTelegram(), JoiningModel()
        store.chat['mode'] = 'all'
        service = Service(Config('fake-telegram', 'fake-openai', frozenset({-1})), store, telegram, model)
        service.accept(1, payload('составь опрос про пиццу'))
        for _ in range(8):
            if service.process_one() is None:
                break
        self.assertNotIn('actions', model.decision_kwargs)
        self.assertIsInstance(model.actions, ChatActions)
        self.assertIn('Meow', telegram.sent)

    def test_directed_replies_get_actions(self):
        store, telegram, model = self.ActionStore(), self.ActionTelegram(), self.RecordingModel()
        service = Service(Config('fake-telegram', 'fake-openai', frozenset({-1})), store, telegram, model)
        service.accept(1, payload('hello @Cat'))
        for _ in range(8):
            if service.process_one() is None:
                break
        self.assertIsInstance(model.actions, ChatActions)
        self.assertEqual(model.actions.update_id, 1)
        plain = Service(Config('fake-telegram', 'fake-openai', frozenset({-1})), MemoryStore(), FakeTelegram(), model)
        self.assertIsNone(plain.actions({'update_id': 1, 'payload': payload()}))


if __name__ == '__main__':
    unittest.main()
