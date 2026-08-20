# -*- coding: utf-8 -*-

MEDIA_COLORS = {
    'movie': 'FFFFD54F',
    'series': 'FF26C6DA',
    'tv': 'FF26C6DA',
}

PROVIDER_COLORS = {
    'RD': 'FF42A5F5',
    'TB': 'FFFF8A65',
    'AD': 'FF66BB6A',
    'PM': 'FFAB47BC',
    'DL': 'FFEC407A',
    'LS': 'FFD4E157',
}

_OTHER_PROVIDER_COLORS = [
    'FF5C6BC0', 'FF26A69A', 'FF8D6E63', 'FF78909C',
    'FF7E57C2', 'FF9CCC65', 'FFFF7043', 'FF29B6F6',
]

STATUS_COLORS = {
    'CACHED': 'FF76FF03',
    'UNCACHED': 'FFFF5252',
    'VERIFY': 'FFFFCA28',
    'UNKNOWN': 'FFB0BEC5',
}

def color_text(text, color):
    return '[COLOR %s]%s[/COLOR]' % (color, str(text))

def media_color(media_type):
    return MEDIA_COLORS.get(str(media_type or '').lower(), 'FFFFFFFF')

def media_text(media_type, text):
    return color_text(text, media_color(media_type))

def media_tag(media_type):
    return media_text(media_type, 'MOVIE' if str(media_type).lower() == 'movie' else 'TV')

def provider_color(code, name=''):
    code = str(code or '').upper()
    if code in PROVIDER_COLORS:
        return PROVIDER_COLORS[code]
    key = (code + '|' + str(name or '')).upper()
    total = sum((i + 1) * ord(ch) for i, ch in enumerate(key))
    return _OTHER_PROVIDER_COLORS[total % len(_OTHER_PROVIDER_COLORS)]

def provider_text(code, text=None, name=''):
    code = str(code or '').upper() or 'RU'
    return color_text(text if text is not None else code, provider_color(code, name))
