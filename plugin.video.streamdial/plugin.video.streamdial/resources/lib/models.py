"""Normalized channel, schedule, language, and genre helpers."""

import datetime
import html
import re
import unicodedata


ALL_PROVIDERS = "all"
ALL_LANGUAGES = "all"
ALL_GENRES = "all"
UNKNOWN_LANGUAGE = "English"

GENRE_ORDER = [
    "News", "Sports", "Movies", "Comedy", "Drama", "Kids & Family",
    "Crime", "Reality", "Lifestyle", "Food", "Music", "History",
    "Science & Nature", "Weather", "International", "Entertainment", "Other",
]

GENRE_KEYWORDS = {
    "News": ("news", "headlines", "current affairs", "politics", "business news", "opinion"),
    "Sports": ("sport", "sports", "football", "soccer", "basketball", "baseball", "hockey", "golf", "racing", "motorsports", "motor sports", "mma", "wrestling", "tennis", "outdoors"),
    "Movies": ("movie", "movies", "cinema", "film", "films"),
    "Comedy": ("comedy", "comedies", "sitcom", "sitcoms", "funny", "stand up"),
    "Drama": ("drama", "dramas", "soap", "soaps", "telenovela", "telenovelas"),
    "Kids & Family": ("kids", "children", "family", "preschool", "animation", "cartoon", "cartoons"),
    "Crime": ("crime", "mystery", "mysteries", "investigation", "court", "forensic", "police"),
    "Reality": ("reality", "competition", "competitions", "game show", "game shows", "unscripted"),
    "Lifestyle": ("lifestyle", "home", "travel", "fashion", "wellness", "diy", "design"),
    "Food": ("food", "cooking", "kitchen", "recipe", "recipes", "culinary"),
    "Music": ("music", "concert", "concerts", "radio", "karaoke"),
    "History": ("history", "historical", "military", "western", "westerns"),
    "Science & Nature": ("science", "nature", "wildlife", "space", "documentary", "documentaries", "technology"),
    "Weather": ("weather", "forecast", "forecasts"),
    "International": ("international", "world", "global", "foreign", "latino"),
}

# Exact provider-category aliases.  These map provider taxonomy labels to one or
# more StreamDial genres without guessing from unrelated words in a title.
GENRE_ALIASES = {
    "news opinion": ("News",),
    "sports outdoors": ("Sports",),
    "motor sports": ("Sports",),
    "motorsports": ("Sports",),
    "home food": ("Lifestyle", "Food"),
    "nature history science": ("Science & Nature", "History"),
    "action drama": ("Drama",),
    "reality tv": ("Reality",),
    "reality competition": ("Reality",),
    "game shows": ("Reality",),
    "lifestyle pop culture": ("Lifestyle",),
    "sci fi horror": ("Drama",),
    "anime gaming": ("Entertainment",),
    "western classic tv": ("Drama", "History"),
    "kids": ("Kids & Family",),
    "latino": ("International",),
    "ambiance": ("Entertainment",),
}


LANGUAGE_NAMES = {
    "en": "English", "eng": "English", "english": "English",
    "es": "Spanish", "spa": "Spanish", "spanish": "Spanish", "español": "Spanish",
    "fr": "French", "fra": "French", "fre": "French", "french": "French",
    "de": "German", "deu": "German", "ger": "German", "german": "German",
    "it": "Italian", "ita": "Italian", "italian": "Italian",
    "pt": "Portuguese", "por": "Portuguese", "portuguese": "Portuguese",
    "ja": "Japanese", "jpn": "Japanese", "japanese": "Japanese",
    "ko": "Korean", "kor": "Korean", "korean": "Korean",
    "zh": "Chinese", "zho": "Chinese", "chi": "Chinese", "chinese": "Chinese",
    "hi": "Hindi", "hin": "Hindi", "hindi": "Hindi",
    "ar": "Arabic", "ara": "Arabic", "arabic": "Arabic",
    "ru": "Russian", "rus": "Russian", "russian": "Russian",
    "nl": "Dutch", "nld": "Dutch", "dut": "Dutch", "dutch": "Dutch",
    "pl": "Polish", "pol": "Polish", "polish": "Polish",
    "tr": "Turkish", "tur": "Turkish", "turkish": "Turkish",
    "tl": "Filipino", "fil": "Filipino", "filipino": "Filipino", "tagalog": "Filipino",
}


def clean_text(value):
    value = html.unescape(str(value or ""))
    return re.sub(r"\s+", " ", value).strip()


def normalize_languages(values, default=UNKNOWN_LANGUAGE):
    if not values:
        return [default] if default else []
    if isinstance(values, str):
        values = re.split(r"[,/|]", values)
    output = []
    for value in values:
        key = clean_text(value).casefold()
        if not key:
            continue
        name = LANGUAGE_NAMES.get(key, clean_text(value).title())
        if name not in output:
            output.append(name)
    return output or ([default] if default else [])



