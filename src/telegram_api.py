import httpx


class TelegramFailure(Exception):
    def __init__(self, code=0, retry_after=0):
        super().__init__('Telegram request failed')
        self.code = code
        self.retry_after = retry_after


class TelegramAPI:
    def __init__(self, token):
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
