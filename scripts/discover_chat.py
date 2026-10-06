"""Local setup helper. Outputs identifiers only; never emits credentials/payloads."""
import argparse
import json
import logging
from pathlib import Path
import sys
import urllib.request

logging.disable(logging.CRITICAL)


def telegram_token(path):
    # The application, not the assistant, reads the secret locally.
    with path.open() as stream:
        for line in stream:
            key, separator, value = line.strip().partition('=')
            if separator and key.strip() == 'TELEGRAM_BOT_TOKEN':
                return value.strip().strip('\"\'')
    return ''


def call(token, method, params):
    request = urllib.request.Request(
        'https://api.telegram.org/bot' + token + '/' + method,
        data=json.dumps(params).encode(), headers={'Content-Type': 'application/json'})
    # Avoid proxies inherited from the development environment.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=15) as response:
        data = json.load(response)
    if not data.get('ok'):
        raise RuntimeError('Telegram request failed')
    return data['result']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', type=int)
    args = parser.parse_args()
    try:
        token = telegram_token(Path(__file__).resolve().parents[1] / '.env')
        if not token:
            print('SETUP_ERROR: TELEGRAM_BOT_TOKEN is missing; no credential displayed.')
            return 1
        if args.candidate:
            try:
                chat = call(token, 'getChat', {'chat_id': args.candidate})
                if chat.get('type') in {'group', 'supergroup'}:
                    print('VERIFIED_GROUP_CHAT_ID=' + str(chat['id']))
                    return 0
            except Exception:
                pass
        # No offset: inspect pending updates without acknowledging/deleting them.
        updates = call(token, 'getUpdates', {'timeout': 0, 'limit': 100, 'allowed_updates': ['message']})
        ids = sorted({u['message']['chat']['id'] for u in updates
            if u.get('message', {}).get('chat', {}).get('type') in {'group', 'supergroup'}})
        for chat_id in ids:
            print('GROUP_CHAT_ID=' + str(chat_id))
        if not ids:
            print('NO_GROUP_UPDATE: Send /help to the bot in your group, then run this helper again.')
        return 0
    except Exception:
        print('SETUP_ERROR: Unable to contact Telegram. Check token, connectivity, and that no other bot instance is polling. No credential displayed.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
