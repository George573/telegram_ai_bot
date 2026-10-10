"""Expose short skill metadata; read its body only for an allowed tool call."""
from functools import lru_cache
from pathlib import Path


SKILL_PATH = Path(__file__).parent / 'skills' / 'nearby-places' / 'SKILL.md'
SKILL_NAME = 'nearby-places'
LOAD_SKILL = {
    'name': 'load_skill',
    'description': 'Load the full instructions for an available skill when its description matches the task.',
    'parameters': {'type': 'object', 'properties': {'name': {'type': 'string', 'enum': [SKILL_NAME]}},
                   'required': ['name'], 'additionalProperties': False},
}


@lru_cache(maxsize=1)
def catalog():
    with SKILL_PATH.open(encoding='utf-8') as source:
        if source.readline().strip() != '---':
            raise ValueError('Invalid bundled skill metadata')
        fields = {}
        for line in source:
            if line.strip() == '---':
                break
            key, separator, value = line.strip().partition(': ')
            if separator:
                fields[key] = value
        else:
            raise ValueError('Invalid bundled skill metadata')
    if fields.get('name') != SKILL_NAME or not fields.get('description'):
        raise ValueError('Invalid bundled skill metadata')
    return ('Available skill: ' + SKILL_NAME + ' — ' + fields['description'] +
            '\nFor matching place requests, call load_skill before researching or recommending. '
            'For unrelated conversation, do not load it. Skill loading is internal; answer the user normally.')


def instructions():
    content = SKILL_PATH.read_text(encoding='utf-8')
    _, separator, body = content.partition('\n---\n')
    if not content.startswith('---\n') or not separator or not body.strip():
        raise ValueError('Invalid bundled place skill')
    return body.strip()