CANONICAL_LANGUAGES = (
    "English", "Spanish", "French", "German", "Italian", "Portuguese",
    "Dutch", "Polish", "Russian", "Turkish", "Arabic", "Hindi",
    "Chinese", "Japanese", "Korean", "Filipino",
)


def primary_language(values, default=UNKNOWN_LANGUAGE):
    """Return one canonical browse language for a channel.

    Provider metadata sometimes mixes language with regional/category labels
    (for example Sling's ``European``).  Browse buckets must be mutually
    exclusive, so only recognized languages are eligible and exactly one is
    selected.  The original ``languages`` metadata remains intact for display.
    """
    normalized = normalize_languages(values, default="")
    allowed = set(CANONICAL_LANGUAGES)
    for value in normalized:
        if value in allowed:
            return value
    return default or UNKNOWN_LANGUAGE

def _genre_text(value):
    text = clean_text(value).casefold().replace("&", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _contains_genre_phrase(text, phrase):
    # Token/phrase matching avoids the old substring bug where e.g. ``sport``
    # could match an unrelated word such as ``transport``.
    needle = _genre_text(phrase)
    return bool(needle and (" " + needle + " ") in (" " + text + " "))


def classify_genres(*values):
    text = _genre_text(" ".join(clean_text(value) for value in values if value))
    if not text:
        return ["Entertainment"]
    found = []
    for genre in GENRE_ORDER:
        if genre in ("Entertainment", "Other"):
            continue
        if any(_contains_genre_phrase(text, keyword) for keyword in GENRE_KEYWORDS.get(genre, ())):
            found.append(genre)
    return found or ["Entertainment"]


def normalize_genres(values, fallback_text=""):
    if isinstance(values, str):
        values = re.split(r"[,/|;]", values)
    values = values or []
    output = []
    canonical = {genre.casefold(): genre for genre in GENRE_ORDER}
    for value in values:
        text = clean_text(value)
        if not text:
            continue
        exact = canonical.get(text.casefold())
        if exact:
            mapped = [exact]
        else:
            mapped = list(GENRE_ALIASES.get(_genre_text(text), ()))
            if not mapped:
                mapped = classify_genres(text)
                if mapped == ["Entertainment"]:
                    # An unknown provider label is not strong evidence that the
                    # channel belongs in Entertainment; defer to other labels or
                    # the explicit fallback text instead.
                    mapped = []
        for genre in mapped:
            if genre not in output:
                output.append(genre)
    if not output:
        output = classify_genres(fallback_text)
    return output


def parse_datetime(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 100000000000:
            timestamp /= 1000.0
        try:
            return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def iso_datetime(value):
    parsed = parse_datetime(value)
    return parsed.astimezone(datetime.timezone.utc).isoformat() if parsed else ""


def program(title, start="", stop="", description="", image=""):
    return {
        "title": clean_text(title) or "Live programming",
        "start": iso_datetime(start),
        "stop": iso_datetime(stop),
        "description": clean_text(description),
        "image": str(image or ""),
    }


def channel(provider, identifier, name, description="", languages=None,
            genres=None, logo="", fanart="", number="", schedule=None, extra=None):
    return {
        "provider": str(provider),
        "id": str(identifier),
        "name": clean_text(name) or "Unnamed channel",
        "description": clean_text(description),
        "languages": normalize_languages(languages),
        "genres": normalize_genres(genres, "{} {}".format(name, description)),
        "logo": str(logo or ""),
        "fanart": str(fanart or ""),
        "number": clean_text(number),
        "schedule": [row for row in (schedule or []) if isinstance(row, dict)],
        "extra": dict(extra or {}),
    }


def event_status(row, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    start = parse_datetime(row.get("start"))
    stop = parse_datetime(row.get("stop"))
    if start and stop and start <= now < stop:
        return "now"
    if start and start >= now:
        return "next"
    return "past"


def current_program(rows):
    now = datetime.datetime.now(datetime.timezone.utc)
    current = [row for row in rows or [] if event_status(row, now) == "now"]
    if current:
        return current[0]
    upcoming = [row for row in rows or [] if event_status(row, now) == "next"]
    upcoming.sort(key=lambda row: parse_datetime(row.get("start")) or now)
    return upcoming[0] if upcoming else None


def local_time(value):
    parsed = parse_datetime(value)
    if not parsed:
        return ""
    local = parsed.astimezone()
    hour = local.strftime("%I").lstrip("0") or "0"
    return "{}:{} {}".format(hour, local.strftime("%M"), local.strftime("%p"))


def schedule_plot(item, hours=8, max_rows=8):
    lines = []
    now = datetime.datetime.now(datetime.timezone.utc)
    horizon = now + datetime.timedelta(hours=max(2, int(hours or 8)))
    rows = []
    for row in item.get("schedule") or []:
        start = parse_datetime(row.get("start"))
        stop = parse_datetime(row.get("stop"))
        if stop and stop <= now:
            continue
        if start and start > horizon:
            continue
        rows.append(row)
    rows.sort(key=lambda row: parse_datetime(row.get("start")) or now)
    for row in rows[:max_rows]:
        prefix = "NOW" if event_status(row, now) == "now" else local_time(row.get("start"))
        lines.append("{}  {}".format(prefix or "LIVE", clean_text(row.get("title"))))
    description = clean_text(item.get("description"))
    if lines:
        return "UP NEXT (local time)\n{}{}".format(
            "\n".join(lines), "\n\n" + description if description else "")
    return description or "Live channel. This provider does not publish a compatible public schedule."


def search_score(item, query):
    terms = [term for term in re.findall(r"[\w'-]+", clean_text(query).casefold()) if term]
    if not terms:
        return 0
    name = clean_text(item.get("name")).casefold()
    provider = clean_text(item.get("provider_name")).casefold()
    metadata = " ".join([
        name, provider, clean_text(item.get("description")).casefold(),
        " ".join(item.get("genres") or []).casefold(),
        " ".join(item.get("languages") or []).casefold(),
        " ".join(clean_text(row.get("title")).casefold() for row in item.get("schedule") or []),
    ])
    if not all(term in metadata for term in terms):
        return 0
    score = 10
    phrase = clean_text(query).casefold()
    if name == phrase:
        score += 100
    elif name.startswith(phrase):
        score += 70
    elif phrase in name:
        score += 45
    if phrase in provider:
        score += 10
    return score


def channel_identity_key(item):
    """Conservative cross-provider channel identity derived from display name.

    This intentionally does not use provider IDs and is only meant to compare
    rows from *different* providers. Generic trailing presentation words are
    ignored so names such as ``Baywatch`` and ``Baywatch Channel`` can match.
    """
    text = clean_text((item or {}).get("name"))
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").casefold()
    folded = folded.replace("&", " and ")
    tokens = re.findall(r"[a-z0-9]+", folded)
    while len(tokens) > 1 and tokens[-1] in {"channel", "tv", "live", "network", "stream", "hd", "uhd", "4k"}:
        tokens.pop()
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]
    return " ".join(tokens)


def _duplicate_provider_score(row):
    """Prefer an originating provider over another platform's restream.

    Provider adapters may set ``extra.origin_provider`` when their own metadata
    identifies the upstream service.  A matching upstream provider wins.  For
    otherwise identical rows, direct FAST services already integrated in
    StreamDial are preferred over device/platform aggregators.
    """
    provider = str((row or {}).get("provider") or "").casefold()
    extra = (row or {}).get("extra") or {}
    origin = str(extra.get("origin_provider") or "").casefold()
    # Lower is better.  Explicit self-origin is strongest evidence.
    if origin and origin == provider:
        return (0, provider)
    direct = {"xumo": 10, "pluto": 11, "tubi": 12, "pbs": 13, "freelivesports": 14}
    aggregators = {"vidaa": 70, "vizio": 71, "sling": 72, "localnow": 73,
                   "samsung": 74, "lg": 75, "tcl": 76, "roku": 77, "plex": 78}
    return (direct.get(provider, aggregators.get(provider, 40)), provider)


def dedupe_across_providers(rows):
    """Best-effort cross-provider de-duplication; never de-dupe within provider.

    When one platform explicitly reports that it is carrying another provider's
    feed, prefer the originating provider if that provider is also present in the
    result set.  Otherwise use a deterministic provider preference and retain
    *all* same-name rows belonging to the selected provider.
    """
    rows = list(rows or [])
    groups = {}
    order = []
    for index, row in enumerate(rows):
        key = channel_identity_key(row) or "__row__{}".format(index)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((index, row))

    chosen = {}
    for key in order:
        provider_rows = {}
        for _, row in groups[key]:
            provider = str((row or {}).get("provider") or "")
            provider_rows.setdefault(provider, row)
        if len(provider_rows) <= 1:
            continue

        # If any restream row explicitly names an origin which is also present,
        # that source of truth wins the identity.
        explicit = []
        for row in provider_rows.values():
            origin = str(((row or {}).get("extra") or {}).get("origin_provider") or "")
            if origin and origin in provider_rows:
                explicit.append(origin)
        if explicit:
            selected = sorted(set(explicit), key=lambda p: _duplicate_provider_score(provider_rows[p]))[0]
        else:
            selected = min(provider_rows, key=lambda p: _duplicate_provider_score(provider_rows[p]))
        chosen[key] = selected

    output = []
    for index, row in enumerate(rows):
        key = channel_identity_key(row) or "__row__{}".format(index)
        selected = chosen.get(key)
        if selected is None or str((row or {}).get("provider") or "") == selected:
            output.append(row)
    return output

