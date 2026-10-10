"""Public review tools: synthetic DOM data and mocked browser/network only."""
import asyncio
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))
try:
    import playwright.async_api  # noqa: F401  (tools image)
except ImportError:
    # The bot image has no Playwright; a stand-in module lets tests patch it.
    package, api = ModuleType('playwright'), ModuleType('playwright.async_api')
    api.TimeoutError = type('TimeoutError', (Exception,), {})
    api.async_playwright = None
    package.async_api = api
    sys.modules.update({'playwright': package, 'playwright.async_api': api})
from providers import Generation, ToolCall
from model import ModelFailure, MAX_REVIEW_READS
from yandex_reviews import (card_url, organization_id, public_resource, normalized_sample, unavailable,
                            MAX_TEXT_CHARS)
import review_browser
import server as tools_server
import tool_client
import test_skill_loading as skill_tests

URL = 'https://yandex.ru/maps/org/123456/reviews/'


class ReviewDataTests(unittest.TestCase):
    def test_cards_are_canonicalized_and_queries_removed(self):
        for url in (URL, 'https://yandex.ru/maps/org/cafe/123456/?utm_source=test',
                    'https://www.yandex.com/maps/org/123456#reviews',
                    'https://yandex.ru/maps/213/moscow/org/cafe/123456/',
                    'https://yandex.ru/maps/2/saint-petersburg/org/123456/reviews/',
                    'https://yandex.ru/maps/?ll=37.6%2C55.7&oid=123456&ol=biz',
                    'https://yandex.kz/maps/org/cafe/123456/', 'https://yandex.by/maps/org/123456/'):
            with self.subTest(url=url):
                self.assertEqual(card_url(url), URL)

    def test_non_cards_and_private_or_credential_urls_rejected(self):
        for url in ('http://yandex.ru/maps/org/1', 'https://127.0.0.1/maps/org/1',
                    'https://yandex.ru.evil/maps/org/1', 'https://evil@yandex.ru/maps/org/1',
                    'https://yandex.ru:444/maps/org/1', 'file:///tmp/test',
                    'https://yandex.ru/maps/search/cafe', 'https://yandex.ru/maps/org/1/../../admin',
                    'https://yandex.ru/maps/org/1\\secret', 123, 'https://yandex.ru/maps/org/%31',
                    'https://yandex.ru/maps/-/CCUabc', 'https://yandex.ru/maps/?text=cafe',
                    'https://yandex.ru/search/?oid=123'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                card_url(url)

    def test_organization_id_ignores_regional_redirect_path(self):
        self.assertEqual(organization_id('https://yandex.ru/maps/213/moscow/org/cafe/123456/reviews/'), '123456')
        self.assertIsNone(organization_id('https://evil.test/'))

    def test_browser_resource_filter_blocks_external_private_and_ad_hosts(self):
        self.assertTrue(public_resource('https://yastatic.net/maps.js'))
        self.assertTrue(public_resource('https://avatars.mds.yandex.net/x'))
        for url in ('https://yandex.net.evil/script.js', 'http://yandex.ru/',
                    'https://localhost/', 'https://10.0.0.1/', 'https://a@yandex.ru/',
                    'file:///tmp/secret', 'https://yandex.ru:555/', 'https://clck.yandex.ru/redir/x',
                    'https://an.yandex.ru/ads', 'https://mc.yandex.ru/watch/1'):
            with self.subTest(url=url):
                self.assertFalse(public_resource(url))

    def test_sample_deduplicates_and_excludes_authors_and_missing_ratings(self):
        result = normalized_sample(URL, {'name': 'Cafe', 'rating': '4,6', 'rating_count': '300',
            'review_count': '180', 'reviews': [{'text': ' Nice coffee ', 'rating': '5',
            'date': '2026-10-01', 'author': 'private-name', 'profile': 'private-profile'},
            {'text': 'Nice coffee', 'rating': 5, 'date': '2026-10-01'},
            {'text': 'Noisy', 'rating': 'NaN'}, {'text': ''}]})
        self.assertEqual(result['returned_count'], 2)
        self.assertEqual(result['company']['rating_count'], 300)
        self.assertEqual(result['company']['review_count'], 180)
        self.assertEqual(result['company']['rating'], 4.6)
        self.assertIsNone(result['reviews'][1]['rating'])
        self.assertNotIn('private-', json.dumps(result))
        self.assertEqual(result['sampling'], 'page_order')

    def test_unicode_digit_counts_are_ignored_not_fatal(self):
        result = normalized_sample(URL, {'rating_count': '²', 'review_count': '١٢', 'reviews': [{'text': 'Ok'}]})
        self.assertIsNone(result['company']['rating_count'])
        self.assertIsNone(result['company']['review_count'])
        self.assertEqual(result['status'], 'ok')

    def test_links_in_review_text_are_removed(self):
        result = normalized_sample(URL, {'name': 'Cafe http://phish.example/x', 'reviews': [
            {'text': 'Great! Order at https://phish.example/pay or www.phish.example now'}]})
        self.assertNotIn('phish', json.dumps(result))
        self.assertIn('[link removed]', result['reviews'][0]['text'])

    def test_large_sample_and_text_are_bounded_with_truncation_disclosed(self):
        result = normalized_sample(URL, {'reviews': [{'text': str(i) + 'x' * 5000} for i in range(100)]})
        self.assertLessEqual(result['returned_count'], 20)
        self.assertEqual(sum(len(r['text']) for r in result['reviews']), MAX_TEXT_CHARS)
        self.assertTrue(all(r['text_truncated'] for r in result['reviews']))
        empty = normalized_sample(URL, {'reviews': [{'rating': 5}]})
        self.assertEqual(empty['status'], 'unavailable')
        self.assertEqual(empty['reason'], 'no_review_texts')


class BrowserReaderTests(unittest.TestCase):
    def test_outer_timeout_returns_unavailable_without_exception_text(self):
        async def slow(url):
            raise TimeoutError('synthetic-private-error')
        with patch('review_browser._read', slow):
            result = review_browser.read_reviews(URL)
        self.assertEqual(result['reason'], 'timeout')
        self.assertNotIn('synthetic-private', json.dumps(result))

    def browser(self, url=URL, status=200, raw=None):
        page = SimpleNamespace(url=url, set_default_timeout=MagicMock(), goto=AsyncMock(
            return_value=SimpleNamespace(status=status)), evaluate=AsyncMock(return_value=raw or {
            'name': 'Cafe', 'reviews': [{'text': 'Good coffee', 'rating': 5}]}))
        locator = SimpleNamespace(wait_for=AsyncMock(), scroll_into_view_if_needed=AsyncMock(), count=AsyncMock(return_value=0))
        page.locator = MagicMock(return_value=SimpleNamespace(first=locator, last=locator, count=locator.count))
        context = SimpleNamespace(route=AsyncMock(), route_web_socket=AsyncMock(),
                                  add_init_script=AsyncMock(), new_page=AsyncMock(return_value=page))
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=playwright)
        manager.__aexit__ = AsyncMock(return_value=False)
        return page, browser, manager, playwright, locator

    def read(self, manager):
        with patch('playwright.async_api.async_playwright', return_value=manager), \
                patch('review_browser.asyncio.sleep', new=AsyncMock()):
            return asyncio.run(review_browser._read(URL))

    def test_browser_closes_on_success_and_gets_no_secret_environment(self):
        page, browser, manager, playwright, _ = self.browser()
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'synthetic-key', 'PGPASSWORD': 'synthetic-db'}):
            result = self.read(manager)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['returned_count'], 1)
        browser.close.assert_awaited_once()
        launch = playwright.chromium.launch.call_args.kwargs
        self.assertEqual(launch['env']['HOME'], '/tmp')
        self.assertNotIn('synthetic', json.dumps(launch['env']))

    def test_scroll_timeout_keeps_reviews_already_read(self):
        page, browser, manager, _, locator = self.browser()
        locator.scroll_into_view_if_needed.side_effect = sys.modules['playwright.async_api'].TimeoutError()
        result = self.read(manager)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['reviews'][0]['text'], 'Good coffee')

    def test_regional_redirect_of_same_card_is_accepted(self):
        page, _, manager, _, _ = self.browser(url='https://yandex.ru/maps/213/moscow/org/cafe/123456/reviews/')
        self.assertEqual(self.read(manager)['status'], 'ok')

    def test_hung_close_is_bounded(self):
        page, browser, manager, _, _ = self.browser()
        async def hang():
            await asyncio.sleep(3600)
        browser.close = hang
        with patch('review_browser.CLOSE_TIMEOUT', 0.01):
            self.assertEqual(self.read(manager)['status'], 'ok')

    def test_browser_captcha_not_found_redirect_and_exception_are_safe(self):
        for url, status, expected in [('https://yandex.ru/showcaptcha', 200, 'captcha'),
                                      (URL, 404, 'not_found'), ('https://evil.test/', 200, 'redirected'),
                                      ('https://yandex.ru/maps/org/999/reviews/', 200, 'redirected')]:
            page, browser, manager, _, _ = self.browser(url=url, status=status)
            result = self.read(manager)
            self.assertEqual(result['reason'], expected)
            browser.close.assert_awaited_once()
            page.evaluate.assert_not_awaited()
        page, browser, manager, _, _ = self.browser()
        page.goto.side_effect = RuntimeError('synthetic-secret-page-text')
        result = self.read(manager)
        self.assertEqual(result['reason'], 'browser_failed')
        self.assertNotIn('synthetic-secret', json.dumps(result))
        browser.close.assert_awaited_once()


