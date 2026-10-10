"""Allowlisted operational metadata; no skill text, user payloads or exceptions."""
import hashlib
import json
import logging
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from providers import PRICES, VISION_PRICES, ZAI_ERROR_CODES
from skill_registry import SKILL_NAMES

LOGGER = logging.getLogger('telegram_bot')
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False
EVENTS = frozenset({
    'bot_started', 'bot_stopped', 'bot_failed', 'poll_failed',
    'ai_request_started', 'ai_request_completed', 'ai_request_failed',
    'ai_request_retry', 'ai_request_delayed', 'ai_decision_retry', 'ai_output_invalid', 'budget_exceeded', 'media_failed',
    'job_processing', 'reply_generated', 'reply_delivered',
    'typing_failed', 'delivery_failed', 'delivery_unknown', 'settings_failed', 'memory_failed',
    'skill_requested', 'skill_returned', 'skill_failed', 'skill_reused',
    'ai_json_repair_started', 'ai_json_repair_completed', 'ai_json_repair_failed', 'ai_json_repair_exhausted',
    'web_search_started', 'web_search_completed', 'web_search_failed',
    'tool_batch_received', 'tool_round_limit_reached',
    'ai_final_answer_started', 'ai_final_answer_completed', 'ai_final_answer_failed',
    'reviews_read_started', 'reviews_read_completed', 'reviews_read_failed',
    'worker_failed', 'job_failed', 'update_failed', 'budget_overrun', 'command_throttled',
    'afisha_read_started', 'afisha_read_completed', 'afisha_read_failed', 'recommendations_rendered',
})
NUMBERS = frozenset({'duration_ms', 'input_tokens', 'output_tokens', 'search_calls',
                     'update_id', 'status_code', 'retry_after', 'attempt', 'delay_ms', 'error_position',
                     'error_line', 'error_column', 'output_chars', 'repair_attempt', 'max_repairs', 'query_chars', 'result_count',
                     'tool_call_count', 'argument_chars', 'returned_chars', 'model_rounds', 'review_count',
                     'failure_count', 'dropped_links'})
AMOUNTS = frozenset({'reserved_usd', 'charged_usd', 'budget_usd'})
MODELS = frozenset(model for models in PRICES.values() for model in models) | frozenset(VISION_PRICES) | {'glm-asr-2512', 'gpt-transcribe'}
ENUMS = {'parser_error': frozenset({'expected_value', 'extra_data', 'expected_key',
         'expected_comma', 'expected_colon', 'unterminated_string', 'control_character', 'other'}),
         'format_hint': frozenset({'object', 'array', 'json_fence', 'fence', 'tagged', 'other'}),
         'validation_error': frozenset({'malformed_json', 'duplicate_fields', 'not_object',
         'missing_fields', 'extra_fields', 'wrong_types', 'reply_required',
         'silent_reply_nonempty', 'reply_too_long', 'invalid_settings', 'wrong_types', 'too_long'}),
         'schema_name': frozenset({'reply_decision', 'character_settings', 'place_recommendations'}),
         'search_site': frozenset({'afisha.yandex.ru', 'yandex.ru/maps'}),
         'search_recency': frozenset({'day', 'week', 'month', 'year'}),
         'format_mode': frozenset({'json_schema', 'json_object'}),
         'json_repair_mode': frozenset({'format_only'}),
         'requested_tool': frozenset({'load_skill', 'search_web', 'web_search', 'read_yandex_reviews',
                                     'read_afisha_listing', 'read_afisha_event', 'other'}),
         'tool_error': frozenset({'tools_disabled', 'step_limit', 'multiple_calls', 'unknown_tool',
                                'missing_call_id', 'argument_type', 'arguments_too_long',
                                'malformed_arguments', 'invalid_arguments', 'repeated_skill_limit',
                                'batch_limit', 'duplicate_call_id', 'search_limit',
                                'too_many_calls', 'review_limit', 'context_budget', 'invalid_url'}),
         'finish_reason': frozenset({'stop', 'tool_calls', 'length', 'sensitive', 'model_context_window_exceeded',
                                    'network_error', 'completed', 'incomplete', 'failed', 'content_filter'}),
         'incomplete_reason': frozenset({'max_output_tokens', 'content_filter'}),
         'provider': frozenset(PRICES), 'model': MODELS,
         'operation': frozenset({'text', 'image', 'transcription', 'web_search'}),
         'stage': frozenset({'configuration', 'database', 'telegram', 'provider', 'running'}),
         'phase': frozenset({'queued', 'prepared', 'generated', 'sending'}),
         'reason': frozenset({'request_failed', 'incomplete', 'invalid_json', 'invalid_settings',
                              'processing_failed', 'telegram_access', 'shutdown', 'startup',
                              'invalid_skill_call', 'skill_loading_failed', 'search_not_executed', 'invalid_search_call',
                              'invalid_review_call', 'browser_unavailable', 'browser_failed', 'timeout', 'captcha',
                              'not_found', 'access_denied', 'redirected', 'reviews_not_loaded', 'no_review_texts',
                              'busy', 'invalid_url', 'context_budget', 'too_many_calls', 'review_limit',
                              'unexpected_error', 'retry_scheduled', 'gave_up', 'invalid_arguments', 'no_events',
                              'event_not_loaded'})}

