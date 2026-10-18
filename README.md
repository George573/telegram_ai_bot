# Telegram roleplay bot

A group bot: Python, configurable AI provider adapters, and PostgreSQL in two Docker Compose containers. PostgreSQL uses a named volume. Supports amd64 PCs and arm64 Raspberry Pi 4 with a 64-bit OS.

## First run

1. Install Docker Engine and the Compose plugin.
2. Edit your private `.env`. Required variables:

```dotenv
TELEGRAM_BOT_TOKEN=your-telegram-token
OPENAI_API_KEY=your-separately-billed-api-key
OPENAI_MODEL=gpt-6-luna
ALLOWED_CHAT_IDS=-882012764
POSTGRES_PASSWORD=a-strong-database-admin-password
BOT_DB_PASSWORD=a-different-strong-application-password
HISTORY_MAX_MESSAGES=50
SUMMARY_MAX_CHARS=4000
BOT_BUDGET_USD=10
REPLY_COOLDOWN_SECONDS=10
```

Replace all placeholder passwords. Use a password manager to generate two different passwords. PostgreSQL initializes passwords only on first volume creation; changing `.env` alone does not rotate an existing database password. The bot receives only its application database password, not the administrator password. The app database role owns its database but cannot administer PostgreSQL or create other roles.

3. In BotFather, use `/setprivacy` to disable privacy for this bot if you want both context modes and ordinary mentions. Remove and re-add it if Telegram requires that for the privacy change. Do not grant the bot group admin privileges. The bot receives group text from Telegram and discards unrelated text in directed mode before storing it or calling the selected AI provider. General group text is not delivered reliably with privacy enabled.
4. Start deliberately when ready for separately billed OpenAI requests:

```sh
docker compose up -d --build
```

5. In the group, use `/help`, then `/setprompt You are our grumpy family cat. Reply briefly and make playful cat jokes.` as an identifiable group administrator.
6. Talk with `/chat hello`, mention the bot, or reply to its message. With multiple bots, target `/help@YourBotUsername` and `/chat@YourBotUsername hello`.

No ports are published. Only the bot has an outbound network. The database has an internal network shared with the bot. The bot runs as a non-root user with a read-only root filesystem. Linux Docker must itself start on boot for automatic bot restart.

## AI providers

The bot uses one internal `Model` interface for replies, summaries, reply decisions and personality inference. Native API adapters in `src/providers.py` translate requests and normalize responses. OpenAI remains the default; existing configuration keeps working.

To select Z.ai, edit your private configuration locally:

```dotenv
MODEL_PROVIDER=zai
ZAI_API_KEY=your-zai-api-key
ZAI_MODEL=glm-4.7
ZAI_VISION_MODEL=glm-4.6v
```

Only the selected provider's key is required. Z.ai uses the standard API endpoint `https://api.z.ai/api/paas/v4/`, not the Coding Plan endpoint. The paid text default is `glm-4.7` ($0.60 input / $2.20 output per million tokens). Set `ZAI_MODEL=glm-5` ($1.00 input / $3.20 output) or `ZAI_MODEL=glm-5.1` ($1.40 input / $4.40 output) to select either newer model. `ZAI_MODEL=glm-5.3-flash` is also supported ($0.15 input / $0.50 output); all rates are per million tokens. Lower-cost alternatives remain selectable: `glm-4.7-flash` (currently free) and `glm-4.7-flashx` ($0.07 / $0.40). Prices come from [Z.ai pricing](https://docs.z.ai/guides/overview/pricing) and can change; free API access remains subject to account limits. Budget accounting conservatively charges cached input at the standard input rate. Configuration rejects unknown providers/models so budget accounting has known rates. There is no automatic provider, model or billing fallback.

