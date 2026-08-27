"""Direct STIRR channel catalog and playback."""

import secrets
import urllib.parse

from ..http import DEFAULT_UA
from ..models import channel, normalize_genres


KEY = "stirr"
NAME = "STIRR"
BROWSABLE = True
BASE = "https://stirr.com"
CATALOG_URL = BASE + "/api/videos/list/"


def _catalog(ctx):
    def load():
        data = ctx.http.get_json(CATALOG_URL, params={
            "categories": "all_categories",
            "content_type": "4",
            "no_limit": "true",
        }, headers={"Accept": "application/json", "Referer": BASE + "/"}, timeout=25,
            max_bytes=16 * 1024 * 1024)
        groups = ((data.get("videos") or {}).get("category_videos") or [])
        output = []
        stack = list(groups if isinstance(groups, list) else [groups])
        while stack:
            value = stack.pop(0)
            if isinstance(value, list):
                stack[0:0] = value
            elif isinstance(value, dict):
                if value.get("videoid") and value.get("title"):
                    output.append(value)
                else:
                    for child in value.values():
                        if isinstance(child, (list, dict)):
                            stack.append(child)
        unique = {}
        for value in output:
            unique[str(value.get("videoid"))] = value
        return list(unique.values())
    return ctx.cache.remember("stirr-catalog-v2", 1800, load)


def channels(ctx):
    output = []
    for row in _catalog(ctx):
        if str(row.get("drm_protected", "no")).lower() not in ("no", "false", "0", ""):
            continue
        if row.get("content_type") not in (4, "4", None):
            continue
        if str(row.get("active", "yes")).lower() not in ("yes", "true", "1"):
            continue
        categories = [
            value.get("category_name") or value.get("name") or ""
            for value in row.get("categories") or [] if isinstance(value, dict)
        ]
        thumbs = row.get("thumbs") or {}
        if not isinstance(thumbs, dict):
            thumbs = {}
        output.append(channel(
            KEY, row.get("videoid"), row.get("title"),
            description=row.get("description") or "",
            languages=row.get("language") or "English",
            genres=normalize_genres(categories, "{} {} {}".format(row.get("title", ""), row.get("tags", ""), " ".join(categories))),
            logo=thumbs.get("768x432") or thumbs.get("original") or "",
            fanart=thumbs.get("1280x720") or thumbs.get("original") or "",
            number=row.get("channel_number") or "",
        ))
    return output


def resolve(ctx, identifier):
    data = ctx.http.post_json(
        BASE + "/api/v2/videos/{}/playable".format(identifier), payload={},
        headers={"Origin": BASE, "Referer": BASE + "/"}, timeout=20)
    media = []
    for row in data.get("data", []):
        values = row.get("media", []) if isinstance(row, dict) else []
        media.extend(values if isinstance(values, list) else [values])
    url = next((value for value in media if isinstance(value, str) and ".m3u8" in value), "")
    if not url:
        raise ValueError("STIRR did not return a public HLS stream")
    nonce = secrets.token_hex(16)
    url = url.replace("[vx_nonce]", nonce).replace("%5Bvx_nonce%5D", nonce).replace("%5bvx_nonce%5d", nonce)
    parts = urllib.parse.urlsplit(url)
    url = urllib.parse.urlunsplit((
        parts.scheme, parts.netloc,
        urllib.parse.quote(parts.path, safe="/%:@"),
        urllib.parse.quote(parts.query, safe="=&%+,:;@/?[]"),
        parts.fragment,
    ))
    headers = {"User-Agent": DEFAULT_UA, "Referer": BASE + "/", "Origin": BASE}
    cookie = ctx.http.cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    return {
        "url": url,
        "headers": headers,
        "mime_type": "application/vnd.apple.mpegurl",
        "manifest_type": "hls",
    }
