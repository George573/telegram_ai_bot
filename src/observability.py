"""Allowlisted operational metadata; no skill text, user payloads or exceptions."""
from collections import OrderedDict
from contextlib import contextmanager
import contextvars
import hashlib
import json
import logging
import re
import sys
import threading
import time
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
    'reply_finished', 'reply_fallback',
    'maps_search_read_started', 'maps_search_read_completed', 'maps_search_read_failed',
    'callback_received', 'callback_failed', 'prompt_created', 'prompt_answered', 'progress_shown',
    'crash_notice_sent', 'status_message_edited', 'status_message_failed', 'commands_registered',
    'action_completed', 'action_failed', 'scheduled_delivered', 'scheduled_failed', 'reply_links_checked',
    'route_link_created', 'route_link_failed',
})
NUMBERS = frozenset({'duration_ms', 'input_tokens', 'output_tokens', 'search_calls',
                     'update_id', 'status_code', 'retry_after', 'attempt', 'delay_ms', 'error_position',
                     'error_line', 'error_column', 'output_chars', 'repair_attempt', 'max_repairs', 'query_chars', 'result_count',
                     'tool_call_count', 'argument_chars', 'returned_chars', 'model_rounds', 'review_count',
                     'failure_count', 'dropped_links', 'kept_links', 'dropped_items', 'demoted_items', 'unconfirmed_count', 'reasoning_tokens', 'model_requests', 'failed_requests',
                     'searches', 'reader_calls', 'reader_ok', 'skills_loaded', 'json_repairs'})
AMOUNTS = frozenset({'reserved_usd', 'charged_usd', 'budget_usd', 'spent_usd'})
OUTCOMES = frozenset({'accepted', 'refused', 'delivered', 'silent', 'fallback_delivered', 'paused', 'not_directed', 'rate_limited',
                      'cooldown', 'revoked', 'media_failed', 'media_disabled', 'throttled', 'delivery_failed',
                      'delivery_unknown', 'gave_up', 'memory_failed', 'model_failed', 'budget_exceeded'})
FALLBACKS = frozenset({'model_failure', 'budget', 'media_failed', 'memory', 'search_failed', 'settings_failed',
                       'search_disabled', 'invalid_request'})
MODELS = frozenset(model for models in PRICES.values() for model in models) | frozenset(VISION_PRICES) | {'glm-asr-2512', 'gpt-transcribe'}
ENUMS = {'parser_error': frozenset({'expected_value', 'extra_data', 'expected_key',
         'expected_comma', 'expected_colon', 'unterminated_string', 'control_character', 'other'}),
         'format_hint': frozenset({'object', 'array', 'json_fence', 'fence', 'tagged', 'other'}),
         'validation_error': frozenset({'malformed_json', 'duplicate_fields', 'not_object',
         'missing_fields', 'extra_fields', 'wrong_types', 'reply_required',
         'silent_reply_nonempty', 'reply_too_long', 'invalid_settings', 'wrong_types', 'too_long'}),
         'schema_name': frozenset({'reply_decision', 'character_settings', 'place_recommendations'}),
         'search_site': frozenset({'afisha.yandex.ru', 'yandex.ru/maps', 'restoclub.ru', 'afisha.ru',
                                   'daily.afisha.ru', 't-j.ru'}),
         'search_recency': frozenset({'day', 'week', 'month', 'year'}),
         'format_mode': frozenset({'json_schema', 'json_object'}),
         'json_repair_mode': frozenset({'format_only'}),
         'requested_tool': frozenset({'load_skill', 'search_web', 'web_search', 'read_yandex_reviews',
                                     'read_afisha_listing', 'read_afisha_event', 'read_yandex_maps_search',
                                     'route_link', 'send_poll', 'schedule_message', 'list_scheduled_messages',
                                     'cancel_scheduled_message', 'other'}),
         'scheduled_kind': frozenset({'text', 'poll'}),
         'tool_error': frozenset({'tools_disabled', 'step_limit', 'multiple_calls', 'unknown_tool',
                                'missing_call_id', 'argument_type', 'arguments_too_long',
                                'malformed_arguments', 'invalid_arguments', 'repeated_skill_limit',
                                'batch_limit', 'duplicate_call_id', 'search_limit',
                                'too_many_calls', 'review_limit', 'context_budget', 'invalid_url',
                                'action_limit', 'pending_limit', 'telegram_rejected', 'rate_limited',
                                'delivery_uncertain', 'not_allowed'}),
         'finish_reason': frozenset({'stop', 'tool_calls', 'length', 'sensitive', 'model_context_window_exceeded',
                                    'network_error', 'completed', 'incomplete', 'failed', 'content_filter'}),
         'incomplete_reason': frozenset({'max_output_tokens', 'content_filter'}),
         'provider': frozenset(PRICES), 'model': MODELS,
         'operation': frozenset({'text', 'image', 'transcription', 'web_search'}),
         'stage': frozenset({'configuration', 'database', 'telegram', 'provider', 'running'}),
         'phase': frozenset({'queued', 'prepared', 'generated', 'sending'}),
         'outcome': OUTCOMES, 'fallback': FALLBACKS,
         'reason': frozenset({'request_failed', 'incomplete', 'invalid_json', 'invalid_settings',
                              'processing_failed', 'telegram_access', 'shutdown', 'startup',
                              'invalid_skill_call', 'skill_loading_failed', 'search_not_executed', 'invalid_search_call',
                              'invalid_review_call', 'browser_unavailable', 'browser_failed', 'timeout', 'captcha',
                              'not_found', 'access_denied', 'redirected', 'reviews_not_loaded', 'no_review_texts',
                              'busy', 'invalid_url', 'context_budget', 'too_many_calls', 'review_limit',
                              'unexpected_error', 'retry_scheduled', 'gave_up', 'invalid_arguments', 'no_events',
                              'event_not_loaded', 'search_limit', 'no_results', 'page_changed', 'too_large',
                              'timeout_or_network', 'formatting_rejected'})}

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


