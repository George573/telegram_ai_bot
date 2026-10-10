"""Structured place/event recommendations: schema, validation, grounded plain-text rendering."""
import re
from urllib.parse import urlsplit, urlunsplit

KINDS = ('cafe', 'restaurant', 'bar', 'park', 'theatre', 'concert', 'standup', 'exhibition', 'museum',
         'kids', 'excursion', 'festival', 'other')
ICONS = {'cafe': '☕', 'restaurant': '🍽', 'bar': '🍸', 'park': '🌳', 'theatre': '🎭', 'concert': '🎵',
         'standup': '🎤', 'exhibition': '🖼', 'museum': '🏛', 'kids': '🧸', 'excursion': '🚶',
         'festival': '🎪', 'other': '📍'}
MAX_ITEMS = 5
LIMITS = {'intro': 300, 'name': 120, 'venue': 150, 'address': 200, 'when': 200, 'price': 100,
          'why': 300, 'reviews': 300, 'caveats': 250, 'relaxed': 150, 'follow_up': 200, 'evidence_gaps': 250}
NAME = 'place_recommendations'


def nullable(limit):
    return {'type': ['string', 'null'], 'maxLength': limit}


ITEM_FIELDS = ('kind', 'name', 'venue', 'address', 'when', 'price', 'why', 'reviews', 'caveats', 'relaxed', 'links')
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['intro', 'items', 'follow_up', 'evidence_gaps'],
    'properties': {
        'intro': {'type': 'string', 'maxLength': LIMITS['intro']},
        'items': {'type': 'array', 'maxItems': MAX_ITEMS, 'items': {
            'type': 'object', 'additionalProperties': False, 'required': list(ITEM_FIELDS),
            'properties': {
                'kind': {'type': 'string', 'enum': list(KINDS)},
                'name': {'type': 'string', 'maxLength': LIMITS['name']},
                'venue': nullable(LIMITS['venue']), 'address': nullable(LIMITS['address']),
                'when': nullable(LIMITS['when']), 'price': nullable(LIMITS['price']),
                'why': {'type': 'string', 'maxLength': LIMITS['why']},
                'reviews': nullable(LIMITS['reviews']), 'caveats': nullable(LIMITS['caveats']),
                'relaxed': nullable(LIMITS['relaxed']),
                'links': {'type': 'array', 'maxItems': 3, 'items': {'type': 'string', 'maxLength': 400}}}}},
        'follow_up': nullable(LIMITS['follow_up']),
        'evidence_gaps': nullable(LIMITS['evidence_gaps'])}}
FORMAT = {'type': 'json_schema', 'name': NAME, 'strict': True, 'schema': SCHEMA}
INSTRUCTIONS = ('\nA place or event skill is active: return the final answer as one JSON object matching the '
                'place_recommendations schema (intro, items, follow_up, evidence_gaps), not plain text. '
                'Write intro and follow_up in the persona voice and the conversation language; keep item fields '
                'factual. Use null for unknown fields. links may contain only URLs returned by tools in this request. '
                'For a near-miss that does not meet a soft preference, set relaxed to what is relaxed; otherwise null. '
                'Never include an item that fails a hard requirement.')
CORRECTIONS = {
    'malformed_json': 'Return one valid JSON object only, without Markdown fences or surrounding text.',
    'duplicate_fields': 'Include each key exactly once.',
    'not_object': 'Return one JSON object, not an array or string.',
    'missing_fields': 'Include intro, items, follow_up and evidence_gaps; every item needs all item fields.',
    'extra_fields': 'Remove fields that are not in the schema.',
    'wrong_types': 'Use strings or null for text fields, an array of URL strings for links, and a known kind.',
    'too_long': 'Shorten the fields to the schema limits and use at most five items.',
    'reply_required': 'Write a nonempty intro; with no items, explain in intro and evidence_gaps what was missing.',
}


class RecommendationError(ValueError):
    """Fixed validation category, never model-generated text."""


