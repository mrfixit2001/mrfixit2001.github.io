"""LG Channels catalog, guide, and playback from LG's public web API."""

import base64
import json
import time
import urllib.parse
import uuid
import zlib

from ..http import DEFAULT_UA, HttpFailure
from ..models import channel, normalize_genres, program


KEY = "lg"
NAME = "LG Channels"
BROWSABLE = True
API_URL = "https://api.lgchannels.com/api/v1.0/schedulelist"
ORIGIN = "https://lgchannels.com"
API_HEADERS = {
    "Accept": "application/json, text/plain, */*", "Origin": ORIGIN,
    "Referer": ORIGIN + "/", "x-device-country": "US",
    "x-device-language": "en", "x-device-type": "WEB",
}


def _payload(ctx):
    def load():
        raw = ctx.http.get_bytes(API_URL, headers=API_HEADERS, timeout=30, max_bytes=12 * 1024 * 1024)
        try:
            value = json.loads(raw.decode("utf-8", "replace"))
        except (TypeError, ValueError):
            try:
                value = json.loads(zlib.decompress(base64.b64decode(raw.strip())).decode("utf-8"))
            except (TypeError, ValueError, OSError, zlib.error) as error:
                raise HttpFailure(message="Invalid LG Channels catalog") from error
        if not isinstance(value, dict):
            raise HttpFailure(message="Invalid LG Channels catalog")
        return value
    return ctx.cache.remember("lg-schedule-v1", 10 * 60, load)


def _rows(ctx):
    output = []
    seen = set()
    for category in _payload(ctx).get("categories") or []:
        if not isinstance(category, dict):
            continue
        category_name = category.get("categoryName") or ""
        for row in category.get("channels") or []:
            identifier = str(row.get("channelId") or "")
            if not identifier or identifier in seen or not row.get("mediaStaticUrl"):
                continue
            seen.add(identifier)
            output.append((category_name, row))
    return output


def channels(ctx):
    output = []
    for category_name, row in _rows(ctx):
        events = []
        fanart = ""
        event_genres = []
        for value in row.get("programs") or []:
            if not isinstance(value, dict) or not value.get("programTitle"):
                continue
            image = value.get("thumbnailUrl") or value.get("imageUrl") or value.get("previewImgUrl") or ""
            fanart = fanart or image
            events.append(program(
                value.get("programTitle"), value.get("startDateTime"), value.get("endDateTime"),
                value.get("description") or "", image,
            ))
            event_genres.extend([value.get("engGenreName") or "", value.get("engSecondGenreName") or ""])
        title = row.get("channelName") or row.get("channelId")
        channel_genres = [value for value in (category_name, row.get("channelGenreName") or "") if value]
        genres = normalize_genres(
            channel_genres if channel_genres else event_genres,
            "{} {}".format(title, category_name),
        )
        output.append(channel(
            KEY, row.get("channelId"), title,
            description=(events[0].get("description") if events else ""),
            languages="English", genres=genres,
            logo=row.get("channelLogoUrl") or "", fanart=fanart,
            number=row.get("channelNumber") or "", schedule=events,
            extra={"stream_url": row.get("mediaStaticUrl")},
        ))
    return output


def _expand(url):
    replacements = {
        "[DEVICE_ID]": str(uuid.uuid4()), "[IFA]": "", "[IFA_TYPE]": "",
        "[LMT]": "0", "[DNS]": "0",
        "[UA]": urllib.parse.quote(DEFAULT_UA, safe=""), "[IP]": "0.0.0.0",
        "[GDPR]": "", "[GDPR_CONSENT]": "", "[COUNTRY]": "US",
        "[US_PRIVACY]": "1---", "[APP_STOREURL]": "", "[APP_BUNDLE]": "",
        "[APP_NAME]": "lgchannels_web", "[APP_VERSION]": "",
        "[DEVICE_TYPE]": urllib.parse.quote("Personal Computer", safe=""),
        "[DEVICE_MAKE]": "", "[DEVICE_MODEL]": "", "[TARGETAD_ALLOWED]": "",
        "[FCK]": "", "[PCS]": "", "[COPPA]": "0", "[VIEWSIZE]": "1920x1080",
        "[NONCE]": str(int(time.time())), "[HOTELTYPE]": "", "[HOTEL_TYPE]": "",
    }
    for key, value in replacements.items():
        url = url.replace(key, value)
    return url


def resolve(ctx, identifier):
    row = next((value for _, value in _rows(ctx) if str(value.get("channelId")) == str(identifier)), None)
    if not row:
        raise ValueError("Unknown LG Channels channel")
    return {
        "url": _expand(str(row["mediaStaticUrl"])),
        "headers": {"User-Agent": DEFAULT_UA, "Origin": ORIGIN, "Referer": ORIGIN + "/"},
        "mime_type": "application/vnd.apple.mpegurl", "manifest_type": "hls",
    }
