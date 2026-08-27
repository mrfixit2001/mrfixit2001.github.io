"""Vizio WatchFree+ direct catalog, EPG, and clear-HLS playback.

Runtime requests go only to Vizio's anonymous WatchFree+ API and to stream/CDN
URLs returned by that API.  Third-party projects were used only as protocol
reference material while implementing this provider.
"""

import datetime
import urllib.parse

from ..http import DEFAULT_UA
from ..models import channel, normalize_genres, program


KEY = "vizio"
NAME = "Vizio WatchFree+"
BROWSABLE = True
CHANNELS_URL = "https://watchfreeplus-epg-prod.smartcasttv.com/api/channels"
AIRINGS_URL = "https://watchfreeplus-epg-prod.smartcasttv.com/api/airings/"
HEADERS = {"User-Agent": "okhttp/4.12.0", "Accept": "application/json"}

_MACROS = {
    "ADID": "00000000-0000-0000-0000-000000000000",
    "USPRIVACY": "1---",
    "IFATYPE": "aaid",
    "LMT": "0",
    "TARGETOPT": "False",
    "APP_NAME": "VIZIO",
    "APP_BUNDLE": "com.vizio.vue.launcher",
    "APP_STORE_URL": "https://play.google.com/store/apps/details?id=com.vizio.vue.launcher",
    "DOMAIN": "https://www.vizio.com",
    "DNT": "0",
    "COPPA": "0",
    "DEVICE_MAKE": "Google",
    "WIDTH": "1080",
    "HEIGHT": "1920",
    "DEVICE_MODEL": "Pixel 7",
    "APP_VERSION": "5.0.0",
    "DEVICE_TYPE": "mobile",
    "SKIPPABLE": "1",
}


def _expand_macros(url):
    value = urllib.parse.unquote(str(url or ""))
    for key, replacement in _MACROS.items():
        value = value.replace("{" + key + "}", urllib.parse.quote(replacement, safe=""))
    # Some Vizio/Xumo-delivered rows carry optional ad macros which the public
    # catalog leaves unresolved.  Xumo's CDN rejects those literal braces with
    # HTTP 400. Remove only unresolved query parameters; never alter the actual
    # manifest path or already-resolved provider values.
    parts = urllib.parse.urlsplit(value)
    if parts.query:
        kept = []
        for piece in parts.query.split("&"):
            if "{" in piece or "}" in piece:
                continue
            kept.append(piece)
        value = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(kept), parts.fragment))
    return value


def _origin_provider_from_urls(urls):
    text = " ".join(str(value or "") for value in (urls or [])).casefold()
    if "xumo" in text or "xumo_" in text:
        return "xumo"
    if "pluto" in text:
        return "pluto"
    return ""


def _language(name, category):
    text = "{} {}".format(name or "", category or "").casefold()
    if any(token in text for token in ("español", "espanol", "spanish", "latino", "latina")):
        return "Spanish"
    return "English"


def _raw_channels(ctx, max_age=10 * 60):
    def load():
        payload = ctx.http.get_json(
            CHANNELS_URL, headers=HEADERS, timeout=30, max_bytes=16 * 1024 * 1024)
        rows = payload.get("channels") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Vizio WatchFree+ returned no channel catalog")
        return rows
    return ctx.cache.remember("vizio-catalog-v1", max_age, load)


def _parse_airings(rows, station_to_id):
    output = {}
    for airing in rows or []:
        if not isinstance(airing, dict):
            continue
        identifier = station_to_id.get(str(airing.get("stationId") or ""))
        if not identifier:
            continue
        series = str(airing.get("seriesTitle") or "").strip()
        raw_title = str(airing.get("title") or "").strip()
        title = series if series and series.casefold() != raw_title.casefold() else raw_title
        if not title:
            title = "Live programming"
        image = airing.get("airingIcon") or ""
        event = program(
            title,
            airing.get("timeStart"),
            airing.get("timeEnd"),
            airing.get("description") or "",
            image,
        )
        output.setdefault(identifier, []).append(event)
    for events in output.values():
        events.sort(key=lambda row: row.get("start") or "")
    return output


