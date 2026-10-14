"""Yandex Maps route links built locally: no request, no key, no routing estimate.

The link opens Yandex Maps with the route already drawn; Yandex computes the real path and time.
"""
import re
from urllib.parse import quote

MODES = {'walk': 'pd', 'transit': 'mt', 'car': 'auto'}
MODE_NAMES = {'walk': 'пешком', 'transit': 'на общественном транспорте', 'car': 'на машине'}
# Moscow and the surrounding region; a point outside is almost certainly a swapped or invented pair.
LAT_RANGE, LON_RANGE = (54.2, 57.2), (35.1, 40.3)
COORDINATES = re.compile(r'\s*(-?\d{1,3}(?:\.\d{1,8})?)\s*,\s*(-?\d{1,3}(?:\.\d{1,8})?)\s*')

ROUTE_LINK = {'name': 'route_link',
    'description': 'Build a Yandex Maps link that opens a ready route between two points in Moscow, on foot, by '
                   'public transport or by car. Use it whenever someone asks how to get somewhere, how far it is '
                   'or how long it takes, and put the returned link in your answer. You cannot compute travel '
                   'time or distance yourself: never state minutes or meters unless a tool returned them; say that '
                   'the link shows the exact route and time instead. Each point is either coordinates "lat,lon" '
                   'copied from a read_yandex_maps_search result (preferred for places: exact) or a street address '
                   'as the person wrote it, e.g. "Алтайская улица, 2". Do not guess coordinates for an address. '
                   'If the starting point is unknown, ask for it rather than inventing one. If the result has '
                   'status "error", fix the named field as described and call again.',
    'parameters': {'type': 'object', 'properties': {
        'origin': {'type': 'string', 'maxLength': 200,
                   'description': 'Start: "lat,lon" from Maps results, or an address.'},
        'destination': {'type': 'string', 'maxLength': 200,
                        'description': 'End: "lat,lon" from Maps results, or an address.'},
        'mode': {'type': 'string', 'enum': list(MODES)}},
        'required': ['origin', 'destination', 'mode'], 'additionalProperties': False}}


class RouteError(ValueError):
    """A mistake the model can fix; the message is written for the model."""
    def __init__(self, message, field):
        super().__init__(message)
        self.field = field


def point(value, field):
    """Normalized rtext point: "lat,lon" for coordinates, else an address with the city."""
    if not isinstance(value, str) or not value.strip():
        raise RouteError(f'{field} must be "lat,lon" from Maps results or an address.', field)
    value = re.sub(r'\s+', ' ', value).strip()
    if len(value) > 200 or re.search(r'[~&#?<>\x00-\x1f]|https?:', value):
        raise RouteError(f'{field} must be plain coordinates or an address, not a link, at most 200 characters.', field)
    match = COORDINATES.fullmatch(value)
    if match:
        lat, lon = float(match[1]), float(match[2])
        if LON_RANGE[0] <= lat <= LON_RANGE[1] and LAT_RANGE[0] <= lon <= LAT_RANGE[1]:
            raise RouteError(f'{field} looks like "lon,lat"; send "lat,lon" (latitude about 55 in Moscow first).', field)
        if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]):
            raise RouteError(f'{field} is outside Moscow. Copy "lat,lon" exactly from a Maps result.', field)
        return f'{lat:.6f},{lon:.6f}'
    if not re.search(r'[A-Za-zА-Яа-яЁё]', value):
        raise RouteError(f'{field} is neither "lat,lon" nor an address.', field)
    return value if 'москва' in value.casefold() else 'Москва, ' + value


def encode(value):
    """Keep letters (Cyrillic too) readable; escape spaces, brackets and URL syntax. ~5x shorter than full escaping."""
    return ''.join(char if char.isalnum() or char in ',.-' else quote(char) for char in value)


def route_link(arguments):
    """{status, url, mode, notice} for a valid request; raises RouteError otherwise."""
    if not isinstance(arguments, dict) or set(arguments) != {'origin', 'destination', 'mode'}:
        raise RouteError('Send exactly origin, destination and mode.', 'origin')
    if arguments['mode'] not in MODES:
        raise RouteError('mode must be "walk", "transit" or "car".', 'mode')
    origin, destination = point(arguments['origin'], 'origin'), point(arguments['destination'], 'destination')
    if origin == destination:
        raise RouteError('origin and destination are the same point.', 'destination')
    url = ('https://yandex.ru/maps/213/moscow/?rtext=' + encode(origin) + '~' + encode(destination)
           + '&rtt=' + MODES[arguments['mode']])
    return {'status': 'ok', 'url': url, 'mode': arguments['mode'],
            'notice': f'Route link only: Yandex Maps draws the route {MODE_NAMES[arguments["mode"]]} and shows the '
                      'exact time and distance when opened. No time or distance was computed; do not state any. '
                      'Put this link in your answer, e.g. [Маршрут пешком](url).'}