class ToolServiceTests(unittest.TestCase):
    def test_service_validates_and_returns_sample(self):
        sample = normalized_sample(URL, {'reviews': [{'text': 'Good'}]})
        with patch('server.read_reviews', return_value=sample) as read:
            status, body = tools_server.reviews(json.dumps({'url': 'https://yandex.ru/maps/org/x/123456/'}))
        self.assertEqual((status, body['status']), (200, 'ok'))
        read.assert_called_once_with(URL)
        for bad in ('not json', '[]', '{"url":"https://127.0.0.1/"}', '{"url":"%s","x":1}' % URL):
            with self.subTest(body=bad), patch('server.read_reviews') as read:
                status, body = tools_server.reviews(bad)
                self.assertEqual((status, body['reason']), (400, 'invalid_url'))
                read.assert_not_called()

    def test_only_one_browser_runs_at_a_time(self):
        with patch('server.BUSY_WAIT_SECONDS', 0.01):
            self.assertTrue(tools_server.BROWSER_SLOTS.acquire(timeout=1))
            try:
                with patch('server.read_reviews') as read:
                    status, body = tools_server.reviews(json.dumps({'url': URL}))
                read.assert_not_called()
                self.assertEqual(body['reason'], 'busy')
            finally:
                tools_server.BROWSER_SLOTS.release()


