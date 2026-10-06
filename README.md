# Telegram roleplay bot

A text-only group bot: Python, OpenAI Responses API, and PostgreSQL in two Docker Compose containers. PostgreSQL uses a named volume. Supports amd64 PCs and arm64 Raspberry Pi 4 with a 64-bit OS.

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

3. In BotFather, use `/setprivacy` to disable privacy for this bot if you want both context modes and ordinary mentions. Remove and re-add it if Telegram requires that for the privacy change. Do not grant the bot group admin privileges. The bot receives group text from Telegram and discards unrelated text in directed mode before storing it or calling OpenAI. General group text is not delivered reliably with privacy enabled.
4. Start deliberately when ready for separately billed OpenAI requests:

```sh
docker compose up -d --build
```

5. In the group, use `/help`, then `/setprompt You are our grumpy family cat. Reply briefly and make playful cat jokes.` as an identifiable group administrator.
6. Talk with `/chat hello`, mention the bot, or reply to its message. With multiple bots, target `/help@YourBotUsername` and `/chat@YourBotUsername hello`.

No ports are published. Only the bot has an outbound network. The database has an internal network shared with the bot. The bot runs as a non-root user with a read-only root filesystem. Linux Docker must itself start on boot for automatic bot restart.

## Commands

| Command | Who | Purpose |
|---|---|---|
| `/help` | Everyone | Usage and applicable commands; no AI request or memory entry |
| `/chat <text>` | Everyone | Request a reply |
| `/setprompt <instructions>` | Admin | Replace persona and clear all conversation memory |
| `/context directed` | Admin | Use only conversations addressed to the bot; clear memory |
| `/context all` | Admin | Use general group text as context; clear memory |
| `/historylimit 10..500` | Admin | Override the retained message count for that group |
| `/resetmemory` | Admin | Remove recent messages and summary |
| `/pause` | Admin | Stop new conversation collection and AI replies |
| `/resume` | Admin | Resume operation |
| `/status` | Admin | Settings, retained count, summary presence, model and tracked allowance |

Both context modes reply only when addressed. Admin privileges are checked with Telegram before every settings operation. Anonymous-admin commands are rejected because the person cannot be identified. Failed verification denies the change. The model cannot authorize commands, execute code, use tools, or change configuration. Only allowed group IDs are processed. Service events, other bots, media, channels, private chats and oversized messages are ignored.

## Memory and restart behavior

Each chat has its own persona, mode, pause setting, count limit, recent history and rolling summary. User and delivered bot messages count toward the limit. Long content also triggers compaction before it exceeds the model context budget. Summaries preserve useful facts, running jokes and unfinished topics; they are lossy and contain personal data from the source messages. No expiry is applied by default, as agreed for conversation continuity. Tell group members which context mode is used and that selected text and summaries are sent to OpenAI.

Older messages are replaced only after every required summarization batch succeeds. Saving a summary and deleting its source rows is transactional. A failed compaction leaves source history intact and rejects that new conversation item; it does not silently exceed the limit. Failed history-limit changes leave the prior limit intact. Summary/source changes can commit before the final limit update, but information remains represented and the setting is never lowered ahead of compaction.

Telegram offsets and a small normalized in-flight job are saved together before acknowledgement. Jobs are deleted after processing. The database prevents two bot instances from working on the same state. A reboot resumes prepared work and generated replies. Persona and history remain persisted.

Delivery is not exactly-once: if the process crashes during a send, its outcome may be unknown. Such sends are not automatically repeated and are not added to assistant history. A generated reply that has not entered delivery can be resumed without another model call. A crash during a model request can require regenerating a reply; the spending reservation is retained conservatively. Downtime causes a response gap. Telegram retains pending updates for at most 24 hours. Deleted group messages are not automatically deleted from the bot's local history; `/resetmemory` clears both messages and summary.

Topics within a group currently share that group's memory, while replies retain the original topic. Separate-topic personas are not supported in this version.

## Spending

The model default is `gpt-6-luna`; an explicit `OPENAI_MODEL` in your private file overrides it. `gpt-5.6-luna` is also supported. Other models are rejected until their cost accounting is implemented. No OAuth/subscription fallback is used. GPT subscription billing does not pay for these API calls.

`BOT_BUDGET_USD` is a lifetime allowance shared across all groups and persisted through reboots. The default is $10. It includes summarization. Before requests, the bot reserves a conservative estimate using UTF-8 input size, known model prices and maximum output tokens. On success it reconciles against reported usage, conservatively pricing all input at the cache-write rate. Failed/unknown requests keep their reservations. This can stop the bot before the real invoice reaches $10. It is not an authoritative invoice or a guarantee against provider price changes. No tools are enabled; reasoning is set to none; output tokens, input length and reply frequency are bounded. Raising the allowance requires editing `.env` and recreating the bot. Memory reset does not reset spend.

Also configure a provider-side hard project spend limit and disable automatic prepaid recharges. OpenAI enforcement may lag. Dashboard alerts alone do not stop requests.

## Secrets and logs

The assistant must never read `.env`, credential files, resolved Compose configuration or container environment. The setup helper and running applications may consume credentials locally but never return them to the assistant. `.env`, credentials, backups and work files are excluded from Git and Docker builds. No message content or raw HTTP exceptions are printed. Host administrators and Docker administrators can access runtime credentials. Protect the Pi's OS and backup media; PostgreSQL storage is not encrypted by this project.

To rediscover IDs, with the live bot stopped:

```sh
python3 scripts/discover_chat.py
```

This uses only Telegram, prints group IDs, and reads pending updates without acknowledging them. If there is no group update, send `/help` to the bot in the group and rerun it. The verified ID for the current group is `-882012764`.

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

See `./scripts/test.sh`. Tests use an explicit synthetic environment and isolated PostgreSQL database; never the private `.env`. The test database uses temporary memory storage and is removed afterward. No Telegram/OpenAI requests occur. Coverage includes permissions, anonymous admins, chat isolation, privacy filtering, memory/reset/summary failures, limit-change failures, delivery recovery, deduplication, persistence, spending reservations and single-instance locking.

Telegram ID discovery has been verified live. OpenAI model access and role-play quality need a separately authorized live test after configuration. Do not claim those are verified by the mocked tests.
