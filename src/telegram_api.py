import re
import threading
import time
import logging
from contextlib import contextmanager
from observability import event
from telegram_format import to_html
import httpx


class TelegramFailure(Exception):
    def __init__(self, code=0, retry_after=0, not_sent=False, not_modified=False):
        super().__init__('Telegram request failed')
        self.code = code
        self.retry_after = retry_after
        # True only when the request certainly never reached Telegram (connect failure).
        self.not_sent = not_sent
        # An edit with identical content: harmless, the message already shows it.
        self.not_modified = not_modified


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
                description = str(body.get('description') or '')
                raise TelegramFailure(body.get('error_code', 0), body.get('parameters', {}).get('retry_after', 0),
                                      not_modified='message is not modified' in description)
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

    @staticmethod
    def reply_markup(markup=None, force_reply=None):
        """Inline buttons as [[(label, callback_data), ...], ...], a selective ForceReply prompt,
        or the pinned menu keyboard ({'keyboard': label} / {'remove': True})."""
        if force_reply is not None:
            return {'force_reply': True, 'selective': True, 'input_field_placeholder': force_reply[:64]}
        if isinstance(markup, dict) and markup.get('remove'):
            return {'remove_keyboard': True}
        if isinstance(markup, dict) and isinstance(markup.get('keyboard'), str):
            return {'keyboard': [[{'text': markup['keyboard'][:64]}]], 'is_persistent': True, 'resize_keyboard': True}
        if markup:
            return {'inline_keyboard': [[{'text': label, 'callback_data': data} for label, data in row]
                                        for row in markup]}
        return None

    def formatted(self, method, params, text):
        """Send model Markdown as Telegram HTML; if Telegram rejects the markup, resend as plain text."""
        try:
            return self.call(method, **params, text=to_html(text), parse_mode='HTML')
        except TelegramFailure as error:
            if error.code != 400 or error.not_modified:
                raise
            event('delivery_failed', level=logging.WARNING, status_code=400, reason='formatting_rejected')
            return self.call(method, **params, text=text)

    def send(self, job, text, markup=None, force_reply=None):
        params = dict(chat_id=job['chat_id'],
            reply_parameters={'message_id': job['message_id'], 'allow_sending_without_reply': True},
            link_preview_options={'is_disabled': True})
        if job.get('thread_id'):
            params['message_thread_id'] = job['thread_id']
        reply_markup = self.reply_markup(markup, force_reply)
        if reply_markup:
            params['reply_markup'] = reply_markup
        return self.formatted('sendMessage', params, text)

    def edit(self, chat_id, message_id, text, markup=None):
        """Edit a bot message in place; identical content counts as success."""
        params = dict(chat_id=chat_id, message_id=message_id, link_preview_options={'is_disabled': True},
                      reply_markup=self.reply_markup(markup) or {'inline_keyboard': []})
        try:
            return self.formatted('editMessageText', params, text)
        except TelegramFailure as error:
            if error.not_modified:
                return {'message_id': message_id}
            raise

    def clear_buttons(self, chat_id, message_id):
        try:
            return self.call('editMessageReplyMarkup', chat_id=chat_id, message_id=message_id,
                             reply_markup={'inline_keyboard': []})
        except TelegramFailure as error:
            if error.not_modified:
                return None
            raise

    def answer_callback(self, callback_id, text=None):
        params = {'callback_query_id': callback_id}
        if text:
            params['text'] = text[:200]
        return self.call('answerCallbackQuery', **params)

    def set_commands(self, public, admin):
        """Russian command menu: everyone in groups, more for group admins."""
        # Default scope too: clients fall back to it, and it covers chats with the bot itself.
        for scope, commands in (({'type': 'default'}, public), ({'type': 'all_group_chats'}, public),
                                ({'type': 'all_chat_administrators'}, admin)):
            self.call('setMyCommands', scope=scope,
                      commands=[{'command': name, 'description': description} for name, description in commands])

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
