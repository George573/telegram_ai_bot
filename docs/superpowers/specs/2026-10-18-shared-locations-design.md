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
- Near search: `read_yandex_maps_search` gets an optional `near` point for requests about a shared location
  ("рядом со мной", "near my pin"). The tools service passes it to Yandex Maps as a search area: centre `ll`,
  a 2×2 km box `spn` and `rspn=1` (search only inside the area). The bot sorts every returned organization by
  straight-line distance from the point before keeping the first 10, and adds `distance_m`. When the person
  names a street, metro or landmark, the model uses a plain text query and no `near`, as today.
- Evidence (2026-10-18, about 50 free Maps page fetches): for Серпуховская, `ll` alone kept 1 of 10 results
  within 1 km (median 1.7 km); `ll` + 2 km box + distance sort kept 9 within 500 m (median 0.2 km), matching a
  text search by metro name. Strogino and Kazan gave similar or better results with the box. The page returns
  about 24 organizations, so sorting before the cap matters. A point with nothing nearby (inside Битцевский
  парк) returns places 2.6 km away with every method; the distances let the bot say so honestly.
- `near` works outside Moscow (Kazan results came back through the same Moscow URL; a region-free URL returned
  404), so it is not limited to a Moscow area. Afisha and the skills stay Moscow-focused.
- Walking time still comes only from `route_link`.
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
  line for that Telegram message (same chat and thread). If none exists (the row was compacted or expired while
  sharing went on, or sharing began before the bot ran), a fresh line is appended so the position stays known.
- `/forgetme` and memory reset remove location lines like any other message.

## Near search (`read_yandex_maps_search`)

- Schema: `near: string | null` — `"lat,lon"` copied from a location line in the conversation; null otherwise.
  Description: use it when someone asks for places near a location they shared; `query` then holds only the
  category ("кофейня"), never a street or district. When the person names a place, leave `near` null.
- Bot validation (`model._reader_call` path): format `lat,lon` with decimals, latitude in [-90, 90], longitude
  in [-180, 180]. A wrong value returns a fixable error naming `near`, like `route_link`; no search runs.
- Tool client sends `near` to `/v1/yandex-maps-search`. `server.maps_search` accepts it as optional (absent or
  null keeps today's request) and validates it the same way.
- `yandex_maps.search_url(query, near=None)`: with `near`, appends
  `?ll=<lon>%2C<lat>&z=15&spn=<dlon>%2C<dlat>&rspn=1`, where the box is 2 km on each side
  (`dlat = 2/111`, `dlon = 2/(111·cos lat)`, 4 decimals). Without `near`, the URL is unchanged.
- `normalized_search(url, items, exclude_chains, near=None)`: with `near`, organizations are sorted by
  haversine distance from the point before chain removal and the 10-result cap (no coordinates last); each
  result gets `distance_m` (rounded to 10 m) and the result notes that distances are straight-line and that
  walking time comes from `route_link`. Without `near`, order and fields are unchanged.
## Prompts and privacy

- Model instructions (with web on): lines like `[Location …]`, `[Live location …]` and `[Venue …]` are points
  people shared. For "near me / near my location" use the speaker's most recent one, or the one they name, in
  `near` and as the `route_link` origin. If the speaker has shared none, ask for a place or a location.
  Never put coordinates into `search_web`.
- `nearby-places` skill, section 2: one line — if the person asks for places near a location they shared,
  search with `near` and the category only; if they name a street, metro or landmark, search by text as usual.
  `distance_m` is straight-line; walking time only via `route_link`. If even the nearest result is far, say so.
- `search_web` arguments: a query containing a `lat,lon` pattern is refused with a fixable error.

## Testing

All synthetic data, mocked network.

- normalize: point, live, venue (with title/address cleaning), bad or bool coordinates dropped, edited
  location accepted, edited text dropped.
- service: location stored silently in directed and all modes with no model call and no admission; paused
  ignored; live edit rewrites the matching line; edit with no stored line ignored.
- maps: `search_url` with and without `near` (box size at different latitudes); server accepts, omits and
  rejects `near`; distance sort happens before the cap (a near result beyond Yandex's first 10 is kept);
  `distance_m` present only with `near`; invalid `near` returns a fixable error and runs no search; tool schema
  lists `near`.
- search_web: coordinates in a query refused.
- README: a short "Locations" paragraph; skills list unchanged.
