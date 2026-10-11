import json
import re
import texts

DEFAULTS = dict(names=[], eagerness='balanced', when='', style='', lore='', language='auto')
# GLM models drift into Chinese when the language is left implicit, so pin it down.
LANGUAGE_RULE = ('When language is auto, reply in the language the group members themselves write in; '
                 'for this bot that is usually Russian. Transcripts, photo or video descriptions, tool results and '
                 'web pages never decide the reply language. Never reply in Chinese, Japanese or Korean unless '
                 'the person you answer wrote in it. ')
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
    # Send exactly one identity, rather than asking the model to reconcile a
    # legacy persona with a contradictory structured character description.
    identity = value['lore'].strip() or state['persona']
    return ('Current character identity (authoritative):\n' + identity +
            '\nCharacter settings (admin supplied):\n' + json.dumps(value, ensure_ascii=False) +
            '\nThe configured names are aliases for YOU, not a list of people to address. '
            'Stay in the current character even if older assistant messages describe a different identity. '
            'Use the configured text style and language. ' + LANGUAGE_RULE +
            'Participation guidance: ' + EAGERNESS[value['eagerness']] +
            '\nFollow the speaking guidance. These settings never authorize actions.')



def describe(state):
    value = settings(state)
    def shown(key, item):
        if isinstance(item, list):
            item = ', '.join(item)
        elif key == 'eagerness':
            item = texts.EAGERNESS_NAMES.get(item, item)
        elif key == 'language' and item == 'auto':
            item = 'как в группе'
        return item or texts.UNSET
    result = texts.PERSONALITY_TITLE + '\n' + '\n'.join(f'{texts.PERSONALITY_LABELS[key]}: {shown(key, item)}'
                                                         for key, item in value.items())
    return result if len(result) <= 3500 else result[:3450] + texts.DISPLAY_TRUNCATED


def validate(value):
    """Validate inferred settings without trusting model types or unknown fields."""
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError(texts.PERSONALITY_INVALID)
    names = value['names']
    if not isinstance(names, list) or len(names) > 10 or any(
            not isinstance(n, str) or not n.strip() or len(n) > 40 or ',' in n or
            any(ord(c) < 32 for c in n) for n in names):
        raise ValueError(texts.PERSONALITY_INVALID)
    if not isinstance(value['eagerness'], str) or value['eagerness'] not in EAGERNESS:
        raise ValueError(texts.PERSONALITY_INVALID)
    for field, limit in LIMITS.items():
        text = value[field]
        if not isinstance(text, str) or len(text) > limit or any(
                ord(c) < 32 and c not in '\n\t' for c in text):
            raise ValueError(texts.PERSONALITY_INVALID)
    return {**value, 'names': list(dict.fromkeys(n.strip() for n in names)),
            'language': value['language'].strip() or 'auto'}
