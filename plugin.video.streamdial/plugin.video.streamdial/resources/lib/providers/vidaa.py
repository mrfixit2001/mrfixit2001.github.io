"""VIDAA Free TV direct catalog, schedule, HLS, and Widevine-DASH playback.

The implementation speaks directly to the signed backend used by Hisense's
VIDAA Channels application. Provider-specific request signing and DRM license
construction intentionally stay inside this module.
"""

import base64
import concurrent.futures
import hashlib
import json
import time
import urllib.parse
import uuid

from ..http import DEFAULT_UA, HttpClient
from ..models import channel, normalize_genres, program


KEY = "vidaa"
NAME = "VIDAA Free TV"
BROWSABLE = True

APP_KEY = "1204099470"
APP_SECRET = "64nprh5fhk2syebs6qlkmmpt3l7s3ljg"
OAUTH_CLIENT_SECRET = "28B3E8943D6FADFB28071C303EF3F26AAC634B4BA1F9937FFD725F0ED516CA1952237D39DC2460219C46E0B7170577AD"
OAUTH_URL = "https://partner.vidaahub.com/ns/account/oauth2.0/access_token"
LAYOUT = "https://partner-layout-ui.vidaahub.com"
DETAIL = "https://partner-detail-ui.vidaahub.com"
LIVE_TYPE = "600007"
DEVICE_ID = "002003059003001007000128pngito97hw8ktrcmhh4njsaof802ig"
SPANISH_COLUMN = "In Spanish"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
COMMON_PARAMS = {
    "appPackageName": "net.vidaatv.hisense.android.tv",
    "appVersion": "os_t.atvchannels.1.00.0.0.Q030800",
    "language": "eng",
    "locale": "USA",
    "country": "USA",
    "brand": "his",
    "capabilityCode": "2026031101",
    "playerFamilyName": "VDAndroid_TV",
    "playerFamilyVersion": "006110000",
    "deviceType": "1",
    "appOwnership": "oem",
    "personalizedRec": "1",
    "localeLanguage": "eng",
}


def _sign_query(params):
    keys = sorted(key for key, value in params.items() if value not in (None, ""))
    data = "&".join("{}={}".format(key, params[key]) for key in keys) + APP_SECRET
    return base64.b64encode(hashlib.md5(data.encode("utf-8")).digest()).decode("ascii")


def _sign_body(body):
    return base64.b64encode(hashlib.md5((body + APP_SECRET).encode("utf-8")).digest()).decode("ascii")


def _token(ctx, force=False):
    key = "vidaa-oauth-v1"
    if force:
        ctx.cache.delete(key)

    def load():
        payload = ctx.http.get_json(
            OAUTH_URL,
            params={
                "client_id": APP_KEY,
                "client_secret": OAUTH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=20,
        )
        value = str((payload or {}).get("access_token") or "")
        if not value:
            raise ValueError("VIDAA did not return an application access token")
        return {"value": value, "expires_in": int((payload or {}).get("expires_in") or 3600)}

    # A conservative TTL avoids using the token close to its provider expiry.
    return ctx.cache.remember(key, 45 * 60, load)["value"]


def _base_params(ctx, token=None, **extra):
    values = dict(COMMON_PARAMS)
    values["deviceId"] = DEVICE_ID
    values["accessToken"] = token or _token(ctx)
    values["commonRandomId"] = uuid.uuid4().hex
    values.update({key: str(value) for key, value in extra.items()})
    return values


def _get(ctx, host, path, params):
    return ctx.http.get_json(
        host + path,
        params=params,
        headers={"User-Agent": UA, "Accept": "application/json", "appKey": APP_KEY, "x-sign-for": _sign_query(params)},
        timeout=30,
        max_bytes=8 * 1024 * 1024,
    )


def _post_http(http, host, path, params, body):
    body_text = json.dumps(body, separators=(",", ":"))
    response, _, _ = http.request(
        host + path,
        method="POST",
        params=params,
        data=body_text.encode("utf-8"),
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "appKey": APP_KEY,
            "x-sign-for": _sign_body(body_text),
        },
        timeout=30,
        max_bytes=8 * 1024 * 1024,
    )
    try:
        return json.loads(response.decode("utf-8", "replace"))
    except (TypeError, ValueError) as error:
        raise ValueError("VIDAA returned invalid media details") from error


def _medias_info(ctx, channel_ids, related_date=None, http=None, token=None):
    token = token or _token(ctx)
    params = _base_params(
        ctx, token=token, sceneCode="ottChannel",
        relatedDate=str(int(related_date if related_date is not None else time.time())),
    )
    body = {"medias": [{"id": int(identifier), "typeCode": LIVE_TYPE} for identifier in channel_ids]}
    return _post_http(http or ctx.http, DETAIL, "/api/v1.0.0/detailApi/mediasInfo", params, body)


