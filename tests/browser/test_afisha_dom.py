"""Afisha extractors in real offline Chromium against synthetic markup shaped like the live pages."""
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
from yandex_afisha import EXTRACT_EVENT, EXTRACT_LISTING, normalized_event, normalized_listing
from browser import BROWSER_ARGS, BROWSER_ENV

LISTING_HTML = '''
<div data-test-id="eventCard.root"><a href="/moscow/musical/mamma-mimo" data-test-id="eventCard.link"></a>
  <span data-test-id="eventCard.ratingBadge">8.9</span>
  <h2 data-test-id="eventCard.eventInfoTitle">Мамма мимо</h2>
  <ul data-test-id="eventCard.eventInfoDetails"><li>Мюзикл</li><li>сегодня 11 октября</li>
    <li data-test-id="eventCard.placeTitle"><a data-test-id="eventCard.placeLink" href="/moscow/theatre/places/x">Театр на Цветном</a></li></ul>
  <a href="/moscow/musical/mamma-mimo#schedule" data-test-id="eventCard.priceLink"><span>от 1 500 ₽</span></a></div>
<div data-test-id="eventCard.root"><a href="/moscow/show/o-chem-molchat" data-test-id="eventCard.link"></a>
  <h2 data-test-id="eventCard.eventInfoTitle">О чём молчат женщины</h2>
  <ul data-test-id="eventCard.eventInfoDetails"><li>Шоу</li><li>15 октября</li></ul></div>'''

EVENT_HTML = '''
<script type="application/ld+json">{"@type":"BreadcrumbList","itemListElement":[]}</script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"TheaterEvent","name":"Мамма мимо",
 "location":{"@type":"Place","name":"Театр на Цветном","address":{"streetAddress":"Цветной бул., 11"}},
 "offers":{"lowPrice":"1500.00","highPrice":"7000.00","priceCurrency":"RUB","availability":"https://schema.org/InStock"},
 "aggregateRating":{"ratingValue":8.9,"ratingCount":1526},"duration":"PT1H40M"}</script>
<div data-test-id="eventPage.headerDesktopInfo.info">Мюзикл Шоу 12+</div>
<div data-test-id="eventPage.placeAddress.metroStations"><div>Цветной бульвар</div><div>Трубная</div><div>Источник изображения: x</div></div>
<ul><li data-test-id="eventPage.scheduleOtherItem.root"><div data-test-id="eventPage.scheduleOtherItem.date">
  <span data-test-id="scheduleDate.date">13</span><span data-test-id="scheduleDate.day">вторник</span>
  <div data-test-id="scheduleDate.month">октября</div></div>
  <div><div data-test-id="eventPage.scheduleOtherItem.session">14:00</div>
       <div data-test-id="eventPage.scheduleOtherItem.place"><a>Основная сцена</a></div><div><button>от 2 000 ₽</button></div></div>
  <div><div data-test-id="eventPage.scheduleOtherItem.session">19:00</div>
       <div data-test-id="eventPage.scheduleOtherItem.place"><a>Основная сцена</a></div><div><button>от 2 500 ₽</button></div></div></li></ul>
<div data-test-id="comment"><span data-test-id="commentInfo.author">Private author</span></div>'''


class AfishaDOMTests(unittest.TestCase):
    def test_extractors_in_offline_chromium(self):
        if not Path('/usr/bin/chromium').is_file():
            self.skipTest('Chromium is not installed in this test environment')
        async def check():
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(executable_path='/usr/bin/chromium', args=BROWSER_ARGS, env=BROWSER_ENV)
                try:
                    context = await browser.new_context(offline=True)
                    await context.route('**/*', lambda route: route.abort())
                    page = await context.new_page()
                    await page.set_content(LISTING_HTML)
                    listing = await page.evaluate(EXTRACT_LISTING)
                    await page.set_content(EVENT_HTML)
                    return listing, await page.evaluate(EXTRACT_EVENT)
                finally:
                    await browser.close()
        raw_listing, raw_event = asyncio.run(check())
        listing = normalized_listing('https://afisha.yandex.ru/moscow/theatre?date=2026-10-11&period=1', raw_listing)
        self.assertEqual([e['title'] for e in listing['events']], ['Мамма мимо', 'О чём молчат женщины'])
        first = listing['events'][0]
        self.assertEqual((first['type'], first['when'], first['venue'], first['price'], first['rating']),
                         ('Мюзикл', 'сегодня 11 октября', 'Театр на Цветном', 'от 1 500 ₽', 8.9))
        event = normalized_event('https://afisha.yandex.ru/moscow/musical/mamma-mimo', raw_event)
        self.assertEqual(event['name'], 'Мамма мимо')
        self.assertEqual(event['venue']['metro'], ['Цветной бульвар', 'Трубная'])
        self.assertEqual(event['age'], '12+')
        self.assertEqual([(s['time'], s['price']) for s in event['sessions']], [('14:00', 'от 2 000 ₽'), ('19:00', 'от 2 500 ₽')])
        self.assertEqual(event['sessions'][0]['date'], '13 октября вторник')
        self.assertNotIn('Private author', str(event))


if __name__ == '__main__':
    unittest.main()
