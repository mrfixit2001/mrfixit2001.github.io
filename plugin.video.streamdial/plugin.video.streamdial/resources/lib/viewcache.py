"""Timestamped per-screen channel-list cache for fast BACK and playback return."""

import hashlib
import time
import uuid

from .cache import JsonCache

_SCHEMA = 3


class ChannelViewCache:
    def __init__(self, profile_path):
        self.cache = JsonCache(profile_path)

    @staticmethod
    def _key(screen_key):
        digest = hashlib.sha1(str(screen_key or "").encode("utf-8")).hexdigest()
        return "channel-view-v3-{}".format(digest)

    def store(self, screen_key, rows, show_provider=False):
        payload = {
            "schema": _SCHEMA,
            "view_id": uuid.uuid4().hex,
            "screen_key": str(screen_key or ""),
            "built_at": time.time(),
            "show_provider": bool(show_provider),
            "rows": list(rows or []),
        }
        self.cache.set(self._key(screen_key), payload)
        self.cache.set("channel-view-v3-current", {"view_id": payload["view_id"], "screen_key": payload["screen_key"]})
        return payload

    def _load(self, screen_key, max_age, view_id=""):
        payload = self.cache.get(self._key(screen_key), None, None)
        if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA:
            return None
        if str(payload.get("screen_key") or "") != str(screen_key or ""):
            return None
        if view_id and str(payload.get("view_id") or "") != str(view_id):
            return None
        try:
            age = time.time() - float(payload.get("built_at") or 0)
        except (TypeError, ValueError):
            return None
        if age < 0 or not isinstance(payload.get("rows"), list):
            return None
        if max_age is not None and age > max(1, int(max_age or 0)):
            return None
        return payload

    def load(self, screen_key, view_id, max_age=30 * 60):
        return self._load(screen_key, max_age, view_id=view_id)

    def load_recent(self, screen_key, max_age=10 * 60):
        """Reuse any still-current final list during normal BACK navigation."""
        return self._load(screen_key, max_age)

    def invalidate(self, view_id="", screen_key=""):
        if screen_key:
            return self.cache.delete(self._key(screen_key))
        if view_id:
            pointer = self.cache.get("channel-view-v3-current", None, None)
            if isinstance(pointer, dict) and str(pointer.get("view_id") or "") == str(view_id):
                return self.cache.delete(self._key(pointer.get("screen_key") or ""))
        return False
