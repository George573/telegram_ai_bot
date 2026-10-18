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
- Near search: `read_yandex_maps_search` gets an optional `near` point. The bot turns it into the nearest
  Moscow metro or MCC station from a bundled station list and searches by text ("кофейня Серпуховская", the form that was tested).
  A check on 2026-10-18 (3 places, 10 results each) found station text search far more local than passing
  coordinates to Yandex (`ll`): 8–10 of 10 results within 1 km versus 1–6, median 0.2–0.5 km versus 0.7–1.7 km;
  zoom made no difference. The bot adds a straight-line `distance_m` from the real point to each result and
  sorts nearest first. Walking time still comes only from `route_link`. The tools service is unchanged.
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

## Station list (`src/moscow_stations.json`)

- Metro and MCC stations with name and coordinates (about 300 entries; MCD and closed stations excluded),
  generated once from Wikidata (CC0, no attribution needed) by `scripts/moscow_stations.py` and committed.
  The script is kept so the file can be refreshed when stations open; the bot never fetches it at runtime.
- Stations sharing a name (interchanges) are merged; a loader validates names and coordinates at import.

## Near search (`read_yandex_maps_search`)

- Schema: `near: string | null` — `"lat,lon"` copied from a location line in the conversation; null otherwise.
  Description: use it when someone asks for places near a location they shared; `query` then holds only the
  category ("кофейня"), never a street or district.
- Bot validation (`model._reader_call` path): format `lat,lon` with decimals; inside a Moscow-area box
  (lat 55.1–56.1, lon 36.8–38.3). A wrong value returns a fixable error naming `near`, like `route_link`.
- Station: the nearest station by haversine distance. If it is farther than 2.5 km, the call returns a fixable
  error telling the model to ask for a street or district instead (no search runs).
- Query sent to the tools service: `<query> <station>`, the tested form. The tools service and its API are unchanged.
- Result: each organization with coordinates gets `distance_m` from the real point (rounded to 10 m), results
  are sorted by it (no coordinates last), and the result carries `near_station: {name, distance_m}` plus a note
  that distances are straight-line.
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
- stations: file loads and validates; nearest station for synthetic points; interchange names merged.
- maps: `near` becomes `<query> <station>`; distances and sorting; invalid or out-of-area `near` and
  a point with no station within 2.5 km return fixable errors and run no search; tool schema lists `near`.
- search_web: coordinates in a query refused.
- README: a short "Locations" paragraph; skills list unchanged.
