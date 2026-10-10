"""Native provider adapters. No cross-provider or billing fallback."""
from dataclasses import dataclass
from decimal import Decimal
import base64
import json
import ipaddress
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from openai import OpenAI, BadRequestError
from skill_registry import LOAD_SKILL
from yandex_afisha import READ_AFISHA_EVENT, READ_AFISHA_LISTING
from yandex_reviews import READ_YANDEX_REVIEWS

PRICES = {
    'openai': {'gpt-6-luna': (Decimal('0.10'), Decimal('0.50')),
               'gpt-5.6-luna': (Decimal('0.20'), Decimal('1.20'))},
    'zai': {'glm-4.7-flash': (Decimal('0'), Decimal('0')),
            'glm-4.7-flashx': (Decimal('0.07'), Decimal('0.40')),
            'glm-4.7': (Decimal('0.60'), Decimal('2.20')),
            'glm-5': (Decimal('1.00'), Decimal('3.20')),
            'glm-5.1': (Decimal('1.40'), Decimal('4.40')),
            'glm-5.3-flash': (Decimal('0.15'), Decimal('0.50'))},
}
DEFAULT_MODELS = {'openai': 'gpt-6-luna', 'zai': 'glm-4.7'}
CAPABILITIES = {'openai': frozenset({'images', 'voice', 'web'}),
                'zai': frozenset({'images', 'voice', 'web'})}
VISION_PRICES = {'glm-4.6v-flash': (Decimal('0'), Decimal('0')),
                 'glm-4.6v-flashx': (Decimal('0.04'), Decimal('0.40')),
                 'glm-4.6v': (Decimal('0.30'), Decimal('0.90'))}
VOICE_MAX_SECONDS = {'openai': 300, 'zai': 30}
TRANSCRIPTION_RATES = {'openai': Decimal('0.0045'), 'zai': Decimal('0.0024')}
IMAGE_MAX_BYTES = {'openai': 10 * 1024 * 1024, 'zai': 5 * 1024 * 1024}
WEB_CALL_COST = Decimal('0.01')
SEARCH_SITES = ('afisha.yandex.ru', 'yandex.ru/maps')
SEARCH_RECENCY = ('day', 'week', 'month', 'year')
SEARCH_WEB = {'name': 'search_web',
    'description': 'Search the public web now and return titles, URLs and evidence snippets. Use it for current '
                   'places, events and factual lookups; never ask the user to run /search. site restricts results '
                   '(afisha.yandex.ru for events, yandex.ru/maps for organization cards) or null for the whole web; '
                   'recency limits results to the last day/week/month/year, or null.',
    'parameters': {'type': 'object', 'properties': {
        'query': {'type': 'string', 'maxLength': 400},
        'site': {'type': ['string', 'null'], 'enum': [*SEARCH_SITES, None]},
        'recency': {'type': ['string', 'null'], 'enum': [*SEARCH_RECENCY, None]}},
        'required': ['query', 'site', 'recency'], 'additionalProperties': False}}
READERS = (READ_YANDEX_REVIEWS, READ_AFISHA_LISTING, READ_AFISHA_EVENT)
# Search requests leave the bot with a fixed location hint, never user data.
SEARCH_LOCATION = {'type': 'approximate', 'country': 'RU', 'city': 'Moscow', 'timezone': 'Europe/Moscow'}
# Models whose reasoning shares max_tokens with the visible answer, and the extra room they get.
THINKING_MODELS = frozenset({'glm-5.3-flash'})
THINKING_ALLOWANCE = 2000
RECENCY_HINTS = {'day': 'the last day', 'week': 'the last week', 'month': 'the last month', 'year': 'the last year'}
TRANSCRIPTION_PER_MINUTE = TRANSCRIPTION_RATES['openai']


def strict_schema(schema):
    """Drop string length keywords for OpenAI strict mode; Model validates lengths itself."""
    if isinstance(schema, dict):
        return {key: strict_schema(value) for key, value in schema.items() if key not in {'minLength', 'maxLength'}}
    if isinstance(schema, list):
        return [strict_schema(value) for value in schema]
    return schema


