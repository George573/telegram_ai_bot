"""Call the separate tools container. Failures become explicit unavailable results."""
import httpx
from yandex_reviews import card_url, unavailable
import yandex_afisha
import yandex_maps

# Server waits up to 25 s for the browser slot, then reads for up to 45 s.
TIMEOUT = httpx.Timeout(80, connect=3)
RESULT_KEYS = {'status', 'reason', 'url', 'fetched_at', 'company', 'reviews', 'returned_count', 'sampling', 'notice'}
LISTING_KEYS = {'status', 'reason', 'url', 'fetched_at', 'city', 'events', 'returned_count', 'sampling', 'notice'}
EVENT_KEYS = {'status', 'reason', 'url', 'fetched_at', 'name', 'kind', 'labels', 'age', 'description', 'venue',
              'run', 'duration', 'price', 'rating', 'sessions', 'returned_count', 'notice', 'events'}
MAPS_KEYS = {'status', 'reason', 'url', 'fetched_at', 'results', 'returned_count', 'chains_excluded',
             'excluded_chains', 'sampling', 'notice'}


def _post(base_url, path, body, url, keys, items, missing):
    """POST to the tools service; any transport, status or shape problem becomes `missing(url, reason)`."""
    if not base_url:
        return missing(url, 'browser_unavailable')
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False, follow_redirects=False) as client:
            response = client.post(base_url.rstrip('/') + path, json=body)
    except Exception:
        # Transport errors may contain internal hostnames; report a fixed reason only.
        return missing(url, 'browser_unavailable')
    try:
        result = response.json()
    except ValueError:
        return missing(url, 'browser_failed')
    if (response.status_code != 200 or not isinstance(result, dict) or not set(result) <= keys
            or result.get('status') not in {'ok', 'unavailable'} or result.get('url') != url
            or not isinstance(result.get(items, []), list) or not isinstance(result.get('returned_count'), int)):
        return missing(url, 'browser_failed')
    return result


def read_reviews(value, base_url):
    url = card_url(value)
    result = _post(base_url, '/v1/yandex-reviews', {'url': url}, url, RESULT_KEYS, 'reviews', unavailable)
    return result if isinstance(result.get('reviews'), list) else unavailable(url, 'browser_failed')


def read_afisha_listing(category, date_from, days, base_url):
    url = yandex_afisha.listing_url(category, date_from, days)
    return _post(base_url, '/v1/afisha-listing', {'category': category, 'date_from': date_from, 'days': days},
                 url, LISTING_KEYS, 'events', yandex_afisha.unavailable)


def read_afisha_event(value, base_url):
    url = yandex_afisha.event_url(value)
    return _post(base_url, '/v1/afisha-event', {'url': url}, url, EVENT_KEYS, 'sessions', yandex_afisha.unavailable)


def read_maps_search(query, exclude_chains, base_url, near=None):
    url = yandex_maps.search_url(query, near)
    body = {'query': query, 'exclude_chains': exclude_chains}
    if near is not None:
        body['near'] = f'{near[0]},{near[1]}'
    return _post(base_url, '/v1/yandex-maps-search', body, url, MAPS_KEYS, 'results', yandex_maps.unavailable)
