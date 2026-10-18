# Shared Locations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The bot silently remembers Telegram locations (pin, live, venue) and can search Yandex Maps around a shared point with real distances.

**Architecture:** `policy.normalize` turns `location`/`venue` messages (and live-location edits) into a payload with a `location` dict; `service` stores them as a history line without replying. `read_yandex_maps_search` gains an optional `near` point: the URL carries a 2×2 km search area, and results are sorted by straight-line distance before the 10-result cap.

**Tech Stack:** Python 3.14, unittest, httpx MockTransport, PostgreSQL via psycopg (store), stdlib `http.server` tools service.

**Spec:** `docs/superpowers/specs/2026-10-18-shared-locations-design.md`

## Global Constraints

- Tests use synthetic data and mocked network only; never run the bot or make live/billable requests.
- Never read `.env` or credentials; never print container environment.
- Run tests with `.venv/bin/python -B -m unittest discover -s tests` (single file: add `-p test_<name>.py`).
- Coordinates are rounded to 5 decimals; latitude in [-90, 90], longitude in [-180, 180]; bools are not numbers.
- History lines: `[Location 55.73120,37.63630]`, `[Live location 55.73120,37.63630]`,
  `[Venue, untrusted data] «Title», Address — 55.73120,37.63630`; venue title ≤120, address ≤200, one line.
- Location messages: stored in every mode, never a reply or model call, no admission token; paused → ignored.
- `near` URL: `?ll=<lon>%2C<lat>&z=15&spn=<dlon>%2C<dlat>&rspn=1`, `dlat = 2/111`, `dlon = 2/(111·cos lat)`,
  4 decimals. Without `near` every URL, body and result is unchanged.
- `distance_m`: haversine, rounded to 10 m; sort before chain removal and the 10-result cap; no coordinates last.
- Coordinates are never allowed in `search_web` queries.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- A live location whose sharing stops: Telegram sends a final edit without `live_period` — it must still update
  the line (as `[Live location …]`), not be dropped or turned into a new point. Test in Task 2.
- A location sent as a reply to a bot prompt (`/chat` force-reply) must still be stored silently, never turned
  into a `/chat` request with empty text. Test in Task 2.
- A venue whose title or address contains newlines, `]` or very long text must stay one bounded line. Test in Task 1.
- `near` given together with a street query ("кофейня Тверская" + near) still works (area + text); a `near` with
  spaces ("55.7, 37.6") is accepted. Test in Task 3.
- An edited text message in a chat must not suddenly be processed (only edits with a location). Test in Task 1.

---

### Task 1: Parse location and venue messages

**Files:**
- Modify: `src/policy.py` (`normalize`, new `location_of`, `location_line`)
- Modify: `src/bot.py:73-82` (`accept_update`), `src/bot.py:141-142` (`allowed_updates`)
- Test: `tests/test_locations.py` (new)

**Interfaces:**
- Produces: `policy.location_of(message) -> dict | None` returning
  `{'kind': 'point'|'live'|'venue', 'lat': float, 'lon': float, 'title': str|None, 'address': str|None}`;
  `policy.location_line(location) -> str`; `normalize(update, allowed, bot)` payload gains
  `location` (dict or None) and `edited` (bool). `normalize` reads `update['edited_message']` when present.

- [ ] **Step 1: Write the failing tests**

