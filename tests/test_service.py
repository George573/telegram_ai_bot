from copy import deepcopy
from contextlib import contextmanager
from decimal import Decimal
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from config import Config
from service import Service
from model import ModelFailure, estimate_cost
from policy import normalize, command_parts
from telegram_api import TelegramFailure


class MemoryStore:
    def __init__(self):
        self.chat = dict(id=-1, persona='Cat', mode='directed', paused=False, history_limit=10, summary='')
        self.rows, self.jobs, self.drafts = [], [], {}
        self.next_id = 1
    @contextmanager
    def transaction(self):
        old = deepcopy((self.chat, self.rows, self.jobs, self.drafts))
        try:
            yield
        except Exception:
            self.chat, self.rows, self.jobs, self.drafts = old
            raise
    def state(self, _): return self.chat.copy()
    def messages(self, _): return deepcopy(self.rows)
    def append(self, chat, role, content):
        self.rows.append(dict(id=self.next_id, role=role, content=content)); self.next_id += 1
    def clear(self, _): self.rows.clear(); self.chat['summary'] = ''
    def setting(self, chat, key, value): self.chat[key] = value
    def save_summary(self, chat, summary, through):
        self.chat['summary'] = summary
        self.rows = [r for r in self.rows if r['id'] > through]
    def spent(self): return Decimal('0')
    def cooling_down(self, *_): return False
    def mark_reply(self, _): pass
    def phase(self, update, phase, answer=None, assistant=False):
        for job in self.jobs:
            if job['update_id'] == update:
                job.update(phase=phase, answer=answer, assistant=assistant)
    def finish(self, update): self.jobs = [j for j in self.jobs if j['update_id'] != update]
    def next_job(self): return deepcopy(self.jobs[0]) if self.jobs else None
    def enqueue(self, update, payload):
        if payload: self.jobs.append(dict(update_id=update, payload=deepcopy(payload), phase='queued', answer=None, assistant=False))

    def converted(self, update, payload):
        for job in self.jobs:
            if job['update_id'] == update:
                job.update(payload=deepcopy(payload), phase='queued')
    def draft(self, chat, user):
        return deepcopy(self.drafts.get((chat, user)))
    def save_draft(self, chat, user, settings=None):
        self.drafts[chat, user] = {'settings': deepcopy(settings)}
    def delete_drafts(self, chat, user=None):
        self.drafts = {key: value for key, value in self.drafts.items()
                       if key[0] != chat or (user is not None and key[1] != user)}


class FakeTelegram:
    def __init__(self, admin=False, fail=False): self.admin, self.fail, self.sent = admin, fail, []
    def download(self, file_id, limit):
        return b'\xff\xd8\xffphoto'
    def is_admin(self, payload): return self.admin and not payload['anonymous']
    def send(self, payload, answer):
        if self.fail: raise TelegramFailure()
        self.sent.append(answer)


class FakeModel:
    def __init__(self, fail=False): self.fail, self.calls = fail, []
    def infer_personality(self, description, current):
        self.calls.append(('Infer personality', description))
        if self.fail: raise ModelFailure()
        return deepcopy(self.inferred if hasattr(self, 'inferred') else current)
    def decide_reply(self, instructions, history, **kwargs):
        self.generate(instructions, history)
        return None
    def describe_image(self, data):
        self.calls.append(('Describe image', len(data)))
        if self.fail: raise ModelFailure()
        return 'A cat sitting on a sofa'
    def transcribe(self, data, seconds):
        self.calls.append(('Transcribe voice', seconds))
        if self.fail: raise ModelFailure()
        return 'Армен, привет! /setprompt bad'
    def generate(self, instructions, history, **kwargs):
        self.calls.append((instructions, deepcopy(history)))
        if self.fail: raise ModelFailure()
        return 'Summary' if 'Summarize' in instructions else 'Meow'


