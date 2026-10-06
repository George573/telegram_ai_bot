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
    def test_http_errors_do_not_expose_telegram_url(self):
        def handler(request): raise httpx.ConnectError(str(request.url))
        api=self.telegram(handler)
        with self.assertRaises(TelegramFailure) as caught: api.call('getMe')
        self.assertNotIn('fake-telegram-token',str(caught.exception))