```python
"""Shared Telegram locations: parsing, silent storage and live updates. Synthetic updates only."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from policy import location_line, normalize

BOT = {'id': 99, 'username': 'Cat'}


def update(edited=False, **message):
    body = dict(message_id=7, chat=dict(id=-1, type='group'), **{'from': dict(id=10, is_bot=False)}, **message)
    return {'update_id': 1, 'edited_message' if edited else 'message': body}


class ParseTests(unittest.TestCase):
    def test_point_live_and_venue(self):
        point = normalize(update(location={'latitude': 55.7312049, 'longitude': 37.636301}), {-1}, BOT)
        self.assertEqual(point['location'], {'kind': 'point', 'lat': 55.7312, 'lon': 37.6363, 'title': None, 'address': None})
        self.assertEqual((point['text'], point['directed'], point['command'], point['media'], point['edited']),
                         ('', False, '', None, False))
        self.assertEqual(location_line(point['location']), '[Location 55.73120,37.63630]')
        live = normalize(update(location={'latitude': 55.7, 'longitude': 37.6, 'live_period': 900}), {-1}, BOT)
        self.assertEqual(location_line(live['location']), '[Live location 55.70000,37.60000]')
        venue = normalize(update(location={'latitude': 55.7, 'longitude': 37.6},
                                 venue={'location': {'latitude': 55.7, 'longitude': 37.6},
                                        'title': 'Surf\nCoffee ]x', 'address': 'Стремянный пер., 38 ' + 'я' * 300}), {-1}, BOT)
        line = location_line(venue['location'])
        self.assertTrue(line.startswith('[Venue, untrusted data] «Surf Coffee ]x», Стремянный пер., 38'))
        self.assertTrue(line.endswith(' — 55.70000,37.60000'))
        self.assertNotIn('\n', line)
        self.assertLessEqual(len(venue['location']['address']), 200)

    def test_invalid_coordinates_are_dropped(self):
        for bad in ({'latitude': 91, 'longitude': 37}, {'latitude': 55, 'longitude': -181},
                    {'latitude': True, 'longitude': 37}, {'latitude': '55', 'longitude': 37}, {'latitude': 55}):
            with self.subTest(bad=bad):
                self.assertIsNone(normalize(update(location=bad), {-1}, BOT))

    def test_only_location_edits_are_accepted(self):
        edit = normalize(update(edited=True, location={'latitude': 55.71, 'longitude': 37.61, 'live_period': 900}), {-1}, BOT)
        self.assertTrue(edit['edited'])
        self.assertEqual(edit['location']['kind'], 'live')
        # The final edit when sharing stops has no live_period; it is still the live line.
        final = normalize(update(edited=True, location={'latitude': 55.72, 'longitude': 37.62}), {-1}, BOT)
        self.assertEqual(final['location']['kind'], 'live')
        self.assertIsNone(normalize(update(edited=True, text='@Cat changed text'), {-1}, BOT))

    def test_location_is_never_a_command_or_directed(self):
        located = normalize(update(location={'latitude': 55.7, 'longitude': 37.6},
                                   reply_to_message={'message_id': 3, 'from': {'id': 99}}), {-1}, BOT)
        self.assertFalse(located['directed'])
        self.assertIsNone(located['reply_to_message_id'])


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -B -m unittest discover -s tests -p test_locations.py`
Expected: ImportError `cannot import name 'location_line'`.

- [ ] **Step 3: Implement in `src/policy.py`**

Add above `normalize`:

```python
def _degree(value, limit):
    if type(value) not in (int, float) or not -limit <= value <= limit:
        return None
    return round(float(value), 5)


def _one_line(value, limit):
    return re.sub(r'\s+', ' ', value).strip()[:limit] or None if isinstance(value, str) else None


def location_of(message, edited=False):
    """A shared point, live location or venue; None when absent or invalid."""
    venue = message.get('venue') if isinstance(message.get('venue'), dict) else None
    point = (venue or {}).get('location') if venue else message.get('location')
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
```

Change the start of `normalize`:

```python
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
```

In the final `return dict(...)` of `normalize`, add `location=None, edited=False,`.

- [ ] **Step 4: Wire edits into polling in `src/bot.py`**

Change `allowed_updates=['message', 'callback_query']` to `allowed_updates=['message', 'edited_message', 'callback_query']`.
`accept_update` already calls `normalize` for anything that is not a callback; no other change.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -B -m unittest discover -s tests -p test_locations.py` → OK.
Run the full suite: `.venv/bin/python -B -m unittest discover -s tests` → OK (payload dicts in other tests gain
two keys; nothing compares whole payloads — if one does, add `location=None, edited=False` to its expectation).

- [ ] **Step 6: Commit**

```bash
git add src/policy.py src/bot.py tests/test_locations.py
git commit -m "feat: parse Telegram locations, live updates and venues

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Store locations silently and follow live updates

**Files:**
- Modify: `src/service.py` (`accept`, `prepare`, new `save_location`)
- Modify: `src/storage.py` (new `replace_message`)
- Modify: `src/observability.py` (`OUTCOMES` += `location_saved`)
- Modify: `tests/test_service.py` (`MemoryStore.append` keeps `message_id`/`sender_id`; add `replace_message`)
- Test: `tests/test_locations.py`, `tests/test_postgres.py`

