"""Yandex Afisha tools: synthetic data, mocked browser and HTTP only."""
import asyncio
from datetime import date
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))
try:
    import playwright.async_api  # noqa: F401  (tools image)
except ImportError:
    package, api = ModuleType('playwright'), ModuleType('playwright.async_api')
    api.TimeoutError = type('TimeoutError', (Exception,), {})
    api.async_playwright = None
    package.async_api = api
    sys.modules.update({'playwright': package, 'playwright.async_api': api})
from providers import Generation, ToolCall
from model import ModelFailure
import yandex_afisha as afisha
import afisha_browser
import server as tools_server
import tool_client
import test_skill_loading as skill_tests

TODAY = date(2026, 10, 11)
EVENT = 'https://afisha.yandex.ru/moscow/musical/mamma-mimo'
LISTING = 'https://afisha.yandex.ru/moscow/theatre?date=2026-10-12&period=3'


class AfishaUrlTests(unittest.TestCase):
    def test_listing_url_is_rebuilt_from_validated_fields(self):
        self.assertEqual(afisha.listing_url('theatre', '2026-10-12', 3, today=TODAY), LISTING)
        for args in (('opera', '2026-10-12', 3), ('theatre', '12.10.2026', 3), ('theatre', '2026-10-12', 0),
                     ('theatre', '2026-10-12', 8), ('theatre', '2026-10-12', True), ('theatre', '2025-01-01', 1),
                     ('theatre', '2027-12-01', 1), ('theatre?search=x', '2026-10-12', 1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                afisha.listing_url(*args, today=TODAY)

    def test_event_urls_are_canonical_and_robots_blocked_paths_rejected(self):
        for url in (EVENT, EVENT + '/', EVENT + '#schedule', EVENT + '?utm_campaign=x',
                    '/moscow/musical/mamma-mimo', 'https://afisha.yandex.ru/moscow/musical/mamma-mimo'):
            with self.subTest(url=url):
                self.assertEqual(afisha.event_url(url), EVENT)
        self.assertEqual(afisha.event_url('https://afisha.yandex.ru/moscow/theatre_show/vysokie-otnosheniia'),
                         'https://afisha.yandex.ru/moscow/theatre_show/vysokie-otnosheniia')
        for url in ('http://afisha.yandex.ru/moscow/musical/x', 'https://afisha.yandex.ru.evil/moscow/musical/x',
                    'https://evil@afisha.yandex.ru/moscow/musical/x', 'https://afisha.yandex.ru/spb/musical/x',
                    'https://afisha.yandex.ru/moscow/theatre', 'https://afisha.yandex.ru/moscow/theatre/places/x',
                    'https://afisha.yandex.ru/moscow/selections/x', 'https://afisha.yandex.ru/moscow/concert/crocus-city-hall-x',
                    'https://afisha.yandex.ru/moscow/immersive-theatre/vernuvshiesia-msk',
                    'https://afisha.yandex.ru/moscow/search/x', 'https://afisha.yandex.ru/api/events/x',
                    'https://afisha.yandex.ru/moscow/musical/x\\y', 'https://yandex.ru/moscow/musical/x', 42):
            with self.subTest(url=url), self.assertRaises(ValueError):
                afisha.event_url(url)


class AfishaDataTests(unittest.TestCase):
    def test_listing_is_bounded_deduplicated_and_link_safe(self):
        raw = {'events': [{'url': '/moscow/musical/mamma-mimo', 'title': 'Мамма мимо', 'details': ['Мюзикл', '13 октября'],
                           'venue': 'Театр на Цветном', 'price': 'от 1 500 ₽', 'rating': '8.9'},
                          {'url': '/moscow/musical/mamma-mimo#schedule', 'title': 'Duplicate'},
                          {'url': 'https://evil.example/x', 'title': 'Off-site'},
                          {'url': '/moscow/show/x', 'title': 'Call https://phish.example now', 'rating': 'NaN'}]
                + [{'url': f'/moscow/concert/c{i}', 'title': f'C{i}'} for i in range(30)]}
        result = afisha.normalized_listing(LISTING, raw)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['returned_count'], afisha.MAX_LISTING_EVENTS)
        first = result['events'][0]
        self.assertEqual((first['url'], first['type'], first['when'], first['rating']),
                         (EVENT, 'Мюзикл', '13 октября', 8.9))
        self.assertNotIn('phish', json.dumps(result))
        self.assertNotIn('evil.example', json.dumps(result))
        self.assertIsNone(result['events'][1]['rating'])
        empty = afisha.normalized_listing(LISTING, {'events': []})
        self.assertEqual((empty['status'], empty['reason']), ('unavailable', 'no_events'))

    def test_event_uses_json_ld_and_sessions_without_comments(self):
        raw = {'ld': {'@type': 'TheaterEvent', 'name': 'Мамма мимо', 'description': 'Пишите sales@example.com '
                      'или https://phish.example', 'startDate': '2023-12-15T21:00:00.000Z', 'duration': 'PT1H40M',
                      'location': {'name': 'Театр на Цветном', 'address': {'streetAddress': 'Цветной бул., 11'}},
                      'offers': {'lowPrice': '1500.00', 'highPrice': '7000.00', 'priceCurrency': 'RUB',
                                 'availability': 'https://schema.org/InStock'},
                      'aggregateRating': {'ratingValue': 8.9, 'ratingCount': 1526}, 'review': [{'author': 'Private'}]},
               'labels': 'МюзиклШоуКомедия12+Мамма мимо', 'metro': ['Цветной бульвар', 'Трубная'],
               'sessions': [{'date': '13 октября вторник', 'time': '19:00', 'place': 'Основная сцена', 'price': 'от 2 000 ₽'}] * 2
                           + [{'date': f'{d} октября', 'time': '19:00'} for d in range(14, 30)]}
        result = afisha.normalized_event(EVENT, raw)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['venue'], {'name': 'Театр на Цветном', 'address': 'Цветной бул., 11',
                                           'metro': ['Цветной бульвар', 'Трубная']})
        self.assertEqual(result['price'], {'low': 1500.0, 'high': 7000.0, 'currency': 'RUB', 'availability': 'InStock'})
        self.assertEqual(result['age'], '12+')
        self.assertEqual(result['rating'], {'value': 8.9, 'count': 1526})
        self.assertEqual(result['returned_count'], afisha.MAX_SESSIONS)
        self.assertEqual(result['sessions'][1]['date'], '14 октября')  # Duplicate session dropped.
        text = json.dumps(result, ensure_ascii=False)
        for private in ('Private', 'sales@example.com', 'phish'):
            self.assertNotIn(private, text)
        self.assertEqual(afisha.normalized_event(EVENT, {})['reason'], 'event_not_loaded')


