"""Structured place/event recommendations: schema, validation, grounded plain-text rendering."""
from collections import namedtuple
import re
from urllib.parse import unquote, urlsplit, urlunsplit

MAX_ITEMS = 5
MAX_UNCONFIRMED = 3
LIMITS = {'intro': 300, 'name': 120, 'address': 200, 'rating': 100, 'when': 200, 'text': 600,
          'note': 200, 'outro': 300}
NAME = 'place_recommendations'
# Route links carry two addresses; longer than any page link, still far below Telegram's message limit.
MAX_URL_CHARS = 2000
# The only fixed lines of a card; everything else in it is the model's own text.
LABELS = {'address': 'Адрес: ', 'rating': 'Рейтинг: ', 'when': 'Когда: '}
UNCONFIRMED_HEADING = 'Не удалось подтвердить:'


def nullable(limit):
    return {'type': ['string', 'null'], 'maxLength': limit}


ITEM_FIELDS = ('name', 'address', 'rating', 'when', 'text', 'links')
UNCONFIRMED_FIELDS = ('name', 'note')
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['intro', 'items', 'unconfirmed', 'outro'],
    'properties': {
        'intro': {'type': 'string', 'maxLength': LIMITS['intro']},
        'items': {'type': 'array', 'maxItems': MAX_ITEMS, 'items': {
            'type': 'object', 'additionalProperties': False, 'required': list(ITEM_FIELDS),
            'properties': {
                'name': {'type': 'string', 'maxLength': LIMITS['name']},
                'address': nullable(LIMITS['address']), 'rating': nullable(LIMITS['rating']),
                'when': nullable(LIMITS['when']),
                'text': {'type': 'string', 'maxLength': LIMITS['text']},
                'links': {'type': 'array', 'maxItems': 3, 'items': {'type': 'string', 'maxLength': 400}}}}},
        'unconfirmed': {'type': 'array', 'maxItems': MAX_UNCONFIRMED, 'items': {
            'type': 'object', 'additionalProperties': False, 'required': list(UNCONFIRMED_FIELDS),
            'properties': {'name': {'type': 'string', 'maxLength': LIMITS['name']},
                           'note': nullable(LIMITS['note'])}}},
        'outro': nullable(LIMITS['outro'])}}
FORMAT = {'type': 'json_schema', 'name': NAME, 'strict': True, 'schema': SCHEMA}
INSTRUCTIONS = ('\nA place or event skill is active: return the final answer as one JSON object matching the '
                'place_recommendations schema (intro, items, unconfirmed, outro), not plain text. '
                'Write intro, each item text and outro in the persona voice and the conversation language. '
                'Item text is free Markdown with your own layout, emphasis and emoji: why it fits, price, caveats, '
                'and for a near-miss what is relaxed. address, rating and when are plain facts from tool results; '
                'use null rather than guessing and never invent addresses, ratings, hours or prices. '
                'A card needs an address and a link returned by tools; put a place you could not confirm into '
                'unconfirmed with a short note instead. links may contain only URLs returned by tools in this '
                'request. Never include an item that fails a hard requirement.')
CORRECTIONS = {
    'malformed_json': 'Return one valid JSON object only, without Markdown fences or surrounding text.',
    'duplicate_fields': 'Include each key exactly once.',
    'not_object': 'Return one JSON object, not an array or string.',
    'missing_fields': 'Include intro, items, unconfirmed and outro; every item needs name, address, rating, when, '
                      'text and links; every unconfirmed entry needs name and note.',
    'extra_fields': 'Remove fields that are not in the schema.',
    'wrong_types': 'Use strings or null for facts, a nonempty name and text, and an array of URL strings for links.',
    'too_long': 'Shorten the fields to the schema limits; use at most five items and three unconfirmed entries.',
    'reply_required': 'Write a nonempty intro; with no items, explain in intro or outro what was missing.',
}


class RecommendationError(ValueError):
    """Fixed validation category, never model-generated text."""


