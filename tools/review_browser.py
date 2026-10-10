"""Read a bounded public Maps review sample in an isolated Chromium context.

Runs only inside the tools container, which holds no bot secrets and cannot
reach the database. DOM selectors informed by mrkhachaturov/ya-reviews-mcp (MIT).
"""
import asyncio
from yandex_reviews import (EXTRACT, MAX_REVIEWS, REVIEW_SELECTOR, card_url, normalized_sample,
                            organization_id, public_resource, unavailable)

# Chromium needs a writable profile/crashpad directory; the root filesystem is read-only.
BROWSER_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp', 'XDG_CONFIG_HOME': '/tmp/.config',
               'XDG_CACHE_HOME': '/tmp/.cache', 'LANG': 'C.UTF-8'}
BROWSER_ARGS = ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--disable-breakpad',
                '--disable-crash-reporter', '--no-first-run']
READ_TIMEOUT = 40
CLOSE_TIMEOUT = 5


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
    try:
        from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout
    except ImportError:
        return unavailable(url, 'browser_unavailable')
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
                                                       args=BROWSER_ARGS, env=BROWSER_ENV)
            try:
                context = await browser.new_context(locale='ru-RU', accept_downloads=False,
                                                    service_workers='block')
                async def route_request(route):
                    if (not public_resource(route.request.url)
                            or route.request.resource_type in {'image', 'media', 'font'}):
                        await route.abort()
                    else:
                        await route.continue_()
                await context.route('**/*', route_request)
                await context.route_web_socket('**/*', lambda socket: socket.close())
                await context.add_init_script('window.RTCPeerConnection = undefined;')
                page = await context.new_page()
                page.set_default_timeout(5000)
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
                    if await page.locator('form[action*="checkcaptcha"], .CheckboxCaptcha, #captcha').count():
                        return unavailable(url, 'captcha')
                    return unavailable(url, 'reviews_not_loaded')
                raw = await _scroll_for_more(page, await page.evaluate(EXTRACT))
                return normalized_sample(url, raw)
            finally:
                try:
                    await asyncio.wait_for(browser.close(), CLOSE_TIMEOUT)
                except Exception:
                    pass
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
