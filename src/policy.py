import re
import texts

ADMIN_COMMANDS = {'/images', '/voice', '/web', '/personality', '/context', '/historylimit', '/resetmemory', '/pause', '/resume', '/settings', '/menukey'}
PUBLIC_COMMANDS = {'/help', '/whoami', '/chat', '/search', '/forgetme', '/skills'}
# Only buttons produce these; typed text never can.
BUTTON_COMMANDS = {'/ask', '/confirm', '/menukey'}
HELP_PAGES = {'home', 'talk', 'search', 'skills', 'privacy', 'admin', 'persona', 'commands'}
ADMIN_PAGES = {'admin', 'persona', 'commands'}
PANELS = {'settings', 'features', 'mode', 'memory'}
ASKABLE = {'/search', '/historylimit', '/chat', '/personality'}


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
    # Never surface int()'s own message ("invalid literal ...") to users.
    text = str(value).strip()
    if not text.isascii() or not text.isdecimal() or not 10 <= int(text) <= 500:
        raise ValueError(texts.LIMIT_INVALID)
    return int(text)


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
    elif message.get('video_note'):
        # Round video message: its sound is transcribed like voice, plus 1–3 frames when photos are on.
        circle = message['video_note']
        if not isinstance(circle, dict) or not isinstance(circle.get('file_id'), str) or not 0 < len(circle['file_id']) <= 1024:
            return None
        media = dict(kind='video_note', file_id=circle['file_id'], bytes=circle.get('file_size', 0),
                     seconds=circle.get('duration', 0))
    if chat.get('id') not in allowed or chat.get('type') not in {'group', 'supergroup'} or sender.get('is_bot'):
        return None
    if media and (type(media['bytes']) is not int or media['bytes'] < 0 or
                  (media['kind'] in {'voice', 'video_note'} and (type(media['seconds']) is not int or media['seconds'] < 0))):
        return None
    if not isinstance(text, str) or (not media and not text.strip()) or len(text) > 8000:
        return None
    replied = message.get('reply_to_message', {})
    reply = replied.get('from', {}).get('id') == bot['id']
    command, argument = command_parts(text, bot['username'])
    command = texts.COMMAND_ALIASES.get(command, command)
    if not command and not media:
        # «помощь», «навыки», «настройки» alone (optionally with a mention) act as commands.
        word = re.sub(r'(?i)@' + re.escape(bot['username']) + r'\b', '', text).strip(' !.?').casefold()
        if word in texts.ALIASES:
            command, argument = texts.ALIASES[word], ''
    if command and (command not in ADMIN_COMMANDS | PUBLIC_COMMANDS or command in BUTTON_COMMANDS):
        return None
    if media and command and command != '/chat':
        return None
    return dict(chat_id=chat['id'], message_id=message['message_id'],
        user_id=sender.get('id'), anonymous=bool(message.get('sender_chat')),
        text=text, command=command, argument=argument, reply_to_bot=reply, media=media,
        directed=bool(command or addressed(text, bot['username'], reply)),
        # Ordinary supergroups also set message_thread_id for reply chains; only
        # forum topics are separate conversations that accept it on send.
        thread_id=message.get('message_thread_id') if message.get('is_topic_message') else None,
        reply_to_message_id=replied.get('message_id') if reply and type(replied.get('message_id')) is int else None)


def callback_action(data):
    """Map validated button data ("v1:<action>:<arg>[:<value>]") to (command, argument, follow-up text)."""
    if not isinstance(data, str) or len(data.encode()) > 64 or not re.fullmatch(r'v1(:[a-z_/-]{1,24}){1,3}', data):
        return None
    _, action, *rest = data.split(':')
    arg = rest[0] if rest else ''
    value = rest[1] if len(rest) > 1 else ''
    if action == 'help' and arg in HELP_PAGES and not value:
        return '/help', arg, ''
    if action == 'panel' and arg in PANELS and not value:
        return '/settings', arg, ''
    if action in {'whoami', 'forget', 'reset'} and not rest:
        return {'whoami': '/whoami', 'forget': '/forgetme', 'reset': '/resetmemory'}[action], '', ''
    if action == 'ask' and ('/' + arg) in ASKABLE and not value:
        return '/ask', '/' + arg, ''
    if action == 'set':
        if arg in {'web', 'images', 'voice'} and value in {'on', 'off'}:
            return '/' + arg, value, ''
        if arg == 'context' and value in {'directed', 'all'}:
            return '/context', value, ''
        if arg in {'pause', 'resume'} and not value:
            return '/' + arg, '', ''
    if action == 'confirm' and arg in {'yes', 'no'} and not value:
        return '/confirm', arg, ''
    if action == 'menukey' and arg in {'on', 'off'} and not value:
        return '/menukey', arg, ''
    if action == 'pers' and arg in {'save', 'cancel', 'show'} and not value:
        return '/personality', arg, ''
    if action == 'more' and arg in texts.FOLLOW_UPS and not value:
        return '', '', texts.FOLLOW_UPS[arg]
    return None


def normalize_callback(update, allowed, bot):
    """A button press becomes an ordinary directed payload from the person who pressed it."""
    query = update.get('callback_query') or {}
    message = query.get('message') or {}
    chat = message.get('chat') or {}
    sender = query.get('from') or {}
    if (chat.get('id') not in allowed or chat.get('type') not in {'group', 'supergroup'} or sender.get('is_bot')
            or type(sender.get('id')) is not int or type(message.get('message_id')) is not int
            or not isinstance(query.get('id'), str) or (message.get('from') or {}).get('id') != bot['id']):
        return None
    action = callback_action(query.get('data'))
    if action is None:
        return None
    command, argument, text = action
    return dict(chat_id=chat['id'], message_id=message['message_id'], user_id=sender['id'], anonymous=False,
                text=text or command, command=command, argument=argument, reply_to_bot=True, media=None,
                directed=True, thread_id=message.get('message_thread_id') if message.get('is_topic_message') else None,
                reply_to_message_id=message['message_id'], callback_id=query['id'], from_button=True,
                panel_message_id=message['message_id'])
