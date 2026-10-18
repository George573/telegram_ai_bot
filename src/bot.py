"""Independent polling with bounded, ordered workers. Never print exceptions."""
import signal
import time
from pathlib import Path
from observability import configure_logging, event
import logging
from concurrent.futures import ThreadPoolExecutor
from chat_actions import deliver_due
from config import Config
from model import Model, RequestPacer
from policy import normalize, normalize_callback
import texts
from service import Service
from storage import Store
from telegram_api import TelegramAPI, TelegramFailure

STOP = False
HEARTBEAT = Path('/tmp/bot-heartbeat')
MAX_UPDATE_FAILURES = 3


def heartbeat():
    try:
        HEARTBEAT.touch()
    except OSError:
        pass


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
                del self.active[chat_id]
                if future.exception() is not None:
                    # Never re-raise into the poller: other chats keep working.
                    event('worker_failed', level=logging.ERROR)
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


def accept_update(service, store, config, identity, update, failures):
    """Accept one update; after repeated unexpected failures, skip it instead of exiting."""
    update_id = update.get('update_id')
    try:
        parse = normalize_callback if 'callback_query' in update else normalize
        service.accept(update_id, parse(update, config.chats, identity))
        failures.pop(update_id, None)
        return True
    except TelegramFailure:
        raise
    except Exception:
        count = failures[update_id] = failures.get(update_id, 0) + 1
        event('update_failed', level=logging.ERROR, update_id=update_id if type(update_id) is int else None,
              failure_count=count, reason='gave_up' if count >= MAX_UPDATE_FAILURES else 'retry_scheduled')
        if type(update_id) is not int:
            return True  # Malformed update: nothing to retry or checkpoint.
        if count < MAX_UPDATE_FAILURES:
            return False
        failures.pop(update_id, None)
        store.enqueue(update_id, None)  # Advance the checkpoint past this update.
        return True


def main():
    configure_logging()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    stage = 'configuration'
    try:
        config = Config.load()
        stage = 'database'
        store = Store(config.history_limit, retention_days=config.memory_retention_days, expire_on_read=False)
        stage = 'telegram'
        telegram = TelegramAPI(config.telegram_token)
        identity = telegram.call('getMe')
        try:
            # Russian command menu; a failure must never block startup.
            telegram.set_commands(texts.PUBLIC_COMMANDS, texts.ADMIN_COMMANDS)
            event('commands_registered', outcome='delivered')
        except TelegramFailure as error:
            event('commands_registered', level=logging.WARNING, outcome='delivery_failed', status_code=error.code)
        if store.discard_backlog_required():
            telegram.call('deleteWebhook', drop_pending_updates=True)
            store.backlog_discarded()
        stage = 'provider'
        service = Service(config, store, telegram, Model(config, store),
                          privacy_enabled=not identity.get('can_read_all_group_messages', False))
        stage = 'running'
        event('bot_started', provider=config.provider, model=config.model, budget_usd=config.budget,
              explicit_search_available=config.provider == 'zai', json_repair_mode='format_only')
        pacer = RequestPacer(config.api_min_interval_seconds)
        next_maintenance = 0.0
        update_failures = {}
        with ThreadPoolExecutor(max_workers=config.worker_count, thread_name_prefix='chat-worker') as workers:
            scheduler = ChatScheduler(workers, config.worker_count,
                lambda chat_id: work_chat(config, chat_id, identity, pacer))
            while not STOP:
                heartbeat()
                if time.monotonic() >= next_maintenance:
                    store.maintenance(scheduler.active)
                    next_maintenance = time.monotonic() + 60
                scheduler.tick(store.runnable_chats())
                try:
                    deliver_due(store, telegram, config.chats)
                except Exception:
                    # A scheduled send must never stop polling; the row stays marked and is not resent.
                    event('scheduled_failed', level=logging.ERROR, reason='unexpected_error')
                try:
                    updates = telegram.call('getUpdates', offset=store.checkpoint(), limit=100,
                        timeout=1, allowed_updates=['message', 'edited_message', 'callback_query'])
                    for update in updates:
                        if not accept_update(service, store, config, identity, update, update_failures):
                            # Keep order: retry from this update on the next poll.
                            time.sleep(1)
                            break
                except TelegramFailure as error:
                    if error.code in {401, 409}:
                        event('bot_stopped', level=logging.ERROR, reason='telegram_access', status_code=error.code)
                        stop()
                        return 1
                    event('poll_failed', level=logging.WARNING, status_code=error.code, retry_after=error.retry_after)
                    # Honor the full server delay, but keep shutdown responsive.
                    deadline = time.monotonic() + max(3, error.retry_after or 3)
                    while not STOP and time.monotonic() < deadline:
                        heartbeat()
                        time.sleep(min(1, max(0, deadline-time.monotonic())))
        event('bot_stopped', reason='shutdown')
        return 0
    except Exception:
        event('bot_failed', level=logging.ERROR, stage=stage)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
