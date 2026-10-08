import sys
from pathlib import Path
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bot import ChatScheduler


class WorkerTests(unittest.TestCase):
    def test_busy_chat_does_not_starve_other_chats(self):
        calls = []
        class Executor:
            def submit(self, work, chat):
                calls.append(chat)
                future = Future()
                future.set_result(None)
                return future
        scheduler = ChatScheduler(Executor(), 1, lambda _: None)
        for _ in range(3):
            scheduler.tick([-3, -2, -1])
        self.assertEqual(calls, [-3, -2, -1])

    def test_blocked_chat_runs_alongside_other_chat_without_duplicate_worker(self):
        started, release, other_done = Event(), Event(), Event()
        calls = []
        def work(chat):
            calls.append(chat)
            if chat == -1:
                started.set()
                release.wait(2)
            else:
                other_done.set()
        with ThreadPoolExecutor(max_workers=2) as executor:
            scheduler = ChatScheduler(executor, 2, work)
            try:
                scheduler.tick([-1, -2])
                self.assertTrue(started.wait(1))
                self.assertTrue(other_done.wait(1))
                scheduler.tick([-1])
                self.assertEqual(calls.count(-1), 1)
            finally:
                release.set()
