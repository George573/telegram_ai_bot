"""Shared Telegram locations: parsing, silent storage and live updates. Synthetic updates only."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from policy import location_line, normalize

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


if __name__ == '__main__':
    unittest.main()