**Interfaces:**
- Consumes: payload `location`, `edited` and `policy.location_line` from Task 1.
- Produces: `Store.replace_message(chat_id, telegram_message_id, content) -> bool` (True when a row changed);
  job outcome `location_saved`.

- [ ] **Step 1: Extend the in-memory store used by service tests**

In `tests/test_service.py`, `MemoryStore`:

```python
    def append(self, chat, role, content, **metadata):
        self.rows.append(dict(id=self.next_id, role=role, content=content,
                              message_id=metadata.get('message_id'), sender_id=metadata.get('sender_id')))
        self.next_id += 1
    def replace_message(self, chat, message_id, content):
        for row in self.rows:
            if row.get('message_id') == message_id and row['role'] == 'user':
                row['content'] = content
                return True
        return False
```

Then run `.venv/bin/python -B -m unittest discover -s tests` and fix any test that compared whole `rows` dicts
by comparing only `role`/`content` (expected: none, since tests read `r['role']`/`r['content']`).

- [ ] **Step 2: Write the failing service tests** (append to `tests/test_locations.py`)

```python
from config import Config
from service import Service
from test_service import FakeModel, FakeTelegram, MemoryStore


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tg, self.model = MemoryStore(), FakeTelegram(), FakeModel()
        self.service = Service(Config('fake-telegram', 'fake-openai', frozenset({-1})), self.store, self.tg, self.model)

    def send(self, update_id, **message):
        edited = message.pop('edited', False)
        self.service.accept(update_id, normalize({**update(edited, **message), 'update_id': update_id}, {-1}, BOT))
        while self.service.process_one() is not None:
            pass

    def test_location_is_stored_silently_in_every_mode(self):
        for mode in ('directed', 'all'):
            with self.subTest(mode=mode):
                self.store.rows.clear()
                self.store.chat['mode'] = mode
                self.send(1, location={'latitude': 55.7312, 'longitude': 37.6363})
                self.assertEqual([r['content'] for r in self.store.rows], ['Speaker 10: [Location 55.73120,37.63630]'])
                self.assertEqual((self.tg.sent, self.model.calls), ([], []))

    def test_paused_chat_ignores_locations(self):
        self.store.chat['paused'] = True
        self.send(1, location={'latitude': 55.7, 'longitude': 37.6})
        self.assertEqual(self.store.rows, [])

    def test_live_updates_rewrite_their_own_line(self):
        self.send(1, location={'latitude': 55.70, 'longitude': 37.60, 'live_period': 900})
        self.send(2, edited=True, location={'latitude': 55.71, 'longitude': 37.61, 'live_period': 900})
        self.send(3, edited=True, location={'latitude': 55.72, 'longitude': 37.62})
        self.assertEqual([r['content'] for r in self.store.rows], ['Speaker 10: [Live location 55.72000,37.62000]'])
        self.assertEqual(self.tg.sent, [])

    def test_edit_without_a_stored_line_is_ignored(self):
        self.send(1, edited=True, location={'latitude': 55.71, 'longitude': 37.61, 'live_period': 900})
        self.assertEqual(self.store.rows, [])

    def test_location_reply_to_a_bot_prompt_is_only_stored(self):
        self.store.save_prompt(-1, 3, 10, '/chat')
        self.send(1, location={'latitude': 55.7, 'longitude': 37.6},
                  reply_to_message={'message_id': 3, 'from': {'id': 99}})
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual((self.tg.sent, self.model.calls), ([], []))
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -B -m unittest discover -s tests -p test_locations.py`
Expected: FAIL — in directed mode no row is stored; in all mode the model is called.

- [ ] **Step 4: Implement in `src/service.py`**

At the top add `from policy import location_line` (extend the existing `policy` import if there is one).

In `accept`, locations must skip prompt handling and the directed filter, so the check goes before
`apply_prompt`. Replace the first two lines of the `if payload:` block with:

```python
        if payload:
            state = self.store.state(payload['chat_id'])
            if payload.get('location'):
                # Stored for later requests in every mode; never a reply and never a prompt answer.
                self.store.enqueue(update_id, None if state['paused'] else payload)
                return
            payload = self.apply_prompt(dict(payload))
```

In `prepare`, before the `paid = ...` line:

```python
        if payload.get('location'):
            self.save_location(job)
            return
```

New method next to `prepare`:

