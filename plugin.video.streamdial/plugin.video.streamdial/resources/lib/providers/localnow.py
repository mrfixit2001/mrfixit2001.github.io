"""Local Now direct channel/EPG discovery and tune-time HLS playback.

The provider bootstraps its current API host and anonymous DSP token from
Local Now's own Next.js page. No third-party catalog or resolver participates at
runtime.
"""

import base64
import json
import re
import urllib.parse

from ..http import DEFAULT_UA
from ..models import channel, normalize_genres, program


KEY = "localnow"
NAME = "Local Now"
BROWSABLE = True
HOME_URL = "https://localnow.com/"
CHANNELS_URL = "https://localnow.com/channels"
DEFAULT_DSP_HOST = "data-store-trans-cdn.api.cms.amdvids.com"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://localnow.com",
    "Referer": "https://localnow.com/",
    "User-Agent": DEFAULT_UA,
}
_CALL_SIGN_RE = re.compile(r"\b[WK][A-Z]{2,4}(?:-TV)?\b", re.IGNORECASE)


def _decode_jwt_exp(token):
    try:
        payload = str(token).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")).get("exp") or 0)
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return 0


def _extract_next_data(text):
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">\s*(.*?)\s*</script>',
        str(text or ""), re.DOTALL)
    if not match:
        raise ValueError("Local Now homepage did not expose runtime configuration")
    try:
        return json.loads(match.group(1))
    except (TypeError, ValueError) as error:
        raise ValueError("Local Now runtime configuration is invalid") from error


def _extract_dma(value):
    if not value:
        return ""
    if isinstance(value, dict):
        for key in ("dmaId", "dma_id", "dma", "zipDma"):
            if value.get(key):
                return str(value.get(key))
        return ""
    match = re.search(r'"?(?:dmaId|dma_id|dma|zipDma)"?\s*[:=]\s*"?(\d+)', str(value))
    return match.group(1) if match else ""


def _extract_market(value):
    if not value:
        return ""
    if isinstance(value, dict):
        for key in ("market", "slug", "citySlug", "marketSlug"):
            if value.get(key):
                return str(value.get(key))
        return ""
    text = str(value)
    match = re.search(r'"?(?:market|slug|citySlug|marketSlug)"?\s*[:=]\s*"?([A-Za-z0-9_-]+)', text)
    if match:
        return match.group(1)
    match = re.search(r"\b([a-z]{2}[A-Z][A-Za-z0-9]+)\b", text)
    return match.group(1) if match else ""


def _pbs_markets(next_data):
    page_props = ((next_data.get("props") or {}).get("pageProps") or {})
    config = page_props.get("config") or {}
    local = config.get("localNow") or {}
    raw = local.get("pbsMarkets") or page_props.get("pbsMarkets") or ""
    if isinstance(raw, str):
        return [value.strip() for value in raw.split(",") if value.strip()]
    if isinstance(raw, list):
        values = []
        for value in raw:
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
            elif isinstance(value, dict):
                candidate = value.get("market") or value.get("slug") or value.get("marketSlug")
                if candidate:
                    values.append(str(candidate))
        return values
    return []


def _bootstrap(ctx, force=False):
    key = "localnow-bootstrap-v1"
    if force:
        ctx.cache.delete(key)

    def load():
        html = ctx.http.get_text(HOME_URL, headers=HEADERS, timeout=25, max_bytes=8 * 1024 * 1024)
        data = _extract_next_data(html)
        runtime = data.get("runtimeConfig") or {}
        token_raw = runtime.get("DSP_TOKEN")
        if not runtime or not token_raw:
            raise ValueError("Local Now did not publish an anonymous runtime token")
        try:
            token_obj = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
        except (TypeError, ValueError) as error:
            raise ValueError("Local Now published an invalid runtime token") from error
        token = str((token_obj or {}).get("token") or "")
        if not token:
            raise ValueError("Local Now runtime token is missing")

        page_props = ((data.get("props") or {}).get("pageProps") or {})
        cookies = page_props.get("serverCookies") or {}
        candidates = [cookies.get("_ln_myMarket"), cookies.get("_ln_myDetectedCity"), cookies.get("_ln_myCity")]
        dma = next((value for value in (_extract_dma(row) for row in candidates) if value), "")
        market = next((value for value in (_extract_market(row) for row in candidates) if value), "")
        pbs = _pbs_markets(data)
        if market and pbs:
            market = ",".join(dict.fromkeys([market] + pbs))
        # Provider reference behavior uses NYC only when Local Now itself does
        # not expose a market in the current bootstrap response.
        dma = dma or "501"
        market = market or "nyNewYorkCity,pbs-wnet,pbs-wedh,pbs-wliw,pbs-wnjt"
        host = str(runtime.get("DSP_API_URL") or DEFAULT_DSP_HOST).strip().rstrip("/")
        host = host.replace("https://", "").replace("http://", "")
        return {
            "host": host,
            "token": token,
            "token_exp": _decode_jwt_exp(token),
            "dma": dma,
            "market": market,
        }

    return ctx.cache.remember(key, 30 * 60, load)


def _api_headers(bootstrap):
    values = dict(HEADERS)
    values["x-access-token"] = bootstrap["token"]
    return values


