"""Direct anonymous Plex Live TV catalog, schedule, and playback."""

import datetime
import json
import re
import uuid
from xml.etree import ElementTree

from ..http import DEFAULT_UA, HttpFailure
from ..models import channel, normalize_genres, program


KEY = "plex"
NAME = "Plex Live TV"
BROWSABLE = True
WATCH_URL = "https://watch.plex.tv/"
ANON_URL = "https://plex.tv/api/v2/users/anonymous"
EPG_HOST = "https://epg.provider.plex.tv"
CHANNELS_URL = EPG_HOST + "/lineups/plex/channels"
GRID_URL = EPG_HOST + "/grid"
PRODUCT = "Plex Mediaverse"
COMPOUND_RE = re.compile(r"^[0-9a-f]{24}-([0-9a-f]{24})$", re.I)

CATEGORY_MAP = {
    "entertainment": "Entertainment", "drama": "Drama", "movies": "Movies",
    "crime": "Crime", "news": "News", "sports": "Sports", "reality": "Reality",
    "classic-tv": "Entertainment", "action": "Drama", "thriller": "Drama",
    "comedy": "Comedy", "daytime-tv": "Entertainment", "game-show": "Reality",
    "nature-travel": "Science & Nature", "history-science": "History",
    "food-home": "Food", "lifestyle": "Lifestyle", "kids-family": "Kids & Family",
    "en-espanol": "International", "international": "International",
    "gaming-anime": "Entertainment", "music": "Music",
}


def _stable_id(value):
    match = COMPOUND_RE.match(str(value or ""))
    return match.group(1) if match else str(value or "")


def _client(ctx):
    return ctx.cache.remember("plex-client-v3", None, lambda: {
        "client_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()),
        "playback_session_id": str(uuid.uuid4()), "playback_id": str(uuid.uuid4()),
    })


def _base_headers(ctx):
    client = _client(ctx)
    return {
        "User-Agent": DEFAULT_UA, "Origin": WATCH_URL.rstrip("/"), "Referer": WATCH_URL,
        "X-Plex-Client-Identifier": client["client_id"], "X-Plex-Device": "Linux",
        "X-Plex-Language": "en", "X-Plex-Platform": "Chrome",
        "X-Plex-Platform-Version": "145.0.0.0",
        "X-Plex-Playback-Session-Id": client["playback_session_id"],
        "X-Plex-Product": PRODUCT, "X-Plex-Provider-Version": "6.5.0",
        "X-Plex-Session-Id": client["session_id"],
    }


def _auth(ctx, force=False):
    key = "plex-anonymous-v3"
    if force:
        ctx.cache.delete(key)

    def load():
        headers = _base_headers(ctx)
        ctx.http.request(WATCH_URL, headers=headers, timeout=15, max_bytes=1024, allow_truncated=True)
        request_headers = dict(headers)
        request_headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        raw, _, _ = ctx.http.request(
            ANON_URL, method="POST", data=b"", headers=request_headers,
            timeout=15, max_bytes=1024 * 1024,
        )
        data = json.loads(raw.decode("utf-8", "replace"))
        if not data.get("authToken"):
            raise ValueError("Plex anonymous token unavailable")
        return {"token": data["authToken"]}
    return ctx.cache.remember(key, 1800, load)["token"]


def _headers(ctx, accept="application/json", force=False):
    values = _base_headers(ctx)
    values.update({"Accept": accept, "X-Plex-Token": _auth(ctx, force=force)})
    return values


def _genre_maps(ctx):
    def load():
        headers = _headers(ctx)
        data = ctx.http.get_json(
            EPG_HOST + "/", params={"X-Plex-Token": headers["X-Plex-Token"]},
            headers=headers, timeout=20,
        )
        slugs = []
        for feature in ((data.get("MediaProvider") or {}).get("Feature") or []):
            if isinstance(feature, dict) and feature.get("GridChannelFilter"):
                slugs.extend(
                    row.get("identifier") for row in feature["GridChannelFilter"]
                    if isinstance(row, dict) and row.get("identifier") in CATEGORY_MAP
                )
        tags, spanish = {}, set()
        for slug in dict.fromkeys(slugs):
            payload = ctx.http.get_json(
                CHANNELS_URL,
                params={"genre": slug, "X-Plex-Token": headers["X-Plex-Token"]},
                headers=headers, timeout=15, max_bytes=6 * 1024 * 1024,
            )
            for row in ((payload.get("MediaContainer") or {}).get("Channel") or []):
                identifier = _stable_id(row.get("gridKey") or row.get("id"))
                if not identifier:
                    continue
                tags.setdefault(identifier, [])
                label = CATEGORY_MAP[slug]
                if label not in tags[identifier]:
                    tags[identifier].append(label)
                if slug == "en-espanol":
                    spanish.add(identifier)
        return {"tags": tags, "spanish": sorted(spanish)}
    try:
        return ctx.cache.remember("plex-genres-v3", 1800, load)
    except Exception:
        return {"tags": {}, "spanish": []}


def _catalog(ctx):
    def load(force=False):
        headers = _headers(ctx, "application/xml", force=force)
        raw = ctx.http.get_bytes(
            CHANNELS_URL, params={"X-Plex-Token": headers["X-Plex-Token"]},
            headers=headers, timeout=30, max_bytes=12 * 1024 * 1024,
        )
        root = ElementTree.fromstring(raw)
        rows = []
        for node in root.findall(".//Channel"):
            record = dict(node.attrib)
            record["full_id"] = record.get("id") or ""
            record["stable_id"] = record.get("gridKey") or _stable_id(record["full_id"])
            if record["stable_id"]:
                rows.append(record)
        return rows

    def cached_load():
        try:
            return load(False)
        except HttpFailure as error:
            if error.status not in (401, 403):
                raise
            return load(True)
    return ctx.cache.remember("plex-catalog-v3", 1800, cached_load)


