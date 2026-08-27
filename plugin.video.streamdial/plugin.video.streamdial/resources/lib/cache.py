"""Atomic JSON cache used for provider catalogs and short-lived schedules."""

import hashlib
import json
import os
import time


class JsonCache:
    def __init__(self, root):
        self.root = root

    def _path(self, key):
        digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
        return os.path.join(self.root, "cache", digest + ".json")

    def get(self, key, max_age, default=None):
        path = self._path(key)
        try:
            if max_age is not None and time.time() - os.path.getmtime(path) > max_age:
                return default
            with open(path, "r", encoding="utf-8") as source:
                payload = json.load(source)
            if not isinstance(payload, dict) or payload.get("key") != str(key):
                return default
            return payload.get("value", default)
        except (OSError, TypeError, ValueError):
            return default

    def set(self, key, value):
        path = self._path(key)
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        temporary = path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as target:
                json.dump({"key": str(key), "value": value}, target, ensure_ascii=False)
            os.replace(temporary, path)
            return value
        except (OSError, TypeError, ValueError):
            try:
                os.remove(temporary)
            except OSError:
                pass
            return value

    def delete(self, key):
        try:
            os.remove(self._path(key))
            return True
        except OSError:
            return False

    def remember(self, key, max_age, loader):
        marker = object()
        value = self.get(key, max_age, marker)
        if value is not marker:
            return value
        return self.set(key, loader())
