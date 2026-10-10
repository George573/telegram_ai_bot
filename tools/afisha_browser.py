"""Read Yandex Afisha (Moscow) listings and event pages in an isolated Chromium context."""
import asyncio
from browser import captcha, page_session, playwright_api, scroll_for_more
from yandex_afisha import (EXTRACT_EVENT, EXTRACT_LISTING, MAX_LISTING_EVENTS, event_url, normalized_event,
                           normalized_listing, unavailable)

READ_TIMEOUT = 40
LISTING_CARD = '[data-test-id="eventCard.root"]'


async def _open(page, url, BrowserTimeout):
    """Navigate and return an unavailable reason, or None when the page is usable."""
    response = await page.goto(url, wait_until='domcontentloaded', timeout=15000)
    if 'captcha' in page.url:
        return 'captcha'
    if response and response.status >= 400:
        return 'not_found' if response.status == 404 else 'access_denied'
    return None


async def _read_listing(url):
    api = playwright_api()
    if api is None:
        return unavailable(url, 'browser_unavailable')
    async_playwright, BrowserTimeout = api
    try:
        async with page_session(async_playwright) as page:
            reason = await _open(page, url, BrowserTimeout)
            if reason:
                return unavailable(url, reason)
            # Listing query parameters may be normalized; only the host/city/category must hold.
            if not page.url.startswith(url.split('?')[0]):
                return unavailable(url, 'redirected')
            try:
                await page.locator(LISTING_CARD).first.wait_for(timeout=10000)
            except BrowserTimeout:
                return unavailable(url, 'captcha' if await captcha(page) else 'no_events')
            raw = await scroll_for_more(page, EXTRACT_LISTING, await page.evaluate(EXTRACT_LISTING),
                                        'events', MAX_LISTING_EVENTS)
            return normalized_listing(url, raw)
    except BrowserTimeout:
        return unavailable(url, 'timeout')
    except Exception:
        # Browser exceptions may contain URLs, page text or local paths.
        return unavailable(url, 'browser_failed')


async def _read_event(url):
    api = playwright_api()
    if api is None:
        return unavailable(url, 'browser_unavailable')
    async_playwright, BrowserTimeout = api
    try:
        async with page_session(async_playwright) as page:
            reason = await _open(page, url, BrowserTimeout)
            if reason:
                return unavailable(url, reason)
            try:
                if event_url(page.url) != url:
                    return unavailable(url, 'redirected')
            except ValueError:
                return unavailable(url, 'redirected')
            try:
                await page.locator('script[type="application/ld+json"]').first.wait_for(state='attached', timeout=10000)
            except BrowserTimeout:
                return unavailable(url, 'captcha' if await captcha(page) else 'event_not_loaded')
            return normalized_event(url, await page.evaluate(EXTRACT_EVENT))
    except BrowserTimeout:
        return unavailable(url, 'timeout')
    except Exception:
        return unavailable(url, 'browser_failed')


def _run(reader, url):
    try:
        return asyncio.run(asyncio.wait_for(reader(url), timeout=READ_TIMEOUT))
    except TimeoutError:
        return unavailable(url, 'timeout')


def read_listing(url):
    return _run(_read_listing, url)


def read_event(url):
    return _run(_read_event, event_url(url))