Z.ai supports text, photos and voice notes. Text requests, including summaries and personality inference, use `ZAI_MODEL`; photo descriptions use the explicitly configured `ZAI_VISION_MODEL` so structured text decisions keep the text API format. Thinking is disabled except for `glm-5.3-flash`, which requires enabled thinking according to [Z.ai documentation](https://docs.z.ai/guides/vlm/glm-5.3-flash). Structured requests on `glm-5.3-flash` use `reasoning_effort=low`; without tools they also get a separate final system instruction requiring a schema instance rather than the schema itself. Output remains bounded; reasoning can still consume the token allowance before a visible answer is produced. JSON mode includes the required schema in the system instructions; the existing local validators reject malformed decisions/settings before use. [Z.ai API reference](https://docs.z.ai/api-reference/llm/chat-completion).

For photos, the paid default is `glm-4.6v`: $0.30 input / $0.90 output per million tokens. Lower-cost alternatives are `glm-4.6v-flashx` ($0.04 / $0.40) and `glm-4.6v-flash` (currently free). The adapter sends a JPEG as an in-memory Base64 image, capped at 5 MiB or your smaller media limit. It never sends Telegram download URLs. Reservations and reconciled usage use the **vision model's rates**, independently of the text model. [Vision models](https://docs.z.ai/guides/vlm/glm-4.6v), [pricing](https://docs.z.ai/guides/overview/pricing).

Voice uses Z.ai's `glm-asr-2512` transcription endpoint with the same Z.ai key. Telegram OGG audio is decoded to mono WAV in memory. Z.ai accepts at most **30 seconds per request**, so longer messages are split into pieces of up to 28 seconds. Each cut falls at the quietest 100 ms near the boundary, and each piece receives the previous piece's text as context (`prompt`). The pieces are transcribed in order and joined. `VOICE_MAX_SECONDS` (default 120, maximum 300) is the only overall length limit. Each piece is a separate request, reserved and charged separately. OpenAI pieces may be up to 298 seconds. Both Telegram metadata and decoded audio duration are checked before upload. The local reservation uses Z.ai's published approximate $0.0024/minute equivalent, plus one second and a 25% allowance; actual provider billing is token-based ($0.03 per million tokens), so this is an estimate, not invoice reconciliation. Failed requests keep their reservations. [Audio API](https://docs.z.ai/api-reference/audio/audio-transcriptions), [pricing](https://docs.z.ai/guides/overview/pricing).

With Z.ai selected, `/images` and `/voice` work normally. `/status` shows the vision model and effective voice limit. When `/web on` is enabled, ordinary replies and explicit `/search` requests use the provider-neutral `search_web` function (see "Structured search" below); `/web on|off` controls permission per group. On Z.ai it calls the configured `/web_search` endpoint (`search-prime`, five results) and returns bounded evidence snippets and validated links. [Explicit API](https://docs.z.ai/api-reference/tools/web-search). Text, selected media and stored summaries are sent to whichever provider you explicitly select; review its data policies and tell group members before switching.

A $5 account top-up is separate from `BOT_BUDGET_USD`. You can set that local allowance to `5`, but it is a lifetime cap that includes previously tracked spend, not a fresh $5 balance. The bot does not query your provider balance or reset spend when switching providers.

Existing explicit `ZAI_MODEL` and `ZAI_VISION_MODEL` values override the new defaults. To upgrade an existing installation, set `ZAI_MODEL=glm-4.7` and `ZAI_VISION_MODEL=glm-4.6v` in your private configuration locally; it is not read or modified during development. Voice already uses the paid `glm-asr-2512`; the current Z.ai audio API does not list a higher-tier transcription alternative. Paid model selection does not guarantee valid JSON or avoid rate limits, so pacing, validation and bounded retries remain enabled.

Recreate the bot deliberately after changing local configuration. This can make billed API requests. No live provider access or reply quality has been verified by mocked tests.

## Commands

The "/" list shows only what people type; everything else is in the menus.

| Command | Who | Purpose |
|---|---|---|
| `/menu` (also `/start`, `/help`, «меню», «помощь», the pinned «🏠 Меню» button) | Everyone | Main menu; no AI request or memory entry |
| `/chat <text>` | Everyone | Request a reply; without text the bot asks for it |
| `/search <public query>` | Everyone | Explicit lookup, only when web search is on; isolated from group history |
| `/settings` (also `/status`, «настройки») | Settings manager | Admin panel |

Hidden shortcuts still work when typed but are not listed, because each has a menu button: `/skills`, `/whoami`, `/forgetme` (asks «Да»/«Нет»), `/personality [description]`, `/web`, `/images`, `/voice on\|off`, `/context directed\|all`, `/historylimit 10..500`, `/resetmemory` (asks «Да»/«Нет»), `/pause` and `/resume`. `/setprompt` was removed: `/personality` covers it with a preview.

**Pinned menu button.** In «Админ-панель», «📌 Кнопка меню» pins a persistent keyboard button for the whole group; pressing it again removes it. With BotFather privacy on (the bot only receives messages that start with "/"), the button is labelled `/menu`. With privacy off, it is labelled «🏠 Меню». The state is stored per group (migration 12, `chats.menu_button`).

Directed mode replies only when addressed. All mode also lets the model decide whether to join other group conversations. Settings managers are identifiable Telegram group admins and configured bot super admins. Group-admin privileges are verified with Telegram for every settings operation; super admins are matched by numeric user ID. Anonymous senders cannot use either role because the person cannot be identified. Failed group-admin verification denies access unless the sender is a configured super admin. The model cannot authorize commands, execute code or change configuration. When `/web on` is enabled, its tools are read-only search and the local `load_skill` function restricted to the bundled place skill. Z.ai additionally exposes explicit public search, with queries of 1–400 characters and no fixed call-count cap. OpenAI caps hosted search at one call per API request, and the bot disables further search on a skill continuation if a search was already reported. Only allowed group IDs are processed. Service events, other bots, unsupported media, channels, private chats and oversized messages are ignored.

## Russian interface, buttons and prompts

All bot messages, buttons, errors and help are in Russian (`src/texts.py`); command names stay Latin because Telegram requires it. At startup the bot registers a Russian command menu with `setMyCommands` for the default scope, all group chats (public commands) and group administrators (plus settings commands). The result is logged as `commands_registered` with `outcome` `delivered` or `delivery_failed`; a failure never blocks startup. Telegram apps cache bot commands, so after the first deployment the "/" list may only appear once the chat is reopened or the app restarted.

**Menus.** `/help` (also `/start`, «меню», «помощь») opens the main menu: what the bot is (plus the current character's name) and up to four buttons. Every page edits the same message in place and ends with «⬅️ Назад»; pages two levels deep also get «🏠 Главное меню».

```
🏠 Главное меню
├─ 💬 Как общаться ─ 🔎 Поиск ─ [🔎 Найти…]
├─ 🧭 Что я умею (/skills) — short skill list; the model chooses which skill to use
├─ 🗑 Мои данные ─ [🆔 Мой ID] [🗑 Удалить мои данные]
└─ ⚙️ Админ-панель (admins only)
   ├─ ⚙️ Настройки (/settings) — status summary
   │  ├─ 🔌 Функции — search / photos / voice toggles
   │  ├─ 👥 Режим и пауза — mode toggle, pause/resume
   │  └─ 🧠 Память — [📏 Лимит истории…] [🧹 Очистить память]
   ├─ 🎭 Персонаж ─ [✏️ Описать персонажа] [👀 Текущий персонаж]
   └─ 📖 Команды
```

Members never see the admin branch. Admin pages and every settings button re-check admin rights on each press; a non-admin press is answered «Только для администраторов» without queuing anything. Button data is a short allowlisted code (`v1:<action>:<arg>`); anything else is ignored, and the internal button commands cannot be typed. Every press is acknowledged at once, so the spinner never hangs.

**Reply prompts.** A command that needs input but got none sends a message with a selective ForceReply. This covers `/setprompt`, `/historylimit`, `/search`, `/chat` and `/personality`. The requester's reply to that exact message becomes the input (prompts table, 30-minute expiry, single use). A reply from someone else is treated as an ordinary message.

**Confirmations.** `/resetmemory` and `/forgetme` (typed or via their buttons) ask «Точно…?» with «Да»/«Нет». Only the requester can confirm, and the message is then edited into the result.

**Follow-ups.** Recommendation cards carry «🔄 Ещё варианты», «📍 Поближе» and «💸 Подешевле». A press becomes a normal, billed request from the presser, sent as a reply to the cards; the used buttons are removed.

**Progress and failures.**
- When research actually starts (a search or a Maps, review or Afisha read), the bot posts «🔎 Ищу…» (or «🗺 Смотрю Яндекс Карты…», «🎭 Смотрю Яндекс Афишу…»), then edits that message into the answer or into the error. If the edit fails, it sends a new message.
- For messages addressed to the bot, an unexpected crash shows «⚠️ Что-то пошло не так, пробую ещё раз…», and after the last retry «😵 Не получилось ответить из-за внутренней ошибки…». This is one message, edited in place, never sent twice.
- Passing group chatter stays silent on failure, as before.
- Logs add `callback_received`, `prompt_created`/`prompt_answered`, `progress_shown`, `crash_notice_sent` and `reply_finished.notified`.

Migration 11 adds `jobs.markup`, `jobs.reply_prompt`, `jobs.status_message_id` and the `prompts` table. The command menu, buttons, prompts and progress edits are covered by mocked tests only; they have not been tried in a live chat.

## Memory and restart behavior

Each group has its own persona, mode, pause setting and count limit. Recent history, rolling summaries and incidental cooldowns are isolated by forum topic (chat ID plus topic ID). Non-topic conversations use topic 0. User and delivered bot messages count toward the limit. Long content also triggers compaction before it exceeds the model context budget. Summaries preserve useful facts, running jokes and unfinished topics; they are lossy and contain personal data from the source messages. Messages and summaries expire after 30 days by default. `MEMORY_RETENTION_DAYS=0` explicitly disables expiry; 1–3650 sets a retention period. Summaries expire based on the oldest source they represent, so rolling updates cannot keep old personal information forever. Idle groups are cleaned periodically. Existing messages without timestamps receive the migration time as a conservative starting point. Tell group members which context mode is used and that selected text and summaries are sent to the selected AI provider.

**Shortened older replies.** The database keeps every bot reply in full, but the model sees older long ones shortened (`policy.memory_view`). The three newest bot replies always stay whole, so follow-ups such as "what's the address of #2?" or "show more besides those" see every detail. Older replies over 800 characters are shortened by fixed rules, with no AI request. Recommendation cards (including ones stored in the earlier emoji format) become just the list of place names, `[Earlier recommendations: …]`, unconfirmed places included; the surrounding conversation already says what was asked and where. A reply counts as cards only if it has an `Адрес:` line or an old emoji card heading, so numbered bold lists in ordinary answers are shortened as plain text. Plain answers keep their opening (about 400 characters) and every later line with a link. Each shortened reply starts with a note telling the model that details were left out and that it must look them up again with tools, or say it no longer has them, rather than reconstruct them. A reply is left whole unless shortening saves more than 300 characters. People's messages are never shortened. Summaries are still written from the full text.

Older messages are replaced only after every required summarization batch succeeds. Saving a summary and deleting its source rows is transactional. A failed compaction leaves source history intact and rejects that new conversation item; it does not silently exceed the limit. Failed history-limit changes leave the prior limit intact. Summary/source changes can commit before the final limit update, but information remains represented and the setting is never lowered ahead of compaction.

Telegram offsets and a small normalized in-flight job are saved together before acknowledgement. Jobs are deleted after processing and expire after 24 hours. Removing a chat from `ALLOWED_CHAT_IDS` cancels its persisted work before further processing. The polling cursor recovers safely after a week without updates, when Telegram may choose a lower random update ID. The database prevents two bot instances from working on the same state. A reboot resumes prepared work and generated replies. Persona and history remain persisted.

Delivery is not exactly-once: if the process crashes during a send, its outcome may be unknown. Such sends are not automatically repeated and are not added to assistant history. A generated reply that has not entered delivery can be resumed without another model call. A crash during a model request can require regenerating a reply; the spending reservation is retained conservatively. Downtime causes a response gap. Telegram retains pending updates for at most 24 hours. Deleted group messages are not automatically deleted from the bot's local history; `/resetmemory` clears both messages and summary.

Forum topics now have separate memory and summaries. Existing group history migrates to topic 0; it is not copied into every topic. Personas and feature switches remain group-wide. `/resetmemory`, persona changes and context-mode changes clear memory across the whole group. `/historylimit` compacts every retained topic before applying a smaller group limit; failure retains the previous setting.

## Spending

The model default is `gpt-6-luna`; an explicit `OPENAI_MODEL` in your private file overrides it. `gpt-5.6-luna` is also supported. These are the OpenAI adapter models; Z.ai selection and rates are documented above. Other models are rejected until their cost accounting is implemented. No OAuth/subscription fallback is used. GPT subscription billing does not pay for these API calls.

`BOT_BUDGET_USD` is a lifetime allowance shared across all groups and persisted through reboots. The default is $10. It includes summarization, photo descriptions, voice transcription and web search. Before requests, the bot reserves a conservative estimate using UTF-8 input size, known model prices and maximum output tokens. On success it reconciles against reported usage, conservatively pricing all input at the cache-write rate. Failed/unknown requests keep their reservations. This can stop the bot before the real invoice reaches $10. It is not an authoritative invoice or a guarantee against provider price changes. When `/web on` is enabled, ordinary conversation replies can choose hosted search; `/web off` removes the tool from replies and blocks `/search`; reasoning is set to none; output tokens, input length and incidental reply frequency are bounded. Raising the allowance requires editing `.env` and recreating the bot. Memory reset does not reset spend.

Also configure a provider-side hard project spend limit and disable automatic prepaid recharges. OpenAI enforcement may lag. Dashboard alerts alone do not stop requests.

## Secrets and logs

The assistant must never read `.env`, credential files, resolved Compose configuration or container environment. The setup helper and running applications may consume credentials locally but never return them to the assistant. `.env`, credentials, backups and work files are excluded from Git and Docker builds. No message content or raw HTTP exceptions are printed. Host administrators and Docker administrators can access runtime credentials. Protect the Pi's OS and backup media; PostgreSQL storage is not encrypted by this project.

To rediscover IDs, with the live bot stopped:

```sh
python3 scripts/discover_chat.py
```

This uses only Telegram, prints group IDs, and reads pending updates without acknowledging them. If there is no group update, send `/help` to the bot in the group and rerun it. The verified ID for the current group is `-882012764`.

## AI request pacing and retries

`API_MIN_INTERVAL_SECONDS=0.25` adds a quarter-second minimum gap between AI request starts across all chat workers. A shared pacing clock covers text, photos, transcription, summaries, personality inference and retries, on either provider. Independent requests may run concurrently. The first request is immediate. Time already spent on other work or retry backoff counts toward the gap. Set any finite value from 0 to 30 seconds; 0 disables pacing. This is separate from Telegram polling and `REPLY_COOLDOWN_SECONDS`, which controls incidental participation. Restart/recreate the bot after changing local configuration; the in-memory pacing clock restarts with it.

Z.ai temporary HTTP 429 errors (provider codes 1302/request limit and 1305/overload, or a gateway 429 without a code) get at most two retries, using `Retry-After` when available or short exponential backoff with jitter. Individual retry waits are bounded at 30 seconds. Longer requested cooldowns are not shortened: the request fails without retry. Balance, access, long-term quota, unknown provider-code errors and uncertain network failures are not retried. Each retry reserves its own conservative budget estimate; earlier failed reservations are retained. There is no automatic model/provider fallback. [Z.ai error codes](https://docs.z.ai/api-reference/api-code).

Pacing can reduce bursts but does not guarantee compliance with account-specific limits. Check your [Z.ai model rate limits](https://z.ai/manage-apikey/rate-limits) if 429s persist. Participation parsing accepts a single complete JSON Markdown fence or leading Unicode BOM, then applies the same strict schema checks. It does not extract objects from surrounding prose, remove thinking blocks, or repair JSON syntax. Parser failures log fixed error labels, numeric positions and format hints without output content.

Participation decisions use an explicit JSON-only prompt with examples for replying and staying silent. Persona/language/style applies inside `reply`, while the outer object must contain exactly `respond` (boolean) and `reply` (string). Logs distinguish malformed JSON, a non-object result, missing/extra/duplicate fields, wrong types, empty required replies, nonempty silent replies, and oversized replies without logging model output.

Structured decisions and character settings permit **three repair attempts after the initial generation** (at most four generations, stopping at the first validated result). Every repair includes the complete last rejected answer as an assistant message and fixed validation feedback with the parser category, position, line and column when applicable. Original system instructions and schema remain authoritative; rejected output is treated as untrusted data and is never logged or persisted as a reply. Earlier failed repair messages are replaced rather than accumulated. Repairs use the same provider/model, normal pacing, context checks and budget reservation; format repairs disable search and skill tools and preserve previously collected source links. They repair the supplied answer rather than repeating research, avoiding additional search fees during formatting repair. Exhaustion fails safely without delivering invalid output. Network and budget failures stop JSON repair immediately; normal temporary-429 retries still apply independently. Each completed API attempt counts toward the allowance. OpenAI receives strict `json_schema`; Z.ai receives its documented `json_object` mode plus schema instructions and local validation, not a provider-enforced strict schema. JSON mode is used only for requests without tools (the final answer step and repairs): with it, GLM models answer in JSON at once and never call search, reader or action tools. While tools are offered, the schema is still in the instructions as the shape of the final answer, and the same local validation and repair apply.

## Typing indicator

While generating a reply, processing selected media, summarizing memory for a conversation, or inferring personality, the bot sends Telegram's `typing` chat action and refreshes it every four seconds. Forum topic IDs are preserved. This runs in a short-lived background thread so it covers blocking provider requests, retries, and pacing waits. Each chat-action request has a three-second timeout; failures are logged without raw exceptions and never prevent the AI operation. Refresh stops when the operation finishes. Telegram clears typing when a bot message arrives, or within five seconds of the final action. All-mode decision requests can show typing and ultimately choose silence. The indicator does not add a message to history. [Telegram chat actions](https://core.telegram.org/bots/api#sendchataction).

Reducing pacing does not shorten provider inference or remove decision retries. Existing explicit `API_MIN_INTERVAL_SECONDS` values in your private configuration override the new default. Set `API_MIN_INTERVAL_SECONDS=0.25` locally and recreate the bot to change an existing installation.

## Logging

The bot writes one JSON event per line to stdout at INFO level, with warnings/errors for failed operations. Read them with `docker compose logs --tail 100 bot`. The existing Docker local logging driver rotates logs at 5 MiB, retaining up to two files.

Events cover startup/shutdown and fatal-error stage, failed Telegram polling, durable job phases, reply generation/delivery, unknown delivery after restart, media/settings/memory failures, and spending-limit rejections. AI events include provider, model, operation, elapsed milliseconds (including pacing/retry waits), reported token usage, and local reserved/charged USD amounts. Missing usage keeps the reservation; transcription costs are estimates. These amounts are local budget accounting, not provider invoices. Telegram update IDs connect job events without logging chat or user IDs. Successful empty polling does not produce a log entry.

Only fixed event names and allowlisted metadata are emitted. Credentials, configuration dumps, Telegram URLs, prompts, conversation/reply content, media bytes, provider response bodies, and raw exceptions/tracebacks are excluded. OpenAI/HTTP client logging is suppressed to prevent SDK debug output from exposing request data. No credentials or conversation data are needed to diagnose ordinary request failures; safe numeric HTTP status codes and allowlisted Z.ai error codes are recorded when available. Pacing/retry events show delay milliseconds and retry attempt numbers. `ai_output_invalid` records an allowlisted `validation_error` category; `ai_decision_retry` identifies the single corrective decision request.

Every event emitted while a job is processed carries its `update_id`, including model, search, reader and skill events, so interleaved chats can be told apart. Each job ends with one `reply_finished` line:
- `outcome`: `delivered`, `fallback_delivered`, `silent`, `paused`, `not_directed`, `cooldown`, `rate_limited`, `throttled`, `media_failed`, `media_disabled`, `memory_failed`, `model_failed`, `budget_exceeded`, `delivery_failed`, `delivery_unknown`, `gave_up` or `revoked`.
- Counts: `directed`, `model_requests`, `failed_requests`, `searches`, `reader_calls`/`reader_ok`, `skills_loaded`, `json_repairs`, input/output/`reasoning_tokens`.
- `spent_usd`: tracked spend for the job, where a failed request counts its kept reservation.
- `round_limit`, `budget_exceeded`, `fallback` and `duration_ms`.

Whenever the user receives an error message instead of an answer, `reply_fallback` records why (`model_failure`, `budget`, `media_failed`, `memory`, `search_failed`, `search_disabled`, `invalid_request`, `settings_failed`). `ai_request_completed`/`ai_request_failed` include `reasoning_tokens` when the provider reports them, which shows how much of a thinking model's budget went to reasoning. A summary appears only for jobs whose phases ran since the last restart; it contains no text, IDs of people, URLs or queries. The tools container logs the same `time`/`level`/`event` JSON shape, with `service: tools`.

Lazy skill calls emit `skill_requested` and `skill_returned`, joined by a locally generated `skill_request_id`. Both record the tool (`load_skill`) and validated `requested` arguments (`{"name":"nearby-places"}`). `skill_returned` includes only `returned.name`, a SHA-256 fingerprint in `returned.sha256`, and `returned_chars`; the skill text is never logged. This describes the local tool result prepared for the next model request, not confirmation that the provider accepted it. Raw model arguments, provider call IDs, and conversation content remain excluded. `skill_failed` records rejected calls or loading failures without exception text; validated calls that fail to load retain the same request ID. Text/image `ai_request_started` events include `web_search_enabled`, indicating whether the `search_web` tool was offered on that API request; this does not imply a search actually ran. The `name` field is `nearby-places` or `city-events`.

Decision `ai_output_invalid` events include a fixed Russian explanation in `validation_detail`, the model, and `output_chars`. Syntax failures also report `error_line` and `error_column` (one-based) alongside `error_position` (zero-based), referring to the normalized JSON candidate after stripping whitespace/BOM or an accepted Markdown wrapper. Explanations distinguish syntax errors from schema/type violations and never include response text, offending field names, or raw parser messages. An error at the first character can indicate a plain-text answer; these diagnostics do not establish why the provider ignored the requested JSON format.

`ai_json_repair_started`, `ai_json_repair_completed`, `ai_json_repair_failed` and `ai_json_repair_exhausted` trace JSON repairs with a shared local `json_request_id`, `schema_name`, `format_mode`, generation `attempt` (1–4), and `repair_attempt` (1–3). Start events show `max_repairs=3`, `previous_answer_included=true`, `error_feedback_included=true`, the preceding validation diagnostic and `web_search_enabled=false`. Success events mean local validation passed and include timing and output length; failures distinguish invalid JSON from request failure. These events contain no rejected/corrected answer text or repair prompt.

Searches emit `web_search_started`, `web_search_completed` or `web_search_failed`. A local `search_request_id` connects start/completion; metadata includes query length, `search_site`, `search_recency`, `filters_applied`, safe result count, elapsed time and local reserved/charged amounts. Query text, result snippets and URLs are not logged. `search_not_executed` means the model ignored required search; `invalid_search_call` means its arguments were rejected. Completed searches have their own events, including when the result count is zero.

Each `search_web` call reserves and charges a local $0.01 estimate separately from text generation. On OpenAI, the search is a separate small request, so its reservation also covers up to 20,000 input and 800 output tokens and is reconciled to reported usage. Pacing and temporary-429 retry accounting apply; failed/uncertain attempts retain reservations. The explicit API uses the configured Z.ai client and credentials, with no provider or billing fallback. These are local estimates, not provider invoices.

## Operations

```sh
docker compose ps
docker compose logs --tail 30 bot
docker compose down
```

Do not use `docker compose down -v` unless deliberately deleting all local state. Never share `docker compose config` or container inspection output: they can contain resolved credentials.

Before updates, back up, then pull code and rebuild:

```sh
./scripts/backup.sh
git pull
docker compose up -d --build
```

Backup files are owner-only PostgreSQL custom-format dumps under ignored `backups/`. They contain conversation data. Keep a protected copy off the Pi and periodically test restoring it. Restore is an explicit destructive operator action, never an automatic development task. Use the restore helper with a conservative lifetime spending floor reconciled from provider records, including charges newer than the backup:

```sh
./scripts/restore.sh backups/your-backup.dump 10.00
# Review the result and spending floor before deliberately starting the bot.
docker compose start bot
```

The restore helper stops the bot and replaces the public schema in one transaction together with all post-processing, so any failure leaves the database unchanged. It works on a fresh volume where the bot has never started. Lifetime spending becomes the largest of archived spending, current local spending and the supplied floor. Each chat keeps the larger of its archived and current spending; chats are not raised to the global total. It cancels restored jobs and drafts. Startup explicitly discards pending Telegram updates after a managed restore to avoid replaying already-handled work; messages sent before restart will also be discarded. The bot remains stopped after restore. A floor at or above the configured allowance will intentionally prevent new AI requests. Retention and `/forgetme` do not erase older backups; protect and expire backup copies separately. The spending ledger records reservations and reconciliations for audit, but a database archive cannot preserve ledger entries created after its capture.

Schema migrations run once in version order, in the same transaction as their version records. Unknown newer versions and gaps are rejected. Future non-additive migrations must be implemented and tested before updating. A database initialized by the old starter using a different database owner needs a manual role migration; do not delete its volume to work around authentication without first backing it up.

On the Pi, clone the repo, transfer `.env` over SSH, restore a backup if moving history, and build locally with `docker compose up -d --build`. Stop the PC bot before starting the Pi bot. The image builds for the host architecture. Pi hardware execution is not yet verified.

## Tests

See `./scripts/test.sh`. Tests use an explicit synthetic environment and isolated PostgreSQL database on an internal-only network; never the private `.env`. The test database uses temporary memory storage and is removed afterward. The script also runs a real Chromium DOM test inside the tools image with the production hardening and no network, and `scripts/test-restore.sh`, which round-trips `backup.sh`/`restore.sh` against a disposable Compose project. No Telegram or AI provider requests occur. Coverage includes permissions, anonymous admins, chat isolation, privacy filtering, memory/reset/summary failures, limit-change failures, delivery recovery, deduplication, persistence, spending reservations and single-instance locking.

Telegram ID discovery has been verified live. OpenAI model access and role-play quality need a separately authorized live test after configuration. Do not claim those are verified by the mocked tests.

In `all` mode, eligible group messages now receive a model decision: reply or stay silent. Decision and reply use one structured-output request without tools; silent decisions still cost tokens. With `/web on`, research tools are offered only after the model decides to join, in a second request that writes the reply. Direct mentions, configured names, replies and `/chat` receive a reply subject to pause and budget limits. The cooldown suppresses unaddressed participation checks after either a check or a delivered reply, including checks that choose silence. Admitted text still enters all-mode memory during cooldown. Incidental failures remain silent. Directed mode still discards unrelated messages.

## Personality setup

Send `/personality` and reply to the bot with an ordinary description in your preferred language. You can also send everything in one message:

```text
/personality Ты — теплый, гостеприимный армянский друг. Откликайся на Армен, Ара и Карен. Часто вступай в разговор. Пиши эмоционально, с легким юмором и уважением, по-русски с армянским сленгом: джан, апер, барев. Любишь хорошую еду, дружбу и семью.
```

The model infers names, eagerness, speaking guidance, style, lore and language and shows a preview. No settings change yet. Send `/personality save` (or reply `да`) to apply it, `/personality cancel` to discard it, or reply with a revised description. `/personality show` displays the saved settings. `/personality reset` previews the defaults for confirmation. No field syntax, quotes or pipes are required; pasted descriptions with those constructs are interpreted as text. Numeric eagerness in a description is mapped to qualitative guidance; it is not a response probability.

Inference uses one bounded AI request and the existing spending allowance. Viewing, saving and cancelling do not call the AI provider. Drafts belong to one user in one group, survive restarts and expire after 30 minutes; expired drafts are removed on the next draft lookup. While a draft is open, only replies to the bot from that user enter the setup flow. Ordinary messages keep their normal behavior. Descriptions are not added to conversation memory. Only the inferred settings are retained as a draft after its job finishes. Permission is checked again before each setup step or save. Saving invalidates other people's pending previews for that group.

Changing lore on save clears history and summary to avoid conflicting character facts. Other personality edits retain conversation. Nonempty character lore is the sole identity sent to the model; otherwise the raw `/setprompt` persona is used. Configured names are aliases for that character. `/setprompt` replaces the active identity by clearing structured lore, retaining other character settings, and cancelling pending personality drafts.

Eagerness is `quiet`, `balanced` (default), or `chatty`; it guides the model. It does not bypass context mode, pause or budget. Cooldown limits only unaddressed participation; per-user and per-group admission quotas also apply to directed turns and media. Use `/context all` if the character should consider joining general group chatter. Named mentions count as directed messages. Names match whole names without case sensitivity. Telegram must deliver messages: disable privacy mode via BotFather for plain-name recognition, then re-add the bot if needed. `/status` shows the BotFather privacy setting reported at startup; a group-admin bot can receive messages even with that global setting enabled. A bot cannot match character names in messages Telegram never delivers. Directed mode discards other messages.

## Bot super admins

Send `/whoami` in the approved group to see your numeric Telegram user ID. Add trusted user IDs to your private `.env`, for example:

```dotenv
BOT_SUPERADMIN_IDS=123456789,987654321
```

Recreate the bot after changing this value. Those users can manage all chat settings without Telegram group-admin status, including personality, prompt, context, memory, pause and resume. Existing identifiable group admins also retain access. Super admins cannot operate outside `ALLOWED_CHAT_IDS`, change credentials or the owner's spending allowance through chat, or be inferred/granted by the model. Usernames and forwarded text do not grant access. Membership in this list can only be changed in local configuration. An empty list adds no super admins. The assistant does not read or update your private `.env`.

Settings and drafts are stored in local PostgreSQL through automatic additive migrations. Model extraction and reply decisions use [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), validated locally before application.

If an older version replied with the wrong character, use `/resetmemory` after updating to remove that reply from context. The saved personality remains intact. The current character identity also takes precedence over conflicting identities in past conversation. Telegram delivery rules are documented under [privacy mode](https://core.telegram.org/bots/features#privacy-mode).


## Video circles

Telegram round video messages (`video_note`) are understood when voice is enabled in the group (`/voice on`):
- **Sound:** extracted with ffmpeg and transcribed like a voice message, split into pieces when longer than the provider allows.
- **Frames:** when photos are also enabled, 1 frame (up to 15 s), 2 frames (up to 40 s) or 3 frames (longer) are taken at even intervals. They are tiled left to right in time order into one 320-pixel-high JPEG and described in a single vision request.
- **Memory:** both results enter memory as untrusted text, labelled with the circle's length.

A circle without sound still gets its frames described, and a spoken character name in a circle addresses the bot, just as in voice messages. Because MP4 metadata can sit at the end of the file, the circle is decoded from one private temporary file (mode 0600) in the container's in-memory `/tmp`, deleted right after. `MEDIA_MAX_BYTES` (default 5 MiB) also applies to circles; long, high-quality circles can exceed it.

## Polls and scheduled messages

When someone addresses the bot (mention, reply, `/chat`, a configured name), or in all mode once the bot has decided to join a conversation, the model can also use four action tools defined in `src/action_tools.py` and run by `src/chat_actions.py`. All-mode requests rarely mention the bot («составь опрос»), so the joined reply needs them too. The join decision itself, `/search`, summaries and photo descriptions never get them, and they work with `/web` on or off. With `/web off`, a joined reply is now written in a second request with the action tools instead of reusing the decision's text, so each join costs one more AI request in those groups. `ai_request_started` logs `actions_enabled` next to `web_search_enabled`.

| Tool | What it does |
|---|---|
| `send_poll` | Posts a native Telegram poll or quiz now, or at a scheduled time |
| `schedule_message` | Posts a text the model writes now at a later time, as a reply to the request |
| `list_scheduled_messages` | Shows the chat's pending items with ids and Moscow times |
| `cancel_scheduled_message` | Cancels one item; the person who scheduled it or a group admin may cancel |

Example: «напомни нам завтра в 10 купить билеты», «сделай опрос: пицца или суши», «в 18:00 запусти голосование, куда идём».

**Times.** The model is given the current Moscow date and time. It passes either `send_at` (`YYYY-MM-DDTHH:MM`, Moscow time unless an offset is given) or `delay_minutes`. A time must be at least 30 seconds and at most 60 days ahead. Scheduled text is sent verbatim, without another AI request.

**Self-correction.** Arguments are validated locally against Telegram's limits: question 1–300 characters, 2–10 distinct options of 1–100 characters, quiz needs `correct_option_index` and a single answer, explanation up to 200 characters with at most 2 line breaks. A regular poll ignores the quiz-only fields, and nullable fields the model leaves out (`correct_option_index`, `explanation`, `send_at`, `delay_minutes`) count as null, since Z.ai does not enforce the tool schema. A mistake does not fail the reply. The tool result is `{"status":"error","field":…,"message":…,"next_step":…}`, naming the field and the fix, and the model is told to correct the call and try again (it gives up and explains after two failures). Malformed JSON arguments and a Telegram `400` on an immediate poll are reported the same way, the latter with Telegram's own description. Rate limits, the pending limit and uncertain delivery tell the model not to retry. The model is told never to claim success without `status: "ok"`.

**Limits and safety.** Per request: 3 polls, 5 scheduled messages, 5 cancellations. At most 25 pending items per chat. Tool calls are deduplicated per job, so a crash-retried job does not post a poll twice. Scheduled items live in the `scheduled_messages` table (migration 13); the poller sends due ones every loop. A send is marked before it starts and is never repeated after a crash (`unknown`); a `429` or failed connection is retried up to 5 times, other rejections become `failed`. Items in chats no longer in `ALLOWED_CHAT_IDS` are dropped. Scheduled items are sent even while the bot is paused, because people asked for them explicitly. `/forgetme` deletes that person's scheduled items. Finished rows are removed after 7 days.

**Logs.** `action_completed` / `action_failed` record the tool and an allowlisted `tool_error`; `scheduled_delivered` / `scheduled_failed` record `scheduled_kind` (`text`/`poll`). Poll text, message text and Telegram error descriptions are never logged.

Mocked tests cover validation, the correction loop and delivery. Real Telegram polls and live model behaviour have not been verified.

## Photos, voice and web search

Recommendations use seven bundled skills in `src/skills/`:
- `nearby-places`: cafes, restaurants, bars and parks.
- `city-events`: theatre, concerts, stand-up, shows, excursions and festivals.
- `art-galleries`: exhibitions, museums and galleries.
- `kids-and-family`: events and places with age and duration as hard limits.
- `evening-plan`: an event plus a place nearby, with timing and a backup.
- `date-ideas`: 2–3 options of different character.
- `group-compromise`: meets every member's must-haves and states who compromises.

All share the same rules:
- Stated requirements are hard filters applied before ranking; preferences only order the results.
- A claim-to-evidence table (a homepage proves existence only; ratings are information, not a filter).
- A fixed research budget per tool.
- When too few options pass, near-miss options that relax only a preference; the card text says what was relaxed.
- Nothing left pending: no promises of later searches.

The skills were written for this bot, informed by the MIT-licensed kirvahe/restaurant-skill and skywain/trip-planner-skill and by Anthropic/OpenAI prompting guides; no text was copied. Search-enabled generations initially receive each skill's name and description plus `load_skill`; only a validated call reads and returns a body, and the model loads only what the request needs (both for "dinner and a show"). Unrelated replies do not read or send any body. Each skill contains a structured search playbook (which `site`/`recency` to use, Russian query templates), per-category verification checklists and the field-by-field output contract. The `read_yandex_reviews` tool opens specific Maps cards in Chromium (in the separate tools service) and returns a bounded sample of real review texts and venue metadata. Routes and full review-history access are not provided; search fragments and page-order samples must be labelled honestly. The bot permits up to five research model rounds plus a final answer request with tools disabled; up to four validated function calls in each response are processed, and extra calls receive a `too_many_calls` result. Unknown tools and invalid or duplicate arguments fail safely; an unsupported Maps URL is instead returned to the model as `invalid_url`. Repeated skill calls return the already loaded body without another file read. Continuations preserve earlier tool messages, but these messages and the skill body are not saved in conversation memory. Each API step has normal pacing, context checks and budget accounting. For explicit `/search`, the model must complete a `search_web` call before the final answer is accepted (OpenAI is additionally forced to call it on the first step); after loading a skill, a successful review or Afisha read also counts as evidence. This applies to both providers. Ignoring the search tool triggers bounded reminders and then a safe failure, rather than delivery of a handoff to `/search`. Empty explicit search results count as a completed lookup, not evidence for invented recommendations. Only metadata is cached; the body is read on demand.

`/web on` enables automatic search during ordinary conversation, including `/chat`, mentions, replies and participation in all mode. Ask naturally for current public information; the assistant can search and answer in the same response, using conversation context to understand constraints. It does not ask users to send another command. Greetings and casual conversation do not require search. Search-backed conversational answers and their source links enter normal conversation memory after delivery, so follow-up questions can refer to them.

Photos and voice are enabled by default. Search is disabled by default for new groups and requires `/web on`. Existing saved web settings apply to automatic conversation search as well as `/search`. Features can be switched off per approved group with `/images off`, `/voice off` or `/web off`. `/status` displays their saved settings. Group admins and bot super admins manage these switches. Rebuilding automatically adds the required columns without deleting history or personalities.

Photos sent as Telegram photos are described once using the selected chat model on OpenAI or `ZAI_VISION_MODEL` on Z.ai. The largest Telegram photo variant no larger than 1280 pixels on either side is used with low-detail vision. Captions and descriptions become text in context and rolling summaries. Small text and fine image detail may be missed; descriptions can be inaccurate. Image documents, videos, stickers and albums as one combined object are not supported; album photos are processed individually.

Voice notes in OGG format are converted to mono WAV with FFmpeg through memory pipes, then transcribed with `gpt-transcribe` on OpenAI or `glm-asr-2512` on Z.ai. The original language is retained. FFmpeg receives no credentials in its environment, accepts only pipe input, has a fixed argument list and a 20-second conversion timeout. Actual decoded duration is checked as well as Telegram metadata; overlong notes are rejected rather than silently truncated. Transcripts become text in context and summaries. Transcription can be inaccurate.

Media defaults: 5 MiB per download and 120 seconds per voice note on OpenAI; Z.ai caps photos at 5 MiB and voice at 30 seconds. Override `MEDIA_MAX_BYTES` (1024–10485760) and `VOICE_MAX_SECONDS` (1–300) in local configuration if needed. Downloads are streamed from Telegram's fixed API host, bounded even without a content length, and reject redirects and unsafe file paths. Bytes are never written to local disk, PostgreSQL or logs, and Telegram token-bearing download URLs are never sent to the selected AI provider. In-flight jobs briefly retain the Telegram file ID needed for recovery; successful conversion replaces it with text before context processing. Reboots after conversion reuse that text. A crash during conversion can cause another billed request; reservations are retained conservatively.

In directed context mode, photos need an addressed caption, `/chat` caption or reply to the bot; voice needs a reply to the bot. Unaddressed media is discarded before downloading or uploading, so spoken names cannot be discovered in directed mode. In all mode, admitted photos and voice notes are processed as context; a spoken character name also counts as addressing the bot. Pause and feature-disable settings prevent new downloads. Telegram privacy still governs whether any media update reaches the bot.

Decoded content is untrusted conversation data. OCR or voice text cannot execute commands or edit settings; only original Telegram text commands do that. Personality setup accepts text descriptions; a voice/photo reply does not become a wizard instruction. Failed conversion does not invent a history entry. Directed requests get a generic failure notice; incidental failures stay silent.

An optional separate `/search <public query>` operation remains available. The query must contain 1–400 characters and should contain only public information. The request receives only that query and a fixed search instruction: no persona, speaker IDs, group history, summaries, photos or voice transcripts are included. Search answers are delivered without adding them to conversation memory. Original Telegram commands select this route; OCR and transcription cannot invoke it. OpenAI caps hosted tool calls at one per request. Z.ai requests three short results but does not expose an authoritative internal search-call count. Returned pages remain untrusted evidence and up to three validated source links are displayed. Provider failures produce a generic failure notice.

Before allowing a search-capable request, the bot reserves one possible search call plus up to 128k search-context input tokens. Completed responses reconcile reported token usage. OpenAI search fees use actual reported tool calls. For Z.ai, the bot conservatively charges one $0.01 search-fee estimate for every search-enabled request, even if no results are returned or search was skipped; the returned-results indicator is not an authoritative call count. This is local accounting, not a provider invoice or an enforced limit on provider-internal searches. This reservation can prevent a reply near the allowance limit even when the model would not have searched; ordinary replies use the same search-capable reservation when `/web on` is enabled, and the smaller search-free reservation when it is disabled. Photo reservations allow conservatively for encoded image size and visual tokens, then reconcile reported usage. Transcription keeps a conservative charge from measured audio duration with rounding allowance. These remain local spending controls, not a guarantee about the provider invoice.

Selected media is sent to the selected provider for processing; locally retained context is text only. Telegram retains original media according to its own rules, and local deletion does not control provider-side retention. Automatic search queries and explicit public queries can be shared with search services. Automatic replies receive normal conversation context; instructions tell the model to derive minimal public search queries and omit speaker IDs and private group history. This is a model instruction, not enforced query isolation. The separate `/search` route excludes group history from the model request, but cannot remove private information a user deliberately includes in the query. Set `/web off` for groups that should never search.

API details and prices checked against official documentation: [image input](https://developers.openai.com/api/docs/guides/images-vision), [file transcription](https://developers.openai.com/api/docs/guides/speech-to-text), [web search](https://developers.openai.com/api/docs/guides/tools-web-search), [GPT Transcribe pricing](https://developers.openai.com/api/docs/models/gpt-transcribe), and [tool pricing](https://developers.openai.com/api/docs/pricing). Transcription is listed at $0.0045 per minute; hosted web search at $0.01 per call plus model tokens. Live model access, media recognition, search quality and Pi hardware performance require a separately authorized integration test.


## Review fixes: operation and configuration

Telegram polling runs independently of AI processing. `WORKER_COUNT` defaults to 2 (range 1–8), with one worker per group at a time and fair scheduling across groups. Each worker uses its own database connection and clients. `/help`, `/whoami`, `/status`, `/pause` and `/resume` are processed by the poller without waiting for an AI worker. Pause suppresses pending AI generation and delivery; an already-started provider request can finish and remain charged. Other settings commands retain group ordering. The coordinator keeps the single-instance database lock. Job locks prevent the poller and workers from processing the same job concurrently.

Telegram flood-control delivery rejections and connection failures (where the request certainly never reached Telegram) persist a retry delay and retry at most five times after the first attempt. Other groups remain eligible during a wait. Unknown delivery outcomes are still discarded rather than repeated. Jobs expire after 24 hours.

Durable token buckets limit admitted AI turns before media downloads or model generation:

| Variable | Default | Meaning |
|---|---|---|
| `USER_REQUESTS_PER_MINUTE` | 6 | User turn capacity and refill per minute |
| `CHAT_REQUESTS_PER_MINUTE` | 30 | Shared group turn capacity and refill per minute |
| `USER_MEDIA_PER_MINUTE` | 3 | User media capacity and refill per minute; group media capacity is four times this value |
| `CHAT_BUDGET_USD` | 0 | 0 divides the global lifetime allowance equally among allowed groups; a positive value sets each group's cap |
| `MEMORY_RETENTION_DAYS` | 30 | Expiry for messages and derived summaries; 0 explicitly disables expiry |
| `WORKER_COUNT` | 2 | Concurrent group workers |
| `MAX_INPUT_CHARS` | 30000 | Instructions plus conversation history per model request (10,000–500,000) |
| `MAX_TOOL_CHARS` | 40000 | Separate budget for tool results in one request: skill body, searches, reviews (5,000–500,000) |

`/help`, `/whoami`, `/status` and `/forgetme` are free but limited to five per user per minute; excess commands are dropped silently. `/forgetme` itself runs at most twice per user per hour. Admin lists from Telegram are cached for 60 seconds, so a promotion or demotion can take up to a minute to apply. If Telegram cannot be asked, settings commands reply "try again" and keep drafts. Ambient all-mode chatter uses its own group bucket, so it cannot exhaust the allowance for directed requests.

An unexpected error in one job is logged as `job_failed` and retried with growing delay; after three failures the job is dropped. A Telegram update that repeatedly fails to be accepted is skipped after three attempts (`update_failed`). Neither stops polling for other groups. Docker marks the bot unhealthy if its poll loop stops touching `/tmp/bot-heartbeat` for two minutes.

Excess turns are ignored without a failure-message flood. Silent participation checks start the incidental cooldown. Quotas survive restarts, and successful admission is recorded on a job so media conversion and recovery do not consume the same quota twice. A turn may require multiple bounded provider calls for summaries or retries; the spending caps apply to all of them. Settings controls stay usable when quotas or AI spending are exhausted. Global and chat spending reservations commit atomically, and memory reset/deletion never reset either allowance. Adding allowed groups can lower the automatic per-chat share; choose an explicit cap when needed. `/status` displays both allowances, quotas and retention.

`/forgetme` requires an identifiable sender. It removes their local source messages and draft, clears group summaries, and cancels affected pending replies. Legacy user messages without reliable sender metadata are cleared conservatively; the bot's own replies stay. Other users' attributed messages remain. Deletion does not control Telegram, provider retention, other users' quoted text, or backup copies.

Compose defaults cap the bot at 768 MiB, the tools service at 768 MiB and PostgreSQL at 512 MiB, one CPU each; PostgreSQL receives 128 MiB of shared memory. Override `BOT_MEMORY_LIMIT`, `TOOLS_MEMORY_LIMIT`, `DB_MEMORY_LIMIT`, `BOT_CPU_LIMIT`, `TOOLS_CPU_LIMIT` and `DB_CPU_LIMIT` for the host. Docker build context uses an explicit file-glob allowlist, and `.env*`, credential files and caches are excluded at any depth. Base images are pinned to multi-arch (amd64/arm64) digests and Python packages are installed with `--require-hashes`; update both deliberately.

The offline regression suite covers revoked persisted jobs, deferred delivery, idle ID rollover, legacy migrations, topic isolation and group-wide limit changes, retention/deletion, spending ledgers, per-chat budgets, rate buckets, worker fairness, job locking and responsive controls. Current and legacy archive restoration has also been verified with synthetic PostgreSQL dumps, including spending floors, ownership, migrations and replay cancellation. No test establishes live provider access or authorizes starting the production bot.

Startup events identify the current tool path with `explicit_search_available` (true for Z.ai) and `json_repair_mode="format_only"`. Incomplete generation logs include allowlisted `finish_reason`, optional `incomplete_reason`, token usage and output length without response text. For example, `length` indicates the provider stopped at its output limit; an empty `stop` response is a different failure. These diagnostics describe provider-reported termination, not an inferred cause.

Tool-call rejection logs include `tool_call_count`, an allowlisted `requested_tool` when a single call is available, `argument_chars` when applicable, and `tool_error`: disabled tools, unknown tool, missing or duplicate call ID, wrong argument type, excessive argument length or malformed/invalid arguments. Unknown tool names are reported as `other`; raw names/arguments are not logged. Every valid repeated `load_skill` call reuses the already loaded body (`skill_reused`), without another file read. Z.ai instructions allow multiple function calls and distinguish the explicit `search_web` function from native `web_search`.

The handler also accepts batches of allowed function calls in a single model response (`tool_batch_received`). At most four calls per response run; each extra call receives a short `too_many_calls` result instead of a search or browser read. It validates all names, IDs, argument schemas and tool availability before any skill read or explicit search, then executes the calls sequentially. Duplicate call IDs fail safely. The original assistant tool-call message is replayed once, followed by one result for each call ID; the next generation receives all results together. Normal pacing and spending limits still apply. Tool results count against `MAX_TOOL_CHARS`, not the conversation limit; when the next result would not fit, it is replaced with a short `context_budget` notice and research ends with a tools-disabled final answer instead of a failed reply. Calls within a batch do not consume additional model rounds; even the final allowed round processes its entire valid batch before the research round limit (`tool_round_limit_reached`). The bot then requests a final answer with all collected tool results, the original schema and tools disabled. This additional request has normal context/budget checks and is logged as `ai_final_answer_started`, `ai_final_answer_completed` or `ai_final_answer_failed`; JSON validation and the three format repairs still apply.


### Structured search

Both providers receive the same local function tools in the same order: `load_skill`, `search_web`, `read_yandex_maps_search`, `read_yandex_reviews`, `read_afisha_listing`, `read_afisha_event`, `route_link`. OpenAI's hosted search and Z.ai's native in-chat search are no longer offered inside the conversation, so switching models changes neither the tool loop nor the skills.

At most five `search_web` calls run per request (extra calls receive `search_limit`), and the model is told when it is on its last research step. The plain-text "Sources:" block lists only results the answer actually mentions (by URL or domain) plus Maps/Afisha pages the bot opened, never the first results of every search. **Links in plain answers are placed by the model.** With `/web on`, it is told it may link a place, event or page inline as `[name](url)` wherever that helps (for example the Yandex Maps card of a place to walk to), using the exact URL from a tool result. Every link is then checked: a URL that appeared neither in this request's tool results nor anywhere in the conversation history is removed. A Markdown link keeps its label; a bare URL is deleted. `reply_links_checked` logs `kept_links` and `dropped_links`. The Sources block skips sources already linked inline. Without `/web on` there are no tools and links are not checked. Instructions forbid promising a later search or follow-up: the final step must answer now or state what could not be found.

`glm-5.3-flash` reasons inside its `max_tokens`, so it always runs with `reasoning_effort=low` and an extra 2,000-token allowance above the reply budget; reservations include the allowance. Without it, a 600-token reply budget could be spent entirely on reasoning, producing an empty answer.

`search_web({"query", "site", "recency"})`: `site` is `afisha.yandex.ru`, `restoclub.ru`, `afisha.ru`, `daily.afisha.ru`, `t-j.ru` (editorial guides), `yandex.ru/maps` (last resort; Maps search is better) or null; `recency` is `day`, `week`, `month`, `year` or null. On Z.ai these map to `search_domain_filter` and `search_recency_filter`. Z.ai documents those filters under a different engine name, so if a filtered request is rejected (HTTP 400) it is retried once as a plain `site:` query and logged with `filters_applied=false`. On OpenAI, `search_web` runs a separate small Responses request with the hosted `web_search` tool, `filters.allowed_domains`, an approximate Moscow `user_location`, and a recency hint; it returns the cited URLs with their cited sentence as the snippet, then other consulted sources. `yandex.ru/maps` cannot be path-filtered, so the query also names Yandex Maps. Both use the same API key and billing as the selected provider. Whether Z.ai accepts the filters, and the exact OpenAI search output, are mocked in tests and have not been verified live.

### Route links

`route_link({"origin", "destination", "mode"})` builds a Yandex Maps link that opens a ready route in Moscow: `mode` is `walk` (`rtt=pd`), `transit` (`mt`) or `car` (`auto`). Each point is either `"lat,lon"` copied from a Maps search result (every result now has `coordinates`) or an address as the person wrote it, prefixed with «Москва, » when no city is given. The link is built locally (`src/routes.py`): no request, no API key, no cost. Yandex computes the real path, time and distance when the link is opened; the bot computes none, and the model is told never to state travel minutes or distance itself. Swapped `lon,lat`, points outside Moscow, links, `~`/`&` and identical points come back to the model as fixable errors naming the field. Letters, including Cyrillic, stay readable in the link; only spaces, brackets and URL syntax are escaped. That keeps a two-address route around 150 characters instead of 450. A built link counts as known for the link check, and links are compared with `%`-escapes decoded, so a route copied either way is kept. Links up to 2,000 characters are matched and checked; the earlier 400-character limit cut long route links in half and left their tail in the reply. Logs: `route_link_created` / `route_link_failed`, without addresses. Whether Yandex resolves every typed address in `rtext` was not live-tested; coordinates are exact.

Maps results report `rating: null` when Yandex sends `ratingValue: 0`: checked on a live search page, Yandex uses 0 with real rating counts to mean "no score shown", not an average of zero.

### Yandex Maps search

`read_yandex_maps_search({"query", "exclude_chains"})` reads `https://yandex.ru/maps/213/moscow/search/<query>/`, which yandex.ru/robots.txt allows. The query is a short plain-text phrase such as "кофейня Никольская"; links, paths and control characters are refused. The page is server-rendered, so the tools container makes one plain HTTPS GET with no browser. It refuses redirects (a CAPTCHA redirect is reported as `captcha`), ignores proxies and caps the response at 4 MB. It shares the one-at-a-time slot with the browser readers.

It returns up to 10 organizations, each with:
- `url`, the place page for people (map, route, hours), which is what answers should link, and `reviews_url`, the same card's reviews tab for `read_yandex_reviews` (the reader accepts either); address and nearest metro with distance;
- rating, rating count and review count;
- hours and open-now status, and whether it is permanently closed;
- up to 10 features, decision signals first (coffee roasting, terrace, music, laptop-friendly, step-free access);
- per-topic review sentiment counts (e.g. drinks, atmosphere);
- chain status.

A venue is treated as a chain when Yandex links it to a chain or it appears at more than three addresses in the results. With `exclude_chains: true`, chains are removed in code before the model sees the results, and their names are reported. Any recommendation card naming an excluded chain is then dropped from the reply (`dropped_items` in `recommendations_rendered`). Review excerpts and reviewer IDs embedded in the page are never returned. At most three searches run per request, and repeated queries are cached.

The JSON shape is undocumented and may change (`page_changed`). Volume can trigger CAPTCHAs. The extractor was checked offline against one saved search page; live fetching from the tools container has not been tested.

### Recommendation cards

Once a skill is loaded in a normal reply (not a JSON decision), the answer must be a `place_recommendations` JSON object: `intro`, up to five `items` (`name`, `address`, `rating`, `when`, `text`, `links`), up to three `unconfirmed` entries (`name`, `note`) and `outro`. `address`, `rating` and `when` are facts from tool results or null; `text` is free Markdown written by the model in the persona's voice, with its own layout and emoji (why it fits, price, caveats, what a near-miss relaxes). OpenAI receives it as a strict schema (string length limits are enforced locally instead); Z.ai receives JSON mode plus the schema in its instructions and may omit `unconfirmed` and `outro`. Invalid output gets up to two format-only repairs with tools disabled; if those fail, a plain-text answer is still delivered, while broken JSON fails safely.

Each card shows a bold name, then `Адрес:`, `Рейтинг:` and `Когда:` lines (only those that are known), the model's text and the card's links. There are no fixed emoji rows. A place becomes a card only with an address and at least one link seen in tool results; otherwise it is listed under «Не удалось подтвердить:» with its note and no links, and the user can ask in plain text to check it. Cards are produced only once a skill is loaded: the skill defines that shape. Using a Maps or Afisha reader without `load_skill` (for example a quick "where is the nearest toilet" lookup) gives a short plain-text answer with the usual Sources block. The chain filter applies to cards and unconfirmed entries; follow-up buttons appear only when at least one card is shown.

All replies are sent with `parse_mode=HTML` (`src/telegram_format.py`). Text is escaped first, then the model's light Markdown becomes Telegram formatting: `**bold**`, `*italic*`/`_italic_`, `~~strike~~`, `` `code` ``, headings as bold lines and `- ` bullets as `•`. Markdown links render as `label (url)`, so a label never hides its URL. If Telegram rejects the markup, the same text is resent as plain text (`delivery_failed` with `reason=formatting_rejected`). Facts stay on one line, and lines in the model's text that imitate a card heading, a fact label or the unconfirmed heading are neutralised, so page text cannot fake extra cards. **Links are grounded**: a card link, or a URL written anywhere in the intro, card text, notes or outro, survives only if it appeared in a tool result during the same request (a Markdown link keeps its label; a bare URL is removed). Long answers drop trailing cards rather than cutting a card. `recommendations_rendered` logs `result_count` (cards shown), `dropped_links`, `dropped_items` (excluded chains), `demoted_items` and `unconfirmed_count`.

### Events from Yandex Afisha (Moscow)

`read_afisha_listing({"category", "date_from", "days"})` opens `https://afisha.yandex.ru/moscow/<category>?date=…&period=…` (categories `theatre`, `concert`, `standup`, `art`, `kids`, `excursions`, `festival`, `show`; 1–7 days from today up to 180 days ahead). The URL is rebuilt from validated fields. It returns up to 15 events in page order: title, type, date text, venue, "from" price, rating and canonical event URL. `read_afisha_event({"url"})` accepts only `https://afisha.yandex.ru/moscow/<type>/<slug>` (queries and fragments dropped). It returns the venue, address, metro, run dates, duration, age rating, price range and availability from schema.org JSON-LD, plus up to 10 sessions shown on the page. At most two listings and three event pages are read per request, and repeated targets are cached.

The readers run in the tools container with the same hardening, one-browser limit and request filtering as the reviews reader. Paths that afisha.yandex.ru/robots.txt disallows for all crawlers (`/api`, `*search*`, `eventId=`, `utm_source`, selections, venue pages and others) are rejected before any browser starts. Extraction uses `data-test-id` hooks and JSON-LD rather than generated class names. It was checked against one saved public listing page and one event page (offline, in the hardened container); comments and their authors are never read. This is an unofficial DOM integration: markup changes, CAPTCHA or blocking return `status=unavailable` with a reason, and the model is told not to invent events. Only Moscow is supported.

### Reading Yandex Maps reviews

With `/web on`, both providers receive `read_yandex_reviews({"url":"https://yandex.ru/maps/org/123456/reviews/"})`. The model finds a specific organization card, calls the reader, verifies its name/address and analyzes the returned texts. This is browser DOM reading rather than search snippets. The result contains `status`, `reason`, canonical `url`, `company` (name, address, rating, separate rating/review counts when available), `reviews` (text, date, rating, truncation flag), `returned_count`, fetch time and `sampling=page_order`. Unknown fields stay null. Up to 20 distinct written reviews and 4,000 text characters are returned after bounded scrolling; links inside page text are replaced with `[link removed]`. At most three cards are read per request, and repeated reads of the same card reuse the first result. Samples are not guaranteed newest, exhaustive or representative. No author/profile information is returned.

Custom tools run in a separate `tools` service (`tools/`), not in the bot. It contains Playwright and Debian Chromium for amd64/arm64 and receives no environment secrets: no Telegram token, provider key or database password. It sits on its own egress network plus an internal network shared only with the bot, so it cannot reach PostgreSQL. The bot calls `POST /v1/yandex-reviews` at `TOOLS_URL` (`http://tools:8080` in Compose); if the service is down, the tool reports `browser_unavailable` and replies continue. No additional API key or reader API fee is used. Review texts sent back to the model consume normal input tokens.

The service runs one browser at a time; a second request waits up to 25 seconds, then receives `busy`. Browsers start only for a reader call, use a temporary anonymous context and a minimal environment (`HOME=/tmp`), and close afterwards. The tools container is read-only, non-root, drops all capabilities and has a 256 MiB `/tmp`; Chromium runs without its own sandbox inside it. Rebuild with `docker compose up -d --build`; the bot image no longer contains Chromium.

Only validated HTTPS Yandex organization cards are accepted: `/maps/org/[name/]<id>/`, regional `/maps/<region>/<city>/org/...` and `/maps/?oid=<id>` on yandex.ru, .com, .kz or .by. Query parameters are removed. Short `/maps/-/` links and other URLs are returned to the model as `invalid_url` so it can retry with the full card; they no longer fail the reply. Browser requests are restricted to Yandex/CDN domains, with ad, click-redirect and metrics hosts blocked. Downloads, service workers, WebSockets and WebRTC are disabled. Authentication, CAPTCHA bypass and stealth are not implemented. A 40-second reader timeout, CAPTCHA, HTTP rejection, redirect to a different organization, missing content or browser failure returns `status=unavailable` to the model so it can disclose the limitation and continue answering. This is an unofficial DOM integration: markup changes can require selector updates. Actual Maps access has not been live-tested during development.

Logs contain `reviews_read_started`, `reviews_read_completed` or `reviews_read_failed` with a local `reviews_request_id`, count of returned reviews, duration and an allowlisted reason. They never contain URLs, texts, names, HTML or browser exception messages. The source of the DOM selector conventions is [ya-reviews-mcp](https://github.com/mrkhachaturov/ya-reviews-mcp); its MIT notice is retained in `src/licenses/ya-reviews-mcp.txt`.
