"""Optional Chromium check against local HTML, with all networking blocked."""
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
from yandex_reviews import EXTRACT, normalized_sample
# Production launch settings, so the hardened-container run catches startup failures.
from review_browser import BROWSER_ARGS, BROWSER_ENV


class ReviewDOMTests(unittest.TestCase):
    def test_review_dom_extraction_in_offline_chromium(self):
        binaries = list(Path('/tmp/telegram-review-browser').glob('chromium_headless_shell-*/chrome-linux/headless_shell'))
        binaries += list(Path('/tmp/telegram-review-browser').glob('chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell'))
        if Path('/usr/bin/chromium').is_file():
            binaries.insert(0, Path('/usr/bin/chromium'))
        if not binaries:
            self.skipTest('Chromium is not installed in this test environment')
        async def check():
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(executable_path=str(binaries[0]), args=BROWSER_ARGS, env=BROWSER_ENV)
                try:
                    context = await browser.new_context(offline=True)
                    await context.route('**/*', lambda route: route.abort())
                    page = await context.new_page()
                    await page.set_content('''
                        <h1 class="orgpage-header-view__header">Synthetic Cafe</h1>
                        <a class="business-contacts-view__address-link">Synthetic Street 1</a>
                        <span class="business-summary-rating-badge-view__rating">4,8</span>
                        <script type="application/ld+json">{"@type":"CafeOrCoffeeShop",
                        "aggregateRating":{"ratingValue":4.8,"ratingCount":300,"reviewCount":120}}</script>
                        <div class="business-reviews-card-view__review">
                            <span itemprop="name">Private author</span>
                            <meta itemprop="datePublished" content="2026-10-10">
                            <meta itemprop="ratingValue" content="5">
                            <div class="business-review-view__body"><span class="spoiler-view__text-container">Good coffee</span></div>
                        </div>
                        <div class="business-reviews-card-view__review">
                            <span class="business-review-view__date">9 октября</span>
                            <div class="business-rating-badge-view__stars"><span class="_full"></span><span class="_full"></span></div>
                            <div class="business-review-view__body">Noisy room</div>
                        </div>''')
                    raw = await page.evaluate(EXTRACT)
                    return normalized_sample('https://yandex.ru/maps/org/123/reviews/', raw)
                finally:
                    await browser.close()
        result = asyncio.run(check())
        self.assertEqual(result['company']['name'], 'Synthetic Cafe')
        self.assertEqual(result['company']['rating_count'], 300)
        self.assertEqual(result['company']['review_count'], 120)
        self.assertEqual(result['company']['rating'], 4.8)
        self.assertEqual([r['text'] for r in result['reviews']], ['Good coffee', 'Noisy room'])
        self.assertEqual([r['rating'] for r in result['reviews']], [5, 2])
        self.assertEqual(result['reviews'][0]['date'], '2026-10-10')
        self.assertNotIn('Private author', str(result))
