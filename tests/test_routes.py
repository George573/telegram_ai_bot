"""Route links built locally from Maps coordinates or addresses; no requests."""
from pathlib import Path
from unittest.mock import patch
import json
import sys
import unittest
from urllib.parse import parse_qs, urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from providers import Generation, ToolCall
from routes import RouteError, route_link
import yandex_maps as maps
import test_skill_loading as skill_tests

TOILET = {'type': 'business', 'id': '110588152984', 'seoname': 'tualet', 'title': 'Туалет № 179',
          'coordinates': [37.797174, 55.811181],
          'ratingData': {'ratingCount': 21, 'ratingValue': 0, 'reviewCount': 15}}


class RouteLinkTests(unittest.TestCase):
    def test_coordinates_and_address_points(self):
        result = route_link({'origin': 'Алтайская улица, 2', 'destination': '55.811181,37.797174', 'mode': 'walk'})
        parts = urlsplit(result['url'])
        self.assertEqual((parts.netloc, parts.path), ('yandex.ru', '/maps/213/moscow/'))
        self.assertEqual(parse_qs(parts.query), {'rtext': ['Москва, Алтайская улица, 2~55.811181,37.797174'],
                                                 'rtt': ['pd']})
        self.assertIn('do not state any', result['notice'])
        transit = route_link({'origin': 'Москва, Тверская, 1', 'destination': '55.75, 37.62', 'mode': 'transit'})
        self.assertTrue(transit['url'].endswith('~55.750000,37.620000&rtt=mt'))
        self.assertEqual(parse_qs(urlsplit(transit['url']).query)['rtext'], ['Москва, Тверская, 1~55.750000,37.620000'])

    def test_mistakes_name_the_field(self):
        cases = [({'origin': '37.797174,55.811181', 'destination': 'Уральская, 1', 'mode': 'walk'}, 'origin', 'lon,lat'),
                 ({'origin': '59.93,30.31', 'destination': 'Уральская, 1', 'mode': 'walk'}, 'origin', 'outside Moscow'),
                 ({'origin': 'https://yandex.ru/maps/org/1/', 'destination': 'Уральская, 1', 'mode': 'walk'}, 'origin', 'not a link'),
                 ({'origin': 'А~Б', 'destination': 'Уральская, 1', 'mode': 'walk'}, 'origin', 'not a link'),
                 ({'origin': '', 'destination': 'Уральская, 1', 'mode': 'walk'}, 'origin', 'address'),
                 ({'origin': 'Уральская, 1', 'destination': 'Уральская, 1', 'mode': 'walk'}, 'destination', 'same point'),
                 ({'origin': 'Уральская, 1', 'destination': 'Тверская, 1', 'mode': 'bike'}, 'mode', 'walk')]
        for arguments, field, hint in cases:
            with self.subTest(hint=hint):
                with self.assertRaises(RouteError) as caught:
                    route_link(arguments)
                self.assertEqual(caught.exception.field, field)
                self.assertIn(hint, str(caught.exception))

    def test_maps_results_carry_route_coordinates_and_no_fake_zero_rating(self):
        result = maps.normalized_search('u', [TOILET])['results'][0]
        self.assertEqual(result['coordinates'], '55.811181,37.797174')
        self.assertIsNone(result['rating'])
        self.assertEqual(result['rating_count'], 21)
        rated = maps.normalized_search('u', [{**TOILET, 'ratingData': {'ratingValue': 4.6, 'ratingCount': 3}}])
        self.assertEqual(rated['results'][0]['rating'], 4.6)
        missing = maps.normalized_search('u', [{**TOILET, 'coordinates': 'bad'}])
        self.assertIsNone(missing['results'][0]['coordinates'])


class RouteLoopTests(unittest.TestCase):
    def test_model_fixes_swapped_coordinates_then_links_the_route(self):
        helper = skill_tests.SkillLoadingTests()
        model = helper.model('zai', lambda _: self.fail('No SDK request needed'))
        self.addCleanup(helper.doCleanups)
        wrong = ToolCall('r1', 'route_link', '{"origin":"Алтайская улица, 2","destination":"37.797174,55.811181","mode":"walk"}')
        right = ToolCall('r2', 'route_link', '{"origin":"Алтайская улица, 2","destination":"55.811181,37.797174","mode":"walk"}')
        results = []
        def attempt(instructions, history, output_tokens, result_format, *args, continuation, **kwargs):
            results.extend(json.loads(item['content']) for item in continuation[len(results) * 2:] if item.get('role') == 'tool')
            if len(results) == 0:
                return Generation('', True, tool_calls=(wrong,), continuation=({'role': 'assistant', 'content': None},))
            if len(results) == 1:
                return Generation('', True, tool_calls=(right,), continuation=({'role': 'assistant', 'content': None},))
            return Generation(f"[Маршрут пешком]({results[1]['url']}) — время покажут Карты. "
                              '[Другой](https://yandex.ru/maps/?rtext=1~2)', True)
        with patch.object(model, '_generation_attempt', side_effect=attempt):
            text = model.generate('Persona', [], allow_search=True)
        self.assertEqual((results[0]['status'], results[0]['field']), ('error', 'destination'))
        self.assertEqual(results[1]['status'], 'ok')
        self.assertIn(f"[Маршрут пешком]({results[1]['url']})", text)
        self.assertNotIn('rtext=1~2', text)  # An invented route link is removed.

    def test_route_link_copied_with_encoded_cyrillic_is_still_known(self):
        from recommendations import canonical, evidence_urls, ground_links
        url = route_link({'origin': 'Алтайская', 'destination': '55.811181,37.797174', 'mode': 'walk'})['url']
        self.assertIn('rtext=Москва,%20Алтайская~', url)  # Letters stay readable, keeping links short.
        encoded = url.replace('Москва', '%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0')
        self.assertEqual(ground_links(f'[Маршрут]({encoded})', evidence_urls(url))[1:], (1, 0))
        self.assertEqual(canonical(url), canonical(encoded))

    def test_long_two_address_route_survives_whole(self):
        # Regression: a 446-character escaped link was cut at 400 characters, leaving its tail in the reply.
        from recommendations import evidence_urls, ground_links
        from telegram_format import to_html
        url = route_link({'origin': 'Алтайская улица, 10', 'mode': 'walk',
                          'destination': 'Сиреневый бульвар, 1 (общественный туалет), Москва'})['url']
        text, kept, dropped = ground_links(f'Готово — [маршрут пешком]({url}).', evidence_urls(url))
        self.assertEqual((kept, dropped), (1, 0))
        self.assertIn('маршрут пешком (' + url.replace('&', '&amp;') + ')', to_html(text))
        long_url = url.replace('rtt=pd', 'x=' + 'а' * 700 + '&rtt=pd')
        self.assertGreater(len(long_url), 400)
        self.assertEqual(ground_links(f'[Маршрут]({long_url})', evidence_urls(long_url))[1:], (1, 0))

if __name__ == '__main__':
    unittest.main()
