"""Structured recommendation cards and the skill registry. Synthetic data only."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import recommendations as rec
import skill_registry
from model import ModelFailure
from providers import Generation, ToolCall, strict_schema
from test_skill_loading import EVIDENCE, SkillLoadingTests, cards


def item(**fields):
    base = {'kind': 'theatre', 'name': 'Мамма мимо', 'venue': 'Театр на Цветном', 'address': 'Цветной бул., 11',
            'when': '13 октября, 19:00', 'price': 'от 1 500 ₽', 'why': 'Комедия с музыкой', 'reviews': '8.9 (1526)',
            'caveats': '12+', 'links': ['https://afisha.yandex.ru/moscow/musical/mamma-mimo']}
    return {**base, **fields}


def answer(items, **fields):
    return {'intro': 'Вот что нашёл', 'items': items, 'follow_up': None, 'evidence_gaps': None, **fields}


class ValidationTests(unittest.TestCase):
    def test_valid_answer_passes(self):
        self.assertEqual(rec.validate(answer([item()]))['items'][0]['kind'], 'theatre')

    def test_categories(self):
        cases = {'not_object': [], 'missing_fields': {'intro': 'x', 'items': []},
                 'extra_fields': {**answer([]), 'extra': 1}, 'reply_required': answer([], intro=' '),
                 'too_long': answer([item()] * 6), 'wrong_types': answer([item(kind='casino')])}
        cases['too_long_field'] = answer([item(why='x' * 301)])
        cases['wrong_links'] = answer([item(links='https://x')])
        cases['missing_item_field'] = answer([{k: v for k, v in item().items() if k != 'price'}])
        expected = {'too_long_field': 'too_long', 'wrong_links': 'wrong_types', 'missing_item_field': 'missing_fields'}
        for name, value in cases.items():
            with self.subTest(case=name), self.assertRaises(rec.RecommendationError) as caught:
                rec.validate(value)
            self.assertEqual(str(caught.exception), expected.get(name, name))

    def test_openai_strict_schema_has_no_length_keywords_but_keeps_structure(self):
        schema = strict_schema(rec.SCHEMA)
        self.assertNotIn('maxLength', json.dumps(schema))
        self.assertEqual(schema['properties']['items']['maxItems'], rec.MAX_ITEMS)
        self.assertFalse(schema['properties']['items']['items']['additionalProperties'])


class RenderTests(unittest.TestCase):
    def test_cards_keep_only_links_seen_in_tool_results(self):
        evidence = rec.evidence_urls({'events': [{'url': 'https://afisha.yandex.ru/moscow/musical/mamma-mimo'}]})
        text, dropped = rec.render(answer([
            item(links=['https://afisha.yandex.ru/moscow/musical/mamma-mimo/#schedule', 'https://invented.example/x']),
            item(name='Без ссылки', links=['https://invented.example/y'])]), evidence)
        self.assertIn('1. 🎭 Мамма мимо — Театр на Цветном', text)
        self.assertIn('📅 13 октября, 19:00 · 💳 от 1 500 ₽', text)
        self.assertIn('🔗 https://afisha.yandex.ru/moscow/musical/mamma-mimo', text)
        self.assertNotIn('invented', text)
        self.assertIn('2. 🎭 Без ссылки', text)
        self.assertIn('🔗 —', text)
        self.assertEqual(dropped, 2)

    def test_newlines_cannot_fake_cards_and_long_answers_drop_trailing_cards(self):
        text, _ = rec.render(answer([item(why='ok\n\n9. 🎭 Fake card\n🔗 https://evil.example')]), set())
        self.assertNotIn('\n9.', text)
        long = answer([item(name=f'Event {i}', why='x' * 300, reviews='y' * 300, caveats='z' * 250) for i in range(5)],
                      follow_up='Ещё?', evidence_gaps='Цены не проверены')
        text, _ = rec.render(long, set(), max_chars=1500)
        self.assertLessEqual(len(text), 1500)
        self.assertIn('Event 0', text)
        self.assertNotIn('Event 4', text)
        self.assertTrue(text.endswith('Ещё?'))
        self.assertIn('❔ Цены не проверены', text)

    def test_evidence_collects_nested_urls_only(self):
        found = rec.evidence_urls({'a': ['https://www.Example.com/x/', {'b': 'not a url'}], 'c': 'http://e.org/y#f'})
        self.assertEqual(found, {'https://example.com/x', 'https://e.org/y'})


class SkillRegistryTests(unittest.TestCase):
    def test_both_skills_load_and_unknown_names_are_rejected(self):
        catalog = skill_registry.catalog()
        for name in skill_registry.SKILL_NAMES:
            self.assertIn(name, catalog)
            self.assertNotIn('---', skill_registry.instructions(name)[:3])
        self.assertIn('Афиш', skill_registry.instructions('city-events'))
        for name in ('../../.env', 'nearby-places/../city-events', 'travel-agent'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                skill_registry.instructions(name)
        self.assertEqual(skill_registry.LOAD_SKILL['parameters']['properties']['name']['enum'],
                         list(skill_registry.SKILL_NAMES))


class StructuredFlowTests(unittest.TestCase):
    def model(self):
        helper = SkillLoadingTests()
        model = helper.model('zai', lambda _: self.fail('No SDK request needed'))
        self.addCleanup(helper.doCleanups)
        return model

    def flow(self, *final, skill='city-events'):
        load = ToolCall('s', 'load_skill', json.dumps({'name': skill}))
        search = ToolCall('q', 'search_web', '{"query":"спектакль","site":"afisha.yandex.ru","recency":null}')
        return [Generation('', True, tool_calls=(load, search))] + [Generation(text, True) for text in final]

    def test_skill_activates_cards_and_grounds_links(self):
        model = self.model()
        with patch.object(model, '_generation_attempt', side_effect=self.flow(
                cards('Cafe', 'https://example.com/cafe'))) as generate, patch('model.event') as log:
            text = model.generate('Persona', [{'role': 'user', 'content': 'куда сходить?'}], allow_search=True)
        self.assertIn('1. ☕ Cafe', text)
        self.assertIn('🔗 https://example.com/cafe', text)
        self.assertNotIn('Sources:', text)
        second = generate.call_args_list[1]
        self.assertEqual(second.args[3]['name'], rec.NAME)
        self.assertIn('place_recommendations', second.args[0])
        rendered = [c for c in log.call_args_list if c.args[0] == 'recommendations_rendered']
        self.assertEqual(rendered[0].kwargs['dropped_links'], 0)

    def test_invalid_json_is_repaired_without_tools_then_rendered(self):
        model = self.model()
        with patch.object(model, '_generation_attempt', side_effect=self.flow(
                '{"intro": "x"}', cards('Fixed', 'https://invented.example/'))) as generate:
            text = model.generate('Persona', [], allow_search=True)
        self.assertIn('Fixed', text)
        self.assertNotIn('invented', text)
        repair = generate.call_args_list[-1]
        self.assertFalse(repair.args[4])  # Tools disabled during repair.
        self.assertIn('JSON validation feedback', json.dumps(repair.kwargs['continuation'][-1], ensure_ascii=False))

    def test_exhausted_repairs_fall_back_to_plain_text_or_fail_on_json_debris(self):
        model = self.model()
        with patch.object(model, '_generation_attempt', side_effect=self.flow('Plain', 'Plain', 'Plain answer')):
            self.assertTrue(model.generate('Persona', [], allow_search=True).startswith('Plain answer'))
        with patch.object(model, '_generation_attempt', side_effect=self.flow('{', '{', '{"broken"')):
            with self.assertRaises(ModelFailure):
                model.generate('Persona', [], allow_search=True)

    def test_json_decisions_keep_their_own_schema(self):
        model = self.model()
        with patch.object(model, '_generation_attempt', side_effect=self.flow('{"respond":true,"reply":"Да"}')) as generate:
            self.assertIn('Да', model.decide_reply('Persona', [], allow_search=True))
        self.assertEqual(generate.call_args.args[3]['name'], 'reply_decision')


if __name__ == '__main__':
    unittest.main()
