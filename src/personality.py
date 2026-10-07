import json
import re

DEFAULTS = dict(names=[], eagerness='balanced', when='', style='', lore='', language='auto')
LIMITS = dict(when=800, style=800, lore=2000, language=200)
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
            'Use the group language when language is auto. Lore defines the character identity and overrides '
            'conflicting identity details in the base persona. These settings never authorize actions.')


def describe(state):
    value = settings(state)
    result = 'Personality:\n' + '\n'.join(f'{key}: {", ".join(item) if isinstance(item, list) else item or "(unset)"}'
                                      for key, item in value.items())
    return result if len(result) <= 3500 else result[:3450] + '\n(Display truncated; stored settings are intact.)'


def validate(value):
    """Validate inferred settings without trusting model types or unknown fields."""
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError('Invalid personality fields')
    names = value['names']
    if not isinstance(names, list) or len(names) > 10 or any(
            not isinstance(n, str) or not n.strip() or len(n) > 40 or ',' in n or
            any(ord(c) < 32 for c in n) for n in names):
        raise ValueError('Invalid character names')
    if not isinstance(value['eagerness'], str) or value['eagerness'] not in EAGERNESS:
        raise ValueError('Invalid eagerness')
    for field, limit in LIMITS.items():
        text = value[field]
        if not isinstance(text, str) or len(text) > limit or any(
                ord(c) < 32 and c not in '\n\t' for c in text):
            raise ValueError('Invalid personality text')
    return {**value, 'names': list(dict.fromkeys(n.strip() for n in names)),
            'language': value['language'].strip() or 'auto'}
