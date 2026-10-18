"""Yandex Maps search tool: synthetic state-view pages, mocked HTTP. Includes the Nikolskaya regression."""
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import re
import sys
import unittest
import urllib.error
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))
from providers import Generation, ToolCall
import recommendations as rec
import skill_registry
import yandex_maps as maps
import maps_search
import server as tools_server
import tool_client
import test_skill_loading as skill_tests


def business(oid, title, chain=None, **extra):
    return {'type': 'business', 'id': str(oid), 'seoname': title.lower().replace(' ', '_'), 'title': title,
            'address': extra.pop('address', 'Никольская ул., 10'), 'chain': chain,
            'ratingData': {'ratingValue': extra.pop('rating', 4.400000095367432), 'ratingCount': 160, 'reviewCount': 120},
            'metro': [{'name': 'Лубянка', 'distance': '117 м'}], 'workingTimeText': 'ежедневно, 08:00–22:00',
            'currentWorkingStatus': {'isOpenNow': True}, 'status': extra.pop('status', 'open'),
            'categories': [{'name': 'Кофейня'}],
            'features': [{'id': 'caring_for_couriers_tea', 'name': 'Чай', 'type': 'bool', 'value': True},
                         {'id': 'coffee_to_go', 'name': 'кофе с собой', 'type': 'bool', 'value': True, 'important': True},
                         {'id': 'roasting_coffee', 'name': 'обжарка кофе', 'type': 'bool', 'value': True},
                         {'id': 'music', 'name': 'музыка', 'type': 'enum', 'value': [{'name': 'chillout'}]}],
            'aspects': [{'text': 'Кофе', 'count': 250, 'positive': 160, 'negative': 54, 'photos': [{'link': 'x'}]}],
            'neurosummaryData': {'review_text': 'Private reviewer text', 'puid': 1493174394},
            'awards': {'goodPlaceYear': '2026'}, **extra}


NIKOLSKAYA = [business(161860189832, 'Surf Coffee', {'id': '200959754479', 'name': 'Surf Coffee'}),
              business(176224105875, 'Кофеин', address='Никольская ул., 11-13с5'),
              business(210704068627, 'Кафе Гурмэ', rating=5),
              {'type': 'collection', 'id': None, 'title': 'Хорошее место 2026'},
              business(45245336022, 'Кофеин', address='Никольская ул., 7-9с4'),
              business(1, 'Brew Bros', address='a'), business(2, 'Brew Bros', address='b'),
              business(3, 'Brew Bros', address='c'), business(4, 'Brew Bros', address='d'),
              business(5, 'Closed Cafe', status='permanent-closed')]


def page(items):
    state = {'stack': [{'results': {'items': items, 'totalResultCount': len(items)}}]}
    return '<html><script type="application/json" class="state-view">' + json.dumps(state) + '</script></html>'


class NormalizerTests(unittest.TestCase):
    def test_nikolskaya_chains_are_removed_in_code_when_requested(self):
        result = maps.normalized_search('u', NIKOLSKAYA, exclude_chains=True)
        names = [r['name'] for r in result['results']]
        self.assertNotIn('Surf Coffee', names)        # Yandex chain link.
        self.assertNotIn('Brew Bros', names)          # More than three Moscow locations.
        self.assertEqual(names[:3], ['Кофеин', 'Кафе Гурмэ', 'Кофеин'])  # Two branches still count as independent.
        self.assertEqual(result['chains_excluded'], 5)
        self.assertEqual(result['excluded_chains'], ['Surf Coffee', 'Brew Bros'])
        self.assertTrue(all(r['independent'] for r in result['results']))
        everything = maps.normalized_search('u', NIKOLSKAYA, exclude_chains=False)
        surf = everything['results'][0]
        self.assertEqual((surf['chain'], surf['independent']), ('Surf Coffee', False))

    def test_records_are_compact_and_private(self):
        first = maps.normalized_search('u', NIKOLSKAYA, exclude_chains=True)['results'][0]
        self.assertEqual(first['url'], 'https://yandex.ru/maps/org/176224105875/')
        self.assertEqual(first['reviews_url'], 'https://yandex.ru/maps/org/176224105875/reviews/')
        # The reviews reader accepts the place page too, so either link works for it.
        self.assertEqual(maps.card_url(first['url']), first['reviews_url'])
        self.assertEqual(first['rating'], 4.4)
        self.assertEqual(first['features'][0], 'обжарка кофе')
        self.assertNotIn('Чай', first['features'])
        self.assertEqual(first['review_topics'], [{'topic': 'Кофе', 'count': 250, 'positive': 160, 'negative': 54}])
        text = json.dumps(maps.normalized_search('u', NIKOLSKAYA), ensure_ascii=False)
        for private in ('Private reviewer', '1493174394', 'photos'):
            self.assertNotIn(private, text)
        self.assertTrue(maps.normalized_search('u', NIKOLSKAYA)['results'][-1]['closed_permanently'])

    def test_query_validation_and_url(self):
        self.assertEqual(maps.search_url(' кофейня   Никольская '),
                         'https://yandex.ru/maps/213/moscow/search/%D0%BA%D0%BE%D1%84%D0%B5%D0%B9%D0%BD%D1%8F%20'
                         '%D0%9D%D0%B8%D0%BA%D0%BE%D0%BB%D1%8C%D1%81%D0%BA%D0%B0%D1%8F/')
        self.assertEqual(maps.search_query('кафе\nНикольская'), 'кафе Никольская')
        for bad in ('', '   ', 'x' * 121, 'кафе/../../org', 'https://evil.example', 'cafe?ll=1', 'site.ru кафе',
                    'кафе\x00Никольская', 42):
            with self.subTest(query=bad), self.assertRaises(ValueError):
                maps.search_query(bad)

    def test_state_items_handles_missing_or_broken_state(self):
        self.assertEqual(len(maps.state_items(page(NIKOLSKAYA))), len(NIKOLSKAYA))
        self.assertIsNone(maps.state_items('<html>captcha</html>'))
        self.assertIsNone(maps.state_items('<script type="application/json" class="state-view">{broken</script>'))


