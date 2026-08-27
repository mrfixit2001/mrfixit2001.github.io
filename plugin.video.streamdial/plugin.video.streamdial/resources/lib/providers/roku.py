"""Anonymous Roku Channel catalog and playback using Roku's web APIs."""

import datetime
import re
import threading
import time
import urllib.parse
import unicodedata

from ..http import DEFAULT_UA, HttpFailure
from ..models import channel, normalize_genres, normalize_languages, program


KEY = "roku"
NAME = "The Roku Channel"
BROWSABLE = True
BASE = "https://therokuchannel.roku.com"
HOME = BASE + "/"
CSRF_URL = BASE + "/api/v1/csrf"
EPG_URL = BASE + "/api/v2/epg"
PLAYBACK_URL = BASE + "/api/v3/playback"
CONTENT_URL = "https://content.sr.roku.com/content/v1/roku-trc/{}"
PROXY_BASE = BASE + "/api/v2/homescreen/content/"
_SESSION_LOCK = threading.Lock()
ROKU_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
_HLS_KEY_RE = re.compile(br"^#EXT-X-KEY:(.+)$", re.IGNORECASE | re.MULTILINE)
_CATALOG_CACHE_KEY = "roku-catalog-v5"
_OLD_CATALOG_CACHE_KEYS = ("roku-catalog-v4", "roku-catalog-v3", "roku-catalog-v2")
_UNAVAILABLE_CACHE_KEY = "roku-direct-unavailable-v1"
_UNAVAILABLE_TTL = 30 * 60


class RokuHlsIncompatible(ValueError):
    """Roku returned an HLS representation Kodi cannot consume."""

    def __init__(self, reason, message=""):
        self.reason = str(reason or "unknown")
        super().__init__(message or "Roku HLS master is not Kodi-compatible ({})".format(self.reason))


class RokuResolveFailure(ValueError):
    """Final Roku failure carrying privacy-safe per-hop diagnostics."""

    def __init__(self, message, attempts=None, cookie_count=0, has_usn=False):
        self.diagnostics = {
            "provider": "roku",
            "attempts": list(attempts or []),
            "cookie_count": int(cookie_count or 0),
            "has_usn": bool(has_usn),
        }
        super().__init__(message)