def validate(value):
    if not isinstance(value, dict):
        raise RecommendationError('not_object')
    expected = set(SCHEMA['required'])
    if expected - set(value):
        raise RecommendationError('missing_fields')
    if set(value) - expected:
        raise RecommendationError('extra_fields')
    if not isinstance(value['intro'], str) or not isinstance(value['items'], list):
        raise RecommendationError('wrong_types')
    if not value['intro'].strip():
        raise RecommendationError('reply_required')
    if len(value['items']) > MAX_ITEMS:
        raise RecommendationError('too_long')
    for key in ('intro', 'follow_up', 'evidence_gaps'):
        check_text(value[key], key, nullable=key != 'intro')
    for item in value['items']:
        if not isinstance(item, dict):
            raise RecommendationError('wrong_types')
        # Optional for JSON-mode providers; strict schemas always send it.
        item.setdefault('relaxed', None)
        if set(ITEM_FIELDS) - set(item):
            raise RecommendationError('missing_fields')
        if set(item) - set(ITEM_FIELDS):
            raise RecommendationError('extra_fields')
        if item['kind'] not in KINDS or not isinstance(item['links'], list) or len(item['links']) > 3:
            raise RecommendationError('wrong_types')
        if not all(isinstance(link, str) and len(link) <= 400 for link in item['links']):
            raise RecommendationError('wrong_types')
        for key in ('name', 'venue', 'address', 'when', 'price', 'why', 'reviews', 'caveats', 'relaxed'):
            check_text(item[key], key, nullable=key not in {'name', 'why'})
        if not item['name'].strip():
            raise RecommendationError('wrong_types')
    return value


def check_text(value, key, nullable):
    if value is None and nullable:
        return
    if not isinstance(value, str):
        raise RecommendationError('wrong_types')
    if len(value) > LIMITS[key]:
        raise RecommendationError('too_long')


def canonical(url):
    """Comparable form: https/http, lowercase host, no fragment, no trailing slash."""
    try:
        parts = urlsplit(url.strip())
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
            return None
        host = parts.hostname.lower().removeprefix('www.')
        return urlunsplit(('https', host, parts.path.rstrip('/') or '/', parts.query, ''))
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
    elif isinstance(value, str) and value.startswith(('http://', 'https://')) and len(value) <= 400:
        url = canonical(value)
        if url:
            found.add(url)
    return found


def clean(text):
    # One line per field: no newlines that could fake extra cards.
    return re.sub(r'\s+', ' ', text).strip() if text else ''


def name_key(name):
    return re.sub(r'[^\w]+', ' ', (name or '').casefold()).strip()


def render(value, evidence, max_chars=3500, excluded_names=()):
    """Plain-text Telegram cards; returns (text, dropped_link_count, dropped_item_count).

    Items naming a chain that a tool excluded at the user's request are removed entirely.
    """
    dropped = 0
    excluded = {name_key(name) for name in excluded_names if name_key(name)}
    def is_excluded(name):
        key = name_key(name)
        # Same name, or a branded variant of it ("Surf Coffee x Boom").
        return any(key == chain or key.startswith(chain + ' ') for chain in excluded)
    items = [item for item in value['items'] if not is_excluded(item['name'])]
    removed = len(value['items']) - len(items)
    cards = []
    for index, item in enumerate(items, 1):
        links = []
        for link in item['links']:
            url = canonical(link)
            if url and url in evidence and url not in links:
                links.append(url)
            else:
                dropped += 1
        head = f"{index}. {ICONS[item['kind']]} {clean(item['name'])}"
        if item['venue']:
            head += ' — ' + clean(item['venue'])
        lines = [head]
        facts = [clean(x) for x in (item['when'] and '📅 ' + item['when'], item['price'] and '💳 ' + item['price']) if x]
        if facts:
            lines.append(' · '.join(facts))
        if item['address']:
            lines.append('📍 ' + clean(item['address']))
        lines.append('💡 ' + clean(item['why']))
        if item['reviews']:
            lines.append('⭐ ' + clean(item['reviews']))
        if item['caveats']:
            lines.append('⚠️ ' + clean(item['caveats']))
        if item.get('relaxed'):
            # A near-miss: say exactly which preference it does not meet.
            lines.append('↔ ' + clean(item['relaxed']))
        lines.append('🔗 ' + ' '.join(links) if links else '🔗 —')
        cards.append('\n'.join(lines))
    head = clean(value['intro'])
    tail = [clean(x) for x in (value['evidence_gaps'] and '❔ ' + value['evidence_gaps'], value['follow_up']) if x]
    # Drop trailing cards rather than cutting a card or its links in half.
    while True:
        text = '\n\n'.join([head] + cards + tail)
        if len(text) <= max_chars or not cards:
            return text[:max_chars], dropped, removed
        cards.pop()