class Response:
    def __init__(self, body): self.body = body
    def read(self, limit): return self.body[:limit]
    def __enter__(self): return self
    def __exit__(self, *_): return False


class FetcherTests(unittest.TestCase):
    def test_reads_page_and_maps_failures_to_reasons(self):
        with patch.object(maps_search.OPENER, 'open', return_value=Response(page(NIKOLSKAYA).encode())):
            self.assertEqual(maps_search.read_search('кофейня Никольская', True)['returned_count'], 4)
        def redirect(*_ , **__):
            headers = Message(); headers['Location'] = 'https://yandex.ru/showcaptcha?x'
            raise urllib.error.HTTPError('u', 302, 'Found', headers, BytesIO())
        cases = [(redirect, 'captcha'), (lambda *_, **__: Response(b'<html>captcha</html>'), 'captcha'),
                 (lambda *_, **__: Response(b'<html>no state</html>'), 'page_changed'),
                 (lambda *_, **__: Response(b'x' * (maps_search.MAX_BYTES + 1)), 'too_large'),
                 (lambda *_, **__: (_ for _ in ()).throw(OSError('synthetic-secret-host')), 'timeout_or_network')]
        for opener, reason in cases:
            with self.subTest(reason=reason), patch.object(maps_search.OPENER, 'open', side_effect=opener):
                result = maps_search.read_search('кофейня Никольская', False)
                self.assertEqual(result['reason'], reason)
                self.assertNotIn('synthetic', json.dumps(result))

    def test_redirects_are_never_followed(self):
        self.assertIsNone(maps_search.NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://evil.example'))


class ServiceAndClientTests(unittest.TestCase):
    def test_route_validates_arguments(self):
        with patch('server.read_search', return_value=maps.normalized_search('u', [])) as read:
            self.assertEqual(tools_server.maps_search(json.dumps({'query': 'кофейня', 'exclude_chains': True}))[0], 200)
            read.assert_called_once_with('кофейня', True, None)
        with patch('server.read_search') as read:
            for bad in ({'query': 'кофейня', 'exclude_chains': 'yes'}, {'query': 'https://x.ru'}, {'query': 'кофейня'}):
                self.assertEqual(tools_server.maps_search(json.dumps(bad))[0], 400)
            read.assert_not_called()

    def test_client_checks_shape(self):
        url = maps.search_url('кофейня Никольская')
        good = maps.normalized_search(url, NIKOLSKAYA, True)
        real = httpx.Client
        def client(response):
            return patch('tool_client.httpx.Client', lambda **kw: real(transport=httpx.MockTransport(lambda _: response), **kw))
        with client(httpx.Response(200, json=good)):
            self.assertEqual(tool_client.read_maps_search('кофейня Никольская', True, 'http://tools:8080')['status'], 'ok')
        with client(httpx.Response(200, json={**good, 'extra': 1})):
            self.assertEqual(tool_client.read_maps_search('кофейня Никольская', True, 'http://tools:8080')['reason'],
                             'browser_failed')


class ModelLoopTests(unittest.TestCase):
    def model(self):
        helper = skill_tests.SkillLoadingTests()
        model = helper.model('zai', lambda _: self.fail('No SDK request needed'))
        self.addCleanup(helper.doCleanups)
        return model

    def test_nikolskaya_regression_cards_only_link_returned_independent_places(self):
        """The reported failure: non-chain coffee on Nikolskaya recommended Surf Coffee with homepage links."""
        model = self.model()
        found = maps.normalized_search(maps.search_url('кофейня Никольская'), NIKOLSKAYA, exclude_chains=True)
        search = ToolCall('m', 'read_yandex_maps_search', '{"query":"кофейня Никольская","exclude_chains":true}')
        load = ToolCall('s', 'load_skill', '{"name":"nearby-places"}')
        answer = json.dumps({'intro': 'Несетевые кофейни на Никольской', 'unconfirmed': [], 'outro': None, 'items': [
            {'name': 'Кофеин', 'address': 'Никольская ул., 11-13с5', 'rating': None, 'when': None,
             'text': 'Своя обжарка', 'links': ['https://yandex.ru/maps/org/176224105875/reviews/']},
            {'name': 'Surf Coffee', 'address': None, 'rating': None, 'when': None, 'text': 'Сетевая, но хорошая',
             'links': ['https://yandex.ru/maps/org/161860189832/reviews/', 'https://surfcoffee.ru/']}]})
        with patch.object(model, '_generation_attempt', side_effect=[Generation('', True, tool_calls=(load, search)),
                Generation(answer, True)]) as generate, patch('model.read_maps_search', return_value=found) as read:
            text = model.generate('Persona', [], allow_search=True)
        read.assert_called_once_with('кофейня Никольская', True, model.config.tools_url, near=None)
        self.assertIn('\nhttps://yandex.ru/maps/org/176224105875/reviews', text)
        # The chain was excluded by the tool, so its card is removed entirely, links included.
        self.assertNotIn('Surf Coffee', text)
        self.assertNotIn('161860189832', text)
        self.assertNotIn('surfcoffee.ru', text)
        self.assertIn('1. **Кофеин**', text)
        result = json.loads(generate.call_args.kwargs['continuation'][-1]['content'])
        self.assertNotIn('Surf Coffee', [r['name'] for r in result['results']])

    def test_invalid_maps_arguments_go_back_to_the_model_and_reads_are_capped(self):
        model = self.model()
        calls = [ToolCall('a', 'read_yandex_maps_search', '{"query":"https://evil","exclude_chains":true}'),
                 ToolCall('b', 'read_yandex_maps_search', '{"query":"кафе","exclude_chains":"yes"}')]
        calls += [ToolCall(f'c{i}', 'read_yandex_maps_search', json.dumps({'query': f'кафе {i}', 'exclude_chains': False}))
                  for i in range(4)]
        with patch.object(model, '_generation_attempt', side_effect=[Generation('', True, tool_calls=tuple(calls[:2])),
                Generation('', True, tool_calls=tuple(calls[2:])), Generation(skill_tests.cards('Done'), True)]) as generate, \
                patch('model.read_maps_search', return_value=maps.normalized_search('u', NIKOLSKAYA)) as read:
            model.generate('Persona', [], allow_search=True)
        self.assertEqual(read.call_count, 3)
        contents = [json.loads(i['content']) for i in generate.call_args.kwargs['continuation'] if i.get('role') == 'tool']
        self.assertEqual([c.get('reason') for c in contents[:2]], ['invalid_arguments', 'invalid_arguments'])
        self.assertEqual(contents[-1]['reason'], 'review_limit')


class SkillAndCardTests(unittest.TestCase):
    def test_all_skills_load_and_catalog_stays_small(self):
        self.assertEqual(len(skill_registry.SKILL_NAMES), 7)
        for name in skill_registry.SKILL_NAMES:
            body = skill_registry.instructions(name)
            self.assertIn('JSON', body)
            self.assertLess(len(body), 8000)
        self.assertLess(len(skill_registry.catalog()), 3000)
        self.assertIn('read_yandex_maps_search', skill_registry.instructions('nearby-places'))

    def test_skills_name_only_current_schema_fields(self):
        removed = {'kind', 'venue', 'price', 'why', 'reviews', 'caveats', 'relaxed', 'follow_up', 'evidence_gaps'}
        for name in skill_registry.SKILL_NAMES:
            with self.subTest(skill=name):
                used = set(re.findall(r'`([a-z_]+)(?:\[\])?`', skill_registry.instructions(name)))
                self.assertFalse(used & removed)
                self.assertLessEqual({'items', 'text', 'unconfirmed'}, used)


class NearTests(unittest.TestCase):
    POINT = (55.7266, 37.6248)

    def spread(self):
        """15 places, the nearest ones last in Yandex order: sorting must happen before the cap."""
        items = []
        for index in range(15):
            km = 3 - index * 0.2  # 3.0 km ... 0.2 km
            items.append(business(1000 + index, f'Cafe {index}', address=f'ул. {index}',
                                  coordinates=[37.6248, 55.7266 + km / 111]))
        items.append(business(2000, 'Nowhere', address='x'))  # no coordinates
        return items

    def test_near_point_validation(self):
        self.assertEqual(maps.near_point('55.7266,37.6248'), (55.7266, 37.6248))
        self.assertEqual(maps.near_point(' 55.7266, 37.6248 '), (55.7266, 37.6248))
        self.assertIsNone(maps.near_point(None))
        for bad in ('55.7266', '91,37', '55,-181', 'Moscow', '55.7;37.6', 7):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                maps.near_point(bad)

    def test_search_url_with_and_without_area(self):
        self.assertEqual(maps.search_url('кофейня'), 'https://yandex.ru/maps/213/moscow/search/%D0%BA%D0%BE%D1%84%D0%B5%D0%B9%D0%BD%D1%8F/')
        url = maps.search_url('кофейня', near=self.POINT)
        self.assertTrue(url.endswith('/?ll=37.6248%2C55.7266&z=15&spn=0.0320%2C0.0180&rspn=1'), url)
        north = maps.search_url('кофейня', near=(64.5, 40.5))  # wider in longitude further north
        self.assertIn('spn=0.0419%2C0.0180', north)

    def test_results_sorted_by_distance_before_the_cap(self):
        result = maps.normalized_search('u', self.spread(), near=self.POINT)
        distances = [r['distance_m'] for r in result['results']]
        self.assertEqual(len(distances), 10)
        self.assertEqual(distances, sorted(distances))
        self.assertEqual(distances[0], 200)
        self.assertIn('straight-line', result['notice'])
        plain = maps.normalized_search('u', self.spread())
        self.assertNotIn('distance_m', plain['results'][0])
        self.assertEqual(plain['results'][0]['name'], 'Cafe 0')

    def test_server_and_client_pass_near(self):
        with patch('server.read_search', return_value=maps.normalized_search('u', [])) as read:
            body = {'query': 'кофейня', 'exclude_chains': False, 'near': '55.7266,37.6248'}
            self.assertEqual(tools_server.maps_search(json.dumps(body))[0], 200)
            read.assert_called_once_with('кофейня', False, (55.7266, 37.6248))
            read.reset_mock()
            self.assertEqual(tools_server.maps_search(json.dumps({'query': 'кофейня', 'exclude_chains': False}))[0], 200)
            read.assert_called_once_with('кофейня', False, None)
            self.assertEqual(tools_server.maps_search(json.dumps({**body, 'near': '91,37'}))[0], 400)
        url = maps.search_url('кофейня', near=self.POINT)
        good = maps.normalized_search(url, self.spread(), near=self.POINT)
        real = httpx.Client
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=good)
        with patch('tool_client.httpx.Client', lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            result = tool_client.read_maps_search('кофейня', False, 'http://tools:8080', near=self.POINT)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(seen[0]['near'], '55.7266,37.6248')

    def test_model_passes_near_and_explains_a_bad_point(self):
        model = ModelLoopTests.model(self)
        good = ToolCall('a', 'read_yandex_maps_search',
                        '{"query":"кофейня Тверская","exclude_chains":false,"near":"55.7266, 37.6248"}')
        bad = ToolCall('b', 'read_yandex_maps_search', '{"query":"кофейня","exclude_chains":false,"near":"рядом"}')
        legacy = ToolCall('c', 'read_yandex_maps_search', '{"query":"кофейня Арбат","exclude_chains":false}')
        with patch.object(model, '_generation_attempt', side_effect=[
                Generation('', True, tool_calls=(good, bad, legacy)), Generation('Готово', True)]) as generate, \
                patch('model.read_maps_search', return_value=maps.normalized_search('u', self.spread(), near=self.POINT)) as read:
            model.generate('Persona', [], allow_search=True)
        self.assertEqual(read.call_args_list[0].args, ('кофейня Тверская', False, model.config.tools_url))
        self.assertEqual(read.call_args_list[0].kwargs, {'near': (55.7266, 37.6248)})
        self.assertEqual(read.call_args_list[1].kwargs, {'near': None})
        contents = [json.loads(i['content']) for i in generate.call_args.kwargs['continuation'] if i.get('role') == 'tool']
        self.assertEqual(contents[1]['reason'], 'invalid_arguments')
        self.assertIn('near', contents[1]['notice'])
        self.assertIn('near', json.dumps(maps.READ_YANDEX_MAPS_SEARCH['parameters']['properties']))


if __name__ == '__main__':
    unittest.main()
