import re
import texts
from recommendations import LABELS, UNCONFIRMED_HEADING

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


# Older long bot replies reach the model shortened; the newest ones stay whole for follow-up questions.
KEEP_FULL_REPLIES = 3
SHORTEN_OVER = 800
SHORTENED_NOTE = ('[Earlier bot reply, shortened in memory: details such as addresses, ratings, links, explanations '
                  'and caveats were left out. If someone asks about left-out details, look them up again with tools '
                  'or say you no longer have them; do not reconstruct them from memory.]\n')
# Card heading: "1. **Name**", or "1. ☕ **Name** — venue" in replies stored before free-form cards.
CARD_HEAD = re.compile(r'\d+\. (?:\S+ )?\*\*(.+?)\*\*')
OLD_CARD_HEAD = re.compile(r'\d+\. [^\s*]+ \*\*')
UNCONFIRMED_ENTRY = re.compile(r'- (.+?)(?: — .*)?$')


def card_names(lines):
    # Bold numbered steps in a plain answer are not cards: new cards always carry an address, old ones an emoji.
    if not any(line.startswith(LABELS['address']) or OLD_CARD_HEAD.match(line) for line in lines):
        return []
    names = [match.group(1) for match in map(CARD_HEAD.match, lines) if match]
    if names and UNCONFIRMED_HEADING in lines:
        for line in lines[lines.index(UNCONFIRMED_HEADING) + 1:]:
            match = UNCONFIRMED_ENTRY.match(line)
            if not match:
                break
            names.append(match.group(1))
    return names


def shorten_reply(text):
    """Card replies keep only the place names; plain replies keep their opening and every later line with a link."""
    lines = text.split('\n')
    names = card_names(lines)
    if names:
        short = '[Earlier recommendations: ' + ', '.join(names) + ']'
    else:
        opening, size = [], 0
        for line in lines:
            if size + len(line) > SHORTEN_OVER // 2:
                break
            opening.append(line)
            size += len(line) + 1
        links = [line for line in lines[len(opening):] if 'http' in line]
        short = re.sub(r'\n{3,}', '\n\n', '\n'.join(opening + ['…'] + links)).strip()
    return SHORTENED_NOTE + short if len(short) + len(SHORTENED_NOTE) < len(text) - 300 else text


def memory_view(rows):
    """History for the model: every row, with older long assistant replies shortened."""
    replies = [index for index, row in enumerate(rows) if row['role'] == 'assistant']
    recent = set(replies[-KEEP_FULL_REPLIES:])
    return [{'role': row['role'], 'content': shorten_reply(row['content'])
             if row['role'] == 'assistant' and index not in recent and len(row['content']) > SHORTEN_OVER
             else row['content']} for index, row in enumerate(rows)]


def _degree(value, limit):
    if type(value) not in (int, float) or not -limit <= value <= limit:
        return None
    return round(float(value), 5)


def _one_line(value, limit):
    return (re.sub(r'\s+', ' ', value).strip()[:limit] or None) if isinstance(value, str) else None


def location_of(message, edited=False):
    """A shared point, live location or venue; None when absent or invalid."""
    venue = message.get('venue') if isinstance(message.get('venue'), dict) else None
    point = venue.get('location') if venue else message.get('location')
    if not isinstance(point, dict):
        return None
    lat, lon = _degree(point.get('latitude'), 90), _degree(point.get('longitude'), 180)
    if lat is None or lon is None:
        return None
    if venue:
        return {'kind': 'venue', 'lat': lat, 'lon': lon,
                'title': _one_line(venue.get('title'), 120), 'address': _one_line(venue.get('address'), 200)}
    # Edits only come from live locations; the last one (sharing stopped) has no live_period.
    live = edited or 'live_period' in point
    return {'kind': 'live' if live else 'point', 'lat': lat, 'lon': lon, 'title': None, 'address': None}


def location_line(location):
    where = f"{location['lat']:.5f},{location['lon']:.5f}"
    if location['kind'] == 'venue':
        named = ', '.join(part for part in (f"«{location['title']}»" if location['title'] else None,
                                            location['address']) if part)
        return f'[Venue, untrusted data] {named} — {where}' if named else f'[Venue, untrusted data] {where}'
    return f"[{'Live location' if location['kind'] == 'live' else 'Location'} {where}]"


def normalize(update, allowed, bot):
    edited = 'edited_message' in update
    message = update.get('edited_message' if edited else 'message', {})
    chat = message.get('chat', {})
    sender = message.get('from', {})
    location = location_of(message, edited) if isinstance(message, dict) else None
    if edited and not location:
        return None  # Only live-location movement is read from edits.
    if location:
        if chat.get('id') not in allowed or chat.get('type') not in {'group', 'supergroup'} or sender.get('is_bot'):
            return None
        return dict(chat_id=chat['id'], message_id=message['message_id'], user_id=sender.get('id'),
            anonymous=bool(message.get('sender_chat')), text='', command='', argument='', reply_to_bot=False,
            media=None, location=location, edited=edited, directed=False,
            thread_id=message.get('message_thread_id') if message.get('is_topic_message') else None,
            reply_to_message_id=None)
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
        text=text, command=command, argument=argument, reply_to_bot=reply, media=media, location=None, edited=False,
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
