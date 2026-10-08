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
from openai import OpenAI

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
TRANSCRIPTION_PER_MINUTE = TRANSCRIPTION_RATES['openai']


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

    def prepare_instructions(self, instructions, response_format):
        return instructions

    def generate(self, *, model, instructions, history, output_tokens,
                 response_format=None, allow_search=False, image=None):
        extra = {'text': {'format': response_format}} if response_format else {}
        if image is not None:
            encoded = base64.b64encode(image).decode('ascii')
            history = history + [{'role': 'user', 'content': [
                {'type': 'input_image', 'image_url': 'data:image/jpeg;base64,' + encoded, 'detail': 'low'}]}]
        if allow_search:
            extra.update(tools=[{'type': 'web_search', 'search_context_size': 'low'}],
                         tool_choice='auto', max_tool_calls=1)
        response = self.client.responses.create(model=model, instructions=instructions,
            input=history, store=False, reasoning={'effort': 'none'},
            max_output_tokens=output_tokens, **extra)
        usage = response.usage
        return Generation(response.output_text, response.status == 'completed',
            usage.input_tokens if usage else None, usage.output_tokens if usage else None,
            tuple(citation_sources(response)) if allow_search else (),
            sum(member(item, 'type') == 'web_search_call' for item in member(response, 'output', []) or []))

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

    def prepare_instructions(self, instructions, response_format):
        if response_format:
            instructions += '\nReturn only a JSON object matching this schema:\n' + json.dumps(response_format['schema'])
        return instructions

    def generate(self, *, model, instructions, history, output_tokens,
                 response_format=None, allow_search=False, image=None):
        if image is not None:
            if model not in VISION_PRICES or len(image) > IMAGE_MAX_BYTES['zai']:
                raise ProviderFailure('Unsupported or oversized image')
            encoded = base64.b64encode(image).decode('ascii')
            history = history + [{'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + encoded}},
                {'type': 'text', 'text': 'Describe this photo.'}]}]
        extra = {}
        if allow_search:
            extra['tools'] = [{'type': 'web_search', 'web_search': {
                'enable': True, 'search_engine': 'search-prime', 'search_result': True,
                'count': 3, 'content_size': 'low'}}]
        if response_format:
            # Z.ai supports JSON mode, not OpenAI's strict schema format.
            # Shared Model validation still checks the resulting object.
            extra['response_format'] = {'type': 'json_object'}
        messages = [{'role': 'system', 'content': instructions}] + history
        extra_body = {'thinking': {'type': 'enabled' if model == 'glm-5.3-flash' else 'disabled'}}
        if response_format and model == 'glm-5.3-flash':
            # This model requires thinking; max effort can exhaust small output
            # budgets before producing JSON. Keep the existing token ceiling.
            extra_body['reasoning_effort'] = 'low'
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
        results = (member(response, 'web_search', []) or []) if allow_search else []
        annotations = [{'type': 'url_citation', 'title': member(item, 'title', 'Source'),
                        'url': member(item, 'link', '')} for item in results]
        sources = citation_sources({'output': [{'type': 'message', 'content': [{'annotations': annotations}]}]})
        return Generation(choice.message.content or '', choice.finish_reason == 'stop',
            usage.prompt_tokens if usage else None, usage.completion_tokens if usage else None,
            tuple(sources), int(bool(results)), WEB_CALL_COST if allow_search else Decimal('0'))

    def transcribe(self, wav):
        return self.client.audio.transcriptions.create(model='glm-asr-2512',
            file=('voice.wav', wav, 'audio/wav'), extra_body={'stream': False}).text


def create_provider(config, client=None):
    if config.provider == 'openai':
        return OpenAIProvider(config, client)
    if config.provider == 'zai':
        return ZaiProvider(config, client)
    raise ValueError('Unsupported model provider')