class JobStats:
    """Per-job tally built from the events themselves; counts and amounts only."""
    def __init__(self):
        self.started = time.monotonic()
        self.model_requests = self.failed_requests = self.searches = 0
        self.reader_calls = self.reader_ok = self.skills_loaded = self.json_repairs = 0
        self.input_tokens = self.output_tokens = self.reasoning_tokens = 0
        self.spent = Decimal('0')
        self.pending = Decimal('0')
        self.round_limit = self.budget_exceeded = False
        self.fallback = None

    def observe(self, name, fields):
        amount = fields.get('charged_usd') if isinstance(fields.get('charged_usd'), Decimal) else None
        if name == 'ai_request_started':
            self.pending = fields.get('reserved_usd') if isinstance(fields.get('reserved_usd'), Decimal) else Decimal('0')
        elif name in {'ai_request_completed', 'ai_request_failed'}:
            self.model_requests += 1
            self.failed_requests += name == 'ai_request_failed'
            # A failed request keeps its reservation: count it as spent.
            self.spent += amount if amount is not None else self.pending
            self.pending = Decimal('0')
            for key in ('input_tokens', 'output_tokens', 'reasoning_tokens'):
                if type(fields.get(key)) is int:
                    setattr(self, key, getattr(self, key) + fields[key])
        elif name == 'web_search_completed' or (name == 'web_search_failed' and fields.get('reason') == 'request_failed'):
            self.searches += 1
            self.spent += amount if amount is not None else (fields.get('reserved_usd') or Decimal('0'))
        elif name in {'reviews_read_completed', 'reviews_read_failed', 'afisha_read_completed', 'afisha_read_failed',
                      'maps_search_read_completed', 'maps_search_read_failed'} \
                and 'review_count' in fields:
            self.reader_calls += 1
            self.reader_ok += name.endswith('_completed')
        elif name == 'skill_returned':
            self.skills_loaded += 1
        elif name == 'ai_json_repair_started':
            self.json_repairs += 1
        elif name == 'tool_round_limit_reached':
            self.round_limit = True
        elif name == 'budget_exceeded':
            self.budget_exceeded = True

    def summary(self):
        return dict(model_requests=self.model_requests, failed_requests=self.failed_requests,
                    searches=self.searches, reader_calls=self.reader_calls, reader_ok=self.reader_ok,
                    skills_loaded=self.skills_loaded, json_repairs=self.json_repairs,
                    input_tokens=self.input_tokens, output_tokens=self.output_tokens,
                    reasoning_tokens=self.reasoning_tokens, spent_usd=self.spent, round_limit=self.round_limit,
                    budget_exceeded=self.budget_exceeded, fallback=self.fallback,
                    duration_ms=int((time.monotonic() - self.started) * 1000))


# A job's phases can run in separate worker turns; keep a bounded tally per update.
_JOB_STATS = OrderedDict()
_JOB_STATS_LOCK = threading.Lock()
_CURRENT_JOB = contextvars.ContextVar('current_job', default=None)


def _stats(update_id):
    with _JOB_STATS_LOCK:
        stats = _JOB_STATS.get(update_id)
        if stats is None:
            stats = _JOB_STATS[update_id] = JobStats()
            while len(_JOB_STATS) > 500:
                _JOB_STATS.popitem(last=False)
        return stats


@contextmanager
def job_context(update_id):
    """Every event in this block carries update_id and feeds the job's summary."""
    token = _CURRENT_JOB.set(update_id if type(update_id) is int else None)
    if type(update_id) is int:
        _stats(update_id)  # Start the clock at the first processed phase.
    try:
        yield
    finally:
        _CURRENT_JOB.reset(token)


def fallback(reason, **fields):
    """The user receives an error message instead of an answer."""
    update_id = _CURRENT_JOB.get()
    if update_id is not None and reason in FALLBACKS:
        _stats(update_id).fallback = reason
    event('reply_fallback', level=logging.WARNING, fallback=reason, **fields)


def job_finished(update_id, outcome, **fields):
    """One summary line per job; clears its tally."""
    with _JOB_STATS_LOCK:
        stats = _JOB_STATS.pop(update_id, None) or JobStats()
    if stats.fallback and outcome == 'delivered':
        outcome = 'fallback_delivered'
    summary = stats.summary()
    event('reply_finished', level=logging.WARNING if outcome in {'delivery_failed', 'gave_up'} else logging.INFO,
          update_id=update_id, outcome=outcome, **{**summary, **fields})


def event(name, *, level=logging.INFO, **fields):
    if name not in EVENTS:
        return
    current = _CURRENT_JOB.get()
    if current is not None:
        fields.setdefault('update_id', current)
        if name not in {'reply_finished', 'reply_fallback'}:
            _stats(current).observe(name, fields)
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
        elif key in {'responded', 'web_search_enabled', 'actions_enabled', 'previous_answer_included', 'error_feedback_included',
                     'explicit_search_available', 'filters_applied', 'round_limit', 'budget_exceeded',
                     'directed', 'notified'} and type(value) is bool:
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
    current = _CURRENT_JOB.get()
    if current is not None:
        # Same job correlation and tally as event(); skill audit lines bypass event().
        data['update_id'] = current
        _stats(current).observe(event_name, data)
    LOGGER.log(logging.WARNING if phase == 'failed' else logging.INFO,
               event_name, extra={'event_data': data})