def _headers(csrf="", navigation=False):
    if navigation:
        return {
            "User-Agent": ROKU_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "max-age=0",
            "Pragma": "no-cache",
            "Referer": HOME,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
    values = {
        "User-Agent": ROKU_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": BASE,
        "Referer": HOME,
        "x-roku-reserved-amoeba-ids": "",
        "x-roku-reserved-experiment-configs": "e30=",
        "x-roku-reserved-experiment-state": "W10=",
        "x-roku-reserved-lat": "0",
    }
    if csrf:
        values.update({"csrf-token": csrf, "Content-Type": "application/json"})
    return values


def _bootstrap(ctx):
    cached = getattr(ctx, "_streamdial_roku_csrf", "")
    if cached:
        return cached
    with _SESSION_LOCK:
        cached = getattr(ctx, "_streamdial_roku_csrf", "")
        if cached:
            return cached
        ctx.http.request(
            HOME, headers=_headers(navigation=True), timeout=20,
            max_bytes=1024, allow_truncated=True)
        last_error = None
        for _ in range(3):
            try:
                payload = ctx.http.get_json(CSRF_URL, headers=_headers(), timeout=15)
                token = str(payload.get("csrf") or "").strip()
                if token:
                    setattr(ctx, "_streamdial_roku_csrf", token)
                    return token
            except HttpFailure as error:
                last_error = error
        if last_error:
            raise last_error
        raise ValueError("Roku did not return an anonymous web session")


def _unavailable_ids(ctx):
    """Return recently confirmed unavailable direct-Roku channel ids."""
    now = time.time()
    raw = ctx.cache.get(_UNAVAILABLE_CACHE_KEY, 24 * 60 * 60, {})
    raw = raw if isinstance(raw, dict) else {}
    active = {}
    for identifier, until in raw.items():
        try:
            until = float(until)
        except (TypeError, ValueError):
            continue
        if until > now:
            active[str(identifier)] = until
    if active != raw:
        ctx.cache.set(_UNAVAILABLE_CACHE_KEY, active)
    return set(active)


def _mark_unavailable(ctx, identifier):
    """Temporarily hide a row proven absent from Roku's current direct APIs."""
    identifier = str(identifier or "")
    if not identifier:
        return
    now = time.time()
    raw = ctx.cache.get(_UNAVAILABLE_CACHE_KEY, 24 * 60 * 60, {})
    raw = raw if isinstance(raw, dict) else {}
    active = {}
    for key, until in raw.items():
        try:
            until = float(until)
        except (TypeError, ValueError):
            continue
        if until > now:
            active[str(key)] = until
    active[identifier] = now + _UNAVAILABLE_TTL
    ctx.cache.set(_UNAVAILABLE_CACHE_KEY, active)
    ctx.cache.delete(_CATALOG_CACHE_KEY)


def _station_languages(station):
    """Best-effort language classification from Roku's own station metadata."""
    values = []

    def add(value):
        if isinstance(value, (list, tuple, set)):
            for nested in value:
                add(nested)
            return
        if isinstance(value, dict):
            for key in ("code", "value", "name", "language", "locale"):
                if value.get(key):
                    add(value.get(key))
                    return
            return
        text = str(value or "").strip()
        if text:
            values.append(text.split("-", 1)[0] if re.match(r"^[a-zA-Z]{2,3}-[A-Za-z]{2,4}$", text) else text)

    meta = station.get("meta") or {}
    for key in ("language", "languages", "audioLanguage", "audioLanguages", "contentLanguage", "locale"):
        add(station.get(key))
        add(meta.get(key))

    tags = {str(tag or "").strip().casefold() for tag in station.get("tags") or []}
    raw_title = str(station.get("title") or station.get("shortName") or "")
    title = raw_title.casefold()
    title_ascii = unicodedata.normalize("NFKD", raw_title).encode("ascii", "ignore").decode("ascii").casefold()
    title_tokens = set(re.findall(r"[a-z0-9]+", title_ascii))

    tag_languages = []
    for tag in tags:
        if tag.endswith("-language") and len(tag) > len("-language"):
            for language in normalize_languages(tag[:-len("-language")].replace("_", " "), default=""):
                if language not in tag_languages:
                    tag_languages.append(language)
    if tag_languages:
        return tag_languages

    # Roku does not consistently populate language tags for every Hispanic
    # FAST row. Strong Spanish-language title markers cover currently observed
    # omissions such as FILMEX Accion without broadly guessing from genre alone.
    spanish_title_tokens = {
        "accion", "clasico", "clasicos", "comedia", "comedias", "pelicula",
        "peliculas", "telenovela", "telenovelas", "noticias", "deportes",
        "crimenes", "romantico", "romantica", "espanol",
    }
    spanish = bool(
        tags.intersection({"spanish", "espanol", "español", "en-espanol", "en-español"})
        or "español" in title or "espanol" in title_ascii
        or title_tokens.intersection(spanish_title_tokens)
        or title_ascii.startswith("filmex ") or title_ascii == "filmex"
        or " en espanol" in " " + title_ascii
    )
    if spanish:
        return ["Spanish"]

    normalized = normalize_languages(values, default="")
    return normalized or ["English"]


def _catalog(ctx):
    # v2 can survive addon upgrades on disk and was observed serving Roku ids
    # that the provider's live EPG no longer contained. Never consult it again.
    for old_key in _OLD_CATALOG_CACHE_KEYS:
        ctx.cache.delete(old_key)

    def load():
        csrf = _bootstrap(ctx)
        payload = ctx.http.get_json(
            EPG_URL, headers=_headers(csrf), timeout=30, max_bytes=30 * 1024 * 1024,
        )
        output = []
        seen = set()
        for collection in payload.get("collections") or []:
            station = ((collection.get("features") or {}).get("station") or {})
            identifier = str((station.get("meta") or {}).get("id") or "")
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            view_options = station.get("viewOptions") or []
            view = view_options[0] if view_options and isinstance(view_options[0], dict) else {}
            image_map = station.get("imageMap") or {}
            logo = ""
            for key in ("gridEpg", "epgLogo", "liveHudLogo", "epgLogoDark"):
                if isinstance(image_map.get(key), dict) and image_map[key].get("path"):
                    logo = image_map[key]["path"]
                    break
            output.append({
                "id": identifier,
                "name": station.get("title") or station.get("shortName") or identifier,
                "number": station.get("displayNumber") or "",
                "logo": logo, "tags": station.get("tags") or [],
                "kids": bool(station.get("kidsDirected")),
                "languages": _station_languages(station),
                "play_id": view.get("playId") or "",
                "has_view_options": bool(view_options),
            })
        if not output:
            raise ValueError("Roku returned no live channels for this region")
        return output

    # Roku's lineup changes often enough that a 30-minute disk cache was too
    # sticky. Ten minutes still keeps browsing fast while dropping retired rows.
    return ctx.cache.remember(_CATALOG_CACHE_KEY, 10 * 60, load)


def channels(ctx):
    unavailable = _unavailable_ids(ctx)
    output = []
    for row in _catalog(ctx):
        if row["id"] in unavailable:
            continue
        # The observed failing Roku rows are still present in /api/v2/epg but
        # carry no viewOptions at all; their per-content proxy also returns 404.
        # With no provider-advertised playback option there is no playId that can
        # be submitted to /api/v3/playback, so do not present those dead rows.
        # Rows with viewOptions but a missing/stale playId are retained because
        # the content proxy can legitimately mint a fresh playId at tune time.
        if not row.get("has_view_options"):
            continue
        raw_tags = row.get("tags") or []
        if not isinstance(raw_tags, (list, tuple, set)):
            raw_tags = [raw_tags]
        tags = list(raw_tags)
        if row.get("kids"):
            tags.insert(0, "Kids & Family")
        output.append(channel(
            KEY, row["id"], row["name"], languages=row.get("languages") or "English",
            genres=normalize_genres(tags, row["name"]), logo=row.get("logo") or "",
            number=row.get("number") or "", extra={"play_id": row.get("play_id") or ""},
        ))
    return output


def _description(content):
    values = content.get("descriptions") or {}
    for key in ("250", "100", "60", "40"):
        value = values.get(key)
        if isinstance(value, dict):
            value = value.get("text")
        if value:
            return str(value)
    return str(content.get("description") or "")


def _schedule(ctx, csrf, identifier):
    cache_key = "roku-guide-v1-{}".format(identifier)

    def load():
        # Roku's current web flow encodes featureInclude into the proxied
        # content URL. Keep the alternate outer-query shape as a 404-only
        # compatibility fallback because individual station metadata can drift.
        target = CONTENT_URL.format(identifier) + "?featureInclude=linearSchedule"
        proxy = PROXY_BASE + urllib.parse.quote(target, safe="")
        try:
            payload = ctx.http.get_json(
                proxy, headers=_headers(csrf), timeout=15,
                max_bytes=4 * 1024 * 1024)
        except HttpFailure as error:
            if error.status != 404:
                raise
            target = CONTENT_URL.format(identifier)
            proxy = PROXY_BASE + urllib.parse.quote(target, safe="")
            try:
                payload = ctx.http.get_json(
                    proxy, params={"featureInclude": "linearSchedule"},
                    headers=_headers(csrf), timeout=15,
                    max_bytes=4 * 1024 * 1024)
            except HttpFailure as fallback_error:
                if fallback_error.status == 404:
                    # A subset of lineup rows are valid/playable channels but
                    # have no content-proxy schedule object. Missing guide data
                    # must not turn channel browsing into a provider failure.
                    return []
                raise
        events = []
        for row in ((payload.get("features") or {}).get("linearSchedule") or []):
            start = str(row.get("date") or "")
            try:
                parsed = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
                stop = parsed + datetime.timedelta(seconds=float(row.get("duration") or 0))
            except (TypeError, ValueError):
                continue
            content = row.get("content") or {}
            series = content.get("series") or {}
            title = series.get("title") or content.get("title") or "Live programming"
            image_map = content.get("imageMap") or {}
            image = ""
            for key in ("gridEpg", "grid"):
                value = image_map.get(key) or {}
                if isinstance(value, dict) and value.get("path"):
                    image = value["path"]
                    break
            events.append(program(title, start, stop.isoformat(), _description(content), image))
        return events

    return ctx.cache.remember(cache_key, 10 * 60, load)


def enrich(ctx, item):
    if item.get("schedule"):
        return item
    item = dict(item)
    csrf = _bootstrap(ctx)
    item["schedule"] = _schedule(ctx, csrf, str(item.get("id") or ""))
    return item


def search_enrich(ctx, rows):
    """Merge only already-cached per-channel Roku guides for keyword search.

    Roku exposes linearSchedule through a per-channel content-proxy request. A
    full search-time fan-out across hundreds of channels would be both slow and
    unnecessarily aggressive, so keyword search reuses schedules fetched while
    browsing without causing new per-channel guide traffic.
    """
    output = []
    marker = object()
    for source in rows or []:
        item = dict(source)
        if not item.get("schedule"):
            identifier = str(item.get("id") or "")
            cached = ctx.cache.get("roku-guide-v1-{}".format(identifier), 10 * 60, marker)
            if cached is not marker and isinstance(cached, list):
                item["schedule"] = cached
        output.append(item)
    return output


def _cookie_state(ctx):
    cookies = list(ctx.http.cookie_jar or []) if ctx.http.cookie_jar else []
    return len(cookies), any(cookie.name == "_usn" for cookie in cookies)


def _record_attempt(attempts, ctx, stage, play_id_source="", media_format="",
                    result="", error=None, reason="", plan=None):
    cookie_count, has_usn = _cookie_state(ctx)
    plan = plan if isinstance(plan, dict) else {}
    row = {
        "stage": str(stage or "unknown"),
        "play_id_source": str(play_id_source or "n/a"),
        "play_id": (
            "present" if (stage != "play-id" and play_id_source and play_id_source != "none")
            or (stage == "play-id" and result == "ok") else "missing" if stage == "play-id" else "n/a"
        ),
        "media_format": str(media_format or "n/a"),
        "result": str(result or "n/a"),
        "status": getattr(error, "status", None),
        "reason": str(reason or ""),
        "url": str(getattr(error, "url", "") or plan.get("url") or ""),
        "content_type": str(getattr(error, "content_type", "") or ""),
        "response_body": str(getattr(error, "response_body", "") or "")[:2048],
        "cookie_count": cookie_count,
        "has_usn": has_usn,
    }
    attempts.append(row)
    return row


def _content_play_id(ctx, csrf, identifier, attempts=None, source="content-proxy"):
    """Fetch the current per-channel playId from Roku's content proxy."""
    target = CONTENT_URL.format(identifier)
    proxy = PROXY_BASE + urllib.parse.quote(target, safe="")
    try:
        payload = ctx.http.get_json(proxy, headers=_headers(csrf), timeout=15)
    except HttpFailure as error:
        if attempts is not None:
            _record_attempt(attempts, ctx, "play-id", source, result="http-error", error=error)
        raise
    view = (payload.get("viewOptions") or [{}])[0]
    value = str(view.get("playId") or "")
    if attempts is not None:
        _record_attempt(
            attempts, ctx, "play-id", source, result="ok" if value else "missing",
            reason="play-id-present" if value else "viewOptions[0].playId missing")
    return value


def _cached_epg_play_id(ctx, identifier, attempts=None):
    row = next((value for value in _catalog(ctx) if value["id"] == str(identifier)), None)
    value = str((row or {}).get("play_id") or "")
    if attempts is not None:
        _record_attempt(
            attempts, ctx, "play-id", "cached-epg", result="ok" if value else "missing",
            reason="play-id-present" if value else "catalog playId missing")
    return value


def _live_epg_play_id(ctx, csrf, identifier, attempts=None):
    """Fetch a same-session EPG playId without consulting StreamDial's disk cache."""
    try:
        payload = ctx.http.get_json(
            EPG_URL, headers=_headers(csrf), timeout=30, max_bytes=30 * 1024 * 1024)
    except HttpFailure as error:
        if attempts is not None:
            _record_attempt(attempts, ctx, "play-id", "live-epg", result="http-error", error=error)
        raise

    value = ""
    channel_present = False
    view_options_present = False
    view_options_count = 0
    total_station_count = 0
    matched_ordinal = 0
    station_keys = []
    meta_keys = []
    collections = payload.get("collections") or []

    # Scan the complete payload even after finding the target.  Earlier
    # diagnostics labelled the target's ordinal position as the station count,
    # which made successive live EPG responses look artificially inconsistent.
    for collection in collections:
        station = ((collection.get("features") or {}).get("station") or {})
        station_id = str((station.get("meta") or {}).get("id") or "")
        if station_id:
            total_station_count += 1
        if station_id != str(identifier) or channel_present:
            continue
        channel_present = True
        matched_ordinal = total_station_count
        station_keys = sorted(str(key) for key in station.keys())[:40]
        meta = station.get("meta") if isinstance(station.get("meta"), dict) else {}
        meta_keys = sorted(str(key) for key in meta.keys())[:30]
        view_options = station.get("viewOptions") or []
        view_options_count = len(view_options) if isinstance(view_options, list) else 0
        view_options_present = bool(view_options_count)
        view = view_options[0] if view_options_count and isinstance(view_options[0], dict) else {}
        value = str(view.get("playId") or "")

    common = (
        "live_epg_stations={} matched_ordinal={} viewOptions_count={} station_keys={} meta_keys={}".format(
            total_station_count, matched_ordinal or "n/a", view_options_count,
            ",".join(station_keys) or "none", ",".join(meta_keys) or "none"
        )
    )
    if value:
        reason = "play-id-present " + common
    elif channel_present:
        reason = "channel-present-playId-missing viewOptions={} {}".format(
            "yes" if view_options_present else "no", common)
    else:
        reason = "channel-absent-from-live-epg " + common
    if attempts is not None:
        _record_attempt(
            attempts, ctx, "play-id", "live-epg", result="ok" if value else "missing",
            reason=reason)
    return value


def _session_id(ctx):
    if ctx.http.cookie_jar:
        return next((cookie.value for cookie in ctx.http.cookie_jar if cookie.name == "_usn"), "streamdial")
    return "streamdial"


def _reset_session(ctx):
    """Discard only this resolve context's anonymous Roku session."""
    setattr(ctx, "_streamdial_roku_csrf", "")
    if ctx.http.cookie_jar:
        try:
            ctx.http.cookie_jar.clear()
        except (KeyError, ValueError):
            pass


def _extract_plan(payload, requested_format="m3u"):
    """Normalize current and legacy Roku playback response layouts."""
    payload = payload if isinstance(payload, dict) else {}
    url = str(payload.get("url") or "")
    selected_drm = {}
    selected_format = ""

    if not url:
        videos = ((payload.get("playbackMedia") or {}).get("videos") or [])
        videos = [row for row in videos if isinstance(row, dict) and row.get("url")]
        want_dash = requested_format == "mpeg-dash"

        def rank(row):
            fmt = str(row.get("streamFormat") or "").casefold()
            if want_dash:
                return 0 if fmt == "dash" else (1 if fmt in ("hls", "m3u", "m3u8") else 9)
            return 0 if fmt in ("hls", "m3u", "m3u8") else (1 if fmt == "dash" else 9)

        selected = min(videos, key=rank) if videos else None
        if selected and rank(selected) < 9:
            url = str(selected.get("url") or "")
            selected_drm = selected.get("drmParams") or {}
            selected_format = str(selected.get("streamFormat") or "").casefold()

    if not url:
        raise ValueError("Roku playback response did not contain a stream URL")

    lowered = url.casefold()
    is_dash = ".mpd" in lowered or selected_format == "dash"
    plan = {
        "url": url,
        "headers": {"User-Agent": ROKU_UA},
        "mime_type": "application/dash+xml" if is_dash else "application/vnd.apple.mpegurl",
        "manifest_type": "mpd" if is_dash else "hls",
    }

    if is_dash:
        license_url = ""
        if str(selected_drm.get("keySystem") or "").casefold() == "widevine":
            license_url = str(selected_drm.get("licenseServerURL") or "")
        if not license_url:
            license_url = str((((payload.get("drm") or {}).get("widevine") or {}).get("licenseServer")) or "")
        if license_url:
            plan.update({
                "license_type": "com.widevine.alpha",
                "license_key": license_url,
            })
    return plan


def _hls_is_compatible(ctx, plan):
    """Return (compatible, reason) after sniffing the HLS master.

    AES-128 identity HLS is normal HLS encryption and is supported. Apple
    FairPlay and SAMPLE-AES/non-identity key formats are not suitable for the
    Linux HLS path; callers may then request Roku's DASH/Widevine rendition.
    """
    try:
        raw, _, _, _ = ctx.http.request_info(
            str(plan.get("url") or ""), headers=plan.get("headers") or {},
            timeout=15, max_bytes=256 * 1024, allow_truncated=True,
        )
    except HttpFailure as error:
        if error.status in (401, 403, 404, 410):
            return False, "manifest-http-{}".format(error.status), error
        return True, "probe-failed:{}".format(error.__class__.__name__), error
    except Exception as error:
        return True, "probe-failed:{}".format(error.__class__.__name__), error

    lowered = raw.lower()
    if b"#extm3u" not in lowered:
        return False, "not-hls", None
    if b"com.apple.streamingkeydelivery" in lowered:
        return False, "apple-fairplay", None
    if b"method=sample-aes" in lowered:
        return False, "sample-aes-drm", None
    for match in _HLS_KEY_RE.findall(raw):
        entry = match.lower()
        if b"method=none" in entry:
            continue
        if b"method=aes-128" in entry and (b"keyformat=" not in entry or b"keyformat=\"identity\"" in entry):
            continue
        return False, "drm-keyformat", None
    return True, "clear-or-aes-hls", None


def _playback(ctx, csrf, identifier, play_id, session, media_format="m3u"):
    if not play_id:
        raise ValueError("Roku returned no playId for this channel")
    payload = {
        "rokuId": str(identifier),
        "playId": str(play_id),
        "mediaFormat": media_format,
        "drmType": "widevine",
        "quality": "fhd",
        "bifUrl": None,
        "adPolicyId": "",
        "providerId": "rokuavod",
        "playbackContextParams": (
            "sessionId={}&pageId=trc-us-live-ml-page-en-current"
            "&isNewSession=0&idType=roku-trc"
        ).format(session),
    }
    return ctx.http.post_json(
        PLAYBACK_URL, payload=payload, headers=_headers(csrf),
        timeout=20, max_bytes=4 * 1024 * 1024)


def _resolve_format(ctx, csrf, identifier, play_id, play_id_source, media_format, attempts):
    try:
        payload = _playback(ctx, csrf, identifier, play_id, _session_id(ctx), media_format=media_format)
    except HttpFailure as error:
        _record_attempt(
            attempts, ctx, "playback", play_id_source, media_format,
            result="http-error", error=error)
        raise
    except ValueError as error:
        _record_attempt(
            attempts, ctx, "playback", play_id_source, media_format,
            result="error", reason=str(error))
        raise

    try:
        plan = _extract_plan(payload, requested_format=media_format)
    except ValueError as error:
        _record_attempt(
            attempts, ctx, "playback", play_id_source, media_format,
            result="no-stream-url", reason=str(error))
        raise

    if plan.get("manifest_type") == "hls":
        compatible, reason, probe_error = _hls_is_compatible(ctx, plan)
        if not compatible:
            _record_attempt(
                attempts, ctx, "manifest", play_id_source, media_format,
                result="incompatible", error=probe_error, reason=reason, plan=plan)
            raise RokuHlsIncompatible(reason)
        _record_attempt(
            attempts, ctx, "manifest", play_id_source, media_format,
            result="ok", reason=reason, plan=plan)
        plan["diagnostics"] = {
            "provider": "roku", "selected_source": "hls",
            "resolution": reason, "play_id": "present",
            "play_id_source": play_id_source,
            "requested_format": media_format,
        }
        return plan

    _record_attempt(
        attempts, ctx, "manifest", play_id_source, media_format,
        result="ok", reason="dash-widevine", plan=plan)
    plan["diagnostics"] = {
        "provider": "roku", "selected_source": "dash-widevine",
        "resolution": "dash-widevine-fallback", "play_id": "present",
        "play_id_source": play_id_source,
        "requested_format": media_format,
    }
    return plan


def _try_play_id(ctx, csrf, identifier, play_id, source, attempts):
    """Try Roku HLS first, then DASH/Widevine when HLS cannot be used."""
    if not play_id:
        return None, None
    hls_error = None
    try:
        return _resolve_format(ctx, csrf, identifier, play_id, source, "m3u", attempts), None
    except (HttpFailure, RokuHlsIncompatible, ValueError) as error:
        hls_error = error
        # A rejected anonymous session should be repaired before asking the same
        # session for a different representation.
        if isinstance(error, HttpFailure) and error.status in (401, 403):
            return None, error

    # FairPlay/SAMPLE-AES masters and m3u 404/502s may still have Roku's valid
    # DASH/Widevine rendition. The user can supply a known-good CDM; StreamDial's
    # existing Widevine preflight still protects genuinely missing CDMs.
    try:
        return _resolve_format(
            ctx, csrf, identifier, play_id, source, "mpeg-dash", attempts), None
    except (HttpFailure, RokuHlsIncompatible, ValueError) as dash_error:
        return None, dash_error or hls_error


def _final_failure(ctx, attempts, last_error):
    cookie_count, has_usn = _cookie_state(ctx)
    status = getattr(last_error, "status", None)
    if status:
        message = "Roku playback resolution failed after playId/session/HLS/DASH retries (last HTTP status {})".format(status)
    else:
        message = "Roku playback resolution failed after playId/session/HLS/DASH retries ({})".format(
            str(last_error) or "unknown error")
    return RokuResolveFailure(message, attempts, cookie_count, has_usn)


def resolve(ctx, identifier):
    """Resolve Roku with same-session playIds, HLS first, DASH fallback.

    Roku's current web flow is stateful: cookies + csrf + playId are correlated.
    Kodi plugin invocations are short-lived, while StreamDial's catalog cache can
    outlive those cookies. Therefore a direct content-proxy playId is preferred;
    cached EPG and a live same-session EPG are fallback sources. A clean session
    reboot gets one final pass. Every failed hop is attached to the final error
    so the upload-friendly diagnostics can show exactly which endpoint failed.
    """
    identifier = str(identifier or "")
    attempts = []
    last_error = None
    found_any_play_id = False

    for session_generation in ("initial", "refreshed"):
        if session_generation == "refreshed":
            _reset_session(ctx)
            ctx.cache.delete(_CATALOG_CACHE_KEY)
            for old_key in _OLD_CATALOG_CACHE_KEYS:
                ctx.cache.delete(old_key)
            _record_attempt(attempts, ctx, "session", "none", result="reset", reason="fresh anonymous session")

        try:
            csrf = _bootstrap(ctx)
            cookie_count, has_usn = _cookie_state(ctx)
            _record_attempt(
                attempts, ctx, "session", "none", result="ok",
                reason="{} csrf=yes cookies={} usn={}".format(
                    session_generation, cookie_count, "yes" if has_usn else "no"))
        except (HttpFailure, ValueError) as error:
            last_error = error
            _record_attempt(attempts, ctx, "session", "none", result="bootstrap-error", error=error)
            continue

        seen_play_ids = set()
        sources = []

        # Prefer a playId minted/fetched in the same anonymous session. This is
        # the current Roku web-flow posture and avoids pairing a disk-cached EPG
        # playId with unrelated cookies.
        try:
            value = _content_play_id(ctx, csrf, identifier, attempts=attempts)
            if value:
                found_any_play_id = True
                sources.append(("content-proxy", value))
        except HttpFailure as error:
            last_error = error
            if error.status not in (401, 403, 404, 502):
                raise

        # Cached EPG still helps channels whose per-content proxy object is
        # missing even though the lineup row carries a usable playId.
        try:
            value = _cached_epg_play_id(ctx, identifier, attempts=attempts)
            if value:
                found_any_play_id = True
                sources.append(("cached-epg", value))
        except (HttpFailure, ValueError) as error:
            last_error = error

        for source, play_id in sources:
            if play_id in seen_play_ids:
                _record_attempt(attempts, ctx, "play-id", source, result="duplicate", reason="same playId already tried")
                continue
            seen_play_ids.add(play_id)
            plan, error = _try_play_id(ctx, csrf, identifier, play_id, source, attempts)
            if plan:
                return plan
            last_error = error or last_error
            if isinstance(error, HttpFailure) and error.status in (401, 403):
                break
        else:
            # Last current-session source: force a fresh full EPG request. This
            # is intentionally done only after cheaper sources fail.
            try:
                live_play_id = _live_epg_play_id(ctx, csrf, identifier, attempts=attempts)
            except HttpFailure as error:
                live_play_id = ""
                last_error = error
            if live_play_id:
                found_any_play_id = True
            if live_play_id and live_play_id not in seen_play_ids:
                plan, error = _try_play_id(
                    ctx, csrf, identifier, live_play_id, "live-epg", attempts)
                if plan:
                    return plan
                last_error = error or last_error
            elif live_play_id:
                _record_attempt(
                    attempts, ctx, "play-id", "live-epg", result="duplicate",
                    reason="same playId already tried")

        # 401/403 and ordinary 404/502 failures all get one entirely fresh
        # anonymous session. Other unexpected statuses are still preserved in
        # the final structured diagnostics rather than being silently swallowed.

    # If both anonymous sessions found no playId at all and the content object
    # itself returned 404, this is not a codec/player failure: the lineup row is
    # stale or currently unavailable through Roku's direct APIs. Hide it briefly
    # and force the next directory load to obtain a fresh lineup.
    if not found_any_play_id and any(
            attempt.get("stage") == "play-id"
            and attempt.get("play_id_source") == "content-proxy"
            and attempt.get("status") == 404
            for attempt in attempts):
        _mark_unavailable(ctx, identifier)
        _record_attempt(
            attempts, ctx, "availability", "none", result="temporarily-hidden",
            reason="no playId from content-proxy/cached-epg/live-epg; catalog refresh forced")

    raise _final_failure(ctx, attempts, last_error)