def _discover_columns(ctx):
    params = _base_params(ctx, scene="osPagingChannel", resourceType="1")
    payload = _get(ctx, LAYOUT, "/api/v1.0.0/layoutApi/activityResources", params)
    rows = (payload or {}).get("columns") or []
    if not isinstance(rows, list) or not rows:
        raise ValueError("VIDAA returned no live-TV category columns")
    return rows


def _column_tiles(ctx, column_id):
    found = {}
    for page in range(1, 21):
        params = _base_params(ctx, columnId=str(column_id), scene="osPagingChannel", ratio="16:9")
        if page > 1:
            params["metaInfo"] = "page={},oneMoreTile=1".format(page)
        payload = _get(ctx, LAYOUT, "/api/v1.0.0/layoutApi/columnData", params)
        tiles = ((payload or {}).get("column") or {}).get("tiles") or []
        if not tiles:
            break
        for tile in tiles:
            if isinstance(tile, dict) and str(tile.get("typeCode") or "") == LIVE_TYPE and tile.get("id") is not None:
                found[str(tile["id"])] = tile
    return list(found.values())


def _clean_stream_url(url):
    parts = urllib.parse.urlsplit(str(url or ""))
    if parts.scheme not in ("http", "https"):
        return ""
    pairs = []
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if any(marker in str(value) for marker in ("[", "]", "{", "}")):
            continue
        if any(marker in str(key) for marker in ("[", "]", "{", "}")):
            continue
        pairs.append((key, value))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(pairs, doseq=True), ""))


def _mini_player(url):
    value = str(url or "")
    if "mini-player" not in value:
        return None
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(value).query)
    stream = urllib.parse.unquote((query.get("streaming_url") or [""])[0])
    drm = urllib.parse.unquote((query.get("drm_url") or [""])[0])
    stream_id = str((query.get("stream_id") or [""])[0])
    stream = _clean_stream_url(stream)
    if not stream or not drm.startswith(("http://", "https://")) or not stream_id:
        return None
    return {"streaming_url": stream, "drm_url": drm, "stream_id": stream_id}


def _media_schedule(media):
    output = []
    for row in ((media.get("channelInfo") or {}).get("scheduleList") or []):
        if not isinstance(row, dict):
            continue
        title = str(row.get("scheduleName") or "").strip()
        start = row.get("startTime")
        stop = row.get("endTime")
        if not title or not start or not stop:
            continue
        output.append(program(
            title, start, stop, row.get("summary") or "", row.get("recommendPic") or ""))
    output.sort(key=lambda row: row.get("start") or "")
    return output



def _origin_provider(media):
    """Best-effort upstream-provider hint from VIDAA's own media metadata."""
    try:
        text = json.dumps(media, separators=(",", ":")).casefold()
    except Exception:
        text = str(media or "").casefold()
    for provider, tokens in (
        ("xumo", ("xumo",)), ("pluto", ("pluto",)), ("tubi", ("tubi",)),
        ("plex", ("plex",)), ("roku", ("roku",)), ("samsung", ("samsung tv plus", "samsungtvplus")),
    ):
        if any(token in text for token in tokens):
            return provider
    return ""

def _build_channel(media, categories):
    identifier = str(media.get("id") or "").strip()
    show = media.get("showInfo") or {}
    info = media.get("channelInfo") or {}
    name = str(show.get("title") or "").strip()
    if not identifier or not name or not info.get("streamingParam"):
        return None
    category_values = [value for value in categories if value and value != SPANISH_COLUMN]
    language = "Spanish" if SPANISH_COLUMN in categories else "English"
    genres = normalize_genres(category_values, name)
    return channel(
        KEY,
        identifier,
        name,
        description=show.get("summary") or show.get("description") or "",
        languages=language,
        genres=genres,
        logo=show.get("frontPic") or show.get("appIcon") or "",
        fanart=(show.get("backgroundPic") or show.get("backPic") or show.get("recommendPic")
                or show.get("posterPic") or show.get("frontPic") or ""),
        schedule=_media_schedule(media),
        extra={
            "encrypted": bool((info.get("streamingDetailParam") or {}).get("encryption")),
            "categories": list(categories),
            "origin_provider": _origin_provider(media),
        },
    )