class AfishaBrowserTests(unittest.TestCase):
    def browser(self, url, raw, status=200):
        page = SimpleNamespace(url=url, set_default_timeout=MagicMock(),
                               goto=AsyncMock(return_value=SimpleNamespace(status=status)),
                               evaluate=AsyncMock(return_value=raw), mouse=SimpleNamespace(wheel=AsyncMock()))
        locator = SimpleNamespace(wait_for=AsyncMock(), count=AsyncMock(return_value=0))
        page.locator = MagicMock(return_value=SimpleNamespace(first=locator, last=locator, count=locator.count))
        context = SimpleNamespace(route=AsyncMock(), route_web_socket=AsyncMock(),
                                  add_init_script=AsyncMock(), new_page=AsyncMock(return_value=page))
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=playwright)
        manager.__aexit__ = AsyncMock(return_value=False)
        return page, browser, manager

    def run_reader(self, reader, url, manager):
        with patch('playwright.async_api.async_playwright', return_value=manager), \
                patch('browser.asyncio.sleep', new=AsyncMock()):
            return asyncio.run(reader(url))

    def test_listing_and_event_readers_close_browser(self):
        page, browser, manager = self.browser(LISTING, {'events': [{'url': '/moscow/musical/mamma-mimo', 'title': 'Мамма мимо'}]})
        self.assertEqual(self.run_reader(afisha_browser._read_listing, LISTING, manager)['returned_count'], 1)
        browser.close.assert_awaited_once()
        page, browser, manager = self.browser(EVENT + '#schedule', {'ld': {'@type': 'TheaterEvent', 'name': 'Мамма мимо'}})
        self.assertEqual(self.run_reader(afisha_browser._read_event, EVENT, manager)['name'], 'Мамма мимо')

    def test_redirects_captcha_and_errors_are_unavailable(self):
        cases = [(afisha_browser._read_event, EVENT, 'https://afisha.yandex.ru/moscow/musical/other', 200, 'redirected'),
                 (afisha_browser._read_listing, LISTING, 'https://afisha.yandex.ru/spb/theatre', 200, 'redirected'),
                 (afisha_browser._read_event, EVENT, 'https://afisha.yandex.ru/showcaptcha?x', 200, 'captcha'),
                 (afisha_browser._read_event, EVENT, EVENT, 404, 'not_found')]
        for reader, url, landed, status, reason in cases:
            with self.subTest(reason=reason, landed=landed):
                page, browser, manager = self.browser(landed, {}, status)
                self.assertEqual(self.run_reader(reader, url, manager)['reason'], reason)
                browser.close.assert_awaited_once()
        page, browser, manager = self.browser(EVENT, {})
        page.goto.side_effect = RuntimeError('synthetic-secret')
        result = self.run_reader(afisha_browser._read_event, EVENT, manager)
        self.assertEqual(result['reason'], 'browser_failed')
        self.assertNotIn('synthetic', json.dumps(result))


