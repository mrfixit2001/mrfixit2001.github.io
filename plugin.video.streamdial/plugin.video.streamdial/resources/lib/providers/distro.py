"""DistroTV catalog, guide, and playback from Distro's public feed."""

import html
import re
import time
import urllib.parse
import uuid

from ..http import DEFAULT_UA
from ..models import channel, normalize_genres, program


KEY = "distro"
NAME = "DistroTV"
BROWSABLE = True
FEED_URL = "https://tv.jsrdn.com/tv_v5/getfeed.php?type=live"
EPG_URL = "https://tv.jsrdn.com/epg/query.php"
ORIGIN = "https://distro.tv"
ANDROID_UA = "Dalvik/2.1.0 (Linux; Android 9) DistroTV/2.0.9"


def _shows(payload):
    if isinstance(payload, dict):
        values = payload.get("shows") or payload.get("data") or payload.get("items") or []
        return list(values.values()) if isinstance(values, dict) else values
    return payload if isinstance(payload, list) else []


def _feed(ctx):
    return ctx.cache.remember(
        "distro-feed-v1", 30 * 60,
        lambda: ctx.http.get_json(
            FEED_URL, headers={"User-Agent": ANDROID_UA, "Accept": "application/json,*/*"},
            timeout=25, max_bytes=12 * 1024 * 1024,
        ),
    )


def _catalog(ctx):
    output = []
    for show in _shows(_feed(ctx)):
        if not isinstance(show, dict) or show.get("type") != "live":
            continue
        seasons = show.get("seasons") or []
        episodes = (seasons[0].get("episodes") or []) if seasons and isinstance(seasons[0], dict) else []
        episode = episodes[0] if episodes and isinstance(episodes[0], dict) else {}
        content = episode.get("content") or {}
        identifier = str(episode.get("id") or "")
        if identifier and show.get("title") and content.get("url"):
            row = dict(show)
            row["_id"] = identifier
            row["_stream"] = content["url"]
            output.append(row)
    return output


def _schedule(ctx, identifiers):
    if not identifiers:
        return {}
    key = "distro-epg-v1-" + ",".join(sorted(identifiers))

    def load():
        payload = ctx.http.get_json(
            EPG_URL, params={"id": ",".join(identifiers), "range": "now,24h"},
            headers={"User-Agent": ANDROID_UA, "Accept": "application/json,*/*"},
            timeout=25, max_bytes=8 * 1024 * 1024,
        )
        output = {}
        for identifier, value in (payload.get("epg") or {}).items():
            events = []
            for row in (value or {}).get("slots") or []:
                events.append(program(
                    html.unescape(str(row.get("title") or "Live programming")),
                    str(row.get("start") or "").replace(" ", "T") + "+00:00",
                    str(row.get("end") or "").replace(" ", "T") + "+00:00",
                    html.unescape(str(row.get("description") or "")),
                    row.get("img_thumbh") or "",
                ))
            output[str(identifier)] = events
        return output
    return ctx.cache.remember(key, 10 * 60, load)


def channels(ctx):
    rows = _catalog(ctx)
    guides = {}
    for offset in range(0, len(rows), 40):
        ids = [row["_id"] for row in rows[offset:offset + 40]]
        try:
            guides.update(_schedule(ctx, ids))
        except Exception:
            pass
    output = []
    for row in rows:
        raw_tags = [value.strip() for value in str(row.get("genre") or "").split(",") if value.strip()]
        language = row.get("language") or next((
            value for value in raw_tags
            if value.casefold() in ("english", "spanish", "french", "portuguese", "hindi", "arabic", "korean", "japanese", "chinese", "russian")
        ), "English")
        output.append(channel(
            KEY, row["_id"], row.get("title"),
            description=row.get("description") or row.get("summary") or "",
            languages=language, genres=normalize_genres(raw_tags, row.get("title") or ""),
            logo=row.get("img_logo") or "", fanart=row.get("img_thumbh") or row.get("img_poster") or "",
            schedule=guides.get(row["_id"]) or [],
            extra={"stream_url": row["_stream"]},
        ))
    return output


def _expand(url):
    replacements = {
        "__CACHE_BUSTER__": str(int(time.time() * 1000)), "__DEVICE_ID__": str(uuid.uuid4()),
        "__LIMIT_AD_TRACKING__": "0", "__IS_GDPR__": "0", "__IS_CCPA__": "0",
        "__GEO_COUNTRY__": "US", "__LATITUDE__": "", "__LONGITUDE__": "",
        "__GEO_DMA__": "", "__GEO_TYPE__": "", "__PAGEURL_ESC__": "https%3A%2F%2Fdistro.tv%2F",
        "__STORE_URL__": "https%3A%2F%2Fdistro.tv%2F", "__APP_BUNDLE__": "distro.tv",
        "__APP_VERSION__": "0", "__APP_CATEGORY__": "", "__WIDTH__": "1920",
        "__HEIGHT__": "1080", "__DEVICE__": "Linux", "__DEVICE_ID_TYPE__": "uuid",
        "__DEVICE_CONNECTION_TYPE__": "", "__DEVICE_CATEGORY__": "desktop",
        "__env.i__": "web", "__env.u__": "web", "__PALN__": "",
        "__GDPR_CONSENT__": "", "__ADVERTISING_ID__": "", "__CLIENT_IP__": "",
    }
    for key, value in replacements.items():
        url = url.replace(key, value)
    url = re.sub(r"__[^_].*?__", "", url)
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode(urllib.parse.parse_qsl(parts.query, keep_blank_values=False))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def resolve(ctx, identifier):
    row = next((value for value in _catalog(ctx) if value["_id"] == str(identifier)), None)
    if not row:
        raise ValueError("Unknown DistroTV channel")
    return {
        "url": _expand(str(row["_stream"])),
        "headers": {"User-Agent": DEFAULT_UA, "Origin": ORIGIN, "Referer": ORIGIN + "/"},
        "mime_type": "application/vnd.apple.mpegurl", "manifest_type": "hls",
    }