class ToolClientTests(unittest.TestCase):
    def client(self, handler):
        real = httpx.Client
        return patch('tool_client.httpx.Client', lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    def test_client_posts_canonical_url_and_returns_result(self):
        sample = normalized_sample(URL, {'reviews': [{'text': 'Good'}]})
        def handler(request):
            self.assertEqual(str(request.url), 'http://tools:8080/v1/yandex-reviews')
            self.assertEqual(json.loads(request.content), {'url': URL})
            return httpx.Response(200, json=sample)
        with self.client(handler):
            self.assertEqual(tool_client.read_reviews(URL, 'http://tools:8080')['status'], 'ok')

    def test_unreachable_or_malformed_service_is_unavailable(self):
        def down(request): raise httpx.ConnectError('synthetic http://tools:8080 detail')
        with self.client(down):
            result = tool_client.read_reviews(URL, 'http://tools:8080')
        self.assertEqual(result['reason'], 'browser_unavailable')
        self.assertNotIn('synthetic', json.dumps(result))
        for response in (httpx.Response(500, json={}), httpx.Response(200, json={'status': 'ok'}),
                         httpx.Response(200, json={**unavailable('https://yandex.ru/maps/org/9/reviews/', 'x')}),
                         httpx.Response(200, text='<html>')):
            with self.subTest(response=response), self.client(lambda _: response):
                self.assertEqual(tool_client.read_reviews(URL, 'http://tools:8080')['reason'], 'browser_failed')
        self.assertEqual(tool_client.read_reviews(URL, '')['reason'], 'browser_unavailable')


class ReviewToolTests(unittest.TestCase):
    def model(self, provider, handler):
        # Reuse SDK/MockTransport setup without inheriting its unrelated tests.
        helper = skill_tests.SkillLoadingTests()
        model = helper.model(provider, handler)
        self.addCleanup(helper.doCleanups)
        return model

    def response(self, provider, **kwargs):
        return skill_tests.SkillLoadingTests().response(provider, **kwargs)

    def test_native_tool_roundtrip_for_both_providers_and_no_search_fee_for_read(self):
        sample = normalized_sample(URL, {'name': 'Cafe', 'reviews': [{'text': 'Good coffee', 'rating': 5}]})
        for provider in ('openai', 'zai'):
            requests = []
            def handler(request):
                body = json.loads(request.content)
                requests.append(body)
                if len(requests) == 1:
                    self.assertIn('read_yandex_reviews', json.dumps(body['tools']))
                    return self.response(provider, call=('read_yandex_reviews', json.dumps({'url': URL})))
                outputs = body['input'] if provider == 'openai' else body['messages']
                self.assertTrue(any('Good coffee' in json.dumps(item) for item in outputs))
                return self.response(provider, text='Cafe has good coffee')
            model = self.model(provider, handler)
            with patch('model.read_reviews', return_value=sample) as read:
                answer = model.generate('Lookup', [], allow_search=True)
            self.assertIn(URL, answer)
            read.assert_called_once_with(URL, model.config.tools_url)
            self.assertEqual(len(model.store.charges), 2)  # Model calls only.

    def test_unavailable_reviews_are_returned_to_model_without_aborting_answer(self):
        model = self.model('zai', lambda _: self.fail('No SDK request needed'))
        call = ToolCall('review', 'read_yandex_reviews', json.dumps({'url': URL}))
        with patch.object(model, '_generation_attempt', side_effect=[Generation('', True, tool_calls=(call,)),
                Generation('Reviews are blocked', True)]), patch('model.read_reviews', return_value=unavailable(URL, 'captcha')):
            self.assertEqual(model.generate('Lookup', [], allow_search=True), 'Reviews are blocked')

    def test_invalid_url_becomes_tool_error_and_other_calls_still_run(self):
        model = self.model('zai', lambda _: self.fail('No SDK request needed'))
        calls = (ToolCall('search', 'search_web', '{"query":"public cafe"}'),
                 ToolCall('read', 'read_yandex_reviews', '{"url":"https://yandex.ru/maps/-/CCUshort"}'))
        with patch.object(model, '_generation_attempt', side_effect=[Generation('', True, tool_calls=calls),
                Generation('Answer', True)]) as generate, \
                patch('model.read_reviews') as read, patch.object(model, '_search_web', return_value=[]) as search:
            self.assertEqual(model.generate('Lookup', [], allow_search=True), 'Answer')
        read.assert_not_called()
        search.assert_called_once()
        self.assertIn('invalid_url', json.dumps(generate.call_args.kwargs['continuation']))

    def test_reader_is_not_used_when_web_disabled(self):
        model = self.model('zai', lambda _: self.fail('No SDK request needed'))
        call = ToolCall('read', 'read_yandex_reviews', json.dumps({'url': URL}))
        with patch.object(model, '_generation_attempt', return_value=Generation('', True, tool_calls=(call,))), \
                patch('model.read_reviews') as read:
            with self.assertRaises(ModelFailure):
                model.generate('Lookup', [], allow_search=False)
        read.assert_not_called()

    def test_review_reads_are_capped_per_request_and_every_call_is_answered(self):
        model = self.model('zai', lambda _: self.fail('No SDK request needed'))
        calls = tuple(ToolCall(f'read{i}', 'read_yandex_reviews', json.dumps({
            'url': f'https://yandex.ru/maps/org/{i + 1}/reviews/'})) for i in range(4))
        sample = normalized_sample(URL, {'reviews': [{'text': 'Coffee review'}]})
        with patch.object(model, '_generation_attempt', side_effect=[Generation('', True, tool_calls=calls,
                continuation=({'role': 'assistant', 'tool_calls': [{'id': c.call_id} for c in calls]},)),
                Generation('Done', True)]) as generate, patch('model.read_reviews', return_value=sample) as read:
            self.assertIn('Done', model.generate('Lookup', [], allow_search=True))
        self.assertEqual(read.call_count, MAX_REVIEW_READS)
        continuation = generate.call_args.kwargs['continuation']
        self.assertEqual(len(continuation), 5)  # One assistant batch, four matching results.
        self.assertEqual([item['tool_call_id'] for item in continuation[1:]], [c.call_id for c in calls])
        self.assertIn('review_limit', continuation[-1]['content'])

    def test_same_card_is_read_once_per_request(self):
        model = self.model('zai', lambda _: self.fail('No SDK request needed'))
        calls = (ToolCall('a', 'read_yandex_reviews', json.dumps({'url': URL})),
                 ToolCall('b', 'read_yandex_reviews', json.dumps({'url': 'https://yandex.ru/maps/org/cafe/123456/'})))
        sample = normalized_sample(URL, {'reviews': [{'text': 'Coffee review'}]})
        with patch.object(model, '_generation_attempt', side_effect=[Generation('', True, tool_calls=calls),
                Generation('Done', True)]), patch('model.read_reviews', return_value=sample) as read:
            model.generate('Lookup', [], allow_search=True)
        read.assert_called_once()

    def test_review_read_does_not_satisfy_explicit_search(self):
        model = self.model('zai', lambda _: self.fail('No SDK request needed'))
        call = ToolCall('read', 'read_yandex_reviews', json.dumps({'url': URL}))
        sample = normalized_sample(URL, {'reviews': [{'text': 'Coffee review'}]})
        responses = [Generation('', True, tool_calls=(call,))] + [Generation('No search', True)] * 6
        with patch.object(model, '_generation_attempt', side_effect=responses), \
                patch('model.read_reviews', return_value=sample):
            with self.assertRaises(ModelFailure):
                model.generate('Lookup', [], allow_search=True, require_search=True)


if __name__ == '__main__':
    unittest.main()
