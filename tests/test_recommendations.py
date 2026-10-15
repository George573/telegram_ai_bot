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


AFISHA = 'https://afisha.yandex.ru/moscow/musical/mamma-mimo'
EVENT_EVIDENCE = {'https://afisha.yandex.ru/moscow/musical/mamma-mimo'}
FIXED_EMOJI = ('📅', '💳', '📍', '💡', '⭐', '⚠️', '↔', '🔗', '❔', '🎭', '☕')


def item(**fields):
    base = {'name': 'Мамма мимо', 'address': 'Цветной бул., 11', 'rating': 'Афиша 8,9 (1526)',
            'when': '13 октября, 19:00', 'text': 'Комедия с музыкой, билеты от 1 500 ₽.', 'links': [AFISHA]}
    return {**base, **fields}


def answer(items, **fields):
    return {'intro': 'Вот что нашёл', 'items': items, 'unconfirmed': [], 'outro': None, **fields}


class ValidationTests(unittest.TestCase):
    def test_minimal_answer_with_nulls_passes(self):
        value = rec.validate(answer([item(address=None, rating=None, when=None, links=[])]))
        self.assertIsNone(value['items'][0]['rating'])

    def test_json_mode_defaults(self):
        value = rec.validate({'intro': 'x', 'items': []})
        self.assertEqual((value['unconfirmed'], value['outro']), ([], None))

    def test_categories(self):
        cases = {'not_object': [], 'missing_fields': {'intro': 'x'},
                 'extra_fields': {**answer([]), 'extra': 1}, 'reply_required': answer([], intro=' '),
                 'too_long': answer([item()] * 6), 'wrong_types': answer([item(text=' ')])}
        cases['too_long_text'] = answer([item(text='x' * 601)])
        cases['too_many_unconfirmed'] = answer([], unconfirmed=[{'name': 'A', 'note': None}] * 4)
        cases['wrong_links'] = answer([item(links='https://x')])
        cases['empty_name'] = answer([item(name='')])
        cases['old_field'] = answer([item(kind='cafe')])
        cases['missing_item_field'] = answer([{k: v for k, v in item().items() if k != 'rating'}])
        cases['missing_note'] = answer([], unconfirmed=[{'name': 'A'}])
        expected = {'too_long_text': 'too_long', 'too_many_unconfirmed': 'too_long', 'wrong_links': 'wrong_types',
                    'empty_name': 'wrong_types', 'old_field': 'extra_fields',
                    'missing_item_field': 'missing_fields', 'missing_note': 'missing_fields'}
        for name, value in cases.items():
            with self.subTest(case=name), self.assertRaises(rec.RecommendationError) as caught:
                rec.validate(value)
            self.assertEqual(str(caught.exception), expected.get(name, name))

    def test_openai_strict_schema_has_no_length_keywords_but_keeps_structure(self):
        schema = strict_schema(rec.SCHEMA)
        self.assertNotIn('maxLength', json.dumps(schema))
        self.assertEqual(schema['required'], ['intro', 'items', 'unconfirmed', 'outro'])
        self.assertEqual(schema['properties']['items']['maxItems'], rec.MAX_ITEMS)
        self.assertEqual(schema['properties']['unconfirmed']['maxItems'], rec.MAX_UNCONFIRMED)
        self.assertFalse(schema['properties']['items']['items']['additionalProperties'])
        self.assertEqual(schema['properties']['items']['items']['required'], list(rec.ITEM_FIELDS))


