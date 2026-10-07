import re

ADMIN_COMMANDS = {'/personality', '/setprompt', '/context', '/historylimit', '/resetmemory', '/pause', '/resume', '/status'}


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
    text = message.get('text')
    if chat.get('id') not in allowed or chat.get('type') not in {'group', 'supergroup'} or sender.get('is_bot'):
        return None
    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
        return None
    reply = message.get('reply_to_message', {}).get('from', {}).get('id') == bot['id']
    command, argument = command_parts(text, bot['username'])
    if command and command not in ADMIN_COMMANDS | {'/help', '/whoami', '/chat'}:
        return None
    if command == '/chat' and not argument.strip():
        command = '/help'
    return dict(chat_id=chat['id'], message_id=message['message_id'],
        user_id=sender.get('id'), anonymous=bool(message.get('sender_chat')),
        text=text, command=command, argument=argument, reply_to_bot=reply,
        directed=bool(command or addressed(text, bot['username'], reply)),
        thread_id=message.get('message_thread_id'))
