"""Sling Freestream anonymous catalog, guide enrichment, and Widevine playback.

Only Sling's free, anonymous Freestream inventory is exposed. Paid/subscription
Sling inventory and login flows are intentionally absent. All runtime endpoints
are Sling/MoveTV infrastructure used by Sling itself.
"""

import json
import re
import urllib.parse
import uuid

from ..http import DEFAULT_UA
from ..models import channel, normalize_genres, program


KEY = "sling"
NAME = "Sling Freestream"
BROWSABLE = True
CMS = "https://cbd46b77.cdn.cms.movetv.com"
SUMMARY_URL = CMS + "/cms/publish3/domain/summary/ums/1.json"
LICENSE_URL = "https://p-drmwv.movetv.com/widevine/proxy"
ORIGIN = "https://watch.sling.com"
_TEST_RE = re.compile(r"^(?:SLATEPO\d|HYBRID-SIGNALTEST-)", re.IGNORECASE)
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": ORIGIN,
    "Referer": ORIGIN + "/",
    "User-Agent": DEFAULT_UA,
    "client-config": "rn-client-config",
    "client-version": "7.1.32",
    "device-model": "Chrome",
    "player-version": "9.1.0",
    "response-config": "ar_browser_1_1",
    "dma": "535",
    "geo-zipcode": "43017",
    "time-zone-id": "America/New_York",
    "timezone": "-0500",
    "features": "enable_ad_tracking,web_browser",
}


def _summary(ctx):
    def load():
        payload = ctx.http.get_json(
            SUMMARY_URL, headers=HEADERS, timeout=35, max_bytes=24 * 1024 * 1024)
        rows = (payload or {}).get("channels") or []
        if not isinstance(rows, list) or not rows:
            raise ValueError("Sling Freestream returned no anonymous channel summary")
        return rows
    return ctx.cache.remember("sling-freestream-summary-v1", 10 * 60, load)


