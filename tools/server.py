"""Custom tool service. Holds no bot secrets; reachable only from the bot's private network.

POST /v1/yandex-reviews {"url": "<Maps card>"} -> review sample or explicit unavailable status.
GET /healthz -> {"status": "ok"}.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading
import time
from yandex_reviews import card_url, unavailable
from review_browser import read_reviews

MAX_BODY = 2048
# One Chromium at a time keeps the container inside its memory limit.
BROWSER_SLOTS = threading.BoundedSemaphore(1)
BUSY_WAIT_SECONDS = 25


def log(name, **fields):
    # Fixed metadata only: never URLs, page text or exception messages.
    print(json.dumps({'event': name, **fields}), file=sys.stdout, flush=True)


def reviews(body):
    try:
        request = json.loads(body)
        if not isinstance(request, dict) or set(request) != {'url'}:
            raise ValueError
        url = card_url(request['url'])
    except (ValueError, TypeError):
        return 400, {'status': 'unavailable', 'reason': 'invalid_url', 'reviews': [], 'returned_count': 0}
    if not BROWSER_SLOTS.acquire(timeout=BUSY_WAIT_SECONDS):
        return 200, unavailable(url, 'busy')
    started = time.monotonic()
    try:
        result = read_reviews(url)
    finally:
        BROWSER_SLOTS.release()
    log('reviews_read', status=result['status'], reason=result.get('reason'),
        review_count=result['returned_count'], duration_ms=int((time.monotonic() - started) * 1000))
    return 200, result


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
        if self.path != '/v1/yandex-reviews':
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
            self.reply(*reviews(self.rfile.read(length)))
        except Exception:
            log('tool_failed', tool='yandex_reviews')
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
