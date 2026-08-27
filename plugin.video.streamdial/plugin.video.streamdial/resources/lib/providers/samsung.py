"""Samsung TV Plus US catalog and direct Samsung HLS playback.

Samsung's public US TV Plus page is used as a provider-owned metadata overlay
for channel numbers, names, and categories.  The bundled seed remains necessary
for Samsung's technical service identifiers/manifest paths because the public
consumer lineup does not expose those tune-time identifiers.
"""

import html
import json
import os
import re
import urllib.parse
import uuid

from ..models import channel, clean_text, normalize_genres


KEY = "samsung"
NAME = "Samsung TV Plus"
BROWSABLE = True
USER_AGENT = "okhttp/4.12.0"
BASE = "https://sis-global.prod.samsungtv.plus/v1/tvpprd/"
OFFICIAL_LINEUP_URL = "https://www.samsung.com/us/tvs/smart-tv/samsung-tv-plus/"

# Current provider taxonomy labels published on Samsung's US TV Plus page. The
# parser deliberately keys off these labels instead of inventing categories.
OFFICIAL_GROUPS = (
    "News & Opinion", "Movies", "Western & Classic TV", "Action & Drama",
    "Crime", "Reality TV", "Home & Food", "Sports & Outdoors", "Motor Sports",
    "Comedy", "Reality Competition", "Game Shows", "Lifestyle & Pop Culture",
    "Sci-Fi & Horror", "Anime & Gaming", "Nature, History & Science", "Kids",
    "Latino", "Music", "Ambiance",
)


def _seed_catalog(ctx):
    path = os.path.join(ctx.addon_path, "resources", "data", "samsung_us.json")
    with open(path, "r", encoding="utf-8") as source:
        data = json.load(source)
    return data if isinstance(data, list) else []


def _lineup_name_key(value):
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).casefold())


def _visible_lines(page):
    text = str(page or "")
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"</(?:li|p|div|section|article|h[1-6]|tr|td|br)\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<(?:br|hr)\b[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return [clean_text(line) for line in text.splitlines() if clean_text(line)]


def _parse_official_lineup(page):
    """Parse Samsung's public consumer lineup into number/name/group rows."""
    groups = {_lineup_name_key(value): value for value in OFFICIAL_GROUPS}
    current_group = ""
    output = []
    seen = set()
    for line in _visible_lines(page):
        normalized_line = re.sub(r"^(?:Image\s+)+", "", line, flags=re.I).strip()
        group = groups.get(_lineup_name_key(normalized_line))
        if group:
            current_group = group
            continue
        if not current_group:
            continue
        match = re.match(r"^(\d{3,5})\s+(.+?)\s*$", normalized_line)
        if not match:
            continue
        number, name = match.groups()
        name = clean_text(name)
        # Guard against FAQ years/footnote text being mistaken for channels.
        if not name or len(name) > 120:
            continue
        key = (number, _lineup_name_key(name))
        if key in seen:
            continue
        seen.add(key)
        output.append({"channel": number, "name": name, "group": current_group})
    return output


def _official_lineup(ctx):
    def load():
        try:
            page = ctx.http.get_text(
                OFFICIAL_LINEUP_URL,
                headers={"Accept": "text/html,*/*", "User-Agent": "Mozilla/5.0"},
                timeout=20, max_bytes=8 * 1024 * 1024,
            )
        except Exception:
            return []
        rows = _parse_official_lineup(page)
        # A partial/changed page must never replace good bundled metadata. The
        # current US lineup contains hundreds of rows; 100 is a conservative
        # signal that the provider page structure was parsed successfully.
        return rows if len(rows) >= 100 else []
    return ctx.cache.remember("samsung-official-lineup-v1", 12 * 60 * 60, load)


