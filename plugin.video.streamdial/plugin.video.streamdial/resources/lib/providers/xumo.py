"""Xumo Play catalog and per-channel playback through Xumo's web APIs."""

import concurrent.futures
import datetime
import re
import threading
import urllib.parse
import uuid

from ..http import DEFAULT_UA
from ..models import channel, normalize_genres, program


KEY = "xumo"
NAME = "Xumo Play"
BROWSABLE = True
BASE = "https://valencia-app-mds.xumo.com"
ANDROID_BASE = "https://android-tv-mds.xumo.com"
ORIGIN = "https://play.xumo.com"
MARKET = "10006"
GEO = "2f08a9b3"
HEADERS = {
    "User-Agent": DEFAULT_UA, "Accept": "application/json, text/plain, */*",
    "Origin": ORIGIN, "Referer": ORIGIN + "/",
}
_GUIDE_LOCK = threading.Lock()


def _ids(ctx):
    return ctx.cache.remember("xumo-device-v1", None, lambda: {
        "device": str(uuid.uuid4()), "ifa": str(uuid.uuid4()),
    })


def _catalog(ctx):
    def load():
        ids = _ids(ctx)
        payload = ctx.http.get_json(
            BASE + "/v2/proxy/channels/list/{}.json".format(MARKET),
            params={
                "sort": "hybrid", "geoId": GEO,
                "deviceId": ids["device"], "ifaId": ids["ifa"],
            },
            headers=HEADERS, timeout=30, max_bytes=16 * 1024 * 1024,
        )
        container = payload.get("channel") or {}
        return container.get("item") or payload.get("items") or []
    return ctx.cache.remember("xumo-catalog-v1", 30 * 60, load)


def _genres(row):
    values = []
    raw = row.get("genre") or []
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    for value in raw:
        if isinstance(value, dict):
            value = value.get("value") or value.get("title")
        if value:
            values.append(value)
    return normalize_genres(values, "{} {}".format(row.get("title"), row.get("description")))


def channels(ctx):
    output = []
    for row in _catalog(ctx):
        if not isinstance(row, dict):
            continue
        props = row.get("properties") or {}
        if str(props.get("is_live", "")).casefold() != "true":
            continue
        callsign = str(row.get("callsign") or "")
        if callsign.endswith(("-DRM", "DRM-CMS")):
            continue
        identifier = str((row.get("guid") or {}).get("value") or "")
        title = row.get("title")
        if not identifier or not title:
            continue
        output.append(channel(
            KEY, identifier, title, description=row.get("description") or row.get("summary") or "",
            languages="English", genres=_genres(row),
            logo="https://image.xumo.com/v1/channels/channel/{}/600x336.jpg?type=channelTile".format(identifier),
            number=row.get("number") or "",
        ))
    return output


def _guide(ctx):
    """Return the current six-hour Xumo guide block keyed by channel ID."""
    now = datetime.datetime.now(datetime.timezone.utc)
    date = now.strftime("%Y%m%d")
    page = min(3, now.hour // 6)
    cache_key = "xumo-guide-v1-{}-{}".format(date, page)

    def load():
        endpoint = BASE + "/v2/epg/{}/{}/{}.json".format(MARKET, date, page)

        def fetch(offset):
            return ctx.http.get_json(
                endpoint,
                params={
                    "f": ["asset.title", "asset.descriptions"],
                    "limit": 50, "offset": offset,
                },
                headers=HEADERS, timeout=20, max_bytes=8 * 1024 * 1024,
            )

        first = fetch(0)
        total = min(1050, max(0, int(first.get("totalChannels") or 0)))
        payloads = [first]
        offsets = list(range(50, total, 50))
        if offsets:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(offsets))) as pool:
                futures = [pool.submit(fetch, offset) for offset in offsets]
                for future in futures:
                    try:
                        payloads.append(future.result())
                    except Exception:
                        # A partial guide is still useful; a completely failed
                        # first page is allowed to reach the diagnostics layer.
                        continue

        assets = {}
        channel_rows = []
        for payload in payloads:
            if isinstance(payload.get("assets"), dict):
                assets.update(payload["assets"])
            channel_rows.extend(payload.get("channels") or [])

        guide = {}
        for row in channel_rows:
            identifier = str(row.get("channelId") or "")
            if not identifier:
                continue
            events = []
            for slot in row.get("schedule") or []:
                asset_id = str(slot.get("assetId") or "")
                asset = assets.get(asset_id) or {}
                descriptions = asset.get("descriptions") or {}
                description = (
                    descriptions.get("large") or descriptions.get("medium")
                    or descriptions.get("small") or descriptions.get("tiny") or ""
                )
                image = ""
                if asset_id.startswith(("SH", "MV", "XT", "XM")) and not asset_id.startswith("XMP"):
                    image = "https://image.xumo.com/v1/assets/asset/{}/800x600.jpg".format(asset_id)
                events.append(program(
                    asset.get("title") or "Live programming",
                    slot.get("start"), slot.get("end"), description, image,
                ))
            guide[identifier] = events
        return guide

    with _GUIDE_LOCK:
        return ctx.cache.remember(cache_key, 10 * 60, load)


def enrich(ctx, item):
    if item.get("schedule"):
        return item
    item = dict(item)
    item["schedule"] = _guide(ctx).get(str(item.get("id") or ""), [])
    return item


def search_enrich(ctx, rows):
    """Attach the bulk Xumo guide before keyword scoring.

    Xumo exposes its current guide in provider-level pages, so searching every
    published program title does not require one HTTP request per channel.
    """
    guide = _guide(ctx)
    output = []
    for source in rows or []:
        item = dict(source)
        if not item.get("schedule"):
            item["schedule"] = guide.get(str(item.get("id") or ""), [])
        output.append(item)
    return output


