# Shared locations

## Goal

People can send Telegram's built-in location (a pin, "send my location", a live location or a venue picked
from Telegram's place list). The bot remembers it silently. Later someone may ask for places near it
("@bot кофе рядом с моей точкой") or for a route from it; the bot then searches around that point and reports
real distances. Sending a location never has to be a reply to the bot, and never by itself makes the bot speak.

## Decisions

- Kinds: point (`location`), venue (`venue`), live location (`location` with `live_period`, then
  `edited_message` updates).
- A location message is stored in every chat mode and never triggers a reply or a model call. It uses no
  request allowance. While the chat is paused it is ignored like everything else.
- Near search: `read_yandex_maps_search` gets an optional `near` point. The tools service passes it to Yandex
  Maps as the map view (`?ll=<lon>,<lat>&z=15`); a live check on 2026-10-18 showed all 10 results within
  ~2.5 km of a Medvedkovo point. The bot adds a straight-line `distance_m` to each result and sorts nearest
  first. Walking time still comes only from `route_link`.
- Coordinates never go into `search_web` queries.
- Out of scope: street names for bare pins (needs a geocoding API), live locations started before the bot ran,
  maps in replies.

## Intake (`policy.normalize`)

- `message.location` → `{'lat', 'lon', 'live'}`; `live` is true when `live_period` is present.
  `message.venue` → its `location` plus `title` (≤120) and `address` (≤200), cleaned to one line.
- Latitude must be a number in [-90, 90], longitude in [-180, 180] (bools rejected); otherwise the update is
  dropped. Coordinates are rounded to 5 decimals (~1 m).
- The payload gets `location` (the dict above, with `kind`: `point` / `live` / `venue`), empty `text`,
  `directed=False`, no command, no media.
- `edited_message` is added to `allowed_updates`. An edited message is accepted only when it carries a
  `location`; the payload is marked `edited=True`. Every other edit is dropped, as today.
- History content (`Speaker <id>: ` prefix as for text):
  - `[Location 55.73120,37.63630]`
  - `[Live location 55.73120,37.63630]`
  - `[Venue, untrusted data] «Title», Address — 55.73120,37.63630`

## Handling (`service.prepare`)

- A payload with `location` is handled before the paused/directed checks that drop undirected messages:
  paused → finish `paused`; otherwise store and finish `location_saved` (new outcome, added to the
  observability allowlist). No admission token, no compaction, no reply.
- New message: `store.append(..., message_id=<telegram id>)` as for text.
- Edited live location: `store.replace_message(chat_id, telegram_message_id, content)` rewrites the stored
  line for that Telegram message (same chat and thread). If none exists (live location started before the bot
  ran, or the row was forgotten), the update is ignored.
- `/forgetme` and memory reset remove location lines like any other message.

## Near search (`read_yandex_maps_search`)

- Schema: `near: string | null` — `"lat,lon"` copied from a location line in the conversation; null otherwise.
  Description: use it when someone asks for places near a location they shared; the query then needs only the
  category ("кофейня"), not a street.
- Bot validation (`model._reader_call` path): format `lat,lon` with decimals; inside a Moscow-area box
  (lat 55.1–56.1, lon 36.8–38.3). A wrong value returns a fixable error naming `near`, like `route_link`.
- Tool client sends `near` to `/v1/yandex-maps-search`; `server.maps_search` accepts it (optional, null
  allowed); `yandex_maps.search_url(query, near=None)` appends `?ll=<lon>%2C<lat>&z=15`.
- `normalized_search(url, items, exclude_chains, near=None)`: when `near` is set, each result with coordinates
  gets `distance_m` (haversine, rounded to 10 m) and results are sorted by it (results without coordinates
  last). The tool result notes that distances are straight-line.

## Prompts and privacy

- Model instructions (with web on): lines like `[Location …]`, `[Live location …]` and `[Venue …]` are points
  people shared. For "near me / near my location" use the speaker's most recent one, or the one they name, in
  `near` and as the `route_link` origin. If the speaker has shared none, ask for a place or a location.
  Never put coordinates into `search_web`.
- `nearby-places` skill, section 2: one line — if the person shared a location, search with `near` and the
  category only; `distance_m` is straight-line, walking time only via `route_link`.
- `search_web` arguments: a query containing a `lat,lon` pattern is refused with a fixable error.

## Testing

All synthetic data, mocked network.

- normalize: point, live, venue (with title/address cleaning), bad or bool coordinates dropped, edited
  location accepted, edited text dropped.
- service: location stored silently in directed and all modes with no model call and no admission; paused
  ignored; live edit rewrites the matching line; edit with no stored line ignored.
- maps: `search_url` with `near`; server accepts and rejects `near`; distances and sorting; invalid `near`
  returns a fixable error; tool schema lists `near`.
- search_web: coordinates in a query refused.
- README: a short "Locations" paragraph; skills list unchanged.
