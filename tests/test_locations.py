"""Shared Telegram locations: parsing, silent storage and live updates. Synthetic updates only."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from policy import location_line, normalize
from config import Config
from service import Service
from test_service import FakeModel, FakeTelegram, MemoryStore
from unittest.mock import patch
import json
from providers import Generation, ToolCall
import test_skill_loading as skill_tests
import skill_registry

BOT = {'id': 99, 'username': 'Cat'}


def update(edited=False, **message):
    body = dict(message_id=7, chat=dict(id=-1, type='group'), **{'from': dict(id=10, is_bot=False)}, **message)
    return {'update_id': 1, 'edited_message' if edited else 'message': body}


class ParseTests(unittest.TestCase):
    def test_point_live_and_venue(self):
        point = normalize(update(location={'latitude': 55.7312049, 'longitude': 37.636301}), {-1}, BOT)
        self.assertEqual(point['location'], {'kind': 'point', 'lat': 55.7312, 'lon': 37.6363, 'title': None, 'address': None})
        self.assertEqual((point['text'], point['directed'], point['command'], point['media'], point['edited']),
                         ('', False, '', None, False))
        self.assertEqual(location_line(point['location']), '[Location 55.73120,37.63630]')
        live = normalize(update(location={'latitude': 55.7, 'longitude': 37.6, 'live_period': 900}), {-1}, BOT)
        self.assertEqual(location_line(live['location']), '[Live location 55.70000,37.60000]')
        venue = normalize(update(location={'latitude': 55.7, 'longitude': 37.6},
                                 venue={'location': {'latitude': 55.7, 'longitude': 37.6},
                                        'title': 'Surf\nCoffee ]x', 'address': 'Стремянный пер., 38 ' + 'я' * 300}), {-1}, BOT)
        line = location_line(venue['location'])
        self.assertTrue(line.startswith('[Venue, untrusted data] «Surf Coffee ]x», Стремянный пер., 38'))
        self.assertTrue(line.endswith(' — 55.70000,37.60000'))
        self.assertNotIn('\n', line)
        self.assertLessEqual(len(venue['location']['address']), 200)

    def test_invalid_coordinates_are_dropped(self):
        for bad in ({'latitude': 91, 'longitude': 37}, {'latitude': 55, 'longitude': -181},
                    {'latitude': True, 'longitude': 37}, {'latitude': '55', 'longitude': 37}, {'latitude': 55}):
            with self.subTest(bad=bad):
                self.assertIsNone(normalize(update(location=bad), {-1}, BOT))

    def test_only_location_edits_are_accepted(self):
        edit = normalize(update(edited=True, location={'latitude': 55.71, 'longitude': 37.61, 'live_period': 900}), {-1}, BOT)
        self.assertTrue(edit['edited'])
        self.assertEqual(edit['location']['kind'], 'live')
        # The final edit when sharing stops has no live_period; it is still the live line.
        final = normalize(update(edited=True, location={'latitude': 55.72, 'longitude': 37.62}), {-1}, BOT)
        self.assertEqual(final['location']['kind'], 'live')
        self.assertIsNone(normalize(update(edited=True, text='@Cat changed text'), {-1}, BOT))

    def test_location_is_never_a_command_or_directed(self):
        located = normalize(update(location={'latitude': 55.7, 'longitude': 37.6},
                                   reply_to_message={'message_id': 3, 'from': {'id': 99}}), {-1}, BOT)
        self.assertFalse(located['directed'])
        self.assertIsNone(located['reply_to_message_id'])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tg, self.model = MemoryStore(), FakeTelegram(), FakeModel()
        self.service = Service(Config('fake-telegram', 'fake-openai', frozenset({-1})), self.store, self.tg, self.model)

    def send(self, update_id, **message):
        edited = message.pop('edited', False)
        self.service.accept(update_id, normalize({**update(edited, **message), 'update_id': update_id}, {-1}, BOT))
        while self.service.process_one() is not None:
            pass

    def test_location_is_stored_silently_in_every_mode(self):
        for mode in ('directed', 'all'):
            with self.subTest(mode=mode):
                self.store.rows.clear()
                self.store.chat['mode'] = mode
                self.send(1, location={'latitude': 55.7312, 'longitude': 37.6363})
                self.assertEqual([r['content'] for r in self.store.rows], ['Speaker 10: [Location 55.73120,37.63630]'])
                self.assertEqual((self.tg.sent, self.model.calls), ([], []))

    def test_paused_chat_ignores_locations(self):
        self.store.chat['paused'] = True
        self.send(1, location={'latitude': 55.7, 'longitude': 37.6})
        self.assertEqual(self.store.rows, [])

    def test_live_updates_rewrite_their_own_line(self):
        self.send(1, location={'latitude': 55.70, 'longitude': 37.60, 'live_period': 900})
        self.send(2, edited=True, location={'latitude': 55.71, 'longitude': 37.61, 'live_period': 900})
        self.send(3, edited=True, location={'latitude': 55.72, 'longitude': 37.62})
        self.assertEqual([r['content'] for r in self.store.rows], ['Speaker 10: [Live location 55.72000,37.62000]'])
        self.assertEqual(self.tg.sent, [])

    def test_edit_without_a_stored_line_is_ignored(self):
        self.send(1, edited=True, location={'latitude': 55.71, 'longitude': 37.61, 'live_period': 900})
        self.assertEqual(self.store.rows, [])

    def test_location_reply_to_a_bot_prompt_is_only_stored(self):
        self.store.save_prompt(-1, 3, 10, '/chat')
        self.send(1, location={'latitude': 55.7, 'longitude': 37.6},
                  reply_to_message={'message_id': 3, 'from': {'id': 99}})
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual((self.tg.sent, self.model.calls), ([], []))


class PromptTests(unittest.TestCase):
    def test_web_search_refuses_coordinates_and_the_reply_continues(self):
        helper = skill_tests.SkillLoadingTests()
        model = helper.model('zai', lambda _: self.fail('No SDK request needed'))
        self.addCleanup(helper.doCleanups)
        searched = []
        model._search_web = lambda query, site=None, recency=None: (searched.append(query), (skill_tests.EVIDENCE, True))[1]
        calls = [ToolCall(f'c{i}', 'search_web', json.dumps({'query': q, 'site': None, 'recency': None}))
                 for i, q in enumerate(('кофейня 55.7312,37.6363', 'cafe near 55.73, 37.63', 'кофейня 1905 года'))]
        with patch.object(model, '_generation_attempt', side_effect=[
                Generation('', True, tool_calls=tuple(calls)), Generation('Готово', True)]) as generate:
            self.assertIn('Готово', model.generate('Persona', [], allow_search=True))
        self.assertEqual(searched, ['кофейня 1905 года'])
        results = [json.loads(i['content']) for i in generate.call_args.kwargs['continuation'] if i.get('role') == 'tool']
        self.assertEqual([r.get('reason') for r in results[:2]], ['coordinates_in_query'] * 2)
        self.assertIn('near', results[0]['notice'])

    def test_instructions_and_skill_explain_shared_locations(self):
        import model
        self.assertIn('[Location', model.LOCATION_INSTRUCTIONS)
        self.assertIn('near', model.LOCATION_INSTRUCTIONS)
        body = skill_registry.instructions('nearby-places')
        self.assertIn('near', body)
        self.assertIn('distance_m', body)


if __name__ == '__main__':
    unittest.main()