def _expand(url, ids, mode="web"):
    android = str(mode or "web").casefold() == "androidtv"
    replacements = {
        "[PLATFORM]": "androidtv" if android else "web",
        "[APP_VERSION]": "androidtv" if android else "1.0.0",
        "[timestamp]": str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)),
        "[app_bundle]": "com.xumo.xumo.tv" if android else "play.xumo.com",
        "[device_make]": "Amazon" if android else "Kodi",
        "[device_model]": "AFTT" if android else "StreamDial", "[content_language]": "en",
        "[IS_LAT]": "0", "[IFA]": ids["ifa"], "[IFA_TYPE]": "aaid",
        "[SESSION_ID]": str(uuid.uuid4()), "[DEVICE_ID]": ids["device"].replace("-", ""),
        "[CCPA_Value]": "1---", "[OS]": "web", "[OS_VERSION]": "",
    }
    for key, value in replacements.items():
        url = url.replace(key, value)
    url = re.sub(r"\[[^\]]+\]", "", url)
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode(urllib.parse.parse_qsl(parts.query, keep_blank_values=False))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


class XumoResolveFailure(ValueError):
    def __init__(self, message, attempts=None):
        self.diagnostics = {"provider": "xumo", "attempts": list(attempts or [])}
        super().__init__(message)


def _xumo_attempt(attempts, stage, result, url="", reason=""):
    attempts.append({
        "stage": stage, "result": result, "url": url, "reason": reason,
        "play_id_source": "n/a", "play_id": "n/a", "media_format": "hls",
    })


def _direct_broadcast_url(payload):
    url = str((payload or {}).get("ssaiStreamUrl") or "")
    if url:
        return url
    for row in (payload or {}).get("ssaiStreamUrls") or []:
        if not isinstance(row, dict) or not row.get("url"):
            continue
        mime = str(row.get("mimeType") or row.get("type") or "").casefold()
        candidate = str(row.get("url") or "")
        if "mpegurl" in mime or "hls" in mime or ".m3u8" in candidate.casefold():
            return candidate
    return ""


def _asset_source_url(ctx, base, asset_id, attempts):
    endpoint = base + "/v2/assets/asset/{}.json".format(asset_id)
    try:
        payload = ctx.http.get_json(endpoint, params={"f": "providers"}, headers=HEADERS, timeout=20, max_bytes=4 * 1024 * 1024)
    except Exception as error:
        _xumo_attempt(attempts, "asset", "error", getattr(error, "url", "") or endpoint, str(error))
        return ""
    preferred = []
    fallback = []
    for provider in payload.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        for source in provider.get("sources") or []:
            if not isinstance(source, dict):
                continue
            uri = str(source.get("uri") or source.get("url") or "")
            if not uri:
                continue
            mime = str(source.get("mimeType") or source.get("type") or "").casefold()
            if "mpegurl" in mime or "hls" in mime or ".m3u8" in uri.casefold():
                preferred.append(uri)
            else:
                fallback.append(uri)
    candidate = (preferred or fallback or [""])[0]
    _xumo_attempt(attempts, "asset", "ok" if candidate else "no-source", endpoint,
                  "asset_id={} providers={}".format(asset_id, len(payload.get("providers") or [])))
    return candidate


def resolve(ctx, identifier):
    identifier = str(identifier or "")
    attempts = []
    hour = datetime.datetime.now(datetime.timezone.utc).hour

    # Primary web-client route, followed by the provider's asset-source route.
    # Some Xumo rows publish an asset for the current broadcast without also
    # filling ssaiStreamUrl; those were previously rejected before playback.
    for base in (BASE, ANDROID_BASE):
        endpoint = base + "/v2/channels/channel/{}/broadcast.json".format(identifier)
        try:
            payload = ctx.http.get_json(
                endpoint, params={"hour": hour}, headers=HEADERS, timeout=20,
                max_bytes=4 * 1024 * 1024)
        except Exception as error:
            _xumo_attempt(attempts, "broadcast", "error", getattr(error, "url", "") or endpoint, str(error))
            continue

        url = _direct_broadcast_url(payload)
        _xumo_attempt(
            attempts, "broadcast", "direct-hls" if url else "no-direct-hls", endpoint,
            "assets={} keys={}".format(len(payload.get("assets") or []), ",".join(sorted(payload.keys())[:20])))
        if not url:
            for asset in payload.get("assets") or []:
                if not isinstance(asset, dict):
                    continue
                asset_id = str(asset.get("id") or "")
                if not asset_id:
                    continue
                url = _asset_source_url(ctx, base, asset_id, attempts)
                if url:
                    break
        if url:
            plan = {
                "url": _expand(str(url), _ids(ctx), "androidtv" if base == ANDROID_BASE else "web"), "headers": HEADERS,
                "mime_type": "application/vnd.apple.mpegurl", "manifest_type": "hls",
                "diagnostics": {
                    "provider": "xumo", "selected_source": "direct-broadcast" if _direct_broadcast_url(payload) else "asset-provider",
                    "api_host": urllib.parse.urlsplit(base).netloc,
                },
            }
            return plan

    # Force the next directory rebuild to refresh Xumo's published lineup in
    # case this was a recently retired catalog row rather than a temporary gap.
    ctx.cache.delete("xumo-catalog-v1")
    raise XumoResolveFailure("Xumo did not return a current HLS broadcast or asset source", attempts)