def _airings(ctx, raw_channels):
    """Fetch Vizio's bulk guide once for the whole catalog."""
    if not raw_channels:
        return {}
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    start = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end = (now + datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    station_to_id = {
        str(row.get("airingsKey")): str(row.get("channelId"))
        for row in raw_channels
        if isinstance(row, dict) and row.get("airingsKey") and row.get("channelId")
    }
    if not station_to_id:
        return {}
    first_id = next((str(row.get("channelId")) for row in raw_channels if row.get("channelId")), "")
    cache_key = "vizio-airings-v1-{}".format(now.strftime("%Y%m%d%H"))

    def load():
        payload = ctx.http.get_json(
            AIRINGS_URL,
            params={
                "start": start,
                "end": end,
                "startChannel": first_id,
                "channelCount": len(raw_channels),
            },
            headers=HEADERS,
            timeout=60,
            max_bytes=24 * 1024 * 1024,
        )
        return _parse_airings((payload or {}).get("airings") or [], station_to_id)

    try:
        return ctx.cache.remember(cache_key, 10 * 60, load)
    except Exception:
        # Guide data is additive. A Vizio EPG outage must not suppress the live
        # catalog or playback.
        return {}


def channels(ctx):
    raw = _raw_channels(ctx)
    schedules = _airings(ctx, raw)
    output = []
    seen = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        identifier = str(row.get("channelId") or "").strip()
        name = str(row.get("channelName") or "").strip()
        urls = row.get("channelUrls") or []
        # Vizio flags token-gated services (currently an NFL service). Keep the
        # provider anonymous by excluding anything whose own catalog says it
        # requires a playback token.
        if not identifier or not name or identifier in seen or row.get("tokenUrl"):
            continue
        if not isinstance(urls, list) or not any(isinstance(value, str) and value.startswith("http") for value in urls):
            continue
        seen.add(identifier)
        category = row.get("category") or ""
        logo = row.get("channelIcon") or row.get("portraitIcon") or row.get("bwIcon") or ""
        output.append(channel(
            KEY,
            identifier,
            name,
            description=row.get("channelDescription") or "",
            languages=_language(name, category),
            genres=normalize_genres(category, name),
            logo=logo,
            number=row.get("channelNumber") or "",
            schedule=schedules.get(identifier) or [],
            extra={
                "airings_key": row.get("airingsKey") or "",
                "tms_station_id": row.get("tmsStationId") or "",
                "featured": str(identifier).upper().startswith("FEATURED"),
                "origin_provider": _origin_provider_from_urls(urls),
            },
        ))
    return output


def search_enrich(ctx, rows):
    # The catalog already contains one bulk 24-hour Vizio guide fetch.
    return rows


def resolve(ctx, identifier):
    # Tune-time resolution deliberately bypasses the 10-minute catalog cache so
    # macro-bearing CDN URLs are always taken from Vizio's current source of truth.
    payload = ctx.http.get_json(
        CHANNELS_URL, headers=HEADERS, timeout=30, max_bytes=16 * 1024 * 1024)
    rows = (payload or {}).get("channels") or []
    row = next((value for value in rows if str((value or {}).get("channelId") or "") == str(identifier)), None)
    if not isinstance(row, dict) or row.get("tokenUrl"):
        raise ValueError("Vizio WatchFree+ channel is unavailable")
    urls = row.get("channelUrls") or []
    candidates = [_expand_macros(value) for value in urls if isinstance(value, str) and value.startswith("http")]
    candidates = [value for value in candidates if value.startswith(("http://", "https://"))]
    if not candidates:
        raise ValueError("Vizio WatchFree+ did not return a current HLS stream")
    # Prefer a clean concrete HLS candidate. Vizio may publish more than one
    # delivery path for the same station; generic/provider-native candidates are
    # preferred over macro-heavy Xumo templates when both exist.
    candidates.sort(key=lambda value: ("xumo" in value.casefold(), len(value)))
    url = candidates[0]
    return {
        "url": url,
        "headers": {"User-Agent": HEADERS["User-Agent"], "Referer": "https://www.vizio.com/"},
        "mime_type": "application/vnd.apple.mpegurl",
        "manifest_type": "hls",
        "diagnostics": {
            "provider": KEY,
            "selected_source": "watchfreeplus-api-hls",
            "token_gated": False,
            "candidate_count": len(candidates),
            "origin_provider": _origin_provider_from_urls(urls),
        },
    }
