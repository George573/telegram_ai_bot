"""Shared hardened Chromium session for every browser-backed tool."""
import asyncio
from contextlib import asynccontextmanager
from yandex_reviews import public_resource

# Chromium needs a writable profile/crashpad directory; the root filesystem is read-only.
BROWSER_ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp', 'XDG_CONFIG_HOME': '/tmp/.config',
               'XDG_CACHE_HOME': '/tmp/.cache', 'LANG': 'C.UTF-8'}
BROWSER_ARGS = ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--disable-breakpad',
                '--disable-crash-reporter', '--no-first-run']
CLOSE_TIMEOUT = 5


def playwright_api():
    """(async_playwright, TimeoutError), or None when Playwright is not installed."""
    try:
        from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout
    except ImportError:
        return None
    return async_playwright, BrowserTimeout


@asynccontextmanager
async def page_session(async_playwright):
    """Yield a page in a fresh anonymous context; requests stay on public Yandex/CDN hosts."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
                                                   args=BROWSER_ARGS, env=BROWSER_ENV)
        try:
            context = await browser.new_context(locale='ru-RU', accept_downloads=False, service_workers='block')
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
            yield page
        finally:
            try:
                await asyncio.wait_for(browser.close(), CLOSE_TIMEOUT)
            except Exception:
                pass


async def captcha(page):
    if 'showcaptcha' in page.url or 'captcha' in page.url:
        return True
    return bool(await page.locator('form[action*="checkcaptcha"], .CheckboxCaptcha, #captcha').count())


async def scroll_for_more(page, extract, raw, key, enough):
    """Best effort lazy-load: a slow scroll must not discard items already collected."""
    for _ in range(3):
        if len(raw.get(key, [])) >= enough:
            break
        previous = len(raw.get(key, []))
        try:
            await page.mouse.wheel(0, 4000)
            await asyncio.sleep(0.7)
            current = await page.evaluate(extract)
        except Exception:
            break
        if len(current.get(key, [])) <= previous:
            break
        raw = current
    return raw
