"""Free Live Sports TV catalog, guide, and playback."""

import datetime
import re
import uuid

from ..http import DEFAULT_UA
from ..models import channel, program


KEY = "freelivesports"
NAME = "Free Live Sports TV"
BROWSABLE = True
CATALOG_URL = (
    "https://epg.unreel.me/v2/sites/freelivesports/live-channels/public/"
    "081f73704b56aaceb6b459804761ec54"
)
ORIGIN = "https://www.freelivesports.tv"
HEADERS = {"User-Agent": DEFAULT_UA, "Origin": ORIGIN, "Referer": ORIGIN + "/"}
MACRO_RE = re.compile(r"\[[A-Z_]+\]")


def _catalog(ctx):
    def load():
        payload = ctx.http.get_json(
            CATALOG_URL, params={"__site": "freelivesports", "__source": "web"},
            headers=HEADERS, timeout=30, max_bytes=24 * 1024 * 1024,
        )
        return payload if isinstance(payload, list) else payload.get("channels") or []
    return ctx.cache.remember("freelivesports-catalog-v1", 15 * 60, load)


def channels(ctx):
    output = []
    for row in _catalog(ctx):
        if not isinstance(row, dict) or not row.get("_id") or not row.get("url"):
            continue
        events = []
        for value in ((row.get("epg") or {}).get("entries") or []):
            events.append(program(
                value.get("title") or "Live sports", value.get("start"), value.get("stop"),
                value.get("description") or "", value.get("image") or "",
            ))
        output.append(channel(
            KEY, row.get("_id"), row.get("name"),
            description=row.get("description") or "", languages=row.get("language") or "English",
            genres=["Sports"], logo=row.get("thumbnail") or "", number=row.get("channelNumber") or "",
            schedule=events, extra={"stream_url": row.get("url")},
        ))
    return output


def _expand(url):
    values = {
        "[DEVICE_ID]": str(uuid.uuid4()), "[DEVICE_MODEL]": "web",
        "[REF]": ORIGIN + "/", "[LAT]": "0", "[GDPR]": "0",
        "[CONSENT_STRING]": "", "[US_PRIVACY]": "1---",
        "[CB]": str(int(datetime.datetime.now(datetime.timezone.utc).timestamp())),
        "[UA]": DEFAULT_UA,
    }
    for key, value in values.items():
        url = url.replace(key, value)
    return MACRO_RE.sub("", url)


def resolve(ctx, identifier):
    row = next((value for value in _catalog(ctx) if str(value.get("_id")) == str(identifier)), None)
    if not row:
        raise ValueError("Unknown Free Live Sports TV channel")
    return {
        "url": _expand(str(row["url"])), "headers": HEADERS,
        "mime_type": "application/vnd.apple.mpegurl", "manifest_type": "hls",
    }
