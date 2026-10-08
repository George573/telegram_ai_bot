from dataclasses import dataclass
from decimal import Decimal
import os
import math
from policy import valid_limit
from providers import PRICES, DEFAULT_MODELS, VISION_PRICES


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
    media_max_bytes: int = 5 * 1024 * 1024
    voice_max_seconds: int = 120
    superadmin_ids: frozenset[int] = frozenset()

    provider: str = 'openai'
    zai_key: str = ''
    zai_vision_model: str = 'glm-4.6v'
    api_min_interval_seconds: float = 0.25

    @classmethod
    def load(cls):
        chats = frozenset(int(x.strip()) for x in os.getenv('ALLOWED_CHAT_IDS', '').split(',') if x.strip())
        token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        provider = os.getenv('MODEL_PROVIDER', 'openai')
        if provider not in DEFAULT_MODELS:
            raise ValueError('Unsupported model provider')
        key = os.getenv('OPENAI_API_KEY', '') if provider == 'openai' else ''
        zai_key = os.getenv('ZAI_API_KEY', '') if provider == 'zai' else ''
        selected_key = key if provider == 'openai' else zai_key
        if not token or not selected_key or not chats or any(x >= 0 for x in chats):
            raise ValueError('Set tokens and approved negative group IDs in .env')
        config = cls(token, key, chats,
            provider=provider, zai_key=zai_key,
            api_min_interval_seconds=float(os.getenv('API_MIN_INTERVAL_SECONDS', '0.25')),
            zai_vision_model=os.getenv('ZAI_VISION_MODEL', 'glm-4.6v') if provider == 'zai' else 'glm-4.6v',
            model=os.getenv('OPENAI_MODEL' if provider == 'openai' else 'ZAI_MODEL', DEFAULT_MODELS[provider]),
            history_limit=valid_limit(os.getenv('HISTORY_MAX_MESSAGES', '50')),
            summary_chars=int(os.getenv('SUMMARY_MAX_CHARS', '4000')),
            budget=Decimal(os.getenv('BOT_BUDGET_USD', '10')),
            cooldown=int(os.getenv('REPLY_COOLDOWN_SECONDS', '10')),
            media_max_bytes=int(os.getenv('MEDIA_MAX_BYTES', '5242880')),
            voice_max_seconds=int(os.getenv('VOICE_MAX_SECONDS', '120')),
            superadmin_ids=frozenset(int(x.strip()) for x in os.getenv('BOT_SUPERADMIN_IDS', '').split(',') if x.strip()))
        if not math.isfinite(config.api_min_interval_seconds) or not 0 <= config.api_min_interval_seconds <= 30:
            raise ValueError('API interval must be between 0 and 30 seconds')
        if not 1024 <= config.media_max_bytes <= 10 * 1024 * 1024 or not 1 <= config.voice_max_seconds <= 300:
            raise ValueError('Invalid media limits')
        if any(x <= 0 for x in config.superadmin_ids):
            raise ValueError('Super admins require positive Telegram user IDs')
        if config.provider == 'zai' and config.zai_vision_model not in VISION_PRICES:
            raise ValueError('Unsupported vision model: cost accounting needs known prices')
        if config.model not in PRICES[config.provider]:
            raise ValueError('Unsupported model: cost accounting needs known prices')
        if not config.budget.is_finite() or config.budget <= 0 or not 200 <= config.summary_chars <= 6000 or not 0 <= config.cooldown <= 3600:
            raise ValueError('Invalid budget, summary size, or cooldown')
        return config
