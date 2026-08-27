"""Direct Pluto TV catalog, guide, artwork, and HLS playback."""

import datetime
import urllib.parse
import uuid

from ..http import DEFAULT_UA
from ..models import channel, normalize_genres, program


KEY = "pluto"
NAME = "Pluto TV"
BROWSABLE = True
GUIDE_URL = "https://service-channels.clusters.pluto.tv/v1/guide"
BOOT_URL = "https://boot.pluto.tv/v4/start"
STITCH_URL = "https://cfd-v4-service-channel-stitcher-use1-1.prd.pluto.tv/v2/stitch/hls/channel/{}/master.m3u8"
APP_VERSION = "7.2.0-57e9a96fe7f66354de7957efff20f469e6772404"


def _pick_image(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("url") or value.get("src") or ""
    if isinstance(value, list):
        for entry in value:
            picked = _pick_image(entry)
            if picked:
                return picked
    return ""


def _guide(ctx):
    def load():
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        device_id = str(uuid.uuid4())
        return ctx.http.get_json(GUIDE_URL, params={
            "start": (now - datetime.timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            "stop": (now + datetime.timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
            "deviceId": device_id,
            "deviceMake": "Chrome",
            "deviceModel": "Chrome",
            "deviceType": "web",
            "deviceVersion": "124",
            "clientID": device_id,
            "sid": str(uuid.uuid4()),
            "appName": "web",
            "appVersion": "7.2.0",
            "serverSideAds": "true",
            "DNT": "1",
        }, headers={"Accept": "application/json", "DNT": "1"}, timeout=25,
            max_bytes=22 * 1024 * 1024)
    return ctx.cache.remember("pluto-guide-v2", 300, load)


def channels(ctx):
    data = _guide(ctx)
    category_names = {
        str(row.get("id")): str(row.get("name") or "")
        for row in data.get("categories", []) if isinstance(row, dict)
    }
    output = []
    for row in data.get("channels", []):
        if not isinstance(row, dict) or not row.get("id"):
            continue
        category = category_names.get(str(row.get("categoryID")), "")
        events = []
        event_genres = []
        fanart = ""
        for timeline in row.get("timelines") or []:
            if not isinstance(timeline, dict):
                continue
            episode = timeline.get("episode") or {}
            series = episode.get("series") or {}
            title = timeline.get("title") or episode.get("name") or series.get("name")
            description = episode.get("description") or series.get("description") or ""
            image = _pick_image(episode.get("featuredImage") or episode.get("thumbnail"))
            fanart = fanart or image
            events.append(program(title, timeline.get("start"), timeline.get("stop"), description, image))
            event_genres.extend([episode.get("genre", ""), episode.get("subGenre", "")])
        logo = _pick_image(row.get("images") or row.get("logo"))
        if not logo:
            logo = "https://images.pluto.tv/channels/{}/colorLogoPNG.png".format(row.get("id"))
        output.append(channel(
            KEY, row.get("id"), row.get("name"),
            description=row.get("summary") or row.get("description") or "",
            languages=row.get("language") or "English",
            # The provider's channel category is authoritative. Program-level
            # genres describe individual scheduled episodes and must not
            # permanently reclassify the channel (e.g. Funny AF as Sports).
            genres=normalize_genres([category] if category else event_genres, "{} {}".format(row.get("name"), category)),
            logo=logo, fanart=fanart,
            number=row.get("number") or row.get("stitchedChannelNumber") or "",
            schedule=events,
        ))
    return output


def _session(ctx):
    def load():
        device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "streamdial-tv-pluto-device"))
        data = ctx.http.get_json(BOOT_URL, params={
            "deviceId": device_id,
            "deviceMake": "Chrome",
            "deviceType": "web",
            "deviceVersion": "114.0.0",
            "deviceModel": "web",
            "DNT": "0",
            "appName": "web",
            "appVersion": APP_VERSION,
            "serverSideAds": "false",
            "drmCapabilities": "widevine:L3",
            "clientID": device_id,
            "clientModelNumber": "1.0.0",
        }, headers={"Accept": "application/json", "Referer": "https://pluto.tv/"}, timeout=20)
        if not data.get("stitcherParams") or not data.get("sessionToken"):
            raise ValueError("Pluto did not return a playback session")
        return {
            "params": data["stitcherParams"],
            "token": data["sessionToken"],
        }
    return ctx.cache.remember("pluto-playback-session-v3", 1800, load)


def resolve(ctx, identifier):
    session = _session(ctx)
    query = str(session["params"]).lstrip("?")
    extra = urllib.parse.urlencode({
        "jwt": session["token"],
        "masterJWTPassthrough": "true",
        "includeExtendedEvents": "true",
    })
    return {
        "url": STITCH_URL.format(identifier) + "?" + query + "&" + extra,
        "headers": {"User-Agent": DEFAULT_UA, "DNT": "0", "Referer": "https://pluto.tv/"},
        "mime_type": "application/vnd.apple.mpegurl",
        "manifest_type": "hls",
        "manifest_config": {
            "hls_ignore_endlist": True,
            "hls_fix_mediasequence": True,
            "hls_fix_discsequence": True,
        },
    }