```python
    def save_location(self, job):
        payload = job['payload']
        if self.store.state(payload['chat_id'])['paused']:
            self.finish(job, 'paused')
            return
        content = f"Speaker {payload.get('user_id') or 'anonymous'}: {location_line(payload['location'])}"
        with self.store.transaction():
            if payload.get('edited'):
                # Live movement updates its own line; an unknown live location is not added.
                self.store.replace_message(payload['chat_id'], payload['message_id'], content)
            else:
                self.store.append(payload['chat_id'], 'user', content, sender_id=payload.get('user_id'),
                                  message_id=payload['message_id'])
            self.finish(job, 'location_saved')
```

In `src/observability.py` add `'location_saved'` to `OUTCOMES`.

- [ ] **Step 5: Implement `Store.replace_message` in `src/storage.py`** (after `append`)

```python
    def replace_message(self, chat_id, message_id, content):
        """Rewrite a stored person's message by its Telegram id (live-location movement)."""
        return self.conn.execute('''UPDATE messages SET content=%s WHERE chat_id=%s AND thread_id=%s
            AND telegram_message_id=%s AND role='user' ''', (content, chat_id, self.thread_id, message_id)).rowcount > 0
```

Add to `tests/test_postgres.py` (inside `PostgresTests`, skipped without the isolated runner):

```python
    def test_replace_message_rewrites_only_the_matching_live_location(self):
        self.store.state(-1)
        self.store.append(-1, 'user', 'Speaker 10: [Live location 55.70000,37.60000]', sender_id=10, message_id=7)
        self.store.append(-1, 'user', 'Speaker 11: hi', sender_id=11, message_id=8)
        self.assertTrue(self.store.replace_message(-1, 7, 'Speaker 10: [Live location 55.71000,37.61000]'))
        self.assertFalse(self.store.replace_message(-1, 99, 'nothing'))
        self.assertEqual([r['content'] for r in self.store.messages(-1)],
                         ['Speaker 10: [Live location 55.71000,37.61000]', 'Speaker 11: hi'])
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/python -B -m unittest discover -s tests` → OK (Postgres tests skipped).

- [ ] **Step 7: Commit**

```bash
git add src/service.py src/storage.py src/observability.py tests/test_service.py tests/test_locations.py tests/test_postgres.py
git commit -m "feat: remember shared locations silently and follow live updates

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Maps search around a point (`near`) with distances

**Files:**
- Modify: `src/yandex_maps.py` (`READ_YANDEX_MAPS_SEARCH`, new `near_point`, `search_url`, `normalized_search`, `distance_m`)
- Modify: `tools/maps_search.py` (`read_search`), `tools/server.py` (`maps_search`, `_request`)
- Modify: `src/tool_client.py` (`read_maps_search`)
- Modify: `src/model.py` (`READER_KEYS`, `READER_HINTS`, `_reader_call`, `_read_tool`)
- Test: `tests/test_maps_search.py`

**Interfaces:**
- Produces: `yandex_maps.near_point(value) -> tuple[float, float] | None` (None for null; ValueError if bad);
  `yandex_maps.search_url(query, near=None)`; `yandex_maps.normalized_search(url, items, exclude_chains=False, near=None)`;
  `maps_search.read_search(query, exclude_chains, near=None)`;
  `tool_client.read_maps_search(query, exclude_chains, base_url, near=None)`;
  tool schema property `near`; result items get `distance_m` (int) only when `near` is set.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_maps_search.py`)

