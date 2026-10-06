# Telegram roleplay bot — development starter

Python + Docker Compose + PostgreSQL named volume. Intended for amd64 PCs and arm64 Raspberry Pi 4.

## Secrets
The assistant must not read `.env` or `credentials/`. Edit `.env` locally. Tests use no credentials and make no network requests. Never paste secrets into chat. `.env` and credentials are excluded from Git and image build context. Host administrators can still inspect runtime credentials.

## Setup
1. Install Docker Engine and Compose.
2. Edit the existing `.env`: Telegram bot token, allowed numeric group IDs, strong PostgreSQL password.
3. Create a standard project API key at https://platform.openai.com/api-keys and configure API billing separately from ChatGPT. Save it locally as `OPENAI_API_KEY` in `.env`; remove the obsolete `OPENAI_ACCESS_TOKEN` entry yourself if present. The assistant will never read or edit your existing `.env`. No subscription token fallback is used. A personal subscription cannot fulfill other group members' requests under the Sign in with ChatGPT terms: https://openai.com/policies/sign-in-with-chatgpt-terms/.
4. In BotFather, disable privacy if you want both context modes and ordinary mentions. Bot admin privileges are unnecessary. The application discards unrelated messages in directed mode.
5. When ready for live API usage (this incurs API charges): `docker compose up -d --build`.
6. Stop: `docker compose down`. Do NOT add `-v` unless intentionally deleting the database.

No database ports are exposed. Runtime secrets are injected into environment; avoid sharing `docker compose config` or inspection output because it can expose them.

## Commands
Everyone: `/help`, `/chat <message>`, mentions and replies.
Admins: `/setprompt <text>`, `/context directed|all`, `/historylimit 10..500`, `/resetmemory`, `/pause`, `/resume`, `/status`.
Prompt/context changes and reset clear messages and summary. Pause prevents collection and model replies. Recent history and summaries persist across restart. Summaries are lossy and are retained information, not anonymization.

## Development checks
`python3 -m unittest discover -s tests`

## Remaining before production
- Verify API billing/model access and add persistent usage accounting with bot-enforced spending limits. Project budget alerts alone should not be treated as a hard cap.
- Durable Telegram checkpoint/deduplication and crash recovery. Current polling does not guarantee exactly-once processing.
- Integration tests for PostgreSQL, admin checks, summary failures and restart behavior; rate limits.
- Migration framework, backup/restore commands and Pi image build verification.
- Handling history-limit decreases transactionally with summarization. Current setting can change before compaction succeeds.
- Delivery state: current assistant history is saved before sending; ambiguous delivery can leave an unsent response in history.

This is a reviewable development starter, not a production-ready deployment. Do not populate credentials until you are ready for deliberate live testing.