def _image(item, *types):
    for expected in types:
        for value in item.get("Image") or []:
            if isinstance(value, dict) and value.get("type") == expected and value.get("url"):
                return value["url"]
    return ""


def _schedule_from_grid(items):
    """Normalize legacy JSON rows retained for compatibility and fixtures."""
    output = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        show, episode = item.get("grandparentTitle") or item.get("parentTitle") or "", item.get("title") or ""
        title = "{} — {}".format(show, episode) if show and episode and show.casefold() != episode.casefold() else (episode or show)
        image = _image(item, "background", "coverArt") or item.get("art") or item.get("thumb") or ""
        for media in item.get("Media") or []:
            if isinstance(media, dict):
                output.append(program(title, media.get("beginsAt"), media.get("endsAt"), item.get("summary") or "", image))
    return output


def _grid_cache_key(grid_key, today=None):
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    return "plex-grid-v3-{}-{}".format(grid_key, today.isoformat())


def _grid(ctx, grid_key):
    today = datetime.datetime.now(datetime.timezone.utc).date()
    cache_key = _grid_cache_key(grid_key, today)

    def load():
        output, headers = [], _headers(ctx, "application/xml")
        for day in (today, today + datetime.timedelta(days=1)):
            text = ctx.http.get_text(
                GRID_URL, params={"channelGridKey": grid_key, "date": day.isoformat()},
                headers=headers, timeout=12, max_bytes=6 * 1024 * 1024,
            )
            root = ElementTree.fromstring(text)
            for video in root.findall(".//Video"):
                show, episode = video.get("grandparentTitle") or video.get("parentTitle") or "", video.get("title") or ""
                title = "{} — {}".format(show, episode) if show and episode and show.casefold() != episode.casefold() else (episode or show)
                media = video.find("Media")
                if media is not None:
                    output.append(program(
                        title, media.get("beginsAt"), media.get("endsAt"),
                        video.get("summary") or "", video.get("art") or video.get("thumb") or "",
                    ))
        return output
    return ctx.cache.remember(cache_key, 300, load)


def channels(ctx):
    maps = _genre_maps(ctx)
    tags, spanish = maps.get("tags") or {}, set(maps.get("spanish") or [])
    output = []
    for row in _catalog(ctx):
        identifier = row["stable_id"]
        title = row.get("title") or row.get("name") or row.get("callSign") or identifier
        genres = tags.get(identifier) or [row.get("category") or row.get("genre") or row.get("type") or ""]
        output.append(channel(
            KEY, identifier, title, description=row.get("summary") or row.get("description") or "",
            languages="Spanish" if identifier in spanish else (row.get("language") or row.get("audioLanguage") or "English"),
            genres=normalize_genres(genres, "{} {}".format(title, row.get("slug", ""))),
            logo=row.get("thumb") or row.get("logo") or "", fanart=row.get("art") or "",
            number=row.get("channelNumber") or row.get("callSign") or "",
            extra={"grid_key": row.get("gridKey") or identifier, "full_id": row.get("full_id") or identifier},
        ))
    return output


def enrich(ctx, item):
    grid_key = (item.get("extra") or {}).get("grid_key")
    if not grid_key or item.get("schedule"):
        return item
    item = dict(item)
    item["schedule"] = _grid(ctx, grid_key)
    return item


def search_enrich(ctx, rows):
    """Merge cached Plex grids without issuing a search-time per-channel fan-out."""
    marker = object()
    output = []
    for source in rows or []:
        item = dict(source)
        grid_key = (item.get("extra") or {}).get("grid_key")
        if grid_key and not item.get("schedule"):
            cached = ctx.cache.get(_grid_cache_key(grid_key), 300, marker)
            if cached is not marker and isinstance(cached, list):
                item["schedule"] = cached
        output.append(item)
    return output


def resolve(ctx, identifier):
    row = next((value for value in _catalog(ctx) if str(value.get("stable_id")) == str(identifier)), None)
    if not row:
        raise ValueError("Unknown Plex channel")
    full_id, headers, client = row.get("full_id") or identifier, _headers(ctx), _client(ctx)
    tune_headers = dict(headers)
    tune_headers.update({"Content-Type": "application/json", "X-Plex-Playback-Id": client["playback_id"]})
    try:
        ctx.http.request(
            EPG_HOST + "/channels/{}/tune".format(full_id), method="POST", data=b"",
            headers=tune_headers, timeout=4, max_bytes=1024, allow_truncated=True,
        )
    except Exception:
        pass
    manifest = EPG_HOST + "/library/parts/{}.m3u8".format(full_id)
    params = {"includeAllStreams": "1", "X-Plex-Product": PRODUCT, "X-Plex-Token": headers["X-Plex-Token"]}
    try:
        final = ctx.http.final_url(manifest, params=params, headers=headers, timeout=15)
    except HttpFailure as error:
        if error.status not in (401, 403):
            raise
        headers = _headers(ctx, force=True)
        params["X-Plex-Token"] = headers["X-Plex-Token"]
        final = ctx.http.final_url(manifest, params=params, headers=headers, timeout=15)
    return {
        "url": final,
        "headers": {"User-Agent": DEFAULT_UA, "Origin": WATCH_URL.rstrip("/"), "Referer": WATCH_URL},
        "mime_type": "application/vnd.apple.mpegurl", "manifest_type": "hls",
    }