```python
class NearTests(unittest.TestCase):
    POINT = (55.7266, 37.6248)

    def spread(self):
        """15 places, the nearest ones last in Yandex order: sorting must happen before the cap."""
        items = []
        for index in range(15):
            km = 3 - index * 0.2  # 3.0 km ... 0.2 km
            items.append(business(1000 + index, f'Cafe {index}', address=f'ул. {index}',
                                  coordinates=[37.6248, 55.7266 + km / 111]))
        items.append(business(2000, 'Nowhere', address='x'))  # no coordinates
        return items

    def test_near_point_validation(self):
        self.assertEqual(maps.near_point('55.7266,37.6248'), (55.7266, 37.6248))
        self.assertEqual(maps.near_point(' 55.7266, 37.6248 '), (55.7266, 37.6248))
        self.assertIsNone(maps.near_point(None))
        for bad in ('55.7266', '91,37', '55,-181', 'Moscow', '55.7;37.6', 7):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                maps.near_point(bad)

    def test_search_url_with_and_without_area(self):
        self.assertEqual(maps.search_url('кофейня'), 'https://yandex.ru/maps/213/moscow/search/%D0%BA%D0%BE%D1%84%D0%B5%D0%B9%D0%BD%D1%8F/')
        url = maps.search_url('кофейня', near=self.POINT)
        self.assertTrue(url.endswith('/?ll=37.6248%2C55.7266&z=15&spn=0.0320%2C0.0180&rspn=1'), url)
        north = maps.search_url('кофейня', near=(64.5, 40.5))  # wider in longitude further north
        self.assertIn('spn=0.0419%2C0.0180', north)

    def test_results_sorted_by_distance_before_the_cap(self):
        result = maps.normalized_search('u', self.spread(), near=self.POINT)
        distances = [r['distance_m'] for r in result['results']]
        self.assertEqual(len(distances), 10)
        self.assertEqual(distances, sorted(distances))
        self.assertEqual(distances[0], 200)
        self.assertIn('straight-line', result['notice'])
        plain = maps.normalized_search('u', self.spread())
        self.assertNotIn('distance_m', plain['results'][0])
        self.assertEqual(plain['results'][0]['name'], 'Cafe 0')

    def test_server_and_client_pass_near(self):
        with patch('server.read_search', return_value=maps.normalized_search('u', [])) as read:
            body = {'query': 'кофейня', 'exclude_chains': False, 'near': '55.7266,37.6248'}
            self.assertEqual(tools_server.maps_search(json.dumps(body))[0], 200)
            read.assert_called_once_with('кофейня', False, (55.7266, 37.6248))
            read.reset_mock()
            self.assertEqual(tools_server.maps_search(json.dumps({'query': 'кофейня', 'exclude_chains': False}))[0], 200)
            read.assert_called_once_with('кофейня', False, None)
            self.assertEqual(tools_server.maps_search(json.dumps({**body, 'near': '91,37'}))[0], 400)
        url = maps.search_url('кофейня', near=self.POINT)
        good = maps.normalized_search(url, self.spread(), near=self.POINT)
        real = httpx.Client
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=good)
        with patch('tool_client.httpx.Client', lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            result = tool_client.read_maps_search('кофейня', False, 'http://tools:8080', near=self.POINT)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(seen[0]['near'], '55.7266,37.6248')

    def test_model_passes_near_and_explains_a_bad_point(self):
        model = ModelLoopTests.model(self)
        good = ToolCall('a', 'read_yandex_maps_search',
                        '{"query":"кофейня Тверская","exclude_chains":false,"near":"55.7266, 37.6248"}')
        bad = ToolCall('b', 'read_yandex_maps_search', '{"query":"кофейня","exclude_chains":false,"near":"рядом"}')
        legacy = ToolCall('c', 'read_yandex_maps_search', '{"query":"кофейня Арбат","exclude_chains":false}')
        with patch.object(model, '_generation_attempt', side_effect=[
                Generation('', True, tool_calls=(good, bad, legacy)), Generation('Готово', True)]) as generate, \
                patch('model.read_maps_search', return_value=maps.normalized_search('u', self.spread(), near=self.POINT)) as read:
            model.generate('Persona', [], allow_search=True)
        self.assertEqual(read.call_args_list[0].args, ('кофейня Тверская', False, model.config.tools_url))
        self.assertEqual(read.call_args_list[0].kwargs, {'near': (55.7266, 37.6248)})
        self.assertEqual(read.call_args_list[1].kwargs, {'near': None})
        contents = [json.loads(i['content']) for i in generate.call_args.kwargs['continuation'] if i.get('role') == 'tool']
        self.assertEqual(contents[1]['reason'], 'invalid_arguments')
        self.assertIn('near', contents[1]['notice'])
        self.assertIn('near', json.dumps(maps.READ_YANDEX_MAPS_SEARCH['parameters']['properties']))
```

