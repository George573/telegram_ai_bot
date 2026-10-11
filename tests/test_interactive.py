"""Russian UI, buttons, reply prompts, progress and crash notices. Synthetic data and mocked Telegram only."""
import ast
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from config import Config
from model import ModelFailure, ModelText
from policy import callback_action, normalize, normalize_callback
from service import Service
import skill_registry
from telegram_api import TelegramAPI, TelegramFailure
import texts
from test_service import MemoryStore, FakeTelegram, FakeModel, payload

BOT = dict(id=99, username='Cat')


def press(data, user_id=10, message_id=700, from_bot=True, chat=-1):
    update = {'update_id': 1, 'callback_query': {'id': 'cb1', 'data': data, 'from': {'id': user_id, 'is_bot': False},
              'message': {'message_id': message_id, 'chat': {'id': chat, 'type': 'supergroup'},
                          'from': {'id': 99 if from_bot else 5, 'is_bot': True}}}}
    return normalize_callback(update, {-1}, BOT)


class ParsingTests(unittest.TestCase):
    def test_button_data_is_allowlisted(self):
        self.assertEqual(callback_action('v1:set:web:on'), ('/web', 'on', ''))
        self.assertEqual(callback_action('v1:panel:features'), ('/settings', 'features', ''))
        self.assertEqual(callback_action('v1:reset'), ('/resetmemory', '', ''))
        self.assertIsNone(callback_action('v1:skill:city-events'))  # Skills are not buttons any more.
        self.assertEqual(callback_action('v1:more:closer')[2], texts.FOLLOW_UPS['closer'])
        for bad in ('v1:set:persona:x', 'v1:run:shell', 'v1:panel:secrets', 'v1:help:nowhere', 'v2:help:talk', 'v1:ask:resetmemory', 'v1:set:web:maybe',
                    'v1:' + 'x' * 70, None, 'v1:help:../../etc'):
            with self.subTest(data=bad):
                self.assertIsNone(callback_action(bad))

    def test_callbacks_only_from_our_messages_in_allowed_chats(self):
        self.assertEqual(press('v1:set:web:on')['command'], '/web')
        self.assertIsNone(press('v1:set:web:on', from_bot=False))
        self.assertIsNone(press('v1:set:web:on', chat=-2))
        result = press('v1:more:more')
        self.assertEqual((result['command'], result['directed'], result['reply_to_bot']), ('', True, True))

    def test_typed_messages_aliases_and_internal_commands(self):
        def message(text, **extra):
            return normalize({'update_id': 1, 'message': {'message_id': 2, 'text': text, 'chat': {'id': -1, 'type': 'group'},
                              'from': {'id': 10, 'is_bot': False}, **extra}}, {-1}, BOT)
        self.assertEqual(message('помощь')['command'], '/help')
        self.assertEqual(message('@Cat навыки!')['command'], '/skills')
        self.assertEqual(message('/chat')['command'], '/chat')  # Asked for input, no longer turned into /help.
        self.assertIsNone(message('/ask /historylimit'))
        self.assertIsNone(message('/confirm yes'))
        replied = message('ответ', reply_to_message={'message_id': 55, 'from': {'id': 99}})
        self.assertEqual(replied['reply_to_message_id'], 55)
        self.assertIsNone(message('ответ', reply_to_message={'message_id': 55, 'from': {'id': 7}})['reply_to_message_id'])


class InteractiveServiceTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tg, self.model = MemoryStore(), FakeTelegram(), FakeModel()
        self.config = Config('synthetic', 'synthetic', frozenset({-1}))
        self.service = Service(self.config, self.store, self.tg, self.model)
        self.update = 1

    def send(self, item):
        self.service.accept(self.update, item)
        self.update += 1
        for _ in range(6):
            if self.service.process_one() is None:
                break

    def test_main_menu_describes_the_bot_and_branches(self):
        self.store.chat['personality'] = {'names': ['Армен']}
        self.send(payload('/start'))
        menu = self.tg.outbox[-1]
        self.assertIn('🏠 Главное меню', menu['text'])
        self.assertIn('ИИ-собеседник', menu['text'])
        self.assertIn('Армен', menu['text'])
        data = [d for row in menu['markup'] for _, d in row]
        self.assertEqual(data, ['v1:help:talk', 'v1:help:skills', 'v1:help:privacy'])  # No admin panel for members.
        self.send(press('v1:help:talk', message_id=menu['message_id']))
        self.assertEqual(self.tg.edits[-1]['message_id'], menu['message_id'])
        self.assertIn('Как со мной общаться', self.tg.edits[-1]['text'])
        self.assertEqual(self.tg.callbacks, [texts.CALLBACK_ACK])

    def test_every_page_has_back_and_few_buttons(self):
        self.tg.admin = True
        pages = ['talk', 'search', 'skills', 'privacy', 'admin', 'persona', 'commands']
        panels = ['settings', 'features', 'mode', 'memory']
        for name in pages + panels:
            with self.subTest(page=name):
                self.send(press(f"v1:{'panel' if name in panels else 'help'}:{name}", message_id=900))
                markup = self.tg.edits[-1]['markup']
                nav = markup[-1]
                self.assertEqual(nav[0][0], texts.BUTTONS['back'])
                deep = name not in {'talk', 'skills', 'privacy', 'admin'}
                self.assertEqual(any(label == texts.BUTTONS['main'] for label, _ in nav), deep)
                self.assertLessEqual(sum(len(row) for row in markup[:-1]), 4)
        self.send(payload('/help'))
        self.assertIn('v1:help:admin', str(self.tg.outbox[-1]['markup']))

    def test_admin_pages_are_hidden_and_refused_for_members(self):
        self.send(press('v1:help:admin'))
        self.assertEqual(self.tg.callbacks[-1], texts.ONLY_ADMINS)
        self.assertFalse(self.store.jobs)
        self.send(payload('/settings'))
        self.assertEqual(self.tg.sent[-1], texts.ADMIN_REQUIRED)

    def test_skills_page_explains_the_model_chooses_and_has_no_skill_buttons(self):
        self.send(payload('/skills'))
        listing = self.tg.outbox[-1]
        for name in skill_registry.SKILL_NAMES:
            self.assertIn(skill_registry.title(name), listing['text'])
        self.assertIn('решаю сам', listing['text'])
        self.assertIn('Поиск в этой группе выключен', listing['text'])
        self.assertEqual(listing['markup'], [[(texts.BUTTONS['back'], 'v1:help:home')]])

    def test_reply_prompt_fills_command_only_for_its_requester(self):
        self.tg.admin = True
        self.send(payload('/historylimit'))
        prompt = self.tg.outbox[-1]
        self.assertEqual(prompt['text'], texts.PROMPTS['/historylimit'])
        other = payload('100', user_id=11); other.update(reply_to_bot=True, reply_to_message_id=prompt['message_id'])
        self.send(other)
        self.assertEqual(self.store.chat['history_limit'], 10)
        mine = payload('20'); mine.update(reply_to_bot=True, reply_to_message_id=prompt['message_id'])
        self.send(mine)
        self.assertEqual(self.store.chat['history_limit'], 20)

    def test_menu_command_and_pinned_menu_key(self):
        self.send(payload('/menu'))
        self.assertIn('🏠 Главное меню', self.tg.outbox[-1]['text'])
        self.tg.admin = True
        self.send(press('v1:help:admin', message_id=910))
        self.assertIn('v1:menukey:on', str(self.tg.edits[-1]['markup']))
        self.service.privacy_enabled = True
        self.send(press('v1:menukey:on', message_id=910))
        pinned = self.tg.outbox[-1]
        self.assertEqual(pinned['markup'], {'keyboard': texts.MENU_KEY_PRIVATE})  # Privacy on: must be a command.
        self.assertTrue(self.store.chat['menu_button'])
        self.service.privacy_enabled = False
        self.send(press('v1:menukey:on', message_id=910))
        self.assertEqual(self.tg.outbox[-1]['markup'], {'keyboard': texts.MENU_KEY_OPEN})
        self.send(payload('🏠 Меню'))
        self.assertIn('🏠 Главное меню', self.tg.outbox[-1]['text'])
        self.send(press('v1:menukey:off', message_id=910))
        self.assertEqual(self.tg.outbox[-1]['markup'], {'remove': True})
        self.assertFalse(self.store.chat['menu_button'])

    def test_command_list_is_short_and_old_commands_are_gone_or_hidden(self):
        self.assertEqual([c for c, _ in texts.PUBLIC_COMMANDS], ['menu', 'chat', 'search'])
        self.assertEqual([c for c, _ in texts.ADMIN_COMMANDS], ['menu', 'chat', 'search', 'settings'])
        def message(text):
            return normalize({'update_id': 1, 'message': {'message_id': 2, 'text': text, 'chat': {'id': -1, 'type': 'group'},
                              'from': {'id': 10, 'is_bot': False}}}, {-1}, BOT)
        self.assertIsNone(message('/setprompt x'))
        self.assertIsNone(message('/menukey on'))  # Button-only.
        self.assertEqual(message('/status')['command'], '/settings')
        self.assertEqual(message('/start')['command'], '/help')
        self.assertEqual(message('/web on')['command'], '/web')  # Hidden shortcut still works.

    def test_non_admin_settings_press_is_refused_without_a_job(self):
        self.send(press('v1:set:web:on'))
        self.assertEqual(self.tg.callbacks, [texts.ONLY_ADMINS])
        self.assertFalse(self.store.jobs)
        self.assertNotIn('web_enabled', self.store.chat)

    def test_admin_toggle_edits_its_panel_with_updated_buttons(self):
        self.tg.admin = True
        self.send(payload('/settings'))
        panel = self.tg.outbox[-1]
        self.assertIn('Поиск: выкл', panel['text'])
        self.assertIn('v1:panel:features', str(panel['markup']))
        self.send(press('v1:panel:features', message_id=panel['message_id']))
        self.assertIn('v1:set:web:on', str(self.tg.edits[-1]['markup']))
        self.send(press('v1:set:web:on', message_id=panel['message_id']))
        self.assertTrue(self.store.chat['web_enabled'])
        self.assertEqual(self.tg.edits[-1]['message_id'], panel['message_id'])
        self.assertIn('v1:set:web:off', str(self.tg.edits[-1]['markup']))  # Still the features panel.
        self.send(press('v1:set:context:all', message_id=panel['message_id']))
        self.assertIn('v1:set:context:directed', str(self.tg.edits[-1]['markup']))

    def test_reset_button_asks_for_confirmation_first(self):
        self.tg.admin = True
        self.store.append(-1, 'user', 'old')
        self.send(press('v1:reset', message_id=901))
        ask = self.tg.outbox[-1]
        self.assertIn('Очистить всю память', ask['text'])
        self.assertEqual(len(self.store.rows), 1)
        self.send(press('v1:confirm:yes', message_id=ask['message_id']))
        self.assertEqual(self.store.rows, [])

    def test_forgetme_needs_the_requesters_confirmation(self):
        self.send(payload('/forgetme'))
        ask = self.tg.outbox[-1]
        self.assertIn('Удалить ваши сообщения', ask['text'])
        self.assertFalse(self.store.forgotten)
        self.send(press('v1:confirm:yes', user_id=11, message_id=ask['message_id']))
        self.assertEqual(self.tg.callbacks[-1], texts.NOT_YOURS)
        self.assertFalse(self.store.forgotten)
        self.send(press('v1:confirm:yes', message_id=ask['message_id']))
        self.assertEqual(self.store.forgotten, [10])
        self.assertEqual(self.tg.edits[-1]['text'], texts.FORGET_DONE)
        self.send(press('v1:confirm:yes', message_id=ask['message_id']))
        self.assertEqual(self.tg.callbacks[-1], texts.EXPIRED)  # Already used.

    def test_progress_message_becomes_the_answer(self):
        def generate(instructions, history, on_research=None, **kw):
            on_research('read_afisha_listing')
            on_research('search_web')  # Only the first call shows progress.
            return ModelText('Вот спектакли', [], cards=True)
        self.model.generate = generate
        self.send(payload())
        progress = self.tg.outbox[0]
        self.assertEqual(progress['text'], texts.PROGRESS['read_afisha_listing'])
        self.assertEqual(len(self.tg.outbox), 1)
        self.assertEqual(self.tg.edits[-1]['message_id'], progress['message_id'])
        self.assertEqual(self.tg.edits[-1]['text'], 'Вот спектакли')
        self.assertIn('v1:more:more', str(self.tg.edits[-1]['markup']))

    def test_failed_progress_edit_falls_back_to_a_new_message(self):
        def generate(instructions, history, on_research=None, **kw):
            on_research('search_web')
            return 'Ответ'
        self.model.generate = generate
        def broken_edit(*_):
            raise TelegramFailure(400)
        self.tg.edit = broken_edit
        self.send(payload())
        self.assertEqual([m['text'] for m in self.tg.outbox], [texts.PROGRESS['search_web'], 'Ответ'])

    def test_model_failure_replaces_progress_instead_of_leaving_it(self):
        def generate(instructions, history, on_research=None, **kw):
            on_research('search_web')
            raise ModelFailure()
        self.model.generate = generate
        self.send(payload())
        self.assertEqual(self.tg.edits[-1]['text'], texts.MODEL_FAILED)

    def test_crash_notice_is_one_message_updated_until_giving_up(self):
        failures = iter([1, 2, 3])
        self.store.record_failure = lambda update_id: next(failures)
        def crash(*_, **__): raise RuntimeError('synthetic')
        self.model.generate = crash
        self.service.accept(1, payload())
        for _ in range(5):
            if self.service.process_one() is None:
                break
        self.assertEqual([m['text'] for m in self.tg.outbox], [texts.CRASH_RETRYING])
        self.assertEqual(self.tg.edits[-1]['text'], texts.CRASH_GAVE_UP)
        self.assertEqual(self.tg.edits[-1]['message_id'], self.tg.outbox[0]['message_id'])
        self.assertFalse(self.store.jobs)

    def test_ambient_crash_stays_silent(self):
        self.store.chat['mode'] = 'all'
        def crash(*_, **__): raise RuntimeError('synthetic')
        self.model.decide_reply = crash
        self.send(payload('просто болтаем'))
        self.assertFalse(self.tg.outbox)

    def test_follow_up_button_asks_again_and_clears_old_buttons(self):
        calls = []
        self.model.generate = lambda instructions, history, **kw: calls.append(history) or 'Ещё три места'
        self.send(press('v1:more:cheaper', message_id=800))
        self.assertIn(texts.FOLLOW_UPS['cheaper'], calls[-1][-1]['content'])
        self.assertEqual(self.tg.cleared, [800])
        self.assertEqual(self.tg.outbox[-1]['reply_to'], 800)


