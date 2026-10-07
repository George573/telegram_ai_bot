import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from policy import addressed, valid_limit

class PolicyTests(unittest.TestCase):
    def test_explicit_mention_and_reply(self):
        self.assertTrue(addressed('hello @CatBot!', 'catbot'))
        self.assertTrue(addressed('hello', 'catbot', True))
    def test_unrelated_messages_and_similar_usernames(self):
        for text in ['hello', '@CatBotOther', 'email@catbot', '@@catbot']:
            self.assertFalse(addressed(text, 'catbot'))
    def test_limits(self):
        for value in ['10', '50', '500']:
            self.assertEqual(valid_limit(value), int(value))
        for value in ['0', '9', '501', 'no']:
            with self.assertRaises(ValueError):
                valid_limit(value)

    def test_character_names_match_whole_names(self):
        from personality import named
        state = {'personality': {'names': ['Cat', 'Mr. Whiskers', 'Кот']}}
        self.assertTrue(named('Hi CAT!', state))
        self.assertTrue(named('Эй Кот!', state))
        self.assertTrue(named('Mr. Whiskers?', state))
        self.assertFalse(named('caterpillar scatter @Catfish', state))

    def test_personality_display_fits_telegram(self):
        from personality import describe
        state = {'personality': dict(style='s'*800, when='w'*800, lore='l'*2000,
                                    names=['n'*40]*10, language='x'*80)}
        self.assertLessEqual(len(describe(state)), 3500)
        self.assertIn('stored settings are intact', describe(state))
