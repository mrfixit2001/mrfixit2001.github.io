"""Anonymous Twitch search and clear HLS playback."""

import random
import re
import urllib.parse

from ..http import DEFAULT_UA
from ..models import channel, normalize_genres


KEY = "twitch"
NAME = "Twitch"
BROWSABLE = False
GQL_URL = "https://gql.twitch.tv/gql"
USHER_URL = "https://usher.ttvnw.net/api/channel/hls/{}.m3u8"
CLIENT_ID = "ue6666qo983tsx6so1t0vnawi233wa"
LOGIN_RE = re.compile(r"^[a-zA-Z0-9_]{1,25}$")

SEARCH_QUERY = r'''
query StreamDialSearch($query: String!, $options: SearchForOptions) {
  searchFor(userQuery: $query, platform: "web", options: $options) {
    channels { edges { item { __typename ... on User {
      login displayName description profileImageURL(width: 300)
      stream { id title type viewersCount previewImageURL(width: 640, height: 360)
               game { name } createdAt }
    } } } }
  }
}
'''

TOKEN_QUERY = r'''
query PlaybackAccessToken($login: String!) {
  streamPlaybackAccessToken(
    channelName: $login,
    params: {platform: "web", playerBackend: "mediaplayer", playerType: "site"}
  ) { value signature }
}
'''


def _gql(ctx, payload):
    return ctx.http.post_json(GQL_URL, payload=payload, headers={
        "Client-ID": CLIENT_ID,
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": "https://www.twitch.tv",
        "Referer": "https://www.twitch.tv/",
    }, timeout=20, max_bytes=4 * 1024 * 1024)


def channels(ctx):
    del ctx
    return []


def search(ctx, query, limit=30):
    data = _gql(ctx, {
        "operationName": "StreamDialSearch",
        "variables": {"query": query, "options": {"targets": [{"index": "CHANNEL"}]}},
        "query": SEARCH_QUERY,
    })
    edges = (((data.get("data") or {}).get("searchFor") or {}).get("channels") or {}).get("edges") or []
    output = []
    for edge in edges:
        user = edge.get("item") if isinstance(edge, dict) else None
        stream = user.get("stream") if isinstance(user, dict) else None
        if not isinstance(stream, dict) or stream.get("type") != "live":
            continue
        login = str(user.get("login") or "")
        if not LOGIN_RE.match(login):
            continue
        title = user.get("displayName") or login
        live_title = stream.get("title") or ""
        category_name = ((stream.get("game") or {}).get("name") or "")
        viewers = stream.get("viewersCount")
        detail = live_title
        if category_name:
            detail += (" · " if detail else "") + category_name
        if isinstance(viewers, int):
            detail += (" · " if detail else "") + "{:,} viewers".format(viewers)
        if user.get("description"):
            detail += (" — " if detail else "") + user.get("description")
        output.append(channel(
            KEY, login, title, description=detail,
            languages="English",
            genres=normalize_genres([category_name], "{} {}".format(category_name, live_title)),
            logo=user.get("profileImageURL") or "",
            fanart=stream.get("previewImageURL") or "",
            extra={"live_title": live_title, "category": category_name},
        ))
        if len(output) >= limit:
            break
    return output


def resolve(ctx, identifier):
    login = str(identifier or "")
    if not LOGIN_RE.match(login):
        raise ValueError("Invalid Twitch channel")
    data = _gql(ctx, {
        "operationName": "PlaybackAccessToken",
        "variables": {"login": login},
        "query": TOKEN_QUERY,
    })
    token = (data.get("data") or {}).get("streamPlaybackAccessToken") or {}
    if not token.get("value") or not token.get("signature"):
        raise ValueError("Twitch channel is no longer live")
    params = {
        "allow_source": "true",
        "allow_audio_only": "false",
        "allow_spectre": "true",
        "p": str(random.randint(1000000, 10000000)),
        "platform": "web",
        "player": "twitchweb",
        "supported_codecs": "h264",
        "playlist_include_framerate": "true",
        "sig": token["signature"],
        "token": token["value"],
    }
    return {
        "url": USHER_URL.format(login) + "?" + urllib.parse.urlencode(params),
        "headers": {"User-Agent": DEFAULT_UA, "Origin": "https://www.twitch.tv", "Referer": "https://www.twitch.tv/"},
        "mime_type": "application/vnd.apple.mpegurl",
        "manifest_type": "hls",
    }
