import re
import httpx


class TelegramFailure(Exception):
    def __init__(self, code=0, retry_after=0):
        super().__init__('Telegram request failed')
        self.code = code
        self.retry_after = retry_after


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

    def send(self, job, text):
        params = dict(chat_id=job['chat_id'], text=text,
            reply_parameters={'message_id': job['message_id'], 'allow_sending_without_reply': False},
            link_preview_options={'is_disabled': True})
        if job.get('thread_id'):
            params['message_thread_id'] = job['thread_id']
        return self.call('sendMessage', **params)

    def is_admin(self, job):
        if job['anonymous'] or not job.get('user_id'):
            return False
        try:
            members = self.call('getChatAdministrators', chat_id=job['chat_id'])
            return any(member.get('user', {}).get('id') == job['user_id'] and
                member.get('status') in {'creator', 'administrator'} for member in members)
        except TelegramFailure:
            return False