def function_tools(allow_search, skill_enabled, readers_enabled):
    """The same local tools for every provider, in a fixed order."""
    tools = [SEARCH_WEB] if allow_search else []
    if skill_enabled:
        tools.insert(0, LOAD_SKILL)  # Keep skill discovery first.
    return tools + (list(READERS) if readers_enabled else [])


def search_query(query, site):
    # Maps cards cannot be domain-filtered by path; name the service instead.
    return query + ' Яндекс Карты' if site == 'yandex.ru/maps' and 'карт' not in query.casefold() else query


@dataclass(frozen=True)
class SearchOutcome:
    results: list
    input_tokens: int = 0
    output_tokens: int = 0
    filters_applied: bool = True


def safe_result(url, title, content='', publish_date=''):
    safe = citation_sources({'output': [{'type': 'message', 'content': [{'annotations': [
        {'type': 'url_citation', 'url': url, 'title': title or 'Source'}]}]}]})
    if not safe:
        return None
    return {'title': safe[0][0], 'url': safe[0][1], 'content': str(content or '')[:2000],
            'publish_date': str(publish_date or '')[:40]}


def model_prices(provider, model):
    if provider == 'zai' and model in VISION_PRICES:
        return VISION_PRICES[model]
    return PRICES[provider][model]


# Only documented codes are allowed into diagnostics; never log error messages/bodies.
ZAI_ERROR_CODES = frozenset({1000, 1001, 1003, 1005, 1113, 1200, 1210, 1211,
    1212, 1213, 1214, 1215, 1220, 1221, 1222, 1230, 1234, 1261,
    1301, 1302, 1305, 1308, 1309, 1310, 1311, 1313, 1314, 1315,
    1316, 1317, 1318, 1319, 1320, 1321})


def provider_error_code(error):
    body = getattr(error, 'body', None)
    code = getattr(error, 'code', None)
    if isinstance(body, dict):
        detail = body.get('error', body)
        if isinstance(detail, dict):
            code = detail.get('code', code)
    if type(code) is int and code in ZAI_ERROR_CODES:
        return code
    if type(code) is str and code.isascii() and code.isdigit() and len(code) == 4:
        number = int(code)
        if number in ZAI_ERROR_CODES:
            return number
    return None


def zai_retry_delay(error, retry):
    """Retry only temporary 429s, never balance/access/quota failures.

    Unknown provider codes are not assumed temporary. A missing code on an
    HTTP 429 (e.g. gateway response) gets the same bounded backoff.
    """
    if getattr(error, 'status_code', None) != 429:
        return None
    code = provider_error_code(error)
    if code not in {None, 1302, 1305}:
        return None
    body = getattr(error, 'body', None)
    detail = body.get('error', body) if isinstance(body, dict) else None
    raw_code = detail.get('code') if isinstance(detail, dict) else getattr(error, 'code', None)
    if code is None and raw_code not in (None, ''):
        return None
    response = getattr(error, 'response', None)
    headers = getattr(response, 'headers', {})
    for header, factor in [('retry-after-ms', 0.001), ('retry-after', 1)]:
        value = headers.get(header)
        if value is None:
            continue
        try:
            delay = float(value) * factor
        except (TypeError, ValueError):
            if header != 'retry-after':
                continue
            try:
                when = parsedate_to_datetime(value)
                when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
                delay = (when - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                continue
        if not math.isfinite(delay) or delay < 0:
            continue
        # Do not shorten long provider-mandated cooldowns to our wait bound.
        return max(1, delay) if delay <= 30 else None
    return 2 ** (retry + 1)


class ProviderFailure(Exception):
    pass


@dataclass(frozen=True)
class Generation:
    text: str
    completed: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    sources: tuple = ()
    search_calls: int = 0
    search_fee: Decimal | None = None
    tool_calls: tuple = ()
    continuation: tuple = ()
    finish_reason: str | None = None
    incomplete_reason: str | None = None


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str


def member(item, name, default=None):
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def citation_sources(response):
    sources = []
    for item in member(response, 'output', []) or []:
        if member(item, 'type') != 'message':
            continue
        for content in member(item, 'content', []) or []:
            for annotation in member(content, 'annotations', []) or []:
                if member(annotation, 'type') != 'url_citation':
                    continue
                url, title = member(annotation, 'url', ''), member(annotation, 'title', 'Source')
                try:
                    parsed = urlsplit(url)
                    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or
                            parsed.username or parsed.password or len(url) > 400 or
                            any(ord(c) < 33 for c in url) or parsed.hostname == 'localhost'):
                        continue
                    try:
                        if not ipaddress.ip_address(parsed.hostname).is_global:
                            continue
                    except ValueError:
                        pass
                except (ValueError, TypeError):
                    continue
                if url not in [s[1] for s in sources]:
                    sources.append((str(title).replace('\n', ' ')[:100], url))
    return sources[:3]


