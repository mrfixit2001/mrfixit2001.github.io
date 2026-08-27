"""PBS station catalog and playback through PBS-operated public services."""

import html
from html.parser import HTMLParser
import json
import re

from ..http import DEFAULT_UA
from ..models import channel, clean_text


KEY = "pbs"
NAME = "PBS"
BROWSABLE = True
STATIONS_URL = "https://station.services.pbs.org/api/public/v1/stations/"
LIVE_ROSTER_URL = "https://help.pbs.org/support/solutions/articles/12000069454-pbs-live-streaming-faq"
LIVE_PAGE = "https://www.pbs.org/livestream/"
PBS_LOGO = "https://www.pbs.org/images/pbs_logotype_blue.png"
# PBS operates a clear national PBS KIDS HLS feed in parallel with the
# station-localized PBS livestream page, whose KIDS entry can advertise only
# DASH/Widevine. Prefer the clear feed so PBS KIDS does not require a CDM.
PBS_KIDS_HLS = "https://livestream.pbskids.org/out/v1/14507d931bbe48a69287e4850e53443c/est.m3u8"
PROFILE_NAMES = {
    "kids-main": ("PBS KIDS 24/7", "Kids & Family"),
    "ga-create": ("PBS Create", "Lifestyle"),
    "ga-world": ("PBS WORLD", "News"),
    "ga-nhk": ("NHK WORLD-JAPAN", "International"),
}


class _ListItemParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts = []
        self.items = []

    def handle_starttag(self, tag, attrs):
        del attrs
        if tag.lower() == "li":
            if self.depth == 0:
                self.parts = []
            self.depth += 1

    def handle_endtag(self, tag):
        if tag.lower() == "li" and self.depth:
            self.depth -= 1
            if self.depth == 0:
                value = clean_text(" ".join(self.parts))
                if value:
                    self.items.append(value)
                self.parts = []

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def _parse_live_station_names(page):
    text = html.unescape(str(page or ""))
    visible = clean_text(re.sub(r"<[^>]+>", " ", text))
    marker = "stations currently offering a live stream"
    if marker not in visible.casefold() and "which local pbs stations offer live streaming" not in visible.casefold():
        return []
    parser = _ListItemParser()
    try:
        parser.feed(text)
    except Exception:
        return []
    output = []
    for value in parser.items:
        # The page also has TOC/troubleshooting list items. Keep plausible
        # station labels; the station-record matcher below is the final filter.
        if 2 <= len(value) <= 100 and value not in output:
            output.append(value)
    return output


def _live_station_names(ctx):
    def load():
        try:
            page = ctx.http.get_text(
                LIVE_ROSTER_URL,
                headers={"Accept": "text/html,*/*", "User-Agent": DEFAULT_UA},
                timeout=20, max_bytes=4 * 1024 * 1024,
            )
        except Exception:
            return []
        names = _parse_live_station_names(page)
        return names if len(names) >= 50 else []
    return ctx.cache.remember("pbs-live-roster-v1", 12 * 60 * 60, load)


def _name_tokens(value):
    words = re.findall(r"[a-z0-9]+", clean_text(value).casefold())
    generic = {
        "the", "of", "and", "pbs", "tv", "television", "public", "broadcasting",
        "broadcast", "media", "network", "channel", "station", "educational",
    }
    return {word for word in words if word not in generic and len(word) > 1}


def _compact(value):
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).casefold())


def _station_matches_roster(row, roster):
    callsign = _compact(row.get("callsign"))
    aliases = [row.get("callsign"), row.get("name"), row.get("short_name")] + list(row.get("aliases") or [])
    alias_compact = {_compact(value) for value in aliases if value}
    alias_tokens = [_name_tokens(value) for value in aliases if value]
    for label in roster:
        compact = _compact(label)
        if not compact:
            continue
        if compact in alias_compact:
            return True
        # FAQ entries often add parenthetical notes after a direct callsign.
        if callsign and len(callsign) >= 3 and re.search(r"(?:^|[^a-z0-9]){}(?:$|[^a-z0-9])".format(re.escape(callsign)), clean_text(label).casefold()):
            return True
        # Common branding can omit the leading W/K from a callsign (GBH/WGBH).
        if callsign and len(compact) >= 3 and (callsign.endswith(compact) or compact.endswith(callsign)):
            return True
        tokens = _name_tokens(label)
        if not tokens:
            continue
        for candidate in alias_tokens:
            if not candidate:
                continue
            shared = tokens & candidate
            if shared and (tokens <= candidate or candidate <= tokens):
                return True
            if len(shared) >= 2 and len(shared) / max(1, min(len(tokens), len(candidate))) >= 0.75:
                return True
    return False


