"""Anonymous Tubi Live TV catalog, guide, and playback."""

import json
import re
import urllib.parse

from ..http import DEFAULT_UA
from ..models import channel, normalize_genres, program


KEY = "tubi"
NAME = "Tubi Live TV"
BROWSABLE = True
LIVE_URL = "https://tubitv.com/live"
EPG_URL = "https://tubitv.com/oz/epg/programming"
ORIGIN = "https://tubitv.com"
SKIP = {
    "favorite_linear_channels", "recommended_linear_channels",
    "featured_channels", "recently_added_channels",
}
HEADERS = {
    "Accept": "*/*", "Origin": ORIGIN, "Referer": ORIGIN + "/",
    "User-Agent": DEFAULT_UA,
}


def _embedded(page):
    marker = page.find("window.__data")
    start = page.find("{", marker)
    if marker < 0 or start < 0:
        raise ValueError("Tubi live catalog was not found")
    depth = 0
    quoted = False
    escaped = False
    end = -1
    for index in range(start, len(page)):
        char = page[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end < 0:
        raise ValueError("Tubi live catalog was incomplete")
    blob = re.sub(r"\bundefined\b", "null", page[start:end])
    blob = re.sub(r'new\s+Date\(("[^"]*")\)', r"\1", blob)
    return json.loads(blob)


def _ids_and_groups(ctx):
    def load():
        page = ctx.http.get_text(LIVE_URL, headers=HEADERS, timeout=25, max_bytes=6 * 1024 * 1024)
        data = _embedded(page)
        containers = ((data.get("epg") or {}).get("contentIdsByContainer") or {})
        ids, groups = [], {}
        for bucket in containers.values():
            for item in bucket or []:
                slug = item.get("container_slug") or ""
                if slug in SKIP:
                    continue
                values = [str(value) for value in item.get("contents") or [] if value]
                for value in values:
                    if value not in ids:
                        ids.append(value)
                if item.get("name"):
                    groups[str(item["name"])] = values
        return {"ids": ids, "groups": groups}
    return ctx.cache.remember("tubi-live-page-v1", 30 * 60, load)


def _epg_rows(ctx, identifiers):
    output = []
    for offset in range(0, len(identifiers), 150):
        batch = identifiers[offset:offset + 150]
        payload = ctx.http.get_json(
            EPG_URL, params={"content_id": ",".join(batch)}, headers=HEADERS,
            timeout=30, max_bytes=16 * 1024 * 1024,
        )
        output.extend(row for row in payload.get("rows") or [] if isinstance(row, dict))
    return output


def _catalog(ctx):
    def load():
        values = _ids_and_groups(ctx)
        return {"rows": _epg_rows(ctx, values["ids"]), "groups": values["groups"]}
    return ctx.cache.remember("tubi-catalog-v1", 15 * 60, load)


def _image(value, *keys):
    images = value.get("images") or {}
    for key in keys:
        row = images.get(key)
        if isinstance(row, list) and row:
            return str(row[0])
        if isinstance(row, str) and row:
            return row
    return ""


def _stream(row):
    resources = row.get("video_resources") or []
    raw = ((resources[0].get("manifest") or {}).get("url") or "") if resources and isinstance(resources[0], dict) else ""
    if not raw:
        return ""
    url = urllib.parse.unquote(raw)
    identifier = str(row.get("content_id") or "")
    return url + ("&" if "?" in url else "?") + urllib.parse.urlencode({"content_id": identifier})


def channels(ctx):
    data = _catalog(ctx)
    groups = data.get("groups") or {}
    output = []
    for row in data.get("rows") or []:
        identifier = str(row.get("content_id") or "")
        title = str(row.get("title") or "").strip()
        stream = _stream(row)
        if not identifier or not title or not stream:
            continue
        tags = [name for name, ids in groups.items() if identifier in ids]
        events = []
        for value in row.get("programs") or []:
            if not isinstance(value, dict):
                continue
            episode = str(value.get("episode_title") or "").strip()
            name = str(value.get("title") or "").strip()
            if episode and episode.casefold() != name.casefold():
                name = "{} — {}".format(name, episode)
            events.append(program(
                name, value.get("start_time"), value.get("end_time"),
                value.get("description") or "", _image(value, "landscape", "hero", "poster"),
            ))
        output.append(channel(
            KEY, identifier, title,
            description=row.get("description") or row.get("summary") or "",
            languages="English", genres=normalize_genres(tags, title),
            logo=_image(row, "thumbnail"), fanart=_image(row, "landscape", "hero"),
            schedule=events, extra={"stream_url": stream},
        ))
    return output


def resolve(ctx, identifier):
    identifier = str(identifier)
    row = next((value for value in (_catalog(ctx).get("rows") or []) if str(value.get("content_id")) == identifier), None)
    if not row:
        rows = _epg_rows(ctx, [identifier])
        row = rows[0] if rows else None
    url = _stream(row or {})
    if not url:
        raise ValueError("Tubi did not return a playable stream")
    return {
        "url": url, "headers": HEADERS,
        "mime_type": "application/vnd.apple.mpegurl", "manifest_type": "hls",
    }
