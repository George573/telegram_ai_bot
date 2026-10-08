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

Z.ai supports text, photos and voice notes. Text requests, including summaries and personality inference, use `ZAI_MODEL`; photo descriptions use the explicitly configured `ZAI_VISION_MODEL` so structured text decisions keep the text API format. Thinking is disabled except for `glm-5.3-flash`, which requires enabled thinking according to [Z.ai documentation](https://docs.z.ai/guides/vlm/glm-5.3-flash). Structured requests on `glm-5.3-flash` use `reasoning_effort=low` and a separate final system instruction requiring a schema instance rather than the schema itself. Output remains bounded; reasoning can still consume the token allowance before a visible answer is produced. JSON mode includes the required schema in the system instructions; the existing local validators reject malformed decisions/settings before use. [Z.ai API reference](https://docs.z.ai/api-reference/llm/chat-completion).

For photos, the paid default is `glm-4.6v`: $0.30 input / $0.90 output per million tokens. Lower-cost alternatives are `glm-4.6v-flashx` ($0.04 / $0.40) and `glm-4.6v-flash` (currently free). The adapter sends a JPEG as an in-memory Base64 image, capped at 5 MiB or your smaller media limit. It never sends Telegram download URLs. Reservations and reconciled usage use the **vision model's rates**, independently of the text model. [Vision models](https://docs.z.ai/guides/vlm/glm-4.6v), [pricing](https://docs.z.ai/guides/overview/pricing).

Voice uses Z.ai's `glm-asr-2512` transcription endpoint with the same Z.ai key. Telegram OGG audio is decoded to mono WAV in memory. Z.ai accepts at most **30 seconds**, so the effective limit is the smaller of 30 and `VOICE_MAX_SECONDS`. Longer notes are rejected, not truncated or split. Both Telegram metadata and decoded audio duration are checked before upload. The local reservation uses Z.ai's published approximate $0.0024/minute equivalent, plus one second and a 25% allowance; actual provider billing is token-based ($0.03 per million tokens), so this is an estimate, not invoice reconciliation. Failed requests keep their reservations. [Audio API](https://docs.z.ai/api-reference/audio/audio-transcriptions), [pricing](https://docs.z.ai/guides/overview/pricing).

With Z.ai selected, `/images` and `/voice` work normally. `/status` shows the vision model and effective voice limit. Web search is available through the native Z.ai search-in-chat tool; `/web on|off` controls permission per group. Requests use `search-prime`, ask for up to three short results, and display validated source links. [Z.ai search](https://docs.z.ai/guides/tools/web-search). Text, selected media and stored summaries are sent to whichever provider you explicitly select; review its data policies and tell group members before switching.

A $5 account top-up is separate from `BOT_BUDGET_USD`. You can set that local allowance to `5`, but it is a lifetime cap that includes previously tracked spend, not a fresh $5 balance. The bot does not query your provider balance or reset spend when switching providers.

Existing explicit `ZAI_MODEL` and `ZAI_VISION_MODEL` values override the new defaults. To upgrade an existing installation, set `ZAI_MODEL=glm-4.7` and `ZAI_VISION_MODEL=glm-4.6v` in your private configuration locally; it is not read or modified during development. Voice already uses the paid `glm-asr-2512`; the current Z.ai audio API does not list a higher-tier transcription alternative. Paid model selection does not guarantee valid JSON or avoid rate limits, so pacing, validation and bounded retries remain enabled.

Recreate the bot deliberately after changing local configuration. This can make billed API requests. No live provider access or reply quality has been verified by mocked tests.

## Commands

| Command | Who | Purpose |
|---|---|---|
| `/help` | Everyone | Usage and applicable commands; no AI request or memory entry |
| `/chat <text>` | Everyone | Request a reply |
| `/whoami` | Everyone | Show your Telegram user ID; no AI request |
| `/personality [description]` | Settings manager | Describe a character, review inferred settings, then save or cancel |
| `/setprompt <instructions>` | Settings manager | Replace persona and clear all conversation memory |
| `/context directed` | Settings manager | Use only conversations addressed to the bot; clear memory |
| `/context all` | Settings manager | Use general group text as context; clear memory |
| `/historylimit 10..500` | Settings manager | Override the retained message count for that group |
| `/resetmemory` | Settings manager | Remove recent messages and summary |
| `/pause` | Settings manager | Stop new conversation collection and AI replies |
| `/resume` | Settings manager | Resume operation |
| `/status` | Settings manager | Settings, retained count, summary presence, model and tracked allowance |
| `/images on\|off` | Settings manager | Enable/disable photo understanding in this group |
| `/voice on\|off` | Settings manager | Enable/disable voice transcription in this group |
| `/web on\|off` | Settings manager | Allow/disallow automatic web search for replies |

Directed mode replies only when addressed. All mode also lets the model decide whether to join other group conversations. Settings managers are identifiable Telegram group admins and configured bot super admins. Group-admin privileges are verified with Telegram for every settings operation; super admins are matched by numeric user ID. Anonymous senders cannot use either role because the person cannot be identified. Failed group-admin verification denies access unless the sender is a configured super admin. The model cannot authorize commands, execute code or change configuration. Its only optional tool is hosted read-only web search, capped at one call per reply. Only allowed group IDs are processed. Service events, other bots, unsupported media, channels, private chats and oversized messages are ignored.

## Memory and restart behavior

Each chat has its own persona, mode, pause setting, count limit, recent history and rolling summary. User and delivered bot messages count toward the limit. Long content also triggers compaction before it exceeds the model context budget. Summaries preserve useful facts, running jokes and unfinished topics; they are lossy and contain personal data from the source messages. No expiry is applied by default, as agreed for conversation continuity. Tell group members which context mode is used and that selected text and summaries are sent to the selected AI provider.

Older messages are replaced only after every required summarization batch succeeds. Saving a summary and deleting its source rows is transactional. A failed compaction leaves source history intact and rejects that new conversation item; it does not silently exceed the limit. Failed history-limit changes leave the prior limit intact. Summary/source changes can commit before the final limit update, but information remains represented and the setting is never lowered ahead of compaction.

Telegram offsets and a small normalized in-flight job are saved together before acknowledgement. Jobs are deleted after processing. The database prevents two bot instances from working on the same state. A reboot resumes prepared work and generated replies. Persona and history remain persisted.

Delivery is not exactly-once: if the process crashes during a send, its outcome may be unknown. Such sends are not automatically repeated and are not added to assistant history. A generated reply that has not entered delivery can be resumed without another model call. A crash during a model request can require regenerating a reply; the spending reservation is retained conservatively. Downtime causes a response gap. Telegram retains pending updates for at most 24 hours. Deleted group messages are not automatically deleted from the bot's local history; `/resetmemory` clears both messages and summary.

Topics within a group currently share that group's memory, while replies retain the original topic. Separate-topic personas are not supported in this version.

## Spending

The model default is `gpt-6-luna`; an explicit `OPENAI_MODEL` in your private file overrides it. `gpt-5.6-luna` is also supported. These are the OpenAI adapter models; Z.ai selection and rates are documented above. Other models are rejected until their cost accounting is implemented. No OAuth/subscription fallback is used. GPT subscription billing does not pay for these API calls.

`BOT_BUDGET_USD` is a lifetime allowance shared across all groups and persisted through reboots. The default is $10. It includes summarization, photo descriptions, voice transcription and web search. Before requests, the bot reserves a conservative estimate using UTF-8 input size, known model prices and maximum output tokens. On success it reconciles against reported usage, conservatively pricing all input at the cache-write rate. Failed/unknown requests keep their reservations. This can stop the bot before the real invoice reaches $10. It is not an authoritative invoice or a guarantee against provider price changes. Only optional hosted web search is enabled for conversational replies; reasoning is set to none; output tokens, input length and incidental reply frequency are bounded. Raising the allowance requires editing `.env` and recreating the bot. Memory reset does not reset spend.

Also configure a provider-side hard project spend limit and disable automatic prepaid recharges. OpenAI enforcement may lag. Dashboard alerts alone do not stop requests.

## Secrets and logs

The assistant must never read `.env`, credential files, resolved Compose configuration or container environment. The setup helper and running applications may consume credentials locally but never return them to the assistant. `.env`, credentials, backups and work files are excluded from Git and Docker builds. No message content or raw HTTP exceptions are printed. Host administrators and Docker administrators can access runtime credentials. Protect the Pi's OS and backup media; PostgreSQL storage is not encrypted by this project.

To rediscover IDs, with the live bot stopped:

```sh
python3 scripts/discover_chat.py
```

This uses only Telegram, prints group IDs, and reads pending updates without acknowledging them. If there is no group update, send `/help` to the bot in the group and rerun it. The verified ID for the current group is `-882012764`.

## AI request pacing and retries

`API_MIN_INTERVAL_SECONDS=0.25` adds a quarter-second minimum gap between completion of one AI request and start of the next. The same clock covers text, photos, transcription, summaries, personality inference and failed attempts in one sequential bot instance, on either provider. The first request is immediate. Time already spent on other work or retry backoff counts toward the gap. Set any finite value from 0 to 30 seconds; 0 disables pacing. This is separate from Telegram polling and `REPLY_COOLDOWN_SECONDS`, which controls incidental participation. Restart/recreate the bot after changing local configuration; the in-memory pacing clock restarts with it.

Z.ai temporary HTTP 429 errors (provider codes 1302/request limit and 1305/overload, or a gateway 429 without a code) get at most two retries, using `Retry-After` when available or short exponential backoff with jitter. Individual retry waits are bounded at 30 seconds. Longer requested cooldowns are not shortened: the request fails without retry. Balance, access, long-term quota, unknown provider-code errors and uncertain network failures are not retried. Each retry reserves its own conservative budget estimate; earlier failed reservations are retained. There is no automatic model/provider fallback. [Z.ai error codes](https://docs.z.ai/api-reference/api-code).

Pacing can reduce bursts but does not guarantee compliance with account-specific limits. Check your [Z.ai model rate limits](https://z.ai/manage-apikey/rate-limits) if 429s persist. Participation parsing accepts a single complete JSON Markdown fence or leading Unicode BOM, then applies the same strict schema checks. It does not extract objects from surrounding prose, remove thinking blocks, or repair JSON syntax. Parser failures log fixed error labels, numeric positions and format hints without output content.

Participation decisions use an explicit JSON-only prompt with examples for replying and staying silent. Persona/language/style applies inside `reply`, while the outer object must contain exactly `respond` (boolean) and `reply` (string). Logs distinguish malformed JSON, a non-object result, missing/extra/duplicate fields, wrong types, empty required replies, nonempty silent replies, and oversized replies without logging model output.

An invalid decision gets **one corrective generation request** with the fixed validation category's guidance. Rejected output is not copied into the correction prompt or stored as a reply. The correction uses the same provider/model and conversation, passes through normal pacing and budget reservation, and does not enable another web search. Normal temporary-429 retries still apply independently to each generation. If validation fails twice, the all-mode job ends silently; no invalid decision is delivered. Network errors and personality validation failures do not trigger decision correction. Every completed attempt still counts toward the spending allowance.

## Typing indicator

While generating a reply, processing selected media, summarizing memory for a conversation, or inferring personality, the bot sends Telegram's `typing` chat action and refreshes it every four seconds. Forum topic IDs are preserved. This runs in a short-lived background thread so it covers blocking provider requests, retries, and pacing waits. Each chat-action request has a three-second timeout; failures are logged without raw exceptions and never prevent the AI operation. Refresh stops when the operation finishes. Telegram clears typing when a bot message arrives, or within five seconds of the final action. All-mode decision requests can show typing and ultimately choose silence. The indicator does not add a message to history. [Telegram chat actions](https://core.telegram.org/bots/api#sendchataction).

Reducing pacing does not shorten provider inference or remove decision retries. Existing explicit `API_MIN_INTERVAL_SECONDS` values in your private configuration override the new default. Set `API_MIN_INTERVAL_SECONDS=0.25` locally and recreate the bot to change an existing installation.

## Logging

The bot writes one JSON event per line to stdout at INFO level, with warnings/errors for failed operations. Read them with `docker compose logs --tail 100 bot`. The existing Docker local logging driver rotates logs at 5 MiB, retaining up to two files.

Events cover startup/shutdown and fatal-error stage, failed Telegram polling, durable job phases, reply generation/delivery, unknown delivery after restart, media/settings/memory failures, and spending-limit rejections. AI events include provider, model, operation, elapsed milliseconds (including pacing/retry waits), reported token usage, and local reserved/charged USD amounts. Missing usage keeps the reservation; transcription costs are estimates. These amounts are local budget accounting, not provider invoices. Telegram update IDs connect job events without logging chat or user IDs. Successful empty polling does not produce a log entry.

Only fixed event names and allowlisted metadata are emitted. Credentials, configuration dumps, Telegram URLs, prompts, conversation/reply content, media bytes, provider response bodies, and raw exceptions/tracebacks are excluded. OpenAI/HTTP client logging is suppressed to prevent SDK debug output from exposing request data. No credentials or conversation data are needed to diagnose ordinary request failures; safe numeric HTTP status codes and allowlisted Z.ai error codes are recorded when available. Pacing/retry events show delay milliseconds and retry attempt numbers. `ai_output_invalid` records an allowlisted `validation_error` category; `ai_decision_retry` identifies the single corrective decision request.

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

Backup files are owner-only PostgreSQL custom-format dumps under ignored `backups/`. They contain conversation data. Keep a protected copy off the Pi and periodically test restoring it. Restore into an empty database while the bot is stopped; this is an explicit destructive operation, never an automatic task:

```sh
docker compose stop bot
docker compose exec -T db pg_restore -U postgres -d roleplay --clean --if-exists < backups/your-backup.dump
docker compose start bot
```

The schema initializes transactionally and adds the current columns/indexes idempotently. Database format version is recorded. Future non-additive migrations must be implemented and tested before updating. A database initialized by the old starter using a different database owner needs a manual role migration; do not delete its volume to work around authentication without first backing it up.

On the Pi, clone the repo, transfer `.env` over SSH, restore a backup if moving history, and build locally with `docker compose up -d --build`. Stop the PC bot before starting the Pi bot. The image builds for the host architecture. Pi hardware execution is not yet verified.

## Tests

See `./scripts/test.sh`. Tests use an explicit synthetic environment and isolated PostgreSQL database; never the private `.env`. The test database uses temporary memory storage and is removed afterward. No Telegram or AI provider requests occur. Coverage includes permissions, anonymous admins, chat isolation, privacy filtering, memory/reset/summary failures, limit-change failures, delivery recovery, deduplication, persistence, spending reservations and single-instance locking.

Telegram ID discovery has been verified live. OpenAI model access and role-play quality need a separately authorized live test after configuration. Do not claim those are verified by the mocked tests.

In `all` mode, eligible group messages now receive a model decision: reply or stay silent. Decision and reply use one structured-output request; silent decisions still cost tokens. Direct mentions, configured names, replies and `/chat` receive a reply subject to pause and budget limits. The cooldown applies only to unaddressed replies; their messages still enter all-mode memory. Incidental failures remain silent. Directed mode still discards unrelated messages.

## Personality setup

Send `/personality` and reply to the bot with an ordinary description in your preferred language. You can also send everything in one message:

```text
/personality Ты — теплый, гостеприимный армянский друг. Откликайся на Армен, Ара и Карен. Часто вступай в разговор. Пиши эмоционально, с легким юмором и уважением, по-русски с армянским сленгом: джан, апер, барев. Любишь хорошую еду, дружбу и семью.
```

The model infers names, eagerness, speaking guidance, style, lore and language and shows a preview. No settings change yet. Send `/personality save` (or reply `да`) to apply it, `/personality cancel` to discard it, or reply with a revised description. `/personality show` displays the saved settings. `/personality reset` previews the defaults for confirmation. No field syntax, quotes or pipes are required; pasted descriptions with those constructs are interpreted as text. Numeric eagerness in a description is mapped to qualitative guidance; it is not a response probability.

Inference uses one bounded AI request and the existing spending allowance. Viewing, saving and cancelling do not call the AI provider. Drafts belong to one user in one group, survive restarts and expire after 30 minutes; expired drafts are removed on the next draft lookup. While a draft is open, only replies to the bot from that user enter the setup flow. Ordinary messages keep their normal behavior. Descriptions are not added to conversation memory. Only the inferred settings are retained as a draft after its job finishes. Permission is checked again before each setup step or save. Saving invalidates other people's pending previews for that group.

Changing lore on save clears history and summary to avoid conflicting character facts. Other personality edits retain conversation. Nonempty character lore is the sole identity sent to the model; otherwise the raw `/setprompt` persona is used. Configured names are aliases for that character. `/setprompt` replaces the active identity by clearing structured lore, retaining other character settings, and cancelling pending personality drafts.

Eagerness is `quiet`, `balanced` (default), or `chatty`; it guides the model. It does not bypass context mode, pause or budget. Cooldown limits only unaddressed participation. Use `/context all` if the character should consider joining general group chatter. Named mentions count as directed messages. Names match whole names without case sensitivity. Telegram must deliver messages: disable privacy mode via BotFather for plain-name recognition, then re-add the bot if needed. `/status` shows the BotFather privacy setting reported at startup; a group-admin bot can receive messages even with that global setting enabled. A bot cannot match character names in messages Telegram never delivers. Directed mode discards other messages.

## Bot super admins

Send `/whoami` in the approved group to see your numeric Telegram user ID. Add trusted user IDs to your private `.env`, for example:

```dotenv
BOT_SUPERADMIN_IDS=123456789,987654321
```

Recreate the bot after changing this value. Those users can manage all chat settings without Telegram group-admin status, including personality, prompt, context, memory, pause and resume. Existing identifiable group admins also retain access. Super admins cannot operate outside `ALLOWED_CHAT_IDS`, change credentials or the owner's spending allowance through chat, or be inferred/granted by the model. Usernames and forwarded text do not grant access. Membership in this list can only be changed in local configuration. An empty list adds no super admins. The assistant does not read or update your private `.env`.

Settings and drafts are stored in local PostgreSQL through automatic additive migrations. Model extraction and reply decisions use [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), validated locally before application.

If an older version replied with the wrong character, use `/resetmemory` after updating to remove that reply from context. The saved personality remains intact. The current character identity also takes precedence over conflicting identities in past conversation. Telegram delivery rules are documented under [privacy mode](https://core.telegram.org/bots/features#privacy-mode).


## Photos, voice and web search

All three features are enabled by default and can be switched off per approved group with `/images off`, `/voice off` or `/web off`. `/status` displays their saved settings. Group admins and bot super admins manage these switches. Rebuilding automatically adds the required columns without deleting history or personalities.

Photos sent as Telegram photos are described once using the selected chat model on OpenAI or `ZAI_VISION_MODEL` on Z.ai. The largest Telegram photo variant no larger than 1280 pixels on either side is used with low-detail vision. Captions and descriptions become text in context and rolling summaries. Small text and fine image detail may be missed; descriptions can be inaccurate. Image documents, videos, stickers and albums as one combined object are not supported; album photos are processed individually.

Voice notes in OGG format are converted to mono WAV with FFmpeg through memory pipes, then transcribed with `gpt-transcribe` on OpenAI or `glm-asr-2512` on Z.ai. The original language is retained. FFmpeg receives no credentials in its environment, accepts only pipe input, has a fixed argument list and a 20-second conversion timeout. Actual decoded duration is checked as well as Telegram metadata; overlong notes are rejected rather than silently truncated. Transcripts become text in context and summaries. Transcription can be inaccurate.

Media defaults: 5 MiB per download and 120 seconds per voice note on OpenAI; Z.ai caps photos at 5 MiB and voice at 30 seconds. Override `MEDIA_MAX_BYTES` (1024–10485760) and `VOICE_MAX_SECONDS` (1–300) in local configuration if needed. Downloads are streamed from Telegram's fixed API host, bounded even without a content length, and reject redirects and unsafe file paths. Bytes are never written to local disk, PostgreSQL or logs, and Telegram token-bearing download URLs are never sent to the selected AI provider. In-flight jobs briefly retain the Telegram file ID needed for recovery; successful conversion replaces it with text before context processing. Reboots after conversion reuse that text. A crash during conversion can cause another billed request; reservations are retained conservatively.

In directed context mode, photos need an addressed caption, `/chat` caption or reply to the bot; voice needs a reply to the bot. Unaddressed media is discarded before downloading or uploading, so spoken names cannot be discovered in directed mode. In all mode, admitted photos and voice notes are processed as context; a spoken character name also counts as addressing the bot. Pause and feature-disable settings prevent new downloads. Telegram privacy still governs whether any media update reaches the bot.

Decoded content is untrusted conversation data. OCR or voice text cannot execute commands or edit settings; only original Telegram text commands do that. Personality setup accepts text descriptions; a voice/photo reply does not become a wizard instruction. Failed conversion does not invent a history entry. Directed requests get a generic failure notice; incidental failures stay silent.

For replies, the model can choose hosted web search when current or uncertain public facts are needed or someone requests a lookup. Ordinary roleplay should not trigger it. OpenAI caps hosted tool calls at one per response, with low search context. Z.ai uses its native search-in-chat integration with three short results requested; it does not expose OpenAI’s tool-call cap or an authoritative search-call count. No local browser, shell, code interpreter, custom tool or URL-fetching tool is exposed to the model. Pages are treated as untrusted evidence. Answers display up to three safe citation links returned by the API; these links and the answer persist as normal assistant text. Provider failures return the usual failure notice instead of an unverified fallback answer. Disabling web leaves media processing and normal roleplay available.

Before allowing a search-capable request, the bot reserves one possible search call plus up to 128k search-context input tokens. Completed responses reconcile reported token usage. OpenAI search fees use actual reported tool calls. For Z.ai, the bot conservatively charges one $0.01 search-fee estimate for every search-enabled request, even if no results are returned or search was skipped; the returned-results indicator is not an authoritative call count. This is local accounting, not a provider invoice or an enforced limit on provider-internal searches. This reservation can prevent a reply near the allowance limit even when the model would not have searched; `/web off` permits ordinary replies with smaller reservations. Photo reservations allow conservatively for encoded image size and visual tokens, then reconcile reported usage. Transcription keeps a conservative charge from measured audio duration with rounding allowance. These remain local spending controls, not a guarantee about the provider invoice.

Selected media is sent to the selected provider for processing; locally retained context is text only. Telegram retains original media according to its own rules, and local deletion does not control provider-side retention. Search queries can be shared with search services; the prompt instructs the model to minimize them and omit private history and speaker IDs, but this is model guidance rather than a technical redaction guarantee. Set `/web off` for groups that should never search.

API details and prices checked against official documentation: [image input](https://developers.openai.com/api/docs/guides/images-vision), [file transcription](https://developers.openai.com/api/docs/guides/speech-to-text), [web search](https://developers.openai.com/api/docs/guides/tools-web-search), [GPT Transcribe pricing](https://developers.openai.com/api/docs/models/gpt-transcribe), and [tool pricing](https://developers.openai.com/api/docs/pricing). Transcription is listed at $0.0045 per minute; hosted web search at $0.01 per call plus model tokens. Live model access, media recognition, search quality and Pi hardware performance require a separately authorized integration test.
