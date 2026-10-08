"""Sequential, durable Telegram polling. Never print secrets or raw exceptions."""
import signal
import time
from observability import configure_logging, event
import logging
from config import Config
from model import Model
from policy import normalize
from service import Service
from storage import Store
from telegram_api import TelegramAPI, TelegramFailure

STOP = False


def stop(*_):
    global STOP
    STOP = True


def main():
    configure_logging()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    stage = 'configuration'
    try:
        config = Config.load()
        stage = 'database'
        store = Store(config.history_limit)
        stage = 'telegram'
        telegram = TelegramAPI(config.telegram_token)
        identity = telegram.call('getMe')
        stage = 'provider'
        service = Service(config, store, telegram, Model(config, store),
                          privacy_enabled=not identity.get('can_read_all_group_messages', False))
        stage = 'running'
        event('bot_started', provider=config.provider, model=config.model, budget_usd=config.budget)
        while not STOP:
            delay = service.process_one()
            if delay is not None:
                if delay:
                    time.sleep(delay)
                continue
            try:
                updates = telegram.call('getUpdates', offset=store.checkpoint(), limit=1,
                    timeout=25, allowed_updates=['message'])
                for update in updates:
                    payload = normalize(update, config.chats, identity)
                    service.accept(update['update_id'], payload)
            except TelegramFailure as error:
                if error.code in {401, 409}:
                    event('bot_stopped', level=logging.ERROR, reason='telegram_access', status_code=error.code)
                    return 1
                event('poll_failed', level=logging.WARNING, status_code=error.code, retry_after=error.retry_after)
                time.sleep(max(3, min(error.retry_after or 3, 60)))
        event('bot_stopped', reason='shutdown')
        return 0
    except Exception:
        event('bot_failed', level=logging.ERROR, stage=stage)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