class RenderTests(unittest.TestCase):
    def test_card_shows_labelled_facts_free_text_and_grounded_links_without_fixed_emoji(self):
        result = rec.render(answer([item(links=[AFISHA + '/#schedule', 'https://invented.example/x'])]), EVENT_EVIDENCE)
        self.assertEqual(result.text, 'Вот что нашёл\n\n1. **Мамма мимо**\nАдрес: Цветной бул., 11\n'
                         'Рейтинг: Афиша 8,9 (1526)\nКогда: 13 октября, 19:00\n'
                         'Комедия с музыкой, билеты от 1 500 ₽.\n' + AFISHA)
        self.assertEqual((result.cards, result.dropped_links, result.demoted_items), (1, 1, 0))
        self.assertFalse(any(emoji in result.text for emoji in FIXED_EMOJI))

    def test_null_facts_are_omitted_and_model_formatting_kept(self):
        text = rec.render(answer([item(rating=None, when=' ', text='🎶 **Весело**\n\n\nИдти с друзьями')]),
                          EVENT_EVIDENCE).text
        self.assertNotIn('Рейтинг:', text)
        self.assertNotIn('Когда:', text)
        self.assertIn('Адрес: Цветной бул., 11\n🎶 **Весело**\nИдти с друзьями\n' + AFISHA, text)

    def test_items_without_address_or_grounded_link_are_demoted(self):
        result = rec.render(answer([item(name='Без адреса', address=' '), item(name='Без ссылки',
                            links=['https://invented.example/y']), item(name='Настоящий')],
                            unconfirmed=[{'name': 'Слух', 'note': 'видел в канале, не проверил'}]), EVENT_EVIDENCE)
        self.assertIn('1. **Настоящий**', result.text)
        self.assertTrue(result.text.endswith('Не удалось подтвердить:\n- Слух — видел в канале, не проверил\n'
                                             '- Без адреса\n- Без ссылки'))
        self.assertNotIn('invented', result.text)
        self.assertEqual((result.cards, result.unconfirmed, result.demoted_items), (1, 3, 2))

    def test_unconfirmed_list_is_capped(self):
        result = rec.render(answer([item(name=f'D{i}', address=None) for i in range(3)],
                                   unconfirmed=[{'name': 'U', 'note': None}]), set())
        self.assertIn('- U\n- D0\n- D1', result.text)
        self.assertNotIn('D2', result.text)
        self.assertEqual(result.unconfirmed, 3)

    def test_all_demoted_answer_has_no_cards_flag(self):
        result = rec.render(answer([item(links=[])], outro='Могу поискать ещё'), EVENT_EVIDENCE)
        self.assertEqual(result.cards, 0)
        self.assertEqual(result.text, 'Вот что нашёл\n\nНе удалось подтвердить:\n- Мамма мимо\n\nМогу поискать ещё')

    def test_excluded_chains_vanish_from_both_lists(self):
        result = rec.render(answer([item(name='Surf Coffee x Boom')],
                                   unconfirmed=[{'name': 'Surf Coffee', 'note': None}]),
                            EVENT_EVIDENCE, excluded_names=['Surf Coffee'])
        self.assertNotIn('Surf', result.text)
        self.assertEqual(result.dropped_items, 2)

    def test_free_text_links_are_grounded(self):
        result = rec.render(answer([item(text='Сайт [театра](https://evil.example/a) и https://evil.example/b. '
                                          'Афиша: ' + AFISHA)], intro='См. https://evil.example/c',
                                   outro='[Ещё](https://evil.example/d)'), EVENT_EVIDENCE)
        self.assertNotIn('evil', result.text)
        self.assertIn('Сайт театра и', result.text)
        self.assertIn('Афиша: ' + AFISHA, result.text)
        self.assertEqual(result.dropped_links, 4)

    def test_text_cannot_fake_structure(self):
        text = rec.render(answer([item(text='ok\n9. **Fake card**\nАдрес: Кремль\nНе удалось подтвердить: всё')],
                                 intro='1. **Fake** intro'), EVENT_EVIDENCE).text
        self.assertNotIn('\n9.', text)
        self.assertFalse(text.startswith('1.'))
        self.assertIn('\n**Fake card**\n– Адрес: Кремль\n– Не удалось подтвердить: всё\n', text)
        self.assertEqual(text.count('\nАдрес: '), 1)

    def test_long_answers_drop_trailing_cards(self):
        long = answer([item(name=f'Event {i}', text='x' * 600) for i in range(5)], outro='Ещё?',
                      unconfirmed=[{'name': 'Слух', 'note': None}])
        result = rec.render(long, EVENT_EVIDENCE, max_chars=1500)
        self.assertLessEqual(len(result.text), 1500)
        self.assertIn('Event 0', result.text)
        self.assertNotIn('Event 4', result.text)
        self.assertTrue(result.text.endswith('- Слух\n\nЕщё?'))
        self.assertEqual(result.cards, result.text.count('**Event'))

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
        self.assertIn('1. **Cafe**', text)
        self.assertIn('\nhttps://example.com/cafe', text)
        self.assertTrue(text.cards)
        self.assertNotIn('Sources:', text)
        second = generate.call_args_list[1]
        self.assertEqual(second.args[3]['name'], rec.NAME)
        self.assertIn('place_recommendations', second.args[0])
        rendered = [c for c in log.call_args_list if c.args[0] == 'recommendations_rendered']
        self.assertEqual(rendered[0].kwargs['dropped_links'], 0)
        self.assertEqual(rendered[0].kwargs['result_count'], 1)
        self.assertEqual(rendered[0].kwargs['demoted_items'], 0)

    def test_unverified_answer_is_delivered_without_cards_flag(self):
        model = self.model()
        with patch.object(model, '_generation_attempt', side_effect=self.flow(
                cards('Ghost', 'https://invented.example/'))):
            text = model.generate('Persona', [], allow_search=True)
        self.assertIn('Не удалось подтвердить:\n- Ghost', text)
        self.assertFalse(text.cards)

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
