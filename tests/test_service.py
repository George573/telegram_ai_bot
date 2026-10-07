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
        self.rows, self.jobs = [], []
        self.next_id = 1
    @contextmanager
    def transaction(self):
        old = deepcopy((self.chat, self.rows, self.jobs))
        try:
            yield
        except Exception:
            self.chat, self.rows, self.jobs = old
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


class FakeTelegram:
    def __init__(self, admin=False, fail=False): self.admin, self.fail, self.sent = admin, fail, []
    def is_admin(self, payload): return self.admin and not payload['anonymous']
    def send(self, payload, answer):
        if self.fail: raise TelegramFailure()
        self.sent.append(answer)


class FakeModel:
    def __init__(self, fail=False): self.fail, self.calls = fail, []
    def decide_reply(self, instructions, history):
        self.generate(instructions, history)
        return None
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
        self.model.decide_reply = lambda *args: 'Meow'
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