def _station_records(ctx):
    def load():
        rows = []
        page = 1
        while page <= 10:
            payload = ctx.http.get_json(
                STATIONS_URL, params={"page": page},
                headers={"Accept": "application/vnd.api+json"},
                timeout=20, max_bytes=4 * 1024 * 1024,
            )
            data = payload.get("data") or []
            for item in data:
                if not isinstance(item, dict):
                    continue
                attributes = item.get("attributes") or {}
                callsign = str(attributes.get("call_sign") or "").upper()
                if not callsign:
                    continue
                images = {
                    image.get("profile"): image.get("url")
                    for image in attributes.get("images") or []
                    if isinstance(image, dict) and image.get("profile") and image.get("url")
                }
                full_name = attributes.get("full_common_name") or ""
                short_name = attributes.get("short_common_name") or ""
                rows.append({
                    "id": str(item.get("id") or ""), "callsign": callsign,
                    "name": full_name or short_name or callsign,
                    "short_name": short_name,
                    "aliases": [full_name, short_name],
                    "city": attributes.get("city") or "", "state": attributes.get("state") or "",
                    "logo": images.get("color-logo") or images.get("black-logo") or images.get("white-logo") or PBS_LOGO,
                })
            links = payload.get("links") or {}
            if not links.get("next") or not data:
                break
            page += 1

        # PBS's own live-streaming FAQ is the online allow-list. If PBS changes
        # the help page enough that it cannot be parsed/matched reliably, retain
        # the official station API rows rather than turning the provider empty.
        roster = _live_station_names(ctx)
        if roster:
            matched = [row for row in rows if _station_matches_roster(row, roster)]
            if len(matched) >= 50:
                return matched
        return rows
    return ctx.cache.remember("pbs-stations-v5", 12 * 60 * 60, load)


def _station(ctx, callsign):
    callsign = str(callsign or "").upper()
    row = next((value for value in _station_records(ctx) if value["callsign"] == callsign), None)
    if row:
        return row
    payload = ctx.http.get_json(
        STATIONS_URL, params={"call_sign": callsign},
        headers={"Accept": "application/vnd.api+json"}, timeout=15,
    )
    item = (payload.get("data") or [None])[0]
    if not isinstance(item, dict):
        raise ValueError("PBS station is unavailable")
    attributes = item.get("attributes") or {}
    return {
        "id": str(item.get("id") or ""), "callsign": callsign,
        "name": attributes.get("full_common_name") or callsign,
        "city": attributes.get("city") or "", "state": attributes.get("state") or "",
        "logo": PBS_LOGO,
    }


def _localized_page(ctx, station):
    key = "pbs-live-v4-{}".format(station["callsign"].casefold())

    def load():
        cookie = "pbsol.station={}; pbsol.station_id={}".format(station["callsign"], station["id"])
        return ctx.http.get_text(
            LIVE_PAGE,
            headers={"Accept": "text/html", "Referer": "https://www.pbs.org/", "Cookie": cookie},
            timeout=25, max_bytes=12 * 1024 * 1024,
        )
    return ctx.cache.remember(key, 15 * 60, load)


def _feeds(page):
    """Decode the livestream_feeds array embedded in PBS's Next.js payload."""
    text = str(page or "")
    for _ in range(2):
        text = html.unescape(text)
    match = re.search(r'\\"livestream_feeds\\":(\[.*?\])\},\\"links\\":', text, re.DOTALL)
    if not match:
        match = re.search(r'"livestream_feeds"\s*:\s*(\[.*?\])\s*,\s*"links"', text, re.DOTALL)
        if not match:
            return []
        encoded = match.group(1)
    else:
        try:
            encoded = json.loads('"' + match.group(1) + '"')
        except (TypeError, ValueError):
            return []
    try:
        values = json.loads(encoded)
    except (TypeError, ValueError):
        return []
    return [value for value in values if isinstance(value, dict) and value.get("profile")]