def _catalog(ctx):
    seed = [dict(row) for row in _seed_catalog(ctx) if isinstance(row, dict)]
    official = _official_lineup(ctx)
    if not official:
        return seed

    by_number = {str(row.get("channel") or ""): row for row in official if row.get("channel")}
    by_name = {_lineup_name_key(row.get("name")): row for row in official if row.get("name")}
    for row in seed:
        match = by_number.get(str(row.get("channel") or "")) or by_name.get(_lineup_name_key(row.get("name")))
        if not match:
            continue
        # Samsung's current provider-owned page wins for presentation metadata;
        # the bundled row retains only the technical playback fields it exposes.
        row["name"] = match.get("name") or row.get("name")
        row["channel"] = match.get("channel") or row.get("channel")
        row["group"] = match.get("group") or row.get("group")
    return seed


def channels(ctx):
    output = []
    for row in _catalog(ctx):
        if not isinstance(row, dict) or not row.get("id") or not row.get("name"):
            continue
        output.append(channel(
            KEY, row["id"], row["name"],
            description=row.get("description") or "",
            languages=row.get("language") or "English",
            genres=normalize_genres([row.get("group") or ""], "{} {}".format(row.get("name", ""), row.get("description", ""))),
            logo=row.get("logo") or "",
            number=row.get("channel") or "",
            extra={"url": row.get("url") or "", "path": row.get("path") or ""},
        ))
    return output


def _expand(url, identifier):
    device_id = str(uuid.uuid4())
    replacements = {
        "PSID": device_id, "TARGETOPT": "1", "US_PRIVACY": "1---",
        "APP_DOMAIN": "plugin.video.streamdial", "APP_NAME": "StreamDial TV",
        "AFSDK_VALUE": "", "CONTENT_LIVE": "1", "IFA": device_id,
        "IFA_TYPE": "uuid", "LMT": "1", "DNS": "1", "IP": "",
        "GDPR": "0", "GDPR_CONSENT": "", "COUNTRY": "US",
        "APP_STOREURL": "", "APP_BUNDLE": "plugin.video.streamdial",
        "DEVICE_ID": device_id, "DEVICE_MAKE": "Kodi",
        "DEVICE_MODEL": "StreamDial", "DEVICE_TYPE": "desktop",
        "CONTENT_LANGUAGE": "en", "CACHEBUSTER": device_id.replace("-", ""),
    }
    for key, value in replacements.items():
        encoded = urllib.parse.quote(str(value), safe="")
        for wrapper in (("%7B", "%7D"), ("%7b", "%7d"), ("%5B", "%5D"), ("%5b", "%5d")):
            url = url.replace(wrapper[0] + key + wrapper[1], encoded)
        url = url.replace("{" + key + "}", encoded).replace("[" + key + "]", encoded)
    url = re.sub(r"(?:%7B|%5B)[^%]+?(?:%7D|%5D)", "", url, flags=re.IGNORECASE)
    url = re.sub(r"[\[{][A-Za-z0-9_.% -]+[\]}]", "", url)
    if "service_id=" not in url and url.startswith(BASE):
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"ads.service_id": identifier})
    return url


def resolve(ctx, identifier):
    row = next((value for value in _catalog(ctx) if str(value.get("id")) == str(identifier)), None)
    if not row or not (row.get("url") or row.get("path")):
        raise ValueError("Unknown Samsung TV Plus channel")
    if row.get("url"):
        url = _expand(str(row["url"]), identifier)
    else:
        params = {
            "ads.device_did": str(uuid.uuid4()),
            "ads.device_dnt": "1",
            "ads.us_privacy": "1---",
            "ads.app_domain": "plugin.video.streamdial",
            "ads.app_name": "StreamDial TV",
            "ads.ssai_vendor": "SSSLIVE",
            "ads.afsdk_params": "",
            "ads.service_id": identifier,
        }
        url = BASE + row["path"] + "?" + urllib.parse.urlencode(params)
    return {
        "url": url,
        "headers": {"User-Agent": USER_AGENT, "DNT": "1"},
        "mime_type": "application/vnd.apple.mpegurl",
        "manifest_type": "hls",
    }