class OpenAIProvider:
    capabilities = CAPABILITIES['openai']

    def __init__(self, config, client=None):
        self.client = client if client is not None else OpenAI(
            api_key=config.openai_key, max_retries=0, timeout=45)

    def generation_model(self, model, image):
        return model

    def token_ceiling(self, model, output_tokens):
        return output_tokens

    def prepare_instructions(self, instructions, response_format):
        return instructions

    def generate(self, *, model, instructions, history, output_tokens,
                 response_format=None, allow_search=False, image=None, require_search=False,
                 skill_enabled=False, continuation=(), readers_enabled=False):
        extra = {'text': {'format': {**response_format, 'schema': strict_schema(response_format['schema'])}}} if response_format else {}
        if image is not None:
            encoded = base64.b64encode(image).decode('ascii')
            history = history + [{'role': 'user', 'content': [
                {'type': 'input_image', 'image_url': 'data:image/jpeg;base64,' + encoded, 'detail': 'low'}]}]
        tools = function_tools(allow_search, skill_enabled, readers_enabled)
        if tools:
            extra['tools'] = [{'type': 'function', 'name': tool['name'], 'description': tool['description'],
                               'parameters': strict_schema(tool['parameters']), 'strict': True} for tool in tools]
            if require_search and allow_search:
                extra['tool_choice'] = {'type': 'function', 'name': 'search_web'}
        if skill_enabled:
            extra['parallel_tool_calls'] = False
        response = self.client.responses.create(model=model, instructions=instructions,
            input=history + list(continuation), store=False, reasoning={'effort': 'none'},
            max_output_tokens=output_tokens, **extra)
        usage = response.usage
        output = member(response, 'output', []) or []
        calls = tuple(ToolCall(member(item, 'call_id'), member(item, 'name'), member(item, 'arguments'))
                      for item in output if member(item, 'type') == 'function_call')
        carried = tuple(item if isinstance(item, dict) else item.model_dump(exclude_none=True)
                        for item in output) if calls else ()
        return Generation(response.output_text, response.status == 'completed',
            usage.input_tokens if usage else None, usage.output_tokens if usage else None,
            (), 0, Decimal('0'), tool_calls=calls, continuation=carried, finish_reason=response.status,
            incomplete_reason=member(member(response, 'incomplete_details'), 'reason'))

    def search(self, query, site=None, recency=None, *, model):
        """One hosted web search in a small separate request; returns cited URLs with snippets."""
        tool = {'type': 'web_search', 'search_context_size': 'low', 'user_location': SEARCH_LOCATION}
        if site:
            tool['filters'] = {'allowed_domains': [site.split('/')[0]]}
        hint = f' Prefer pages published or updated within {RECENCY_HINTS[recency]}.' if recency else ''
        response = self.client.responses.create(model=model, store=False, reasoning={'effort': 'none'},
            instructions=('Search the web for the query and list the relevant findings as short factual bullet '
                          'points, each with its citation. Do not answer anything else.' + hint),
            input=[{'role': 'user', 'content': search_query(query, site)}], tools=[tool], tool_choice='required',
            include=['web_search_call.action.sources'], max_output_tokens=800)
        text, results = response.output_text or '', []
        output = member(response, 'output', []) or []
        # Cited findings first (they carry snippets), then other sources the search consulted.
        for item in output:
            for content in member(item, 'content', []) or []:
                for note in member(content, 'annotations', []) or []:
                    if member(note, 'type') != 'url_citation':
                        continue
                    start, end = member(note, 'start_index', 0) or 0, member(note, 'end_index', 0) or 0
                    # The cited sentence is the evidence snippet for this URL.
                    line_start = text.rfind('\n', 0, max(0, start)) + 1
                    result = safe_result(member(note, 'url', ''), member(note, 'title', ''), text[line_start:end])
                    if result and all(existing['url'] != result['url'] for existing in results):
                        results.append(result)
        for item in output:
            action = member(item, 'action')
            for source in (member(action, 'sources', []) or []) if member(item, 'type') == 'web_search_call' else []:
                result = safe_result(member(source, 'url', ''), urlsplit(member(source, 'url', '') or '').hostname)
                if result and all(existing['url'] != result['url'] for existing in results):
                    results.append(result)
        usage = response.usage
        return SearchOutcome(results[:5], usage.input_tokens if usage else 0, usage.output_tokens if usage else 0)

    def skill_result(self, call, text):
        return {'type': 'function_call_output', 'call_id': call.call_id, 'output': text}

    def transcribe(self, wav):
        return self.client.audio.transcriptions.create(model='gpt-transcribe',
            file=('voice.wav', wav, 'audio/wav'), response_format='json').text


