"""TCLtv+ catalog, guide, and playback through TCL's public web gateway."""

import concurrent.futures
import urllib.parse
import uuid

from ..http import DEFAULT_UA
from ..models import channel, normalize_genres, program


KEY = "tcl"
NAME = "TCLtv+"
BROWSABLE = True
BASE = "https://gateway-prod.ideonow.com"
IMAGE_BASE = "https://tcl-channel-cdn.ideonow.com"
ORIGIN = "https://tcltv.plus"
DEVICE_ID = "1776786148042-4c4uc"
HEADERS = {
    "Accept": "application/json, text/plain, */*", "Origin": ORIGIN,
    "Referer": ORIGIN + "/", "User-Agent": DEFAULT_UA,
}


def _params():
    return {
        "userId": DEVICE_ID, "device_type": "web", "device_model": "web",
        "device_id": DEVICE_ID, "app_version": "1.0",
        "country_code": "US", "state_code": "OH",
    }


def _image(value):
    value = str(value or "")
    return IMAGE_BASE + value if value.startswith("/") else value


def _catalog(ctx):
    def load():
        tab = ctx.http.get_json(
            BASE + "/api/metadata/v2/livetab", params=_params(),
            headers=HEADERS, timeout=25,
        )
        categories = [
            (str(row.get("id")), str(row.get("name") or "Entertainment"))
            for row in tab.get("lines") or [] if row.get("id")
        ]

        def fetch(pair):
            identifier, name = pair
            params = _params()
            params["category_id"] = identifier
            payload = ctx.http.get_json(
                BASE + "/api/metadata/v1/epg/programlist/by/category",
                params=params, headers=HEADERS, timeout=30, max_bytes=12 * 1024 * 1024,
            )
            return name, payload.get("channels") or []

        output = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(fetch, pair) for pair in categories]
            for future in concurrent.futures.as_completed(futures):
                try:
                    name, rows = future.result()
                    output.extend({"category": name, "channel": row} for row in rows if isinstance(row, dict))
                except Exception:
                    continue
        if not output:
            raise ValueError("TCLtv+ returned no channels")
        return output
    return ctx.cache.remember("tcl-catalog-v1", 60 * 60, load)


def channels(ctx):
    output = []
    seen = set()
    for value in _catalog(ctx):
        row = value["channel"]
        identifier = str(row.get("bundle_id") or row.get("id") or "")
        if not identifier or identifier in seen or not row.get("media"):
            continue
        seen.add(identifier)
        category = value["category"]
        events = [
            program(item.get("title") or "Live programming", item.get("start"), item.get("end"))
            for item in row.get("programs") or []
            if isinstance(item, dict)
        ]
        title = row.get("name") or identifier
        raw_genres = row.get("genre") or []
        if not isinstance(raw_genres, (list, tuple, set)):
            raw_genres = [raw_genres]
        output.append(channel(
            KEY, identifier, title, description=row.get("description") or "",
            languages="Spanish" if category.casefold() in ("en español", "noticias") else "English",
            genres=normalize_genres([category] + list(raw_genres), title),
            logo=_image(row.get("logo_color") or row.get("logo_white") or row.get("logo_blackwhite")),
            fanart=_image(row.get("poster_h_large") or row.get("poster_h_medium")),
            schedule=events,
            extra={"source": row.get("source") or "", "media": row.get("media") or ""},
        ))
    return output


def resolve(ctx, identifier):
    value = next((
        item for item in _catalog(ctx)
        if str((item.get("channel") or {}).get("bundle_id") or (item.get("channel") or {}).get("id")) == str(identifier)
    ), None)
    row = (value or {}).get("channel") or {}
    if not row.get("media"):
        raise ValueError("Unknown TCLtv+ channel")
    payload = ctx.http.post_json(
        BASE + "/api/metadata/v1/format-stream-url",
        params={"country_code": "US", "app_version": "3.2.7"},
        payload={
            "type": "channel", "bundle_id": str(identifier),
            "device_id": DEVICE_ID, "source": row.get("source") or None,
            "stream_url": row["media"],
        },
        headers=HEADERS, timeout=25,
    )
    url = payload.get("stream_url") or row["media"]
    parts = urllib.parse.urlsplit(str(url))
    device_id = str(uuid.uuid4())
    replacements = {
        "device[did]": device_id, "device[dnt]": "1", "gender": "",
        "app_bundle": "plugin.video.streamdial", "app_name": "StreamDial TV",
        "app_store_url": "", "url": ORIGIN + "/", "genre": "", "ic": "",
        "us_privacy": "1---", "gdpr": "0", "gdpr_consent": "", "schain": "",
        "ifa_type": "uuid", "device_make": "Kodi", "device_model": "StreamDial",
    }
    placeholder_values = {
        "[TCL_APP_BUNDLE]": "plugin.video.streamdial",
        "[TCL_APP_STORE_URL]": "",
        "[DEVICE_MAKE]": "Kodi",
        "[DEVICE_MODEL]": "StreamDial",
        "[LMT]": "1",
        "[APP_NAME]": "StreamDial TV",
        "[APP_VERSION]": "1.1.0",
        "[IFA]": device_id,
        "[IFA_TYPE]": "uuid",
    }
    query = []
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if value in placeholder_values:
            value = placeholder_values[value]
        elif str(value).replace("_", "").casefold() == "replaceme":
            value = replacements.get(key, "")
        query.append((key, value))
    url = urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urllib.parse.urlencode(query, doseq=True), parts.fragment,
    ))
    return {
        "url": str(url), "headers": HEADERS,
        "mime_type": "application/vnd.apple.mpegurl", "manifest_type": "hls",
    }
