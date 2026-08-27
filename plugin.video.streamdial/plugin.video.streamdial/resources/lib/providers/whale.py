"""Whale TV+ catalog, guide, and playback through Whale/Zeasn services."""

import datetime
import re
import time
import uuid

from ..http import DEFAULT_UA
from ..models import channel, normalize_genres, program


KEY = "whale"
NAME = "Whale TV+"
BROWSABLE = True
API_TOKEN = "4ef13b5f3d2744e3b0a569feb8dde298"
AUTH_URL = "https://rlaxx.zeasn.tv/livetv/api/v1/auth/access"
CHANNELS_URL = "https://rlaxx.zeasn.tv/livetv/api/device/browser/v1/category/channels"
EPG_URL = "https://rlaxx.zeasn.tv/livetv/api/device/browser/v1/epg"
ORIGIN = "https://watch.whaletvplus.com"
LOGO_BASE = "https://d3b6luslimvglo.cloudfront.net/images/79/rlaxximages/channels-rescaled/icon-white"
HEADERS = {
    "User-Agent": DEFAULT_UA, "Origin": ORIGIN, "Referer": ORIGIN + "/",
    "Accept": "application/json, text/plain, */*",
}
SKIP_CATEGORIES = {"All", "Featured all other countries"}
MACRO_RE = re.compile(r"\[%?[^\]]+%?\]")


def _token(ctx, force=False):
    key = "whale-token-v1"
    if force:
        ctx.cache.delete(key)

    def load():
        payload = ctx.http.get_json(
            AUTH_URL, params={"uuid": "1", "apiToken": API_TOKEN, "langCode": "en"},
            headers=HEADERS, timeout=20,
        )
        value = (payload.get("data") or payload).get("token")
        if not value:
            raise ValueError("Whale TV+ authorization is unavailable")
        return str(value)
    return ctx.cache.remember(key, 23 * 60 * 60, load)


def _raw(ctx):
    def load():
        headers = dict(HEADERS)
        headers["token"] = _token(ctx)
        payload = ctx.http.get_json(
            CHANNELS_URL, params={"langCode": "en", "countryCode": "US"},
            headers=headers, timeout=25, max_bytes=12 * 1024 * 1024,
        )
        output = []
        seen = set()
        for category in payload.get("data") or []:
            name = category.get("ctgName") or ""
            if name in SKIP_CATEGORIES:
                continue
            for row in category.get("channels") or []:
                identifier = str(row.get("chlId") or "")
                if identifier and identifier not in seen and row.get("chlUrl"):
                    value = dict(row)
                    value["_category"] = name
                    output.append(value)
                    seen.add(identifier)
        return output
    return ctx.cache.remember("whale-channels-v1", 30 * 60, load)


def _guides(ctx, ids):
    output = {}
    if not ids:
        return output
    headers = dict(HEADERS)
    headers["token"] = _token(ctx)
    now = datetime.datetime.now(datetime.timezone.utc)
    start_ms = int(now.timestamp() * 1000)
    end_ms = int((now + datetime.timedelta(hours=36)).timestamp() * 1000)
    for offset in range(0, len(ids), 10):
        batch = ids[offset:offset + 10]
        try:
            payload = ctx.http.get_json(
                EPG_URL,
                params={
                    "channelIds": ",".join(batch), "startTime": start_ms,
                    "endTime": end_ms, "langCode": "en", "countryCode": "US",
                },
                headers=headers, timeout=25, max_bytes=8 * 1024 * 1024,
            )
        except Exception:
            continue
        for channel_row in payload.get("data") or []:
            identifier = str(channel_row.get("chlId") or "")
            events = []
            for row in channel_row.get("ptList") or []:
                events.append(program(
                    row.get("prgTitle") or "Live programming",
                    row.get("prgStm"), row.get("prgEtm"), row.get("prgDesc") or "",
                ))
            output[identifier] = events
    return output


def channels(ctx):
    rows = _raw(ctx)
    guides = ctx.cache.remember(
        "whale-guide-v1", 10 * 60,
        lambda: _guides(ctx, [str(row.get("chlId")) for row in rows]),
    )
    output = []
    for row in rows:
        identifier = str(row.get("chlId"))
        current = row.get("currentProgram") or {}
        events = guides.get(identifier) or []
        if not events and current:
            events = [program(
                current.get("seriesTitle") or current.get("prgTitle") or "Live programming",
                current.get("prgStm"), current.get("prgEtm"), current.get("prgDesc") or "",
            )]
        image_id = row.get("imageIdentifier") or ""
        raw_tags = row.get("tags") or []
        if not isinstance(raw_tags, (list, tuple, set)):
            raw_tags = [raw_tags]
        output.append(channel(
            KEY, identifier, row.get("chlName"),
            description=row.get("description") or current.get("prgDesc") or "",
            languages=str(row.get("chlLangCode") or "English").split("-", 1)[0],
            genres=normalize_genres(
                [row.get("_category") or ""] + list(raw_tags),
                "{} {}".format(row.get("chlName"), row.get("description")),
            ),
            logo="{}/{}_white.png".format(LOGO_BASE, image_id) if image_id else "",
            number=row.get("chlNum") or "", schedule=events,
            extra={"stream_url": row.get("chlUrl")},
        ))
    return output


def _expand(url):
    replacements = {
        "[did]": str(uuid.uuid4()), "[session_id]": str(uuid.uuid4()),
        "[cachebuster]": str(int(time.time() * 1000)), "[dnt]": "0",
        "[lmt]": "0", "[consent]": "", "[content_id]": "",
        "[content_language]": "en", "[content_duration]": "",
        "[content_season]": "", "[content_episode]": "",
    }
    for key, value in replacements.items():
        url = url.replace(key, value)
    return MACRO_RE.sub("", url)


def resolve(ctx, identifier):
    row = next((value for value in _raw(ctx) if str(value.get("chlId")) == str(identifier)), None)
    if not row:
        raise ValueError("Unknown Whale TV+ channel")
    return {
        "url": _expand(str(row["chlUrl"])), "headers": HEADERS,
        "mime_type": "application/vnd.apple.mpegurl", "manifest_type": "hls",
    }