Note: `test_model_passes_near_and_explains_a_bad_point` reuses `ModelLoopTests.model`; it is an instance method
that calls `self.addCleanup` and `self.fail`, so passing a `NearTests` instance works.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -B -m unittest discover -s tests -p test_maps_search.py`
Expected: errors — `module 'yandex_maps' has no attribute 'near_point'`.

- [ ] **Step 3: Implement in `src/yandex_maps.py`**

Schema — add the property and make it required (strict mode needs every key; null allowed):

```python
    'parameters': {'type': 'object', 'properties': {
        'query': {'type': 'string', 'description': 'Category plus street, district or metro, in Russian; '
                  'only the category ("кофейня") when near is set'},
        'exclude_chains': {'type': 'boolean'},
        'near': {'type': ['string', 'null'], 'description':
                 '"lat,lon" of a location someone shared in this chat (copy it from a [Location …], '
                 '[Live location …] or [Venue …] line) when they ask for places near it; results are then '
                 'limited to about 2 km around it and sorted by distance_m. null when the person names a '
                 'street, metro or landmark, or shared no location.'}},
        'required': ['query', 'exclude_chains', 'near'], 'additionalProperties': False},
```

Append to the description string: `' With near, each result has distance_m: straight-line meters from that point, not walking time (use route_link for that).'`

Functions (replace `search_url`, add the rest above `normalized_search`):

```python
AREA_KM = 2  # Search box side around a shared point; tested 2026-10-18.


def near_point(value):
    """(lat, lon) from "lat,lon"; None for null. ValueError for anything else."""
    if value is None:
        return None
    match = re.fullmatch(r'\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*', value) if isinstance(value, str) else None
    if not match:
        raise ValueError('near must be "lat,lon"')
    lat, lon = float(match.group(1)), float(match.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError('near is out of range')
    return round(lat, 5), round(lon, 5)


def search_url(value, near=None):
    url = SEARCH_URL.format(quote(search_query(value), safe=''))
    if near is None:
        return url
    lat, lon = near
    dlat = AREA_KM / 111
    dlon = AREA_KM / (111 * max(math.cos(math.radians(lat)), 0.01))
    return url + f'?ll={lon:.4f}%2C{lat:.4f}&z=15&spn={dlon:.4f}%2C{dlat:.4f}&rspn=1'


def distance_m(near, coordinates):
    """Haversine meters from near (lat, lon) to Yandex [lon, lat], rounded to 10 m; None without coordinates."""
    if not isinstance(coordinates, list) or len(coordinates) != 2:
        return None
    lon, lat = _number(coordinates[0], -180, 180), _number(coordinates[1], -90, 90)
    if lat is None or lon is None:
        return None
    p1, p2 = math.radians(near[0]), math.radians(lat)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon - near[1]) / 2) ** 2)
    return int(round(2 * 6371000 * math.asin(math.sqrt(a)), -1))
```

The test values are computed: `2/111` → `0.0180`; `2/(111·cos 55.7266°)` → `0.0320`; at 64.5° → `0.0419`.

`normalized_search(url, items, exclude_chains=False, near=None)`: after building `businesses`, sort when `near`:

```python
    if near is not None:
        # The page holds ~24 places; sort them all so the nearest survive the 10-result cap.
        businesses.sort(key=lambda item: (distance_m(near, item.get('coordinates')) is None,
                                          distance_m(near, item.get('coordinates')) or 0))
```

In the `results.append({...})` dict, after `'coordinates': ...`, add nothing; after the append:

```python
        if near is not None:
            results[-1]['distance_m'] = distance_m(near, item.get('coordinates'))
```

In the returned dict: `'sampling': 'nearest_first' if near is not None else 'search_order',` and
`'notice': NOTICE + (NEAR_NOTICE if near is not None else ''),` with

```python
NEAR_NOTICE = (' Results are limited to about 2 km around the shared point and sorted by distance_m, the '
               'straight-line distance in meters; walking time and the real route come only from route_link. '
               'If even the first result is far, say so instead of calling it nearby.')
```

- [ ] **Step 4: Tools service** — `tools/maps_search.py`:

```python
def read_search(query, exclude_chains, near=None):
    url = search_url(query, near)
    ...
    return normalized_search(url, items, exclude_chains, near)
```

`tools/server.py`: let `_request` accept optional keys, then use it in `maps_search`:

```python
def _request(body, keys, optional=frozenset()):
    request = json.loads(body)
    if not isinstance(request, dict) or not keys <= set(request) <= keys | optional:
        raise ValueError
    return request


def maps_search(body):
    try:
        request = _request(body, {'query', 'exclude_chains'}, {'near'})
        if type(request['exclude_chains']) is not bool:
            raise ValueError
        near = yandex_maps.near_point(request.get('near'))
        url = yandex_maps.search_url(request['query'], near)
    except (ValueError, TypeError):
        return 400, yandex_maps.unavailable(None, 'invalid_arguments')
    return _browse('maps_search_read', request['query'],
                   lambda query: read_search(query, request['exclude_chains'], near),
                   lambda _query, reason: yandex_maps.unavailable(url, reason))