VALIDATION_DETAILS = {
    'malformed_json': 'Ответ не удалось разобрать как JSON.',
    'duplicate_fields': 'В JSON повторяется имя поля.',
    'not_object': 'Получен JSON другого типа; требуется объект с respond и reply.',
    'missing_fields': 'Отсутствует обязательное поле respond или reply.',
    'extra_fields': 'Объект содержит поля помимо respond и reply.',
    'wrong_types': 'Неверный тип поля: respond должен быть boolean, reply — строкой.',
    'reply_required': 'respond=true, но reply пустой.',
    'silent_reply_nonempty': 'respond=false, но reply содержит текст.',
    'reply_too_long': 'reply превышает ограничение 3500 символов.',
    'invalid_settings': 'Настройки персонажа не соответствуют допустимым полям, типам или ограничениям.',
    'wrong_types': 'Поле подборки имеет неверный тип или неизвестный kind.',
    'too_long': 'Поле подборки длиннее лимита схемы или карточек больше пяти.',
}
PARSER_DETAILS = {
    'expected_value': 'В указанной позиции ожидалось JSON-значение.',
    'extra_data': 'После первого JSON-значения остался лишний текст или ещё одно значение.',
    'expected_key': 'Ожидалось имя поля в двойных кавычках.',
    'expected_comma': 'Ожидалась запятая между элементами или полями.',
    'expected_colon': 'Ожидалось двоеточие после имени поля.',
    'unterminated_string': 'JSON-строка не закрыта двойной кавычкой.',
    'control_character': 'В JSON-строке обнаружен неэкранированный управляющий символ.',
}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Do not include record.exc_info, stack_info, or arbitrary record extras.
        return json.dumps({'time': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                           'level': record.levelname, **record.event_data}, ensure_ascii=False)


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
        elif key in {'json_request_id', 'search_request_id', 'reviews_request_id'} and isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value):
            data[key] = value
        elif key in {'responded', 'web_search_enabled', 'previous_answer_included', 'error_feedback_included',
                     'explicit_search_available', 'filters_applied'} and type(value) is bool:
            data[key] = value
    if name in {'ai_output_invalid', 'ai_json_repair_started', 'ai_json_repair_failed',
                'ai_json_repair_exhausted'} and data.get('validation_error') in VALIDATION_DETAILS:
        detail = VALIDATION_DETAILS[data['validation_error']]
        if data['validation_error'] == 'malformed_json':
            detail += ' ' + PARSER_DETAILS.get(data.get('parser_error'), 'Причина парсера не классифицирована.')
            if data.get('parser_error') == 'expected_value' and data.get('error_position') == 0:
                detail += ' Начало ответа не является допустимым JSON-значением; возможно, получен обычный текст.'
        data['validation_detail'] = detail
    LOGGER.log(level, name, extra={'event_data': data})


def skill_event(phase, *, request_id, provider, model, name, content=None):
    """Audit validated skill metadata; hash the returned body without logging it."""
    if phase not in {'requested', 'returned', 'failed'}:
        return
    if not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}', request_id):
        return
    if phase == 'returned' and not isinstance(content, str):
        return
    if name not in SKILL_NAMES:
        return
    event_name = 'skill_' + phase
    data = {'event': event_name, 'skill_request_id': request_id, 'tool': 'load_skill',
            'requested': {'name': name}}
    for key, value in (('provider', provider), ('model', model)):
        if isinstance(value, str) and value in ENUMS[key]:
            data[key] = value
    if phase == 'returned':
        data['returned'] = {'name': name,
                            'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()}
        data['returned_chars'] = len(content)
    elif phase == 'failed':
        data['reason'] = 'skill_loading_failed'
    LOGGER.log(logging.WARNING if phase == 'failed' else logging.INFO,
               event_name, extra={'event_data': data})
