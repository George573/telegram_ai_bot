"""Call the separate tools container. Failures become explicit unavailable results."""
import httpx
from yandex_reviews import card_url, unavailable

# Server waits up to 25 s for the browser slot, then reads for up to 45 s.
TIMEOUT = httpx.Timeout(80, connect=3)
RESULT_KEYS = {'status', 'reason', 'url', 'fetched_at', 'company', 'reviews', 'returned_count', 'sampling', 'notice'}


def read_reviews(value, base_url):
    url = card_url(value)
    if not base_url:
        return unavailable(url, 'browser_unavailable')
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False, follow_redirects=False) as client:
            response = client.post(base_url.rstrip('/') + '/v1/yandex-reviews', json={'url': url})
    except Exception:
        # Transport errors may contain internal hostnames; report a fixed reason only.
        return unavailable(url, 'browser_unavailable')
    try:
        result = response.json()
    except ValueError:
        return unavailable(url, 'browser_failed')
    if (response.status_code != 200 or not isinstance(result, dict) or not set(result) <= RESULT_KEYS
            or result.get('status') not in {'ok', 'unavailable'} or result.get('url') != url
            or not isinstance(result.get('reviews'), list) or not isinstance(result.get('returned_count'), int)):
        return unavailable(url, 'browser_failed')
    return result
