"""Turn the light Markdown models write into Telegram HTML (parse_mode=HTML).

Everything is escaped first, so text from users, pages or the model can never inject tags.
Only <b>, <i>, <s> and <code> are produced. Markdown links keep their URL visible
("label (url)") so a label cannot disguise where a link goes.
"""
import html
import re

_CODE = re.compile(r'`([^`\n]{1,300})`')
_LINK = re.compile(r'\[([^\]\n]{1,200})\]\((https?://[^\s)]{1,400})\)')
_BOLD = re.compile(r'(\*\*|__)(?=\S)([^\n]+?)(?<=\S)\1')
_STRIKE = re.compile(r'~~(?=\S)(.+?)(?<=\S)~~')
_ITALIC_STAR = re.compile(r'(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])')
_ITALIC_UNDERSCORE = re.compile(r'(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])')
_HEADING = re.compile(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$', re.M)
_BULLET = re.compile(r'^(\s*)[-*+]\s+', re.M)


def to_html(text):
    if not text:
        return text
    codes = []
    def keep_code(match):
        codes.append(match.group(1))
        return f'\x00{len(codes) - 1}\x00'
    text = _CODE.sub(keep_code, text)
    text = html.escape(text, quote=False)
    text = _LINK.sub(lambda m: f'{m.group(1)} ({m.group(2)})', text)
    text = _HEADING.sub(lambda m: f'<b>{m.group(1)}</b>', text)
    # Bullets before italics, so "* item" is a list marker rather than emphasis.
    text = _BULLET.sub(lambda m: m.group(1) + '• ', text)
    text = _BOLD.sub(lambda m: f'<b>{m.group(2)}</b>', text)
    text = _STRIKE.sub(lambda m: f'<s>{m.group(1)}</s>', text)
    text = _ITALIC_STAR.sub(lambda m: f'<i>{m.group(1)}</i>', text)
    text = _ITALIC_UNDERSCORE.sub(lambda m: f'<i>{m.group(1)}</i>', text)
    return re.sub(r'\x00(\d+)\x00', lambda m: f'<code>{html.escape(codes[int(m.group(1))], quote=False)}</code>', text)
