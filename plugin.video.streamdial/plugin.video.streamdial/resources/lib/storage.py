"""Persistent search history and pin storage."""

import json
import os


class Storage:
    def __init__(self, profile_path):
        self.profile_path = profile_path

    def _read(self, name, default):
        path = os.path.join(self.profile_path, name)
        try:
            with open(path, "r", encoding="utf-8") as source:
                value = json.load(source)
            return value
        except (OSError, TypeError, ValueError):
            return default

    def _write(self, name, value):
        os.makedirs(self.profile_path, exist_ok=True)
        path = os.path.join(self.profile_path, name)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, indent=2, sort_keys=True)
            target.write("\n")
        os.replace(temporary, path)

    def history(self):
        values = self._read("search_history.json", [])
        if not isinstance(values, list):
            return []
        return [str(value).strip() for value in values if str(value).strip()]

    def remember_search(self, query, limit=30):
        query = str(query or "").strip()
        if not query:
            return
        folded = query.casefold()
        values = [value for value in self.history() if value.casefold() != folded]
        self._write("search_history.json", [query] + values[:max(0, limit - 1)])

    def forget_search(self, query):
        folded = str(query or "").strip().casefold()
        values = self.history()
        updated = [value for value in values if value.casefold() != folded]
        if updated == values:
            return False
        self._write("search_history.json", updated)
        return True

    @staticmethod
    def pin_key(pin):
        return "|".join(str(pin.get(key, "")) for key in (
            "kind", "provider", "language", "genre", "channel_id"))

    def pins(self):
        values = self._read("pins.json", [])
        if not isinstance(values, list):
            return []
        output = []
        seen = set()
        for value in values:
            if not isinstance(value, dict) or value.get("kind") not in (
                    "provider", "language", "genre", "channel"):
                continue
            key = self.pin_key(value)
            if key in seen:
                continue
            seen.add(key)
            output.append(value)
        return output

    def is_pinned(self, pin):
        key = self.pin_key(pin)
        return any(self.pin_key(value) == key for value in self.pins())

    def add_pin(self, pin):
        values = self.pins()
        key = self.pin_key(pin)
        if any(self.pin_key(value) == key for value in values):
            return False
        values.append(dict(pin))
        self._write("pins.json", values)
        return True

    def remove_pin(self, pin):
        values = self.pins()
        key = self.pin_key(pin)
        updated = [value for value in values if self.pin_key(value) != key]
        if updated == values:
            return False
        self._write("pins.json", updated)
        return True
