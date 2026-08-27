"""One-screen channel-list snapshot cache for fast playback returns.

The cache intentionally stores only the most recently rendered channel screen.
It is not a general navigation cache: explicit navigation rebuilds/replaces the
snapshot, while a playback item carries the snapshot id so Kodi can reuse the
exact screen on every directory reload while returning from playback. Explicit
navigation replaces/clears that eligibility, and a timestamp limits how long a
snapshot can survive a long playback session.
"""

import time
import uuid

from .cache import JsonCache


_CACHE_KEY = "current-channel-view-v2"
_SCHEMA = 2


class ChannelViewCache:
    def __init__(self, profile_path):
        self.cache = JsonCache(profile_path)

    def store(self, screen_key, rows, show_provider=False):
        payload = {
            "schema": _SCHEMA,
            "view_id": uuid.uuid4().hex,
            "screen_key": str(screen_key or ""),
            "built_at": time.time(),
            "show_provider": bool(show_provider),
            "rows": list(rows or []),
        }
        self.cache.set(_CACHE_KEY, payload)
        return payload

    def load(self, screen_key, view_id, max_age=30 * 60):
        payload = self.cache.get(_CACHE_KEY, None, None)
        if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA:
            return None
        if str(payload.get("screen_key") or "") != str(screen_key or ""):
            return None
        if str(payload.get("view_id") or "") != str(view_id or ""):
            return None
        try:
            age = time.time() - float(payload.get("built_at") or 0)
        except (TypeError, ValueError):
            return None
        if age < 0 or age > max(1, int(max_age or 0)):
            return None
        if not isinstance(payload.get("rows"), list):
            return None
        return payload

    def invalidate(self, view_id=""):
        """Delete the current snapshot, optionally only when its id matches."""
        if view_id:
            payload = self.cache.get(_CACHE_KEY, None, None)
            if not isinstance(payload, dict):
                return False
            if str(payload.get("view_id") or "") != str(view_id):
                return False
        return self.cache.delete(_CACHE_KEY)
