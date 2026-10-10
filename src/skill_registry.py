"""Expose short skill metadata; read a skill body only for an allowed load_skill call."""
from functools import lru_cache
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / 'skills'
# Fixed allowlist: the model can never name a path.
SKILL_NAMES = ('nearby-places', 'city-events')
LOAD_SKILL = {
    'name': 'load_skill',
    'description': 'Load the full instructions for an available skill when its description matches the task.',
    'parameters': {'type': 'object', 'properties': {'name': {'type': 'string', 'enum': list(SKILL_NAMES)}},
                   'required': ['name'], 'additionalProperties': False},
}


def _path(name):
    if name not in SKILL_NAMES:
        raise ValueError('Unknown skill')
    return SKILLS_DIR / name / 'SKILL.md'


@lru_cache(maxsize=len(SKILL_NAMES))
def metadata(name):
    with _path(name).open(encoding='utf-8') as source:
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
    if fields.get('name') != name or not fields.get('description'):
        raise ValueError('Invalid bundled skill metadata')
    return fields['description']


def catalog():
    lines = [f'- {name}: {metadata(name)}' for name in SKILL_NAMES]
    return ('Available skills:\n' + '\n'.join(lines) +
            '\nFor a matching request, call load_skill with that skill name before researching or recommending. '
            'Load only the skills the request needs. For unrelated conversation, do not load any. '
            'Skill loading is internal; answer the user normally.')


def instructions(name):
    content = _path(name).read_text(encoding='utf-8')
    _, separator, body = content.partition('\n---\n')
    if not content.startswith('---\n') or not separator or not body.strip():
        raise ValueError('Invalid bundled skill')
    return body.strip()