def validate(value):
    if not isinstance(value, dict):
        raise RecommendationError('not_object')
    # Optional for JSON-mode providers; strict schemas always send them.
    value.setdefault('unconfirmed', [])
    value.setdefault('outro', None)
    check_keys(value, SCHEMA['required'])
    if not all(isinstance(value[key], list) for key in ('items', 'unconfirmed')):
        raise RecommendationError('wrong_types')
    check_text(value['intro'], 'intro', nullable=False)
    if not value['intro'].strip():
        raise RecommendationError('reply_required')
    if len(value['items']) > MAX_ITEMS or len(value['unconfirmed']) > MAX_UNCONFIRMED:
        raise RecommendationError('too_long')
    check_text(value['outro'], 'outro', nullable=True)
    for item in value['items']:
        check_keys(item, ITEM_FIELDS)
        if not isinstance(item['links'], list) or len(item['links']) > 3:
            raise RecommendationError('wrong_types')
        if not all(isinstance(link, str) and len(link) <= 400 for link in item['links']):
            raise RecommendationError('wrong_types')
        for key in ('name', 'address', 'rating', 'when', 'text'):
            check_text(item[key], key, nullable=key in LABELS)
        if not item['name'].strip() or not item['text'].strip():
            raise RecommendationError('wrong_types')
    for entry in value['unconfirmed']:
        check_keys(entry, UNCONFIRMED_FIELDS)
        check_text(entry['name'], 'name', nullable=False)
        check_text(entry['note'], 'note', nullable=True)
        if not entry['name'].strip():
            raise RecommendationError('wrong_types')
    return value


def check_keys(value, fields):
    if not isinstance(value, dict):
        raise RecommendationError('wrong_types')
    if set(fields) - set(value):
        raise RecommendationError('missing_fields')
    if set(value) - set(fields):
        raise RecommendationError('extra_fields')


def check_text(value, key, nullable):
    if value is None and nullable:
        return
    if not isinstance(value, str):
        raise RecommendationError('wrong_types')
    if len(value) > LIMITS[key]:
        raise RecommendationError('too_long')


def canonical(url):
    """Comparable form: https/http, lowercase host, no fragment, no trailing slash, %-escapes decoded."""
    try:
        parts = urlsplit(url.strip())
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
            return None
        host = parts.hostname.lower().removeprefix('www.')
        # "%D0%90" and "А" are the same link; models sometimes copy one as the other.
        return urlunsplit(('https', host, unquote(parts.path).rstrip('/') or '/', unquote(parts.query), ''))
    except (ValueError, AttributeError):
        return None


