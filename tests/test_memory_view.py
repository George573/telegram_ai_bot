"""Older long bot replies are shortened for the model; the newest stay whole for follow-ups."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from policy import SHORTENED_NOTE, memory_view, shorten_reply
from config import Config
from service import Service
from test_service import MemoryStore, FakeTelegram, FakeModel, payload

OLD_CARD = ('Вот варианты на вечер.\n\n'
            '1. ☕ **Кофейня Зерно** — у метро\n📅 ежедневно 08:00–22:00 · 💳 от 300 ₽\n📍 Уральская ул., 1\n'
            '💡 ' + 'Тихо и уютно. ' * 30 + '\n⭐ ' + 'Хвалят кофе. ' * 20 + '\n⚠️ Бывает очередь.\n↔ Не матча.\n'
            '🔗 https://yandex.ru/maps/org/1/\n\n'
            '2. 🎭 **Спектакль** — Театр\n📅 сб 19:00 · 💳 2000 ₽\n💡 Смешно.\n🔗 —\n\n'
            '❔ Цены не проверены.\n\nСкажите, какой район удобнее.')
NEW_CARD = ('Вот варианты на вечер.\n\n'
            '1. **Кофейня Зерно**\nАдрес: Уральская ул., 1\nРейтинг: 4,9 (210)\n' + 'Тихо и уютно. ' * 30 +
            '\nhttps://yandex.ru/maps/org/1/\n\n'
            '2. **Спектакль**\nАдрес: Театр, 2\nКогда: сб 19:00\n' + 'Смешно. ' * 30 +
            '\n9. Не карточка, а пункт списка\nhttps://afisha.yandex.ru/x\n\n'
            'Не удалось подтвердить:\n- Кафе Призрак — не нашлось на Картах\n- Бар Тень\n\n'
            'Скажите, какой район удобнее.')


class MemoryViewTests(unittest.TestCase):
    def test_card_replies_keep_only_place_names(self):
        for card, names in ((NEW_CARD, 'Кофейня Зерно, Спектакль, Кафе Призрак, Бар Тень'),
                            (OLD_CARD, 'Кофейня Зерно, Спектакль')):
            with self.subTest(names=names):
                self.assertEqual(shorten_reply(card), SHORTENED_NOTE + f'[Earlier recommendations: {names}]')

    def test_fake_heading_in_text_is_not_a_name(self):
        self.assertNotIn('Не карточка', shorten_reply(NEW_CARD))

    def test_bold_numbered_steps_are_not_cards(self):
        text = ('Как начать.\n\n1. **Установка**\n' + 'Скачайте пакет и распакуйте. ' * 20 +
                '\nДокументация: https://example.com/docs\n\n2. **Настройка**\n' + 'Заполните файл настроек. ' * 20)
        short = shorten_reply(text)
        self.assertTrue(short.startswith(SHORTENED_NOTE))
        self.assertIn('Документация: https://example.com/docs', short)
        self.assertNotIn('Earlier recommendations', short)

    def test_plain_text_keeps_opening_and_source_links(self):
        text = 'Начало ответа.\n' + 'Длинное объяснение строки.\n' * 60 + 'Источники:\nСайт: https://example.com/a'
        short = shorten_reply(text)
        self.assertIn('Начало ответа.', short)
        self.assertIn('…\nСайт: https://example.com/a', short)
        self.assertLess(len(short), 800)

    def test_small_savings_keep_the_original(self):
        text = 'Строка ответа.\n' * 60
        self.assertEqual(shorten_reply(text[:700]), text[:700])

    def test_only_older_long_replies_are_shortened(self):
        rows = [{'role': 'user', 'content': 'Вопрос ' * 200},
                {'role': 'assistant', 'content': NEW_CARD},
                {'role': 'assistant', 'content': 'Короткий ответ'},
                {'role': 'user', 'content': 'Ещё'},
                {'role': 'assistant', 'content': OLD_CARD},
                {'role': 'assistant', 'content': NEW_CARD},
                {'role': 'assistant', 'content': OLD_CARD}]
        view = memory_view(rows)
        self.assertEqual([row['role'] for row in view], [row['role'] for row in rows])
        self.assertEqual(view[0]['content'], rows[0]['content'])  # People's messages are never shortened.
        self.assertTrue(view[1]['content'].startswith(SHORTENED_NOTE))
        self.assertEqual(view[2]['content'], 'Короткий ответ')
        self.assertEqual([row['content'] for row in view[4:]], [OLD_CARD, NEW_CARD, OLD_CARD])  # Three newest whole.


class ServiceMemoryViewTests(unittest.TestCase):
    def test_generation_uses_shortened_history_but_store_keeps_full_text(self):
        self.store, self.model = MemoryStore(), FakeModel()
        service = Service(Config('fake-telegram', 'fake-openai', frozenset({-1})), self.store, FakeTelegram(), self.model)
        for text in (NEW_CARD, NEW_CARD, NEW_CARD, NEW_CARD):
            self.store.append(-1, 'assistant', text)
        service.accept(1, payload('hello @Cat'))
        for _ in range(8):
            if service.process_one() is None:
                break
        history = [h for instructions, h in self.model.calls if 'Summarize' not in instructions][-1]
        contents = [row['content'] for row in history if row['role'] == 'assistant']
        self.assertTrue(contents[0].startswith(SHORTENED_NOTE))
        self.assertEqual(contents[1:4], [NEW_CARD] * 3)
        self.assertTrue(all(row['content'] in (NEW_CARD, 'Meow') for row in self.store.rows if row['role'] == 'assistant'))


if __name__ == '__main__':
    unittest.main()
