"""Custom tool service. Holds no bot secrets; reachable only from the bot's private network.

POST /v1/yandex-reviews {"url": "<Maps card>"} -> review sample or explicit unavailable status.
POST /v1/afisha-listing {"category", "date_from", "days"} -> Moscow Afisha events.
POST /v1/afisha-event {"url": "<Afisha event page>"} -> event details and sessions.
POST /v1/yandex-maps-search {"query", "exclude_chains"} -> Moscow organizations from Maps search.
GET /healthz -> {"status": "ok"}.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
import json
import os
import sys
import threading
import time
from yandex_reviews import card_url, unavailable
from review_browser import read_reviews
import yandex_afisha
from afisha_browser import read_event, read_listing
import yandex_maps
from maps_search import read_search

MAX_BODY = 2048
# One Chromium at a time keeps the container inside its memory limit.
BROWSER_SLOTS = threading.BoundedSemaphore(1)
BUSY_WAIT_SECONDS = 25


def log(name, level='INFO', **fields):
    # Same shape as the bot's JSON logs. Fixed metadata only: never URLs, page text or exception messages.
    record = {'time': datetime.now(timezone.utc).isoformat(), 'level': level, 'service': 'tools', 'event': name}
    print(json.dumps({**record, **{k: v for k, v in fields.items() if v is not None}}), file=sys.stdout, flush=True)


def _browse(name, url, reader, busy):
    if not BROWSER_SLOTS.acquire(timeout=BUSY_WAIT_SECONDS):
        log(name, 'WARNING', status='unavailable', reason='busy')
        return 200, busy(url, 'busy')
    started = time.monotonic()
    try:
        result = reader(url)
    finally:
        BROWSER_SLOTS.release()
    log(name, 'INFO' if result['status'] == 'ok' else 'WARNING', status=result['status'], reason=result.get('reason'),
        returned_count=result['returned_count'], duration_ms=int((time.monotonic() - started) * 1000))
    return 200, result


def _request(body, keys, optional=frozenset()):
    request = json.loads(body)
    if not isinstance(request, dict) or not keys <= set(request) <= keys | optional:
        raise ValueError
    return request


def reviews(body):
    try:
        url = card_url(_request(body, {'url'})['url'])
    except (ValueError, TypeError):
        return 400, {'status': 'unavailable', 'reason': 'invalid_url', 'reviews': [], 'returned_count': 0}
    return _browse('reviews_read', url, read_reviews, unavailable)


def afisha_listing(body):
    try:
        request = _request(body, {'category', 'date_from', 'days'})
        url = yandex_afisha.listing_url(request['category'], request['date_from'], request['days'])
    except (ValueError, TypeError):
        return 400, yandex_afisha.unavailable(None, 'invalid_arguments')
    return _browse('afisha_listing_read', url, read_listing, yandex_afisha.unavailable)


def afisha_event(body):
    try:
        url = yandex_afisha.event_url(_request(body, {'url'})['url'])
    except (ValueError, TypeError):
        return 400, yandex_afisha.unavailable(None, 'invalid_url')
    return _browse('afisha_event_read', url, read_event, yandex_afisha.unavailable)


def maps_search(body):
    try:
        request = _request(body, {'query', 'exclude_chains'}, {'near'})
        if type(request['exclude_chains']) is not bool:
            raise ValueError
        near = yandex_maps.near_point(request.get('near'))
        url = yandex_maps.search_url(request['query'], near)
    except (ValueError, TypeError):
        return 400, yandex_maps.unavailable(None, 'invalid_arguments')
    # Same single slot as the browsers: one outbound Yandex request at a time.
    return _browse('maps_search_read', request['query'],
                   lambda query: read_search(query, request['exclude_chains'], near),
                   lambda _query, reason: yandex_maps.unavailable(url, reason))


ROUTES = {'/v1/yandex-reviews': reviews, '/v1/afisha-listing': afisha_listing, '/v1/afisha-event': afisha_event,
          '/v1/yandex-maps-search': maps_search}


class Handler(BaseHTTPRequestHandler):
    server_version = 'tools'
    sys_version = ''

    def log_message(self, *_):
        pass

    def reply(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/healthz':
            self.reply(200, {'status': 'ok'})
        else:
            self.reply(404, {'status': 'not_found'})

    def do_POST(self):
        handler = ROUTES.get(self.path)
        if handler is None:
            self.reply(404, {'status': 'not_found'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            self.reply(413 if length > MAX_BODY else 400, {'status': 'invalid_request'})
            return
        try:
            self.reply(*handler(self.rfile.read(length)))
        except Exception:
            log('tool_failed', 'ERROR', path=self.path)
            self.reply(500, {'status': 'unavailable', 'reason': 'tool_failed', 'reviews': [], 'returned_count': 0})


def main():
    port = int(os.getenv('TOOLS_PORT', '8080'))
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    server.daemon_threads = True
    log('tools_started', port=port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
