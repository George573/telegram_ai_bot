"""Independent polling with bounded, ordered workers. Never print exceptions."""
import signal
import time
from observability import configure_logging, event
import logging
from concurrent.futures import ThreadPoolExecutor
from config import Config
from model import Model, RequestPacer
from policy import normalize
from service import Service
from storage import Store
from telegram_api import TelegramAPI, TelegramFailure

STOP = False


def stop(*_):
    global STOP
    STOP = True


class ChatScheduler:
    def __init__(self, executor, limit, work):
        self.executor, self.limit, self.work = executor, limit, work
        self.active, self.last_scheduled = {}, {}
        self.turn = 0

    def tick(self, chats):
        for chat_id, future in list(self.active.items()):
            if future.done():
                future.result()
                del self.active[chat_id]
        for chat_id in sorted(chats, key=lambda chat: self.last_scheduled.get(chat, -1)):
            if len(self.active) >= self.limit:
                break
            if chat_id not in self.active:
                self.active[chat_id] = self.executor.submit(self.work, chat_id)
                self.last_scheduled[chat_id] = self.turn
                self.turn += 1


def work_chat(config, chat_id, identity, pacer):
    store = Store(config.history_limit, retention_days=config.memory_retention_days, worker=True)
    telegram = TelegramAPI(config.telegram_token)
    model = Model(config, store, pacer=pacer)
    try:
        service = Service(config, store, telegram, model,
                          privacy_enabled=not identity.get('can_read_all_group_messages', False))
        # Yield between turns so a busy group cannot monopolize a worker.
        for _ in range(8):
            if STOP or service.process_one(chat_id) is None:
                break
    finally:
        store.conn.close()
        telegram._http.close()
        model.provider.client.close()


def main():
    configure_logging()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    stage = 'configuration'
    try:
        config = Config.load()
        stage = 'database'
        store = Store(config.history_limit, retention_days=config.memory_retention_days)
        stage = 'telegram'
        telegram = TelegramAPI(config.telegram_token)
        identity = telegram.call('getMe')
        if store.discard_backlog_required():
            telegram.call('deleteWebhook', drop_pending_updates=True)
            store.backlog_discarded()
        stage = 'provider'
        service = Service(config, store, telegram, Model(config, store),
                          privacy_enabled=not identity.get('can_read_all_group_messages', False))
        stage = 'running'
        event('bot_started', provider=config.provider, model=config.model, budget_usd=config.budget)
        pacer = RequestPacer(config.api_min_interval_seconds)
        with ThreadPoolExecutor(max_workers=config.worker_count, thread_name_prefix='chat-worker') as workers:
            scheduler = ChatScheduler(workers, config.worker_count,
                lambda chat_id: work_chat(config, chat_id, identity, pacer))
            while not STOP:
                scheduler.tick(store.runnable_chats())
                try:
                    updates = telegram.call('getUpdates', offset=store.checkpoint(), limit=100,
                        timeout=1, allowed_updates=['message'])
                    for update in updates:
                        payload = normalize(update, config.chats, identity)
                        service.accept(update['update_id'], payload)
                except TelegramFailure as error:
                    if error.code in {401, 409}:
                        event('bot_stopped', level=logging.ERROR, reason='telegram_access', status_code=error.code)
                        stop()
                        return 1
                    event('poll_failed', level=logging.WARNING, status_code=error.code, retry_after=error.retry_after)
                    # Honor the full server delay, but keep shutdown responsive.
                    deadline = time.monotonic() + max(3, error.retry_after or 3)
                    while not STOP and time.monotonic() < deadline:
                        time.sleep(min(1, max(0, deadline-time.monotonic())))
        event('bot_stopped', reason='shutdown')
        return 0
    except Exception:
        event('bot_failed', level=logging.ERROR, stage=stage)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
