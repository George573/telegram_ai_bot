import re

ADMIN_COMMANDS = {'/images', '/voice', '/web', '/personality', '/setprompt', '/context', '/historylimit', '/resetmemory', '/pause', '/resume', '/status'}


def addressed(text, username, reply_to_bot=False):
    return reply_to_bot or bool(re.search(r'(?<![\w@])@' + re.escape(username) + r'(?!\w)', text, re.IGNORECASE))


def command_parts(text, username):
    if not text.strip().startswith('/'):
        return '', ''
    token, *rest = text.strip().split(maxsplit=1)
    command, _, target = token.partition('@')
    if target and target.lower() != username.lower():
        return '/other', ''
    return command.lower(), rest[0] if rest else ''


def valid_limit(value):
    number = int(value)
    if not 10 <= number <= 500:
        raise ValueError('Use a number from 10 to 500')
    return number


def compact_plan(rows, limit, slots=0):
    if len(rows) + slots <= limit:
        return []
    return rows[:max(1, len(rows) - max(1, limit // 2 - slots))]


def normalize(update, allowed, bot):
    message = update.get('message', {})
    chat = message.get('chat', {})
    sender = message.get('from', {})
    text = message.get('text', message.get('caption', ''))
    media = None
    photos = message.get('photo', [])
    if photos:
        candidates = [p for p in photos if isinstance(p, dict) and isinstance(p.get('file_id'), str) and
                      0 < len(p['file_id']) <= 1024 and type(p.get('width')) is int and
                      type(p.get('height')) is int and 0 < p['width'] <= 1280 and 0 < p['height'] <= 1280]
        if not candidates:
            return None
        photo = max(candidates, key=lambda p: p['width'] * p['height'])
        media = dict(kind='photo', file_id=photo['file_id'], bytes=photo.get('file_size', 0))
    elif message.get('voice'):
        voice = message['voice']
        if not isinstance(voice, dict) or not isinstance(voice.get('file_id'), str) or not 0 < len(voice['file_id']) <= 1024:
            return None
        media = dict(kind='voice', file_id=voice['file_id'], bytes=voice.get('file_size', 0),
                     seconds=voice.get('duration', 0))
    if chat.get('id') not in allowed or chat.get('type') not in {'group', 'supergroup'} or sender.get('is_bot'):
        return None
    if media and (type(media['bytes']) is not int or media['bytes'] < 0 or
                  (media['kind'] == 'voice' and (type(media['seconds']) is not int or media['seconds'] < 0))):
        return None
    if not isinstance(text, str) or (not media and not text.strip()) or len(text) > 8000:
        return None
    reply = message.get('reply_to_message', {}).get('from', {}).get('id') == bot['id']
    command, argument = command_parts(text, bot['username'])
    if command and command not in ADMIN_COMMANDS | {'/help', '/whoami', '/chat', '/search'}:
        return None
    if media and command and command != '/chat':
        return None
    if command == '/chat' and not argument.strip() and not media:
        command = '/help'
    return dict(chat_id=chat['id'], message_id=message['message_id'],
        user_id=sender.get('id'), anonymous=bool(message.get('sender_chat')),
        text=text, command=command, argument=argument, reply_to_bot=reply, media=media,
        directed=bool(command or addressed(text, bot['username'], reply)),
        thread_id=message.get('message_thread_id'))
