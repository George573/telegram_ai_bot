"""Fetch one Yandex Maps search page (server-rendered) and normalize its organizations.

No browser: the data is in the HTML. One GET to a fixed host; redirects are refused so a
CAPTCHA or another site can never be followed.
"""
import urllib.error
import urllib.request
from yandex_maps import normalized_search, search_url, state_items, unavailable

MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 15
HEADERS = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
                         'Chrome/154.0 Safari/537.36',
           'Accept': 'text/html', 'Accept-Language': 'ru-RU,ru;q=0.9'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)


def read_search(query, exclude_chains, near=None):
    url = search_url(query, near)
    try:
        with OPENER.open(urllib.request.Request(url, headers=HEADERS), timeout=TIMEOUT) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        location = error.headers.get('Location', '') if error.headers else ''
        if 300 <= error.code < 400:
            return unavailable(url, 'captcha' if 'captcha' in location else 'redirected')
        return unavailable(url, 'not_found' if error.code == 404 else 'access_denied')
    except Exception:
        # Transport errors may include hostnames or local details.
        return unavailable(url, 'timeout_or_network')
    if len(body) > MAX_BYTES:
        return unavailable(url, 'too_large')
    html = body.decode('utf-8', errors='replace')
    items = state_items(html)
    if items is None:
        return unavailable(url, 'captcha' if 'captcha' in html[:20000].casefold() else 'page_changed')
    return normalized_search(url, items, exclude_chains, near)
