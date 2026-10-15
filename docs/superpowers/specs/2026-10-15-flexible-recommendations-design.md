# Flexible recommendation cards

## Goal

Recommendation cards (`place_recommendations`) are too rigid: every item must fill eleven fields, and the bot
renders each with a fixed emoji row (📅 💳 📍 💡 ⭐ ⚠️ ↔ 🔗). That makes answers bulky, pushes the model to
invent filler for rows it has no evidence for, and leaves the persona's voice only in `intro`/`follow_up`.

The bot should enforce only the facts that make a card trustworthy (name, address, rating, timing, grounded links) and let the model write the rest of each card freely, in the persona's voice.

## Decisions

- The bot keeps rendering cards (option 3 from brainstorming), but most of each card is model-written text.
- A card must be verifiable: no address or no grounded link means it is not a card.
- Places the model could not confirm go to a separate, clearly labelled "unconfirmed" block. The user can ask
  in plain text to check one ("проверь второй"); no new buttons. The bot is a text assistant.
- Existing follow-up buttons (ещё / ближе / дешевле) stay.
- Length behaviour is out of scope: item counts in skills ("2–4 варианта"), `MAX_ITEMS = 5` and the 3500-char
  message limit stay as they are.

## Schema

```
intro        string, required, nonempty, ≤300           persona voice, conversation language
items[]      0–5 (MAX_ITEMS)
  name       string, required, nonempty, ≤120
  address    string | null, ≤200                        from a tool result
  rating     string | null, ≤100                        e.g. "4,8 (312 оценок)" / "Афиша 8,9 (1526)"
  when       string | null, ≤200                        hours for the needed day, or event dates/sessions
  text       string, required, nonempty, ≤600           free Markdown: why it fits, price, caveats,
                                                         near-miss note, own emoji — only what evidence supports
  links      string[], ≤3, each ≤400
unconfirmed[] 0–3
  name       string, required, nonempty, ≤120
  note       string | null, ≤200                        why it is interesting / what could not be checked
outro        string | null, ≤300                        persona voice; replaces follow_up and evidence_gaps
```

Removed: `kind` (and `KINDS`, `ICONS`), `venue`, `price`, `why`, `reviews`, `caveats`, `relaxed`,
`follow_up`, `evidence_gaps`. Metro/distance goes into `address` or `text`; price into `text`.

All keys are required (OpenAI strict mode needs that); nullable fields use `null`. String lengths are
validated locally as today (`strict_schema` strips them for OpenAI). For JSON-mode providers (Z.ai),
`validate` defaults a missing `unconfirmed` to `[]` and a missing `outro` to `null`, the same way `relaxed`
is defaulted today. Other missing or extra keys are errors.

## Validation and repair

`validate` keeps its fixed error categories (`not_object`, `missing_fields`, `extra_fields`, `wrong_types`,
`too_long`, `reply_required`, ...). `CORRECTIONS` texts are rewritten for the new field names. The repair
loop in `model.py` is unchanged.

## Rendering (`recommendations.render`)

1. Remove items and unconfirmed entries whose name matches an excluded chain (same rule as today).
2. Ground each item's links against `evidence` (as today; unknown links are dropped and counted).
3. Demote: an item with an empty/null `address` or no grounded link moves to the unconfirmed list
   (its `text` is dropped, no note). Demoted entries go after the model's own unconfirmed entries; the
   combined list is capped at 3.
4. Card layout, no fixed emoji:

```
1. **Name**
Адрес: …            (always present: a card without an address is demoted)
Рейтинг: …          (omitted if null)
Когда: …            (omitted if null)
<model text, line breaks kept>
<grounded links, space-separated>
```

   `name`, `address`, `rating`, `when` and `note` stay one-line (`clean`). `text` keeps line breaks
   (collapsed to at most single blank lines). To stop text faking structure, any `text` line that would
   match a card heading (`N. **`) or starts with a fact label (`Адрес:`, `Рейтинг:`, `Когда:`) or the
   unconfirmed heading has that prefix neutralised (numbering removed / label prefixed with `– `).
5. Unconfirmed block, after the cards:

```
Не удалось подтвердить:
- Name — note
```

   No links, no address. Omitted when empty.
6. Message: `intro`, cards, unconfirmed block, `outro`, joined by blank lines; trailing cards are dropped
   until it fits `max_chars` (as today).
7. `ModelText.cards` is true when at least one card is rendered (buttons appear only then).

The return value grows a demoted count: `render` returns `(text, dropped_links, dropped_items, demoted_items)`.
`recommendations_rendered` logs `result_count` (cards shown), `dropped_links`, `dropped_items`,
`demoted_items` and `unconfirmed_count`; the new names are added to the observability allowlist.

Fact labels are Russian, matching the bot's Russian interface.

## Memory shortening (`policy.memory_view` / `shorten_reply`)

Fixed rules, no AI request.

- The three newest bot replies stay whole (`KEEP_FULL_REPLIES` 2 → 3).
- Older long card replies (new or old stored format, detected by `\d+\. (\S+ )?\*\*` headings) become a
  brief holding only the place names: `[Earlier recommendations: Name1, Name2, …]` (card names, then
  unconfirmed names). Intro, facts, links and text are dropped; the surrounding conversation already says
  where and what was asked.
- Older long plain replies keep today's rule (opening plus lines with links).
- `SHORTENED_NOTE` is reworded: details such as addresses, ratings and links were left out; if asked, look the
  place up again with tools rather than reconstruct it. `PROSE_LINES` is removed.
- The `SHORTEN_OVER` (800) threshold and the "saves more than 300 chars" rule stay.

## Prompts

- `recommendations.INSTRUCTIONS`: describe the new fields; the model writes `text` freely in the persona's
  voice and may use its own formatting and emoji; never invent addresses, ratings, hours or prices; a place
  without a tool-confirmed address and link belongs in `unconfirmed`; use `null` rather than guessing.
- All seven `SKILL.md` "Ответ" sections are rewritten to the new fields. Each keeps its item count and its
  domain hints (what to mention in `text`: e.g. ages and duration for kids, booking and timing for evening
  plans, "запасной" for the backup option, who loses out in group compromises). References to `relaxed`,
  `caveats`, `evidence_gaps`, `follow_up`, `kind` are removed; near-miss handling becomes "say in `text` what
  is relaxed". The rest of each skill (search playbooks, verification tables) is untouched, except mentions
  of removed field names (e.g. "назови допущения в `intro`" stays; "`relaxed`" becomes "в `text`").

## Docs

README "Recommendation cards", "Follow-ups" and "Shortened older replies" paragraphs and the skills list
(near-miss cards) are updated to the new shape.

## Testing

Update fixtures in `tests/test_recommendations.py`, `tests/test_skill_loading.py`, `tests/test_maps_search.py`
and any other test building `place_recommendations` JSON. New/changed tests:

- schema: minimal valid object (one item, nulls); missing/extra keys; JSON-mode defaults for `unconfirmed`
  and `outro`; length limits; empty `text` or `name` rejected.
- render: no fixed emoji in output; labelled fact lines; null facts omitted; multi-line `text` preserved;
  structure-faking lines in `text` neutralised; item without address demoted; item whose links are all
  ungrounded demoted; unconfirmed block rendering and cap of 3; excluded chains removed from both lists;
  `cards` false when everything was demoted; trailing-card truncation.
- memory: three newest replies kept whole; older new-format and old-format card replies become a
  name-only list; plain replies unchanged.
- skills: every `SKILL.md` mentions only fields that exist in the schema.

All tests use synthetic data and mocked network calls; no bot run, no live requests.