```

Update `ServiceAndClientTests.test_route_validates_arguments`: `read.assert_called_once_with('кофейня', True, None)`.

- [ ] **Step 5: Tool client** — `src/tool_client.py`:

```python
def read_maps_search(query, exclude_chains, base_url, near=None):
    url = yandex_maps.search_url(query, near)
    body = {'query': query, 'exclude_chains': exclude_chains}
    if near is not None:
        body['near'] = f'{near[0]},{near[1]}'
    return _post(base_url, '/v1/yandex-maps-search', body, url, MAPS_KEYS, 'results', yandex_maps.unavailable)
```

- [ ] **Step 6: Model** — `src/model.py`:

`READER_KEYS['read_yandex_maps_search'] = {'query', 'exclude_chains', 'near'}`. Z.ai may omit `near`, so in
`_reader_call` and `_tool_batch_error` treat it as optional: at the top of `_reader_call` add

```python
        if name == 'read_yandex_maps_search' and isinstance(arguments, dict):
            arguments = {'near': None, **arguments}  # Optional for providers without strict schemas.
```

(insert it as the first statement after `name = call.name`, before the `READER_KEYS` check)

and in `_tool_batch_error` replace `elif set(arguments) != READER_KEYS[call.name]:` with
`elif set(arguments) | ({'near'} if call.name == 'read_yandex_maps_search' else set()) != READER_KEYS[call.name]:`.

In the `read_yandex_maps_search` branch of `_reader_call`:

```python
                near = yandex_maps.near_point(arguments['near'])
                target = (yandex_maps.search_url(arguments['query'], near), arguments['exclude_chains'])
```

In `_read_tool`: `call = lambda: read_maps_search(arguments['query'], arguments['exclude_chains'], self.config.tools_url, near=yandex_maps.near_point(arguments.get('near')))`.
Because `_reader_call` passes its normalised `arguments` to `_read_tool`, `near` is always present there.

`READER_HINTS['read_yandex_maps_search']` append: `' near is "lat,lon" copied from a shared location line, or null.'`

Existing tests that call the reader with two keys keep passing (near defaults to null); the assertion
`read.assert_called_once_with('кофейня Никольская', True, model.config.tools_url)` must become
`read.assert_called_once_with('кофейня Никольская', True, model.config.tools_url, near=None)`.

- [ ] **Step 7: Run tests**

Run: `.venv/bin/python -B -m unittest discover -s tests` → OK.

- [ ] **Step 8: Commit**

```bash
git add src/yandex_maps.py tools/maps_search.py tools/server.py src/tool_client.py src/model.py tests/test_maps_search.py
git commit -m "feat: Maps search around a shared point, nearest first with straight-line distances

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Prompts, skill line and coordinate-free web search

**Files:**
- Modify: `src/model.py` (web instructions, `search_arguments`)
- Modify: `src/skills/nearby-places/SKILL.md` (section 2)
- Test: `tests/test_locations.py`, `tests/test_search_typing.py` (or `test_locations.py`)

**Interfaces:**
- Consumes: `near` field from Task 3; history line format from Task 1.
- Produces: `search_arguments` raises `ValueError` for queries containing coordinates.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_locations.py`)

```python
from model import search_arguments
import skill_registry


