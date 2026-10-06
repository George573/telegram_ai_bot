import re

def addressed(text, username, reply_to_bot=False):
    return reply_to_bot or bool(re.search(r"(?<![\w@])@" + re.escape(username) + r"(?!\w)", text, re.IGNORECASE))

def valid_limit(value):
    number = int(value)
    if not 10 <= number <= 500:
        raise ValueError("Use a number from 10 to 500")
    return number
