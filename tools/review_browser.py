"""Read a bounded public Maps review sample in an isolated Chromium context.

Runs only inside the tools container, which holds no bot secrets and cannot
reach the database. DOM selectors informed by mrkhachaturov/ya-reviews-mcp (MIT).
"""
import asyncio
from yandex_reviews import (EXTRACT, MAX_REVIEWS, REVIEW_SELECTOR, card_url, normalized_sample,
                            organization_id, unavailable)
from browser import BROWSER_ARGS, BROWSER_ENV, captcha, page_session, playwright_api  # noqa: F401 (re-export)

READ_TIMEOUT = 40


async def _scroll_for_more(page, raw):
    """Best effort: a slow lazy load must not discard reviews already collected."""
    for _ in range(3):
        if len(raw.get('reviews', [])) >= MAX_REVIEWS:
            break
        previous = len(raw.get('reviews', []))
        try:
            await page.locator(REVIEW_SELECTOR).last.scroll_into_view_if_needed()
            await asyncio.sleep(0.7)
            current = await page.evaluate(EXTRACT)
        except Exception:
            break
        if len(current.get('reviews', [])) <= previous:
            break
        raw = current
    return raw


async def _read(url):
    # Lazy import keeps validation importable without Playwright installed.
    api = playwright_api()
    if api is None:
        return unavailable(url, 'browser_unavailable')
    async_playwright, BrowserTimeout = api
    try:
        async with page_session(async_playwright) as page:
            response = await page.goto(url, wait_until='domcontentloaded', timeout=15000)
            if 'showcaptcha' in page.url or 'captcha' in page.url:
                return unavailable(url, 'captcha')
            if response and response.status >= 400:
                return unavailable(url, 'not_found' if response.status == 404 else 'access_denied')
            if organization_id(page.url) != organization_id(url):
                return unavailable(url, 'redirected')
            try:
                await page.locator(REVIEW_SELECTOR).first.wait_for(timeout=10000)
            except BrowserTimeout:
                if await captcha(page):
                    return unavailable(url, 'captcha')
                return unavailable(url, 'reviews_not_loaded')
            raw = await _scroll_for_more(page, await page.evaluate(EXTRACT))
            return normalized_sample(url, raw)
    except BrowserTimeout:
        return unavailable(url, 'timeout')
    except Exception:
        # Browser exceptions may contain URLs, page text or local paths.
        return unavailable(url, 'browser_failed')


def read_reviews(value):
    url = card_url(value)
    try:
        return asyncio.run(asyncio.wait_for(_read(url), timeout=READ_TIMEOUT))
    except TimeoutError:
        return unavailable(url, 'timeout')
