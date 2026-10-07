from decimal import Decimal
import json
from openai import OpenAI
from storage import BudgetExceeded

PRICES = {'gpt-6-luna': (Decimal('0.10'), Decimal('0.50')),
          'gpt-5.6-luna': (Decimal('0.20'), Decimal('1.20'))}


class ModelFailure(Exception):
    pass


def estimate_cost(model, instructions, history, output_limit):
    input_rate, output_rate = PRICES[model]
    # UTF-8 bytes upper-bound ordinary text tokens, with allowance for wrappers
    # and cache-write pricing. Deliberately conservative, not billing authority.
    byte_bound = len(json.dumps(history, ensure_ascii=False).encode()) + len(instructions.encode()) + 2048
    return (Decimal(byte_bound) * input_rate * Decimal('1.25') + output_limit * output_rate) / 1000000


class Model:
    def __init__(self, config, store, client=None):
        self.config, self.store = config, store
        self.client = client or OpenAI(api_key=config.openai_key, max_retries=0, timeout=45)

    def generate(self, instructions, history, output_tokens=None, max_chars=3500, response_format=None):
        output_tokens = output_tokens or self.config.max_output_tokens
        if len(instructions) + sum(len(x['content']) for x in history) > self.config.max_input_chars:
            raise ModelFailure('Context too large')
        reserved = estimate_cost(self.config.model, instructions, history, output_tokens)
        self.store.reserve(reserved, self.config.budget)
        # If a request fails or the process crashes, keep the conservative reservation.
        try:
            extra = {'text': {'format': response_format}} if response_format else {}
            response = self.client.responses.create(model=self.config.model,
                instructions=instructions, input=history, store=False,
                reasoning={'effort': 'none'}, max_output_tokens=output_tokens, **extra)
            usage = response.usage
            if usage:
                input_rate, output_rate = PRICES[self.config.model]
                actual = (Decimal(usage.input_tokens) * input_rate * Decimal('1.25') + Decimal(usage.output_tokens) * output_rate) / 1000000
                self.store.reconcile(reserved, actual)
            if response.status != 'completed' or not response.output_text.strip():
                raise ModelFailure('Incomplete response')
            return response.output_text.strip()[:max_chars] if max_chars else response.output_text.strip()
        except ModelFailure:
            raise
        except Exception:
            raise ModelFailure('Model request failed') from None

    def decide_reply(self, instructions, history):
        schema = {'type': 'object', 'additionalProperties': False,
                  'properties': {'respond': {'type': 'boolean'}, 'reply': {'type': 'string'}},
                  'required': ['respond', 'reply']}
        raw = self.generate(instructions + '\nDecide whether joining this conversation adds value. '
            'Prefer silence for unrelated chatter, repetitive jokes or conversations between others. '
            'Return respond=false and an empty reply to stay silent; otherwise include your short reply.',
            history, max_chars=None, response_format={'type': 'json_schema', 'name': 'reply_decision',
                                                     'strict': True, 'schema': schema})
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {'respond', 'reply'}:
                raise ValueError()
            if type(value['respond']) is not bool or not isinstance(value['reply'], str):
                raise ValueError()
            reply = value['reply'].strip()
            if (value['respond'] and not reply) or (not value['respond'] and reply) or len(reply) > 3500:
                raise ValueError()
            return reply if value['respond'] else None
        except (ValueError, TypeError):
            raise ModelFailure('Invalid reply decision') from None