def _catalog(ctx):
    def load():
        columns = _discover_columns(ctx)
        categories_by_id = {}
        for column_value in columns:
            if not isinstance(column_value, dict) or column_value.get("id") is None:
                continue
            title = str(column_value.get("title") or "").strip()
            for tile in _column_tiles(ctx, column_value["id"]):
                identifier = str(tile.get("id") or "")
                if identifier:
                    categories_by_id.setdefault(identifier, [])
                    if title and title not in categories_by_id[identifier]:
                        categories_by_id[identifier].append(title)
        identifiers = list(categories_by_id)
        if not identifiers:
            raise ValueError("VIDAA returned no linear channel identifiers")

        batches = [identifiers[index:index + 5] for index in range(0, len(identifiers), 5)]
        token = _token(ctx)
        media_rows = []

        def fetch(batch):
            # No cookies are involved. An independent urllib opener per worker
            # prevents cross-thread state from leaking into the provider client.
            http = HttpClient(user_agent=UA, cookies=False)
            return (_medias_info(ctx, batch, http=http, token=token) or {}).get("medias") or []

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(batches) or 1)) as pool:
            futures = {pool.submit(fetch, batch): batch for batch in batches}
            for future in concurrent.futures.as_completed(futures):
                batch = futures[future]
                try:
                    values = future.result()
                except Exception:
                    # A single provider request must not discard the entire VIDAA
                    # catalog. Retry only that provider-defined five-ID batch once
                    # through the normal context client, then continue with the
                    # remaining provider data if the retry also fails.
                    try:
                        values = (_medias_info(ctx, batch, token=token) or {}).get("medias") or []
                    except Exception:
                        values = []
                media_rows.extend(value for value in values if isinstance(value, dict))

        output = []
        for media in media_rows:
            identifier = str(media.get("id") or "")
            value = _build_channel(media, categories_by_id.get(identifier, []))
            if value:
                output.append(value)
        if not output:
            raise ValueError("VIDAA returned no usable live channels")
        return output

    return ctx.cache.remember("vidaa-catalog-v1", 10 * 60, load)


def channels(ctx):
    return _catalog(ctx)


def search_enrich(ctx, rows):
    # Current scheduleList data is bundled with mediasInfo during catalog build.
    return rows


def enrich(ctx, item):
    # Refresh just this channel's provider-published current schedule without
    # forcing a full five-day guide fan-out.
    value = dict(item)
    try:
        payload = _medias_info(ctx, [int(item.get("id"))])
        media = ((payload or {}).get("medias") or [None])[0]
        if isinstance(media, dict):
            value["schedule"] = _media_schedule(media)
            if not value.get("fanart"):
                show = media.get("showInfo") or {}
                value["fanart"] = (show.get("backgroundPic") or show.get("backPic") or show.get("recommendPic")
                                   or show.get("posterPic") or show.get("frontPic") or "")
            if not value.get("fanart"):
                value["fanart"] = next((row.get("image") for row in value.get("schedule") or [] if row.get("image")), "")
    except Exception:
        pass
    return value


def _playback_request_header(stream_id):
    payload = {
        "assetId": str(stream_id),
        "clientMetadata": {
            "name": "vod",
            "version": "1.0.0",
            "os": {"name": "Linux", "version": "5.15.148"},
            "device": {"type": "vidaaOS"},
            "supportedDrms": [{"type": "widevine"}, {"type": "playready"}],
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def resolve(ctx, identifier):
    payload = _medias_info(ctx, [int(identifier)])
    media = ((payload or {}).get("medias") or [None])[0]
    if not isinstance(media, dict):
        raise ValueError("VIDAA channel is unavailable")
    info = media.get("channelInfo") or {}
    raw = str(info.get("streamingParam") or "")
    encrypted = bool((info.get("streamingDetailParam") or {}).get("encryption"))
    headers = {"User-Agent": UA}
    if not encrypted:
        url = _clean_stream_url(raw)
        if not url:
            raise ValueError("VIDAA did not return a current HLS stream")
        return {
            "url": url,
            "headers": headers,
            "mime_type": "application/vnd.apple.mpegurl",
            "manifest_type": "hls",
            "diagnostics": {"provider": KEY, "selected_source": "vidaa-direct-hls", "encrypted": False},
        }

    wrapped = _mini_player(raw)
    if not wrapped:
        raise ValueError("VIDAA returned an unrecognized encrypted stream wrapper")
    license_url = "{}{}streamId={}&reqFrom=v_tvch_m".format(
        wrapped["drm_url"], "&" if "?" in wrapped["drm_url"] else "?",
        urllib.parse.quote(wrapped["stream_id"], safe=""))
    license_headers = urllib.parse.urlencode({
        "playback-request": _playback_request_header(wrapped["stream_id"]),
        "User-Agent": UA,
    })
    return {
        "url": wrapped["streaming_url"],
        "headers": headers,
        "mime_type": "application/dash+xml",
        "manifest_type": "mpd",
        "license_type": "com.widevine.alpha",
        # Widevine receives the raw CDM challenge body. The provider-specific
        # playback-request header carries the anonymous app metadata Vidaa expects.
        "license_key": "{}|{}|R{{SSM}}|R".format(license_url, license_headers),
        "diagnostics": {
            "provider": KEY,
            "selected_source": "vidaa-direct-dash-widevine",
            "encrypted": True,
            "license_host": urllib.parse.urlsplit(license_url).netloc,
        },
    }
