"""Markdown → Telegram HTML, HTML delivery with plain fallback, cards without a skill. Synthetic only."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from providers import Generation, ToolCall
from telegram_api import TelegramAPI
from telegram_format import to_html
import test_skill_loading as skill_tests
import yandex_maps as maps


class ConverterTests(unittest.TestCase):
    def test_common_model_markdown(self):
        self.assertEqual(to_html('**Правда Кофе** — Никольская'), '<b>Правда Кофе</b> — Никольская')
        self.assertEqual(to_html('- 4.9 (76 оценок)\n* фильтры'), '• 4.9 (76 оценок)\n• фильтры')
        self.assertEqual(to_html('## Итог'), '<b>Итог</b>')
        self.assertEqual(to_html('очень *тихо* и _уютно_'), 'очень <i>тихо</i> и <i>уютно</i>')
        self.assertEqual(to_html('~~закрыто~~ `v60`'), '<s>закрыто</s> <code>v60</code>')

    def test_untrusted_text_cannot_inject_markup(self):
        self.assertEqual(to_html('<a href="x">клик</a> & <b>'), '&lt;a href="x"&gt;клик&lt;/a&gt; &amp; &lt;b&gt;')
        self.assertEqual(to_html('`<script>`'), '<code>&lt;script&gt;</code>')

    def test_links_keep_their_url_visible_and_plain_text_survives(self):
        self.assertEqual(to_html('[Нажми](https://evil.example/pay)'), 'Нажми (https://evil.example/pay)')
        for plain in ('https://yandex.ru/maps/org/some_cafe_name/1/', 'snake_case_name', '4.9 * 2 = 9.8', '5*3',
                      '**незакрытый жирный', 'a ** b'):
            with self.subTest(text=plain):
                self.assertNotIn('<', to_html(plain))


class DeliveryTests(unittest.TestCase):
    def api(self, handler):
        api = TelegramAPI('synthetic-token')
        api._http.close()
        api._http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(api._http.close)
        return api

    def test_html_is_sent_and_rejected_markup_falls_back_to_plain_text(self):
        bodies = []
        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            if body.get('parse_mode') == 'HTML' and 'broken' in body['text']:
                return httpx.Response(400, json={'ok': False, 'error_code': 400,
                                                 'description': "Bad Request: can't parse entities"})
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 3}})
        api = self.api(handler)
        api.send({'chat_id': -1, 'message_id': 2}, '**Кофеин**')
        self.assertEqual((bodies[-1]['text'], bodies[-1]['parse_mode']), ('<b>Кофеин</b>', 'HTML'))
        self.assertEqual(api.edit(-1, 3, '**broken'), {'message_id': 3})
        self.assertEqual(bodies[-1]['text'], '**broken')
        self.assertNotIn('parse_mode', bodies[-1])

    def test_command_menu_covers_default_group_and_admin_scopes(self):
        scopes = []
        def handler(request):
            scopes.append(json.loads(request.content)['scope']['type'])
            return httpx.Response(200, json={'ok': True, 'result': True})
        self.api(handler).set_commands([('help', 'Помощь')], [('help', 'Помощь'), ('settings', 'Настройки')])
        self.assertEqual(scopes, ['default', 'all_group_chats', 'all_chat_administrators'])


class CardsWithoutSkillTests(unittest.TestCase):
    def test_maps_research_without_load_skill_still_renders_grounded_cards(self):
        helper = skill_tests.SkillLoadingTests()
        model = helper.model('zai', lambda _: self.fail('No SDK request needed'))
        self.addCleanup(helper.doCleanups)
        items = [{'type': 'business', 'id': '176224105875', 'seoname': 'kofein', 'title': 'Кофеин', 'chain': None},
                 {'type': 'business', 'id': '161860189832', 'seoname': 'surf', 'title': 'Surf Coffee',
                  'chain': {'name': 'Surf Coffee'}}]
        found = maps.normalized_search(maps.search_url('кофейня Никольская'), items, exclude_chains=True)
        search = ToolCall('m', 'read_yandex_maps_search', '{"query":"кофейня Никольская","exclude_chains":true}')
        answer = skill_tests.cards('Кофеин', 'https://yandex.ru/maps/org/176224105875/reviews/')
        with patch.object(model, '_generation_attempt', side_effect=[Generation('', True, tool_calls=(search,)),
                Generation(answer, True)]) as generate, patch('model.read_maps_search', return_value=found):
            text = model.generate('Persona', [], allow_search=True)
        self.assertEqual(generate.call_args_list[1].args[3]['name'], 'place_recommendations')
        self.assertTrue(text.cards)
        self.assertIn('1. ☕ **Кофеин**', text)


if __name__ == '__main__':
    unittest.main()