class TelegramApiTests(unittest.TestCase):
    def api(self, handler):
        api = TelegramAPI('synthetic-token')
        api._http.close()
        api._http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(api._http.close)
        return api

    def test_markup_force_reply_edit_callbacks_and_commands(self):
        bodies = []
        def handler(request):
            body = json.loads(request.content)
            bodies.append((request.url.path.rsplit('/', 1)[-1], body))
            if request.url.path.endswith('/editMessageText') and body['text'] == 'same':
                return httpx.Response(400, json={'ok': False, 'error_code': 400,
                                                 'description': 'Bad Request: message is not modified'})
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 9}})
        api = self.api(handler)
        api.send({'chat_id': -1, 'message_id': 2}, 'x', markup=[[('A', 'v1:skills')]])
        self.assertEqual(bodies[-1][1]['reply_markup'], {'inline_keyboard': [[{'text': 'A', 'callback_data': 'v1:skills'}]]})
        api.send({'chat_id': -1, 'message_id': 2}, 'x', force_reply='Что ищем')
        self.assertEqual(bodies[-1][1]['reply_markup'], {'force_reply': True, 'selective': True,
                                                         'input_field_placeholder': 'Что ищем'})
        api.send({'chat_id': -1, 'message_id': 2}, 'x', markup={'keyboard': '/menu'})
        self.assertEqual(bodies[-1][1]['reply_markup'], {'keyboard': [[{'text': '/menu'}]], 'is_persistent': True,
                                                         'resize_keyboard': True})
        api.send({'chat_id': -1, 'message_id': 2}, 'x', markup={'remove': True})
        self.assertEqual(bodies[-1][1]['reply_markup'], {'remove_keyboard': True})
        self.assertEqual(api.edit(-1, 9, 'same'), {'message_id': 9})
        api.answer_callback('cb', '⏳')
        self.assertEqual(bodies[-1], ('answerCallbackQuery', {'callback_query_id': 'cb', 'text': '⏳'}))
        api.set_commands(texts.PUBLIC_COMMANDS, texts.ADMIN_COMMANDS)
        scopes = [body['scope']['type'] for name, body in bodies if name == 'setMyCommands']
        self.assertEqual(scopes, ['default', 'all_group_chats', 'all_chat_administrators'])
        self.assertTrue(all(re.search('[а-яё]', c['description'], re.I) for _, b in bodies if _ == 'setMyCommands'
                            for c in b['commands']))