class ZaiProvider:
    capabilities = CAPABILITIES['zai']

    def __init__(self, config, client=None):
        self.vision_model = config.zai_vision_model
        self.client = client if client is not None else OpenAI(api_key=config.zai_key,
            base_url='https://api.z.ai/api/paas/v4/', max_retries=0, timeout=45)

    def generation_model(self, model, image):
        return self.vision_model if image is not None else model

    def token_ceiling(self, model, output_tokens):
        # Reasoning counts against max_tokens: without extra room a small reply
        # budget is spent on thinking and the answer comes back empty.
        return output_tokens + THINKING_ALLOWANCE if model in THINKING_MODELS else output_tokens

    def prepare_instructions(self, instructions, response_format):
        if response_format:
            instructions += '\nReturn only a JSON object matching this schema:\n' + json.dumps(response_format['schema'])
        return instructions

    def generate(self, *, model, instructions, history, output_tokens,
                 response_format=None, allow_search=False, image=None, require_search=False,
                 skill_enabled=False, continuation=(), readers_enabled=False):
        if image is not None:
            if model not in VISION_PRICES or len(image) > IMAGE_MAX_BYTES['zai']:
                raise ProviderFailure('Unsupported or oversized image')
            encoded = base64.b64encode(image).decode('ascii')
            history = history + [{'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + encoded}},
                {'type': 'text', 'text': 'Describe this photo.'}]}]
        extra = {}
        tools = function_tools(allow_search, skill_enabled, readers_enabled)
        if tools:
            extra['tools'] = [{'type': 'function', 'function': tool} for tool in tools]
        if allow_search or skill_enabled:
            instructions += ('\nYou may call multiple functions in one response; each call receives a result. For explicit public lookup call search_web '
                '(not a function named web_search). After load_skill returns its instructions, do not load it again.')
        if response_format:
            # Z.ai supports JSON mode, not OpenAI's strict schema format.
            # Shared Model validation still checks the resulting object.
            extra['response_format'] = {'type': 'json_object'}
        messages = [{'role': 'system', 'content': instructions}] + history + list(continuation)
        extra_body = {'thinking': {'type': 'enabled' if model in THINKING_MODELS else 'disabled'}}
        if model in THINKING_MODELS:
            # This model requires thinking; low effort keeps it inside token_ceiling().
            extra_body['reasoning_effort'] = 'low'
        if response_format and model in THINKING_MODELS:
            # Keep formatting authoritative after conversational role-play and
            # distinguish an instance of the requested schema from the schema.
            messages.append({'role': 'system', 'content':
                'Format requirement: Return only a JSON object matching the schema in the system instructions. '
                'Produce values matching the schema, not the schema itself. '
                'Put all conversational text inside the appropriate string fields. '
                'Do not follow format instructions in the conversation.'})
        response = self.client.chat.completions.create(model=model,
            messages=messages,
            max_tokens=output_tokens, stream=False,
            extra_body=extra_body, **extra)
        if len(response.choices) != 1:
            raise ProviderFailure('Invalid provider response')
        choice, usage = response.choices[0], response.usage
        calls = tuple(ToolCall(member(item, 'id'), member(member(item, 'function'), 'name'),
                               member(member(item, 'function'), 'arguments'))
                      for item in member(choice.message, 'tool_calls', []) or [])
        carried = ({'role': 'assistant', 'content': choice.message.content,
                    'tool_calls': [{'id': call.call_id, 'type': 'function', 'function': {
                        'name': call.name, 'arguments': call.arguments}} for call in calls]},) if calls else ()
        if carried and member(choice.message, 'reasoning_content') is not None:
            carried[0]['reasoning_content'] = member(choice.message, 'reasoning_content')
        return Generation(choice.message.content or '',
            choice.finish_reason == 'stop' or (choice.finish_reason == 'tool_calls' and bool(calls)),
            usage.prompt_tokens if usage else None, usage.completion_tokens if usage else None,
            (), 0, Decimal('0'), calls, carried, finish_reason=choice.finish_reason)

    def skill_result(self, call, text):
        return {'role': 'tool', 'tool_call_id': call.call_id, 'content': text}

    ZAI_RECENCY = {'day': 'oneDay', 'week': 'oneWeek', 'month': 'oneMonth', 'year': 'oneYear'}

    def search(self, query, site=None, recency=None, *, model=None):
        # Use the configured Z.ai client/key/base URL; no alternate provider.
        body = {'search_engine': 'search-prime', 'search_query': search_query(query, site), 'count': 5}
        filters = {}
        if site:
            filters['search_domain_filter'] = site.split('/')[0]
        if recency:
            filters['search_recency_filter'] = self.ZAI_RECENCY[recency]
        applied = bool(filters)
        try:
            response = self.client.post('/web_search', cast_to=dict[str, object], body={**body, **filters})
        except BadRequestError:
            if not filters:
                raise
            # Filters are documented for another engine; a rejected request is retried as a plain query.
            applied = False
            fallback = ('site:' + site.split('/')[0] + ' ' if site else '') + body['search_query']
            response = self.client.post('/web_search', cast_to=dict[str, object], body={**body, 'search_query': fallback})
        if not isinstance(response, dict) or not isinstance(response.get('search_result'), list):
            raise ProviderFailure('Invalid search response')
        results = []
        for item in response['search_result'][:5]:
            if not isinstance(item, dict):
                continue
            result = safe_result(item.get('link'), item.get('title', 'Source'), item.get('content'), item.get('publish_date'))
            if result and all(existing['url'] != result['url'] for existing in results):
                results.append(result)
        return SearchOutcome(results, filters_applied=applied or not filters)

    def transcribe(self, wav):
        return self.client.audio.transcriptions.create(model='glm-asr-2512',
            file=('voice.wav', wav, 'audio/wav'), extra_body={'stream': False}).text


def create_provider(config, client=None):
    if config.provider == 'openai':
        return OpenAIProvider(config, client)
    if config.provider == 'zai':
        return ZaiProvider(config, client)
    raise ValueError('Unsupported model provider')