class AfishaServiceTests(unittest.TestCase):
    def test_routes_validate_before_browsing(self):
        with patch('server.read_listing', return_value=afisha.normalized_listing(LISTING, {'events': []})) as read, \
                patch('yandex_afisha.datetime') as clock:
            clock.now.return_value.date.return_value = TODAY
            status, body = tools_server.afisha_listing(json.dumps({'category': 'theatre', 'date_from': '2026-10-12', 'days': 3}))
        self.assertEqual(status, 200)
        read.assert_called_once_with(LISTING)
        with patch('server.read_event') as read:
            for bad in ('{"url":"https://evil.example/x"}', '{"url":"%s","x":1}' % EVENT, '[]'):
                self.assertEqual(tools_server.afisha_event(bad)[0], 400)
            read.assert_not_called()
        self.assertEqual(tools_server.ROUTES['/v1/afisha-event'], tools_server.afisha_event)

    def test_client_checks_response_shape(self):
        sample = afisha.normalized_event(EVENT, {'ld': {'@type': 'TheaterEvent', 'name': 'Мамма мимо'}})
        real = httpx.Client
        def client(handler):
            return patch('tool_client.httpx.Client', lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
        def handler(request):
            self.assertEqual(request.url.path, '/v1/afisha-event')
            self.assertEqual(json.loads(request.content), {'url': EVENT})
            return httpx.Response(200, json=sample)
        with client(handler):
            self.assertEqual(tool_client.read_afisha_event(EVENT + '/', 'http://tools:8080')['status'], 'ok')
        with client(lambda _: httpx.Response(200, json={**sample, 'url': 'https://afisha.yandex.ru/moscow/x/y'})):
            self.assertEqual(tool_client.read_afisha_event(EVENT, 'http://tools:8080')['reason'], 'browser_failed')
        def down(request): raise httpx.ConnectError('synthetic')
        with client(down):
            self.assertEqual(tool_client.read_afisha_event(EVENT, 'http://tools:8080')['reason'], 'browser_unavailable')


class AfishaToolLoopTests(unittest.TestCase):
    def model(self):
        helper = skill_tests.SkillLoadingTests()
        model = helper.model('zai', lambda _: self.fail('No SDK request needed'))
        self.addCleanup(helper.doCleanups)
        return model

    def test_listing_then_event_reads_feed_evidence_and_are_capped(self):
        model = self.model()
        listing = afisha.normalized_listing(LISTING, {'events': [{'url': '/moscow/musical/mamma-mimo', 'title': 'Мамма мимо'}]})
        event = afisha.normalized_event(EVENT, {'ld': {'@type': 'TheaterEvent', 'name': 'Мамма мимо'}})
        reads = [ToolCall('l', 'read_afisha_listing', '{"category":"theatre","date_from":"2026-10-12","days":3}')]
        reads += [ToolCall(f'e{i}', 'read_afisha_event', json.dumps({'url': f'https://afisha.yandex.ru/moscow/musical/m{i}'}))
                  for i in range(4)]
        with patch.object(model, '_generation_attempt', side_effect=[
                Generation('', True, tool_calls=(reads[0],)), Generation('', True, tool_calls=tuple(reads[1:])),
                Generation('Done', True)]) as generate, \
                patch('model.read_afisha_listing', return_value=listing) as read_listing, \
                patch('model.read_afisha_event', side_effect=lambda url, base: {**event, 'url': url}) as read_event, \
                patch('model.datetime') as clock:
            clock.now.return_value.date.return_value = TODAY
            self.assertIn('Done', model.generate('Theatre?', [], allow_search=True))
        read_listing.assert_called_once_with('theatre', '2026-10-12', 3, model.config.tools_url)
        self.assertEqual(read_event.call_count, 3)
        self.assertIn('review_limit', generate.call_args.kwargs['continuation'][-1]['content'])

    def test_invalid_afisha_targets_go_back_to_the_model(self):
        model = self.model()
        calls = (ToolCall('a', 'read_afisha_event', '{"url":"https://afisha.yandex.ru/moscow/search/x"}'),
                 ToolCall('b', 'read_afisha_listing', '{"category":"opera","date_from":"2026-10-12","days":3}'))
        with patch.object(model, '_generation_attempt', side_effect=[Generation('', True, tool_calls=calls),
                Generation('Done', True)]) as generate, patch('model.read_afisha_event') as read_event, \
                patch('model.read_afisha_listing') as read_listing:
            model.generate('Theatre?', [], allow_search=True)
        read_event.assert_not_called()
        read_listing.assert_not_called()
        results = [json.loads(item['content']) for item in generate.call_args.kwargs['continuation']
                   if item.get('role') == 'tool']
        self.assertEqual([r['reason'] for r in results], ['invalid_url', 'invalid_arguments'])

    def test_wrong_reader_argument_keys_are_a_protocol_error(self):
        model = self.model()
        call = ToolCall('a', 'read_afisha_listing', '{"category":"theatre"}')
        with patch.object(model, '_generation_attempt', return_value=Generation('', True, tool_calls=(call,))), \
                patch('model.read_afisha_listing') as read, self.assertRaises(ModelFailure):
            model.generate('Theatre?', [], allow_search=True)
        read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
