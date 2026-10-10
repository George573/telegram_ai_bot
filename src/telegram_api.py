import re
import threading
import time
import logging
from contextlib import contextmanager
from observability import event
import httpx


class TelegramFailure(Exception):
    def __init__(self, code=0, retry_after=0, not_sent=False):
        super().__init__('Telegram request failed')
        self.code = code
        self.retry_after = retry_after
        # True only when the request certainly never reached Telegram (connect failure).
        self.not_sent = not_sent


# Shared by poller and worker clients: repeated commands must not flood getChatAdministrators.
ADMIN_CACHE_SECONDS = 60
_admin_cache = {}
_admin_cache_lock = threading.Lock()


class TelegramAPI:
    def __init__(self, token):
        self._file_base = 'https://api.telegram.org/file/bot' + token + '/'
        self._base = 'https://api.telegram.org/bot' + token + '/'
        self._http = httpx.Client(timeout=40, trust_env=False)

    def call(self, method, **params):
        try:
            response = self._http.post(self._base + method, json=params)
            body = response.json()
            if not body.get('ok'):
                raise TelegramFailure(body.get('error_code', 0), body.get('parameters', {}).get('retry_after', 0))
            return body['result']
        except TelegramFailure:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise TelegramFailure(not_sent=True) from None
        except Exception:
            # No raw HTTP exceptions: URLs include the bot credential.
            raise TelegramFailure() from None

    def download(self, file_id, limit):
        try:
            info = self.call('getFile', file_id=file_id)
            path = info.get('file_path', '')
            if (not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or path.startswith('/') or
                    any(p in {'', '.', '..'} for p in path.split('/')) or info.get('file_size', 0) > limit):
                raise TelegramFailure()
            with self._http.stream('GET', self._file_base + path, follow_redirects=False) as response:
                if response.status_code != 200 or int(response.headers.get('content-length', '0')) > limit:
                    raise TelegramFailure()
                output = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(output) + len(chunk) > limit:
                        raise TelegramFailure()
                    output.extend(chunk)
                if not output:
                    raise TelegramFailure()
                return bytes(output)
        except TelegramFailure:
            raise
        except Exception:
            raise TelegramFailure() from None

    def send_typing(self, job):
        params = {'chat_id': job['chat_id'], 'action': 'typing'}
        if job.get('thread_id'):
            params['message_thread_id'] = job['thread_id']
        try:
            response = self._http.post(self._base + 'sendChatAction', json=params, timeout=3)
            if response.status_code != 200 or not response.json().get('ok'):
                event('typing_failed', level=logging.WARNING, status_code=response.status_code)
                return False
            return True
        except Exception:
            event('typing_failed', level=logging.WARNING)
            return False

    @contextmanager
    def typing(self, job):
        stopped = threading.Event()
        def refresh():
            while not stopped.is_set():
                if not self.send_typing(job):
                    return
                if stopped.wait(4):
                    return
        worker = threading.Thread(target=refresh, name='telegram-typing', daemon=True)
        try:
            worker.start()
        except Exception:
            event('typing_failed', level=logging.WARNING)
            yield
            return
        try:
            yield
        finally:
            stopped.set()
            # A bounded HTTP call may still be in flight. Do not hold up delivery.
            worker.join(timeout=0)

    def send(self, job, text):
        params = dict(chat_id=job['chat_id'], text=text,
            reply_parameters={'message_id': job['message_id'], 'allow_sending_without_reply': False},
            link_preview_options={'is_disabled': True})
        if job.get('thread_id'):
            params['message_thread_id'] = job['thread_id']
        return self.call('sendMessage', **params)

    def is_admin(self, job):
        """True/False when known; None when Telegram could not be asked."""
        if job['anonymous'] or not job.get('user_id'):
            return False
        with _admin_cache_lock:
            cached = _admin_cache.get(job['chat_id'])
        if cached and time.monotonic() - cached[0] < ADMIN_CACHE_SECONDS:
            admins = cached[1]
        else:
            try:
                members = self.call('getChatAdministrators', chat_id=job['chat_id'])
            except TelegramFailure as error:
                # A definite refusal (e.g. 400/403) denies; transient failures are unknown.
                return False if 400 <= error.code < 429 else None
            admins = frozenset(member.get('user', {}).get('id') for member in members
                               if member.get('status') in {'creator', 'administrator'})
            with _admin_cache_lock:
                _admin_cache[job['chat_id']] = (time.monotonic(), admins)
        return job['user_id'] in admins