def _raw_channels(ctx):
    def load():
        boot = _bootstrap(ctx)
        payload = ctx.http.get_json(
            "https://{}/live/epg/US/website".format(boot["host"]),
            params={"dma": boot["dma"], "market": boot["market"]},
            headers=_api_headers(boot), timeout=35, max_bytes=24 * 1024 * 1024,
        )
        rows = (payload or {}).get("channels") or []
        if not isinstance(rows, list) or not rows:
            raise ValueError("Local Now returned no channels for the current market")
        return rows
    return ctx.cache.remember("localnow-catalog-v1", 10 * 60, load)


def _schedule(row):
    output = []
    for item in row.get("program") or []:
        if not isinstance(item, dict):
            continue
        title = item.get("program_title") or item.get("title") or row.get("name") or "Live programming"
        output.append(program(
            title,
            item.get("starts_at"),
            item.get("ends_at"),
            item.get("program_description") or row.get("description") or "",
            item.get("image") or row.get("poster") or "",
        ))
    output.sort(key=lambda value: value.get("start") or "")
    return output


def _genres(row):
    values = []
    for key in ("genres", "iab_genres"):
        raw = row.get(key) or []
        if not isinstance(raw, (list, tuple, set)):
            raw = [raw]
        values.extend(value for value in raw if value)
    name = str(row.get("name") or "")
    slug = str(row.get("slug") or "").casefold()
    if "My City" in values or "hyperlocal" in slug or slug.startswith("epg-local-now") or _CALL_SIGN_RE.search(name):
        values.insert(0, "News")
    return normalize_genres(values, "{} {}".format(name, row.get("description") or ""))


def channels(ctx):
    output = []
    seen = set()
    for row in _raw_channels(ctx):
        if not isinstance(row, dict):
            continue
        identifier = str(row.get("video_id") or row.get("_id") or "").strip()
        name = str(row.get("name") or "").strip()
        access = row.get("subscription_access") or {}
        if not identifier or not name or identifier in seen:
            continue
        if isinstance(access, dict) and access.get("unlocked") is False:
            continue
        seen.add(identifier)
        output.append(channel(
            KEY,
            identifier,
            name,
            description=row.get("description") or "",
            languages=row.get("language") or ("Spanish" if "español" in name.casefold() else "English"),
            genres=_genres(row),
            logo=row.get("logo") or "",
            fanart=row.get("poster") or "",
            number=row.get("channel_number") or "",
            schedule=_schedule(row),
            extra={"slug": row.get("slug") or "", "dma": _bootstrap(ctx).get("dma") or ""},
        ))
    return output


def search_enrich(ctx, rows):
    # Local Now's catalog endpoint already returns its currently available
    # per-channel program list, so searching the catalog also searches programs.
    return rows


def _cached_page_url(ctx, identifier):
    # The provider's playback API accepts the page that initiated playback. Use
    # the current provider-published channel slug when the 10-minute Local Now
    # catalog is already cached, without forcing a browse/catalog request during
    # tune-time resolution.
    rows = ctx.cache.get("localnow-catalog-v1", 10 * 60, []) or []
    wanted = str(identifier)
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate = str(row.get("video_id") or row.get("_id") or "")
        if candidate != wanted:
            continue
        slug_value = str(row.get("slug") or "").strip().strip("/")
        if slug_value:
            return CHANNELS_URL + "/" + urllib.parse.quote(slug_value, safe="")
        break
    return CHANNELS_URL


def resolve(ctx, identifier):
    # Refresh the provider bootstrap at tune time if needed, but never resolve
    # through the browse snapshot or a third-party service.
    boot = _bootstrap(ctx)
    page_url = _cached_page_url(ctx, identifier)
    url = "https://{}/video/play/{}/1920/1080".format(boot["host"], urllib.parse.quote(str(identifier), safe=""))
    payload = ctx.http.get_json(
        url,
        params={
            "page_url": urllib.parse.quote(page_url, safe=""),
            "device_devicetype": "desktop_web",
            "app_version": "0.0.0",
            "app_bundle": "web.localnow",
            "ccpa_us_privacy": "1YNY",
        },
        headers=_api_headers(boot), timeout=25, max_bytes=2 * 1024 * 1024,
    )
    stream = str((payload or {}).get("video_m3u8") or (payload or {}).get("session_m3u8") or "")
    if not stream.startswith(("http://", "https://")):
        # One forced provider bootstrap handles the common expired-token case.
        boot = _bootstrap(ctx, force=True)
        payload = ctx.http.get_json(
            "https://{}/video/play/{}/1920/1080".format(boot["host"], urllib.parse.quote(str(identifier), safe="")),
            params={
                "page_url": urllib.parse.quote(page_url, safe=""),
                "device_devicetype": "desktop_web", "app_version": "0.0.0",
                "app_bundle": "web.localnow", "ccpa_us_privacy": "1YNY",
            },
            headers=_api_headers(boot), timeout=25, max_bytes=2 * 1024 * 1024,
        )
        stream = str((payload or {}).get("video_m3u8") or (payload or {}).get("session_m3u8") or "")
    if not stream.startswith(("http://", "https://")):
        raise ValueError("Local Now did not return a current HLS stream")
    return {
        "url": stream,
        "headers": {"User-Agent": DEFAULT_UA, "Origin": "https://localnow.com", "Referer": "https://localnow.com/"},
        "mime_type": "application/vnd.apple.mpegurl",
        "manifest_type": "hls",
        "diagnostics": {"provider": KEY, "selected_source": "localnow-dsp-hls", "dma": boot.get("dma")},
    }