def evidence_urls(value):
    """Collect every http(s) URL inside a tool result (nested dicts/lists/strings)."""
    found = set()
    if isinstance(value, dict):
        for item in value.values():
            found |= evidence_urls(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= evidence_urls(item)
    elif isinstance(value, str) and value.startswith(('http://', 'https://')) and len(value) <= MAX_URL_CHARS:
        url = canonical(value)
        if url:
            found.add(url)
    return found


MARKDOWN_LINK = re.compile(r'\[([^\]\n]{1,200})\]\((https?://[^\s)]{1,%d})\)' % MAX_URL_CHARS)
BARE_URL = re.compile(r'https?://[^\s<>()\[\]"\']{1,%d}' % MAX_URL_CHARS)
LINK_OR_URL = re.compile(MARKDOWN_LINK.pattern + '|(' + BARE_URL.pattern + ')')


def text_urls(text):
    """Canonical URLs written anywhere in free text (conversation messages)."""
    return {url for url in (canonical(match.rstrip('.,;:!?')) for match in BARE_URL.findall(text or '')) if url}


def ground_links(text, allowed):
    """Keep links the model placed itself only when their URL is known; returns (text, kept, dropped).

    A Markdown link with an unknown URL keeps its label; an unknown bare URL is removed.
    """
    counts = {'kept': 0, 'dropped': 0}
    def check(match):
        if match.group(1) is not None:  # [label](url)
            url, keep, lose = match.group(2), match.group(0), match.group(1)
        else:
            url = match.group(3).rstrip('.,;:!?')
            keep, lose = match.group(3), match.group(3)[len(url):]
        known = canonical(url) in allowed
        counts['kept' if known else 'dropped'] += 1
        return keep if known else lose
    grounded = LINK_OR_URL.sub(check, text)
    if counts['dropped']:
        grounded = re.sub(r'[ \t]+([.,;:!?])', r'\1', re.sub(r'[ \t]{2,}', ' ', grounded))
        # A comma left before a removed trailing URL: "нет, <url>!" becomes "нет!".
        grounded = re.sub(r'[,;:]([.!?])', r'\1', grounded)
    return grounded, counts['kept'], counts['dropped']


# Model text may not imitate the bot's own card structure.
FAKE_HEAD = re.compile(r'^\d+[.)]\s+(?=(?:\S+ )?\*\*)')
FAKE_LABEL = re.compile(r'(?:%s)\s*:|%s' % ('|'.join(label.strip(': ') for label in LABELS.values()),
                                            UNCONFIRMED_HEADING.rstrip(':')), re.I)


def clean(text):
    # One line per fact: no newlines that could fake extra cards.
    return re.sub(r'\s+', ' ', text).strip() if text else ''


def one_line(text):
    return FAKE_HEAD.sub('', clean(text))


def block(text):
    """Free card text: single line breaks kept; blank lines and structure-faking prefixes removed."""
    lines = []
    for line in (text or '').splitlines():
        line = FAKE_HEAD.sub('', re.sub(r'[ \t]+', ' ', line).strip())
        if line:
            lines.append('– ' + line if FAKE_LABEL.match(line) else line)
    return '\n'.join(lines)


def name_key(name):
    return re.sub(r'[^\w]+', ' ', (name or '').casefold()).strip()


Rendered = namedtuple('Rendered', 'text cards unconfirmed dropped_links dropped_items demoted_items')


def render(value, evidence, max_chars=3500, excluded_names=()):
    """Plain-text Telegram cards plus a list of places that could not be confirmed.

    Places naming a chain that a tool excluded at the user's request are removed entirely. An item without an
    address or without any link seen in tool results is listed as unconfirmed instead of getting a card.
    """
    dropped = 0
    excluded = {name_key(name) for name in excluded_names if name_key(name)}
    def is_excluded(name):
        key = name_key(name)
        # Same name, or a branded variant of it ("Surf Coffee x Boom").
        return any(key == chain or key.startswith(chain + ' ') for chain in excluded)
    def grounded(text):
        nonlocal dropped
        text, _, lost = ground_links(text or '', evidence)
        dropped += lost
        return text
    items = [item for item in value['items'] if not is_excluded(item['name'])]
    unconfirmed = [(entry['name'], entry['note']) for entry in value['unconfirmed'] if not is_excluded(entry['name'])]
    removed = len(value['items']) - len(items) + len(value['unconfirmed']) - len(unconfirmed)
    cards, demoted = [], []
    for item in items:
        links = []
        for link in item['links']:
            url = canonical(link)
            if url and url in evidence and url not in links:
                links.append(url)
            else:
                dropped += 1
        if not (item['address'] or '').strip() or not links:
            demoted.append((item['name'], None))
            continue
        lines = [f"{len(cards) + 1}. **{one_line(item['name']).replace('*', '')}**"]
        lines += [LABELS[key] + one_line(item[key]) for key in LABELS if (item[key] or '').strip()]
        body = block(grounded(item['text']))
        if body:
            lines.append(body)
        lines.append(' '.join(links))
        cards.append('\n'.join(lines))
    unconfirmed = (unconfirmed + demoted)[:MAX_UNCONFIRMED]
    entries = []
    for name, note in unconfirmed:
        note = one_line(grounded(note))
        entries.append('- ' + one_line(name).replace('*', '') + (' — ' + note if note else ''))
    head = one_line(grounded(value['intro']))
    tail = [part for part in ('\n'.join([UNCONFIRMED_HEADING] + entries) if entries else '',
                              one_line(grounded(value['outro']))) if part]
    # Drop trailing cards rather than cutting a card or its links in half.
    while True:
        text = '\n\n'.join([head] + cards + tail)
        if len(text) <= max_chars or not cards:
            return Rendered(text[:max_chars], len(cards), len(entries), dropped, removed, len(demoted))
        cards.pop()
