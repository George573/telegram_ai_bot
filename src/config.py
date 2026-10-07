from dataclasses import dataclass
from decimal import Decimal
import os
from policy import valid_limit


@dataclass(frozen=True)
class Config:
    telegram_token: str
    openai_key: str
    chats: frozenset[int]
    model: str = 'gpt-6-luna'
    history_limit: int = 50
    summary_chars: int = 4000
    budget: Decimal = Decimal('10')
    cooldown: int = 10
    max_input_chars: int = 30000
    max_output_tokens: int = 600
    superadmin_ids: frozenset[int] = frozenset()

    @classmethod
    def load(cls):
        chats = frozenset(int(x.strip()) for x in os.getenv('ALLOWED_CHAT_IDS', '').split(',') if x.strip())
        token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        key = os.getenv('OPENAI_API_KEY', '')
        if not token or not key or not chats or any(x >= 0 for x in chats):
            raise ValueError('Set tokens and approved negative group IDs in .env')
        config = cls(token, key, chats,
            model=os.getenv('OPENAI_MODEL', 'gpt-6-luna'),
            history_limit=valid_limit(os.getenv('HISTORY_MAX_MESSAGES', '50')),
            summary_chars=int(os.getenv('SUMMARY_MAX_CHARS', '4000')),
            budget=Decimal(os.getenv('BOT_BUDGET_USD', '10')),
            cooldown=int(os.getenv('REPLY_COOLDOWN_SECONDS', '10')),
            superadmin_ids=frozenset(int(x.strip()) for x in os.getenv('BOT_SUPERADMIN_IDS', '').split(',') if x.strip()))
        if any(x <= 0 for x in config.superadmin_ids):
            raise ValueError('Super admins require positive Telegram user IDs')
        if config.model not in {'gpt-6-luna', 'gpt-5.6-luna'}:
            raise ValueError('Unsupported model: cost accounting needs known prices')
        if not config.budget.is_finite() or config.budget <= 0 or not 200 <= config.summary_chars <= 6000 or not 0 <= config.cooldown <= 3600:
            raise ValueError('Invalid budget, summary size, or cooldown')
        return config
