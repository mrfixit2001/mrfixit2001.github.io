"""Kodi playback-plan application."""

import json
import urllib.parse

from .http import header_string


def normalized(plan):
    if not isinstance(plan, dict) or not str(plan.get("url") or "").startswith(("http://", "https://")):
        raise ValueError("Provider returned no playable URL")
    return {
        "url": str(plan["url"]),
        "headers": dict(plan.get("headers") or {}),
        "mime_type": str(plan.get("mime_type") or "application/vnd.apple.mpegurl"),
        "manifest_type": str(plan.get("manifest_type") or "hls"),
        "license_type": str(plan.get("license_type") or ""),
        "license_key": str(plan.get("license_key") or ""),
        "manifest_config": dict(plan.get("manifest_config") or {}),
    }


def apply_to_listitem(item, plan, use_inputstream=True, inputstream_available=True):
    plan = normalized(plan)
    headers = header_string(plan["headers"])
    use_isa = bool(use_inputstream and inputstream_available and plan["manifest_type"] in ("hls", "mpd"))
    url = plan["url"]
    if use_isa:
        item.setProperty("inputstream", "inputstream.adaptive")
        item.setProperty("inputstream.adaptive.manifest_type", plan["manifest_type"])
        if headers:
            item.setProperty("inputstream.adaptive.manifest_headers", headers)
            item.setProperty("inputstream.adaptive.stream_headers", headers)
        if plan["manifest_config"]:
            item.setProperty(
                "inputstream.adaptive.manifest_config",
                json.dumps(plan["manifest_config"], separators=(",", ":")),
            )
        if plan["license_type"] and plan["license_key"]:
            item.setProperty("inputstream.adaptive.license_type", plan["license_type"])
            item.setProperty("inputstream.adaptive.license_key", plan["license_key"])
    elif headers:
        url += ("&" if "|" in url else "|") + headers
    item.setPath(url)
    item.setMimeType(plan["mime_type"])
    item.setContentLookup(False)
    item.setProperty("IsPlayable", "true")
    return url