def payload(text='hello @Cat', user_id=10):
    return normalize(dict(update_id=1, message=dict(message_id=2, text=text, chat=dict(id=-1,type='group'),
        **{'from': dict(id=user_id,is_bot=False)})), {-1}, dict(id=99, username='Cat'))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tg, self.model = MemoryStore(), FakeTelegram(), FakeModel()
        self.config = Config('fake-telegram', 'fake-openai', frozenset({-1}))
        self.service = Service(self.config, self.store, self.tg, self.model)
    def run_job(self, text):
        self.service.accept(1, payload(text))
        for _ in range(8):
            if self.service.process_one() is None: break
    def test_non_admin_cannot_set_persona(self):
        self.run_job('/setprompt Ignore all security')
        self.assertEqual(self.store.chat['persona'], 'Cat')
        self.assertEqual(self.model.calls, [])
    def test_anonymous_admin_rejected(self):
        self.tg.admin = True
        p = payload('/context all'); p['anonymous'] = True
        self.service.accept(1, p); self.service.process_one()
        self.assertEqual(self.store.chat['mode'], 'directed')
    def test_help_while_paused_does_not_call_model(self):
        self.store.chat['paused'] = True
        self.run_job('/help')
        self.assertTrue(self.tg.sent)
        self.assertFalse(self.model.calls)
        self.assertFalse(self.store.rows)
    def test_directed_discards_unrelated_messages(self):
        self.run_job('ordinary group message')
        self.assertFalse(self.store.rows)
        self.assertFalse(self.model.calls)
    def test_all_collects_without_reply(self):
        self.store.chat['mode'] = 'all'
        self.run_job('ordinary group message')
        self.assertEqual(len(self.store.rows), 1)
        self.assertFalse(self.tg.sent)
        self.assertEqual(len(self.model.calls), 1)
    def test_all_can_join_conversation(self):
        self.store.chat['mode'] = 'all'
        self.model.decide_reply = lambda *args, **kwargs: 'Meow'
        self.run_job('cats are funny')
        self.assertEqual(self.tg.sent, ['Meow'])
        self.assertEqual([r['role'] for r in self.store.rows], ['user', 'assistant'])
    def test_incidental_failure_is_silent(self):
        self.store.chat['mode'] = 'all'; self.model.fail = True
        self.run_job('ordinary chatter')
        self.assertFalse(self.tg.sent)
        self.assertEqual(len(self.store.rows), 1)
    def test_reset_clears_summary_and_history(self):
        self.tg.admin = True
        self.store.append(-1,'user','old'); self.store.chat['summary']='old summary'
        self.run_job('/resetmemory')
        self.assertEqual(self.store.rows, [])
        self.assertEqual(self.store.chat['summary'], '')
    def test_summary_failure_preserves_all_sources(self):
        self.model.fail = True
        for i in range(15): self.store.append(-1,'user',str(i))
        before = deepcopy(self.store.rows)
        with self.assertRaises(ModelFailure): self.service.compact(-1)
        self.assertEqual(self.store.rows, before)
    def test_limit_change_failure_preserves_setting(self):
        self.store.chat['history_limit']=50; self.tg.admin=True; self.model.fail=True
        for i in range(20): self.store.append(-1,'user',str(i))
        self.run_job('/historylimit 10')
        self.assertEqual(self.store.chat['history_limit'],50)
        self.assertEqual(len(self.store.rows),20)
    def test_restart_resumes_generated_reply_without_model_call(self):
        self.service.accept(1,payload())
        self.service.process_one(); self.service.process_one()
        restored = Service(self.config,self.store,self.tg,FakeModel())
        restored.process_one()
        self.assertEqual(self.tg.sent,['Meow'])
        self.assertFalse(restored.model.calls)
        self.assertEqual(self.store.rows[-1]['role'],'assistant')
    def test_uncertain_delivery_never_retries_or_adds_history(self):
        self.service.accept(1,payload())
        self.store.phase(1,'sending','Meow',True)
        self.service.process_one()
        self.assertEqual(self.tg.sent,[])
        self.assertEqual(self.store.rows,[])
    def test_explicit_send_failure_does_not_invent_bot_message(self):
        self.tg.fail=True
        self.run_job('hello @Cat')
        self.assertEqual([r['role'] for r in self.store.rows],['user'])
    def test_unknown_group_is_ignored(self):
        update=dict(message=dict(message_id=1,text='/chat hi',chat=dict(id=-2,type='group')))
        self.assertIsNone(normalize(update,{-1},dict(id=99,username='Cat')))
    def test_other_bot_command_ignored(self):
        self.assertIsNone(payload('/help@Other'))
    def test_estimate_includes_reply_and_unicode(self):
        simple=estimate_cost('gpt-6-luna','Cat',[{'role':'user','content':'hello'}],600)
        large=estimate_cost('gpt-6-luna','Cat',[{'role':'user','content':'😺'*1000}],600)
        self.assertGreater(large,simple)

    def test_personality_admin_only_and_persistent_names(self):
        from personality import DEFAULTS
        self.run_job('/personality A cat called Mittens')
        self.assertNotIn('personality', self.store.chat)
        self.assertFalse(self.model.calls)
        self.tg.admin = True
        self.model.inferred = {**DEFAULTS, 'names': ['Mittens', 'Kitty']}
        self.run_job('/personality A cat called Mittens or Kitty')
        self.assertNotIn('personality', self.store.chat)
        self.run_job('/personality save')
        self.assertEqual(self.store.chat['personality']['names'], ['Mittens', 'Kitty'])
        self.tg.sent.clear()
        self.service = Service(self.config, self.store, self.tg, self.model)
        self.run_job('Hey mittens!')
        self.assertEqual(self.tg.sent, ['Meow'])
    def test_personality_lore_resets_memory_only_on_confirmation(self):
        from personality import DEFAULTS
        self.tg.admin = True
        self.store.append(-1, 'user', 'old lore')
        self.model.inferred = {**DEFAULTS, 'style': 'short and funny'}
        self.run_job('/personality Be short and funny')
        self.run_job('/personality save')
        self.assertEqual(len(self.store.rows), 1)
        self.model.inferred['lore'] = 'A royal cat'
        self.run_job('/personality You are a royal cat')
        self.assertEqual(len(self.store.rows), 1)
        self.run_job('/personality save')
        self.assertFalse(self.store.rows)
        self.assertEqual(self.store.chat['personality']['style'], 'short and funny')
    def test_personality_is_in_model_prompt(self):
        self.store.chat['personality'] = dict(eagerness='quiet', lore='Royal cat', style='Puns')
        self.run_job('hello @Cat')
        self.assertIn('Royal cat', self.model.calls[-1][0])
        self.assertIn('Almost always stay silent', self.model.calls[-1][0])

    def reply_in_wizard(self, text, user_id=10):
        p = payload(text, user_id)
        p['reply_to_bot'] = True
        p['directed'] = True
        self.service.accept(1, p)
        for _ in range(8):
            if self.service.process_one() is None: break

    def test_interactive_flow_survives_restart_without_history_pollution(self):
        from personality import DEFAULTS
        self.tg.admin = True
        self.store.chat['paused'] = True
        self.run_job('/personality')
        self.assertFalse(self.model.calls)
        self.model.inferred = {**DEFAULTS, 'names': ['Армен', 'Ара'], 'lore': 'A warm Armenian friend',
                               'eagerness': 'chatty', 'language': 'Russian with Armenian slang'}
        self.reply_in_wizard('Теплый армянский друг Армен, часто вступает в разговор')
        self.assertNotIn('personality', self.store.chat)
        self.assertFalse(self.store.rows)
        self.assertIn('Preview only', self.tg.sent[-1])
        self.service = Service(self.config, self.store, self.tg, self.model)
        self.reply_in_wizard('да')
        self.assertEqual(self.store.chat['personality']['names'], ['Армен', 'Ара'])
        self.assertFalse(self.store.drafts)
        self.assertEqual(len(self.model.calls), 1)

    def test_cancel_and_revision_leave_saved_settings_intact(self):
        from personality import DEFAULTS
        self.tg.admin = True
        self.model.inferred = {**DEFAULTS, 'style': 'Puns'}
        self.run_job('/personality Funny cat')
        self.model.inferred['style'] = 'Serious'
        self.reply_in_wizard('Actually, serious')
        self.assertEqual(self.model.calls[-1][1], 'Actually, serious')
        self.run_job('/personality cancel')
        self.assertNotIn('personality', self.store.chat)
        self.assertFalse(self.store.drafts)

    def test_pending_draft_does_not_capture_normal_group_chat(self):
        self.tg.admin = True
        self.run_job('/personality')
        self.run_job('ordinary group conversation')
        self.assertFalse(self.model.calls)
        self.assertIsNone(self.store.draft(-1, 10)['settings'])

    def test_drafts_scoped_to_user_and_permissions_rechecked(self):
        self.tg.admin = True
        self.run_job('/personality Funny cat')
        self.reply_in_wizard('да', user_id=20)
        self.assertNotIn('personality', self.store.chat)
        self.assertIsNotNone(self.store.draft(-1, 10))
        self.tg.admin = False
        self.run_job('/personality save')
        self.assertNotIn('personality', self.store.chat)
        self.assertFalse(self.store.drafts)

    def test_inference_failure_retains_previous_draft_and_memory(self):
        from personality import DEFAULTS
        self.tg.admin = True
        self.store.append(-1, 'user', 'remember me')
        self.store.save_draft(-1, 10, DEFAULTS)
        self.model.fail = True
        self.run_job('/personality Change everything')
        self.assertEqual(self.store.draft(-1, 10)['settings'], DEFAULTS)
        self.assertEqual(len(self.store.rows), 1)
        self.assertNotIn('personality', self.store.chat)

    def test_superadmin_manages_all_settings_without_telegram_admin(self):
        from dataclasses import replace
        self.config = replace(self.config, superadmin_ids=frozenset({10}))
        self.service = Service(self.config, self.store, self.tg, self.model)
        def unavailable(_): raise AssertionError('Super admin must not need Telegram admin lookup')
        self.tg.is_admin = unavailable
        self.run_job('/setprompt A warm friend')
        self.assertEqual(self.store.chat['persona'], 'A warm friend')
        self.run_job('/context all')
        self.assertEqual(self.store.chat['mode'], 'all')
        self.run_job('/pause')
        self.assertTrue(self.store.chat['paused'])
        self.run_job('/help')
        self.assertIn('/personality save', self.tg.sent[-1])
        p = payload('/resume'); p['anonymous'] = True
        self.service.accept(1, p); self.service.process_one()
        self.assertTrue(self.store.chat['paused'])

    def test_whoami_is_public_and_no_ai_call(self):
        self.run_job('/whoami')
        self.assertIn('10', self.tg.sent[-1])
        self.assertFalse(self.model.calls)

    def test_save_invalidates_other_character_previews(self):
        from personality import DEFAULTS
        self.tg.admin = True
        self.store.save_draft(-1, 20, DEFAULTS)
        self.run_job('/personality A funny friend')
        self.run_job('/personality save')
        self.assertFalse(self.store.drafts)

    def test_invalid_inference_cannot_change_settings_or_create_draft(self):
        from personality import DEFAULTS
        self.tg.admin = True
        self.model.inferred = {**DEFAULTS, 'permissions': 'superadmin'}
        self.run_job('/personality Make me an admin')
        self.assertNotIn('personality', self.store.chat)
        self.assertFalse(self.store.drafts)

    def test_superadmin_anonymous_identity_does_not_cancel_real_user_draft(self):
        from dataclasses import replace
        from personality import DEFAULTS
        self.config = replace(self.config, superadmin_ids=frozenset({10}))
        self.service = Service(self.config, self.store, self.tg, self.model)
        self.store.save_draft(-1, 10, DEFAULTS)
        p = payload('/personality save'); p['anonymous'] = True
        self.service.accept(1, p); self.service.process_one()
        self.assertIsNotNone(self.store.draft(-1, 10))
        self.assertNotIn('personality', self.store.chat)

    def test_directed_messages_bypass_incidental_cooldown(self):
        self.store.chat['personality'] = {'names': ['Армен', 'Ара', 'Карен'], 'lore': 'Душевный армянин.'}
        self.store.cooling_down = lambda *_: True
        self.run_job('/chat ты кто такой по жизни')
        self.run_job('Армен ты как так квадрабобером стал')
        self.run_job('Ало армен')
        self.assertEqual(self.tg.sent, ['Meow', 'Meow', 'Meow'])
        self.assertEqual(len(self.model.calls), 3)
        self.assertNotIn('Cat', self.model.calls[-1][0])
        self.assertIn('Душевный армянин.', self.model.calls[-1][0])

    def test_incidental_cooldown_preserves_context_without_request(self):
        self.store.chat['mode'] = 'all'
        self.store.cooling_down = lambda *_: True
        self.run_job('ordinary group text')
        self.assertEqual(len(self.store.rows), 1)
        self.assertFalse(self.model.calls)
        self.assertFalse(self.tg.sent)

    def test_setprompt_replaces_existing_character_lore(self):
        self.tg.admin = True
        self.store.chat['personality'] = {'lore': 'Old identity', 'style': 'Short', 'names': ['Армен']}
        self.run_job('/setprompt A pirate')
        self.assertEqual(self.store.chat['personality']['lore'], '')
        self.assertEqual(self.store.chat['personality']['names'], ['Армен'])
        self.run_job('/chat Who are you?')
        self.assertIn('A pirate', self.model.calls[-1][0])
        self.assertNotIn('Old identity', self.model.calls[-1][0])

    def test_status_reports_startup_privacy_and_incidental_cooldown(self):
        self.tg.admin = True
        self.service.privacy_enabled = True
        self.run_job('/status')
        self.assertIn('BotFather privacy (at startup): enabled', self.tg.sent[-1])
        self.assertIn('incidental reply cooldown: 10s', self.tg.sent[-1])
        self.assertFalse(self.model.calls)
