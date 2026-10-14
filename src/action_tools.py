"""Action tool schemas offered to the model; no bot imports, so providers can load them without an import cycle."""
from datetime import timedelta, timezone

# Moscow has used UTC+3 without DST since 2014; naive times from the model are Moscow time.
MOSCOW = timezone(timedelta(hours=3), 'Europe/Moscow')
MAX_SCHEDULE_DAYS = 60
MIN_LEAD_SECONDS = 30
MAX_PENDING_PER_CHAT = 25
MAX_TEXT_CHARS = 3500
# Telegram Bot API sendPoll limits.
POLL_QUESTION_CHARS = 300
POLL_OPTION_CHARS = 100
POLL_MIN_OPTIONS, POLL_MAX_OPTIONS = 2, 10
POLL_EXPLANATION_CHARS = 200
# Per model request: a reply may post a couple of polls or reminders, not flood the chat.
ACTION_LIMITS = {'send_poll': 3, 'schedule_message': 5, 'cancel_scheduled_message': 5,
                 'list_scheduled_messages': 3}
ACTION_ARGUMENT_CHARS = 8000
MAX_DELIVERY_ATTEMPTS = 5

WHEN_PROPERTIES = {
    'send_at': {'type': ['string', 'null'], 'description':
        'Absolute Moscow time "YYYY-MM-DDTHH:MM", e.g. "2026-10-12T09:00". Use for clock times and dates '
        '("tomorrow at 9", "on Friday at 18:30"). null when delay_minutes is used.'},
    'delay_minutes': {'type': ['integer', 'null'], 'description':
        'Whole minutes from now, 1 to 86400. Use for relative requests ("in 20 minutes", "in 2 hours" = 120). '
        'null when send_at is used.'}}

SEND_POLL = {'name': 'send_poll',
    'description': 'Post a native Telegram poll (or quiz) in this group chat, now or at a scheduled time. '
                   'Use it when the requester asks for a poll, vote or quiz, or asks the group to choose between '
                   'concrete options ("let\'s vote where to go", "make a poll: pizza or sushi"). Do not use it '
                   'for rhetorical questions or when nobody asked for a vote. '
                   'Rules: question 1-300 characters; 2-10 distinct options, each 1-100 characters, no numbering '
                   'or bullets. type "quiz" needs correct_option_index (0-based) and allows exactly one answer; '
                   'explanation (quiz only, up to 200 characters) is shown after answering. '
                   'Leave send_at and delay_minutes both null to post immediately; set exactly one of them to '
                   'schedule. is_anonymous=false shows who voted (usual for group decisions). '
                   'If the result has status "error", fix the named field as described and call again.',
    'parameters': {'type': 'object', 'properties': {
        'question': {'type': 'string', 'maxLength': POLL_QUESTION_CHARS},
        'options': {'type': 'array', 'items': {'type': 'string', 'maxLength': POLL_OPTION_CHARS},
                    'description': '2-10 answer options in display order.'},
        'type': {'type': 'string', 'enum': ['regular', 'quiz']},
        'allows_multiple_answers': {'type': 'boolean', 'description': 'Regular polls only; false for quizzes.'},
        'is_anonymous': {'type': 'boolean'},
        'correct_option_index': {'type': ['integer', 'null'],
                                 'description': '0-based index of the right option; quiz only, otherwise null.'},
        'explanation': {'type': ['string', 'null'], 'maxLength': POLL_EXPLANATION_CHARS,
                        'description': 'Quiz only, otherwise null.'},
        **WHEN_PROPERTIES},
        'required': ['question', 'options', 'type', 'allows_multiple_answers', 'is_anonymous',
                     'correct_option_index', 'explanation', 'send_at', 'delay_minutes'],
        'additionalProperties': False}}

SCHEDULE_MESSAGE = {'name': 'schedule_message',
    'description': 'Schedule a text message that the bot will post in this chat later, as a reply to the '
                   'current request. Use it for reminders and delayed announcements the requester asks for '
                   '("remind us tomorrow at 10 to buy tickets", "in 30 minutes tell Anna the pizza is here"). '
                   'Never schedule anything nobody asked for. text is posted verbatim at send time with no AI '
                   'step, so write the complete final message now, in the conversation\'s language, in your '
                   'persona, including who and what it is about (e.g. "Напоминаю: в 19:00 бронь в кафе"). '
                   'Up to 3500 characters; light Markdown is allowed. Set exactly one of send_at or '
                   'delay_minutes; the time must be at least a minute ahead and at most 60 days ahead. '
                   'After status "ok", confirm the scheduled Moscow time to the user. If the result has '
                   'status "error", fix the named field as described and call again.',
    'parameters': {'type': 'object', 'properties': {
        'text': {'type': 'string', 'maxLength': MAX_TEXT_CHARS}, **WHEN_PROPERTIES},
        'required': ['text', 'send_at', 'delay_minutes'], 'additionalProperties': False}}

LIST_SCHEDULED = {'name': 'list_scheduled_messages',
    'description': 'List this chat\'s pending scheduled messages and polls with their ids and Moscow send '
                   'times. Use it when someone asks what is scheduled, and before cancelling or changing a '
                   'reminder when you do not know its id.',
    'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}

CANCEL_SCHEDULED = {'name': 'cancel_scheduled_message',
    'description': 'Cancel one pending scheduled message or poll by its id (from schedule_message, send_poll '
                   'or list_scheduled_messages). People may cancel their own items; group admins may cancel '
                   'any. To change a reminder, cancel it and schedule a new one.',
    'parameters': {'type': 'object', 'properties': {'id': {'type': 'integer'}},
                   'required': ['id'], 'additionalProperties': False}}

TOOLS = (SEND_POLL, SCHEDULE_MESSAGE, LIST_SCHEDULED, CANCEL_SCHEDULED)
NAMES = frozenset(tool['name'] for tool in TOOLS)
KEYS = {tool['name']: frozenset(tool['parameters']['properties']) for tool in TOOLS}