def _name(row):
    metadata = row.get("metadata") or {}
    for value in (
        metadata.get("channel_name"), row.get("network_affiliate_name"),
        row.get("title"), metadata.get("call_sign"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _genres(row, name):
    metadata = row.get("metadata") or {}
    raw = metadata.get("genre") or []
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    ignored = {"sling free", "freestream"}
    values = [value for value in raw if isinstance(value, str) and value.strip().casefold() not in ignored]
    # "International" is useful only when it is the actual provider category;
    # retain it rather than deleting it when no more specific genre is present.
    return normalize_genres(values, name)


def _language(row, name):
    metadata = row.get("metadata") or {}
    raw = metadata.get("language")
    values = raw if isinstance(raw, list) else [raw]
    aliases = {
        "english": "English", "en": "English", "eng": "English",
        "spanish": "Spanish", "es": "Spanish", "spa": "Spanish", "español": "Spanish",
        "french": "French", "fr": "French", "german": "German", "de": "German",
        "italian": "Italian", "it": "Italian", "portuguese": "Portuguese", "pt": "Portuguese",
        "hindi": "Hindi", "hi": "Hindi", "arabic": "Arabic", "ar": "Arabic",
        "chinese": "Chinese", "zh": "Chinese", "japanese": "Japanese", "ja": "Japanese",
        "korean": "Korean", "ko": "Korean", "russian": "Russian", "ru": "Russian",
        "polish": "Polish", "pl": "Polish", "turkish": "Turkish", "tr": "Turkish",
        "filipino": "Filipino", "tagalog": "Filipino",
    }
    # Sling also places regional programming labels such as "European" in this
    # field. Only actual languages are allowed into StreamDial language menus.
    for value in values:
        key = str(value or "").strip().casefold()
        if key in aliases:
            return aliases[key]
    text = name.casefold()
    if any(token in text for token in ("español", "espanol", "spanish", "latino")):
        return "Spanish"
    return "English"


def channels(ctx):
    output = []
    seen = set()
    for row in _summary(ctx):
        if not isinstance(row, dict):
            continue
        metadata = row.get("metadata") or {}
        visibility = row.get("visibility") or {}
        identifier = str(row.get("channel_guid") or row.get("external_id") or "").strip()
        qvt = str(row.get("qvt_url") or row.get("qvt") or "").strip()
        call_sign = str(metadata.get("call_sign") or "")
        raw_genres = metadata.get("genre") or []
        if not isinstance(raw_genres, list):
            raw_genres = [raw_genres]
        if not identifier or identifier in seen or not qvt:
            continue
        if not visibility.get("visible", True) or not metadata.get("is_linear_channel") or not metadata.get("is_free"):
            continue
        if row.get("cmw_hide_channel") or _TEST_RE.search(call_sign) or _TEST_RE.search(str(row.get("title") or "")):
            continue
        if any(str(value).strip().casefold() == "test" for value in raw_genres):
            continue
        name = _name(row)
        if not name:
            continue
        seen.add(identifier)
        thumbnail = row.get("thumbnail") or {}
        output.append(channel(
            KEY,
            identifier,
            name,
            description=metadata.get("description") or row.get("description") or "",
            languages=_language(row, name),
            genres=_genres(row, name),
            logo=thumbnail.get("url") if isinstance(thumbnail, dict) else "",
            number=row.get("channel_number") or "",
            extra={
                "qvt": qvt,
                "call_sign": call_sign,
                "gracenote": row.get("gracenote_channel_id") or "",
            },
        ))
    if not output:
        raise ValueError("Sling Freestream returned no free linear channels")
    return output


def _schedule_url(identifier):
    return CMS + "/playermetadata/sling/v1/api/channels/{}/current/schedule.qvt".format(
        urllib.parse.quote(str(identifier), safe=""))


def _program(payload, identifier):
    playback = (payload or {}).get("playback_info") or {}
    asset = playback.get("asset") or {}
    title = asset.get("title") or asset.get("franchise_title")
    start = asset.get("schedule_start")
    stop = asset.get("schedule_end")
    if not title or not start or not stop:
        return None
    image = ""
    shows = (payload or {}).get("shows") or []
    if shows and isinstance(shows[0], dict):
        thumb = shows[0].get("thumbnail") or {}
        image = thumb.get("url") if isinstance(thumb, dict) else ""
    return program(title, start, stop, "", image)


def _schedule(ctx, identifier, max_windows=12):
    cache_key = "sling-schedule-v1-{}-{}".format(identifier, max_windows)

    def load():
        url = _schedule_url(identifier)
        seen = set()
        output = []
        for _ in range(max(1, int(max_windows))):
            if not url or url in seen or not str(url).startswith(("http://", "https://")):
                break
            seen.add(url)
            payload = ctx.http.get_json(
                url, headers=HEADERS, timeout=18, max_bytes=2 * 1024 * 1024)
            event = _program(payload, identifier)
            if event:
                output.append(event)
            url = (payload or {}).get("_next") or ""
        unique = {}
        for row in output:
            unique[(row.get("start"), row.get("title"))] = row
        return sorted(unique.values(), key=lambda row: row.get("start") or "")

    return ctx.cache.remember(cache_key, 10 * 60, load)


def enrich(ctx, item):
    value = dict(item)
    value["schedule"] = _schedule(ctx, item.get("id"), max_windows=12)
    return value


def search_enrich(ctx, rows):
    # Sling's provider API publishes the guide as a per-channel QVT chain rather
    # than a bulk feed. Avoid thousands of provider requests on every search.
    # Registry's timestamped schedule index makes any schedules fetched while
    # browsing immediately searchable; visible Sling rows are enriched above.
    return rows


def resolve(ctx, identifier):
    payload = ctx.http.get_json(
        _schedule_url(identifier), headers=HEADERS, timeout=25, max_bytes=3 * 1024 * 1024)
    playback = (payload or {}).get("playback_info") or {}
    url = ""
    selected = ""
    for key in ("dash_manifest_url", "live_m3u8_url_template", "m3u8_url_template"):
        candidate = str(playback.get(key) or "").strip()
        if candidate.startswith(("http://", "https://")) and "{" not in candidate:
            url = candidate
            selected = key
            break
    if not url:
        raise ValueError("Sling Freestream did not return a current playback manifest")

    if selected != "dash_manifest_url" and not url.casefold().endswith(".mpd"):
        # Freestream is currently DRM-first, but preserve a provider-returned
        # concrete HLS path if Sling supplies one for a channel.
        return {
            "url": url,
            "headers": {"User-Agent": DEFAULT_UA, "Origin": ORIGIN, "Referer": ORIGIN + "/"},
            "mime_type": "application/vnd.apple.mpegurl",
            "manifest_type": "hls",
            "diagnostics": {"provider": KEY, "selected_source": "sling-freestream-hls", "manifest_field": selected},
        }

    user_id = str(uuid.uuid4())
    post_data = json.dumps({
        "env": "production",
        "user_id": user_id,
        "channel_id": str(identifier),
        # Kodi InputStream Adaptive expands D{SSM} to the raw Widevine challenge
        # as comma-separated decimal bytes, exactly matching Sling's JSON array.
        "message": "__STREAMDIAL_SSM__",
    }, separators=(",", ":")).replace('"__STREAMDIAL_SSM__"', "[D{SSM}]")
    license_headers = urllib.parse.urlencode({
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "User-Agent": DEFAULT_UA,
    })
    return {
        "url": url,
        "headers": {"User-Agent": DEFAULT_UA, "Origin": ORIGIN, "Referer": ORIGIN + "/"},
        "mime_type": "application/dash+xml",
        "manifest_type": "mpd",
        "license_type": "com.widevine.alpha",
        "license_key": "{}|{}|{}|R".format(LICENSE_URL, license_headers, post_data),
        "diagnostics": {
            "provider": KEY,
            "selected_source": "sling-freestream-dash-widevine",
            "manifest_field": selected,
            "anonymous_license": True,
        },
    }
