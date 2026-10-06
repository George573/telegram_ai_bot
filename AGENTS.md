# Project rules

- Never read `.env`, `.env.*` except `.env.example`, credential files, or runtime environment values during development.
- Never print resolved Compose configuration, container environment, or raw exceptions containing secrets.
- Never overwrite an existing `.env`.
- Use synthetic credentials and mocked network calls in tests. Live integration requires explicit user instruction.
- Exclude secrets from Git, Docker build context, logs, and diagnostic output.
- Subscription authentication must use official OAuth; no separately billed API fallback.
