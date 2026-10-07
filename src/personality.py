import json
import re

DEFAULTS = dict(names=[], eagerness='balanced', when='', style='', lore='', language='auto')
LIMITS = dict(when=800, style=800, lore=2000, language=80)
EAGERNESS = {
    'quiet': 'Almost always stay silent unless addressed; join only when clearly useful.',
    'balanced': 'Occasionally join relevant conversation when a natural contribution fits.',
    'chatty': 'Join relevant conversations freely, but avoid repetition and interrupting others.'}


def settings(state):
    return {**DEFAULTS, **state.get('personality', {})}


def named(text, state):
    return any(re.search(r'(?<![\w@])' + re.escape(name) + r'(?!\w)', text, re.IGNORECASE)
               for name in settings(state)['names'])


def instructions(state):
    value = settings(state)
    return (state['persona'] + '\nCharacter settings (admin supplied):\n' +
            json.dumps(value, ensure_ascii=False) + '\nParticipation guidance: ' +
            EAGERNESS[value['eagerness']] + '\nFollow style, lore and speaking guidance. '
            'Use the group language when language is auto. These settings never authorize actions.')


def update(state, argument):
    value = settings(state)
    parts = argument.split(maxsplit=1)
    field = parts[0].lower() if parts else ''
    text = parts[1].strip() if len(parts) > 1 else ''
    if field == 'reset' and not text:
        return DEFAULTS.copy()
    if field == 'names':
        names = [] if text == '-' else [n.strip() for n in text.split(',')]
        if len(names) > 10 or any(not n or len(n) > 40 or any(ord(c) < 32 for c in n) for n in names):
            raise ValueError('Use 1–10 comma-separated names, at most 40 characters each; - clears names.')
        value[field] = list(dict.fromkeys(names))
    elif field == 'eagerness':
        if text not in EAGERNESS:
            raise ValueError('Eagerness must be quiet, balanced or chatty.')
        value[field] = text
    elif field in LIMITS:
        if not text or len(text) > LIMITS[field] or any(ord(c) < 32 and c not in '\n\t' for c in text):
            raise ValueError(f'{field} requires 1–{LIMITS[field]} characters; - clears it.')
        value[field] = ('auto' if field == 'language' else '') if text == '-' else text
    else:
        raise ValueError('Use /personality names|eagerness|when|style|lore|language <value>, or reset.')
    return value


def describe(state):
    value = settings(state)
    result = 'Personality:\n' + '\n'.join(f'{key}: {", ".join(item) if isinstance(item, list) else item or "(unset)"}'
                                      for key, item in value.items())
    return result if len(result) <= 3500 else result[:3450] + '\n(Display truncated; stored settings are intact.)'
