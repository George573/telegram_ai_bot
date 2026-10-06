"""Sequential, durable Telegram polling. Never print secrets or raw exceptions."""
import logging
import signal
import time
from config import Config
from model import Model
from policy import normalize
from service import Service
from storage import Store
from telegram_api import TelegramAPI, TelegramFailure

logging.disable(logging.CRITICAL)
STOP = False


def stop(*_):
    global STOP
    STOP = True


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        config = Config.load()
        store = Store(config.history_limit)
        telegram = TelegramAPI(config.telegram_token)
        identity = telegram.call('getMe')
        service = Service(config, store, telegram, Model(config, store))
        print('Bot started; local persistent storage ready.', flush=True)
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
                    print('Bot stopped: invalid Telegram access or another polling instance.', flush=True)
                    return 1
                time.sleep(max(3, min(error.retry_after or 3, 60)))
        print('Bot stopped cleanly.', flush=True)
        return 0
    except Exception:
        print('Bot stopped safely. Check local configuration, database connectivity, and model access; no secrets displayed.', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