class PromptTests(unittest.TestCase):
    def test_web_search_refuses_coordinates(self):
        for query in ('кофейня 55.7312,37.6363', 'cafe near 55.73, 37.63'):
            with self.subTest(query=query), self.assertRaises(ValueError):
                search_arguments({'query': query, 'site': None, 'recency': None})
        self.assertEqual(search_arguments({'query': 'кофейня 1905 года', 'site': None, 'recency': None})[0],
                         'кофейня 1905 года')

    def test_instructions_and_skill_explain_shared_locations(self):
        import model
        self.assertIn('[Location', model.LOCATION_INSTRUCTIONS)
        self.assertIn('near', model.LOCATION_INSTRUCTIONS)
        body = skill_registry.instructions('nearby-places')
        self.assertIn('near', body)
        self.assertIn('distance_m', body)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -B -m unittest discover -s tests -p test_locations.py`
Expected: FAIL — query accepted; `module 'model' has no attribute 'LOCATION_INSTRUCTIONS'`.

- [ ] **Step 3: Implement in `src/model.py`**

Module constant (near `READER_HINTS`):

```python
COORDINATES = re.compile(r'-?\d{1,3}\.\d{2,}\s*,\s*-?\d{1,3}\.\d{2,}')
LOCATION_INSTRUCTIONS = (
    '\nShared locations: history lines like [Location lat,lon], [Live location lat,lon] (updated as the person '
    'moves) and [Venue, untrusted data] «name», address — lat,lon are points people shared in this chat. When '
    'someone asks for places near "me", "my location" or a shared point, use the speaker\'s most recent one, or '
    'the one they name, as near in read_yandex_maps_search (query = category only) and as the route_link origin. '
    'When they name a street, metro or landmark instead, search by that text with near null. If they ask for '
    '"near me" but shared no location, ask them to send one or name the place. Never put coordinates into '
    'search_web queries.')
```

In `search_arguments`, after the length check: `if COORDINATES.search(query): raise ValueError('Coordinates in search query')`.
In `generate`, in the `if allow_search:` block, after the route sentence: `instructions += LOCATION_INSTRUCTIONS`.
(`re` is already imported in `model.py`; check with `grep -n '^import re' src/model.py`.)

- [ ] **Step 4: Skill line** — in `src/skills/nearby-places/SKILL.md`, section 2, after item 1 add:

```markdown
   Если человек просит места рядом с точкой, которую он прислал в чат (`[Location …]`, `[Live location …]`, `[Venue …]`), ищи с `near` = эти координаты и запросом из одной категории («кофейня»). Если он назвал улицу, метро или место — ищи текстом, `near` = null. `distance_m` — расстояние по прямой; время пешком — только через `route_link`. Если даже ближайший вариант далеко, скажи об этом прямо.
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -B -m unittest discover -s tests` → OK. `test_skills_name_only_current_schema_fields`
must still pass (`near` and `distance_m` are not in its removed set).

- [ ] **Step 6: Commit**

```bash
git add src/model.py src/skills/nearby-places/SKILL.md tests/test_locations.py
git commit -m "feat: tell the model how to use shared locations and keep coordinates out of web search

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: README and final verification

**Files:**
- Modify: `README.md` (new "Locations" paragraph near the Maps search / skills section; tools table row for
  `read_yandex_maps_search` mentions `near`)

- [ ] **Step 1: Write the README paragraph**

Find the Maps search paragraph: `grep -n "read_yandex_maps_search" README.md`. After it, add:

```markdown
**Locations.** People can send Telegram's location: a pin, "send my location", a live location or a venue from Telegram's place list. The bot never replies to it; in every chat mode (not while paused) it stores one history line such as `[Location 55.73120,37.63630]`, `[Live location …]` or `[Venue, untrusted data] «Name», address — …`. A live location updates that same line as the person moves (`edited_message` updates) and never adds new lines; one started before the bot ran is ignored. When someone later asks for places near their point, the model passes it as `near` to `read_yandex_maps_search`: the search is limited to about 2×2 km around it, all returned places are sorted by straight-line `distance_m` before the first 10 are kept, and the result says the distances are not walking time (that comes from `route_link`). A check on 2026-10-18 found this as local as searching by metro name (Серпуховская: 9 of 10 within 500 m, versus 1 of 10 within 1 km with the point alone), and it works outside Moscow. Named streets or stations are still searched as text. Coordinates are refused in `search_web` queries. Bare pins have no street name: that would need a geocoding API. With Telegram privacy mode on, a group bot never receives location messages, so this needs privacy mode off or the bot as a group admin.
```

- [ ] **Step 2: Final verification**

- Run `.venv/bin/python -B -m unittest discover -s tests` → `OK`, no failures (skips are the isolated-Postgres tests).
- `grep -n "edited_message" src/bot.py` → the `allowed_updates` line.
- `git diff --stat main..HEAD -- tools/` → only `maps_search.py` and `server.py`.
- Privacy mode: with Telegram privacy mode on, a group bot does not receive plain location messages at all.
  Check that the README says locations need privacy mode off (or the bot as admin); the service already knows
  `privacy_enabled`.
- Do not run the bot or `scripts/test.sh` against live services. Rebuilding (`docker compose up -d --build`)
  is the user's decision.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: describe shared locations and near search

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