class RussianTextTests(unittest.TestCase):
    def test_service_sends_no_english_literals(self):
        """Every string handed to users goes through texts.py."""
        tree = ast.parse((ROOT / 'src' / 'service.py').read_text(encoding='utf-8'))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, 'attr', None) in {'queue_answer', 'status_message'}:
                for arg in node.args[1:2]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        offenders.append(arg.value)
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and getattr(node.exc.func, 'id', '') == 'ValueError':
                if any(isinstance(a, ast.Constant) for a in node.exc.args):
                    offenders.append(ast.dump(node.exc))
        self.assertEqual(offenders, [])

    def test_user_texts_are_russian(self):
        samples = [texts.main_text(), *texts.PAGES.values(), texts.SKILLS_OUTRO, texts.MODEL_FAILED, texts.CRASH_GAVE_UP,
                   texts.LIMIT_INVALID, texts.PERSONALITY_INVALID, *texts.PROMPTS.values(), *texts.BUTTONS.values()]
        for text in samples:
            with self.subTest(text=text[:30]):
                letters = re.findall(r'[A-Za-zА-Яа-яЁё]', re.sub(r'\{\w+\}|/\w+|\b(on|off|directed|all|BotFather|ID|Telegram|ИИ)\b', '', text))
                cyrillic = [c for c in letters if re.match('[А-Яа-яЁё]', c)]
                self.assertGreater(len(cyrillic), len(letters) * 0.8)

    def test_history_limit_never_leaks_python_errors(self):
        from policy import valid_limit
        for bad in ('abc', '', '5', '9999', '١٢'):
            with self.subTest(value=bad), self.assertRaises(ValueError) as caught:
                valid_limit(bad)
            self.assertEqual(str(caught.exception), texts.LIMIT_INVALID)
        self.assertEqual(valid_limit(' 50 '), 50)


if __name__ == '__main__':
    unittest.main()