def _feed(ctx, callsign, profile):
    station = _station(ctx, callsign)
    feeds = _feeds(_localized_page(ctx, station))
    row = next((value for value in feeds if value.get("profile") == profile), None)
    if not row:
        available = ",".join(sorted(str(value.get("profile")) for value in feeds if value.get("profile"))) or "none"
        raise ValueError(
            "PBS feed unavailable callsign={} requested_profile={} available_profiles={}".format(
                station["callsign"], profile, available))
    return station, row


def channels(ctx):
    rows = []
    stations = _station_records(ctx)
    for station in stations:
        location = ", ".join(value for value in (station["city"], station["state"]) if value)
        description = "Live {} programming{}; regional availability is controlled by PBS.".format(
            station["name"], " from " + location if location else "")
        rows.append(channel(
            KEY, "station:" + station["callsign"], station["name"],
            description=description, languages="English", genres=["Entertainment"],
            logo=station["logo"], number=station["callsign"],
            extra={"callsign": station["callsign"], "profile": "ga-main"},
        ))

    anchor = next((row for row in stations if row["callsign"] == "KETC"), stations[0] if stations else None)
    if anchor:
        for profile, (name, genre) in PROFILE_NAMES.items():
            rows.append(channel(
                KEY, "profile:" + profile, name,
                description="Official PBS live {} feed. Availability is controlled by PBS.".format(name),
                languages="English", genres=[genre], logo=PBS_LOGO,
                extra={"callsign": anchor["callsign"], "profile": profile},
            ))
    return rows


def resolve(ctx, identifier):
    identifier = str(identifier or "")
    if identifier.startswith("station:"):
        callsign, profile = identifier.split(":", 1)[1], "ga-main"
    elif identifier.startswith("profile:"):
        callsign, profile = "KETC", identifier.split(":", 1)[1]
    else:
        raise ValueError("Unknown PBS channel")

    if profile == "kids-main":
        return {
            "url": PBS_KIDS_HLS,
            "headers": {"User-Agent": DEFAULT_UA, "Referer": "https://pbskids.org/"},
            "mime_type": "application/vnd.apple.mpegurl", "manifest_type": "hls",
            "diagnostics": {
                "station_callsign": "national", "station_name": "PBS KIDS",
                "feed_profile": profile, "selected_source": "pbskids-clear-hls",
                "feed_has_hls": True, "feed_has_dash": False, "feed_has_widevine": False,
            },
        }

    station, feed = _feed(ctx, callsign, profile)
    cookie = "pbsol.station={}; pbsol.station_id={}".format(station["callsign"], station["id"])
    headers = {"User-Agent": DEFAULT_UA, "Referer": LIVE_PAGE, "Cookie": cookie}
    clear_url = str(feed.get("non_drm_url") or "")
    dash_url = str(feed.get("drm_dash_url") or "")
    license_url = str(feed.get("widevine_license") or "")
    diagnostics = {
        "station_callsign": station["callsign"], "station_name": station["name"],
        "feed_profile": profile, "selected_source": "hls" if clear_url else "dash-widevine",
        "feed_has_hls": bool(clear_url), "feed_has_dash": bool(dash_url),
        "feed_has_widevine": bool(license_url),
    }
    if clear_url:
        return {
            "url": clear_url, "headers": headers,
            "mime_type": "application/vnd.apple.mpegurl", "manifest_type": "hls",
            "diagnostics": diagnostics,
        }
    if not dash_url or not license_url:
        raise ValueError(
            "PBS returned no compatible playback callsign={} profile={} has_hls={} has_dash={} has_widevine={}".format(
                station["callsign"], profile, bool(clear_url), bool(dash_url), bool(license_url)))
    return {
        "url": dash_url, "headers": headers,
        "mime_type": "application/dash+xml", "manifest_type": "mpd",
        "license_type": "com.widevine.alpha", "license_key": license_url,
        "diagnostics": diagnostics,
    }
