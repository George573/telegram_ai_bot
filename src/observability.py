"""Allowlisted operational logs; never serialize payloads or exceptions."""
import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from providers import PRICES, VISION_PRICES, ZAI_ERROR_CODES

LOGGER = logging.getLogger('telegram_bot')
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False
EVENTS = frozenset({
    'bot_started', 'bot_stopped', 'bot_failed', 'poll_failed',
    'ai_request_started', 'ai_request_completed', 'ai_request_failed',
    'ai_request_retry', 'ai_request_delayed', 'ai_decision_retry', 'ai_output_invalid', 'budget_exceeded', 'media_failed',
    'job_processing', 'reply_generated', 'reply_delivered',
    'typing_failed', 'delivery_failed', 'delivery_unknown', 'settings_failed', 'memory_failed',
})
NUMBERS = frozenset({'duration_ms', 'input_tokens', 'output_tokens', 'search_calls',
                     'update_id', 'status_code', 'retry_after', 'attempt', 'delay_ms', 'error_position'})
AMOUNTS = frozenset({'reserved_usd', 'charged_usd', 'budget_usd'})
MODELS = frozenset(model for models in PRICES.values() for model in models) | frozenset(VISION_PRICES) | {'glm-asr-2512', 'gpt-transcribe'}
ENUMS = {'parser_error': frozenset({'expected_value', 'extra_data', 'expected_key',
         'expected_comma', 'expected_colon', 'unterminated_string', 'control_character', 'other'}),
         'format_hint': frozenset({'object', 'array', 'json_fence', 'fence', 'tagged', 'other'}),
         'validation_error': frozenset({'malformed_json', 'duplicate_fields', 'not_object',
         'missing_fields', 'extra_fields', 'wrong_types', 'reply_required',
         'silent_reply_nonempty', 'reply_too_long'}),
         'provider': frozenset(PRICES), 'model': MODELS,
         'operation': frozenset({'text', 'image', 'transcription'}),
         'stage': frozenset({'configuration', 'database', 'telegram', 'provider', 'running'}),
         'phase': frozenset({'queued', 'prepared', 'generated', 'sending'}),
         'reason': frozenset({'request_failed', 'incomplete', 'invalid_json', 'invalid_settings',
                              'processing_failed', 'telegram_access', 'shutdown', 'startup'})}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Do not include record.exc_info, stack_info, or arbitrary record extras.
        return json.dumps({'time': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                           'level': record.levelname, **record.event_data}, ensure_ascii=True)


class EventFilter(logging.Filter):
    def filter(self, record):
        return isinstance(getattr(record, 'event_data', None), dict) and record.event_data.get('event') in EVENTS


def configure_logging(stream=None):
    logging.disable(logging.NOTSET)
    # SDK/HTTP debug logs can contain token-bearing URLs or request bodies.
    logging.getLogger().handlers = [logging.NullHandler()]
    for name in ('openai', 'httpx', 'httpcore'):
        external = logging.getLogger(name)
        external.handlers = [logging.NullHandler()]
        external.propagate = False
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(EventFilter())
    LOGGER.handlers = [handler]
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


def event(name, *, level=logging.INFO, **fields):
    if name not in EVENTS:
        return
    data = {'event': name}
    for key, value in fields.items():
        if key in ENUMS and isinstance(value, str) and value in ENUMS[key]:
            data[key] = value
        elif key == 'provider_code' and type(value) is int and value in ZAI_ERROR_CODES:
            data[key] = value
        elif key in NUMBERS and type(value) is int and value >= 0:
            data[key] = value
        elif key in AMOUNTS and isinstance(value, Decimal) and value.is_finite() and value >= 0:
            data[key] = str(value)
        elif key == 'responded' and type(value) is bool:
            data[key] = value
    LOGGER.log(level, name, extra={'event_data': data})
