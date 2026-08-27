"""Provider registry and normalized multi-provider queries."""

import concurrent.futures
import copy
import json
import os
import time

from .cache import JsonCache
from .diagnostics import log_failure
from .http import HttpClient
from .models import ALL_GENRES, ALL_LANGUAGES, ALL_PROVIDERS, GENRE_ORDER, search_score
from .providers import (
    distro, freelivesports, lg, pbs, plex, pluto, roku, samsung, stirr,
    tcl, tubi, twitch, whale, xumo,
)


MODULES = (
    pluto, plex, samsung, pbs, stirr, roku, lg, xumo, tubi, tcl,
    distro, whale, freelivesports, twitch,
)

_SCHEDULE_INDEX_KEY = "schedule-search-index-v1"
_SCHEDULE_INDEX_MAX_AGE = 30 * 60
_BROWSE_SNAPSHOT_MAX_AGE = 10 * 60
_BROWSE_SNAPSHOT_VERSION = 1


class ProviderContext:
    def __init__(self, addon_path, profile_path):
        self.addon_path = addon_path
        self.profile_path = profile_path
        self.cache = JsonCache(profile_path)
        self.http = HttpClient()


class Registry:
    def __init__(self, addon_path, profile_path, enabled=None, diagnostics=False):
        self.addon_path = addon_path
        self.profile_path = profile_path
        self.enabled = set(enabled or [module.KEY for module in MODULES])
        self.modules = {module.KEY: module for module in MODULES}
        self.menu_cache = JsonCache(profile_path)
        self.fallback_counts = self._bundled_provider_counts()
        self.diagnostics_enabled = bool(diagnostics)

    def context(self):
        return ProviderContext(self.addon_path, self.profile_path)

    def definitions(self, browsable_only=False):
        rows = []
        for module in MODULES:
            if module.KEY not in self.enabled:
                continue
            if browsable_only and not module.BROWSABLE:
                continue
            rows.append({"key": module.KEY, "name": module.NAME, "browsable": module.BROWSABLE})
        return rows

    def provider_name(self, key):
        module = self.modules.get(key)
        return module.NAME if module else key.title()

    def _bundled_provider_counts(self):
        path = os.path.join(self.addon_path, "resources", "data", "provider_counts.json")
        try:
            with open(path, "r", encoding="utf-8") as source:
                payload = json.load(source)
            values = payload.get("counts") if isinstance(payload, dict) else None
            return {str(key): int(count) for key, count in (values or {}).items()}
        except (OSError, TypeError, ValueError):
            return {}

    @staticmethod
    def _provider_count_key(key):
        return "provider-count-v2-{}".format(key)

    def _browse_snapshot_key(self):
        keys = [row["key"] for row in self.definitions(browsable_only=True)]
        return "browse-catalog-v{}-{}".format(_BROWSE_SNAPSHOT_VERSION, ",".join(keys))

    def cached_browse_snapshot(self, max_age=_BROWSE_SNAPSHOT_MAX_AGE):
        """Return the shared browse catalog while it is timestamp-valid.

        Every provider/language/genre browse menu is derived from this one raw
        snapshot.  Cross-provider de-duplication remains a presentation choice
        in default.py so toggling that setting never requires a provider reload.
        """
        payload = self.menu_cache.get(self._browse_snapshot_key(), max_age, None)
        if not isinstance(payload, dict):
            return None
        rows = payload.get("rows")
        failures = payload.get("failures")
        if not isinstance(rows, list) or not isinstance(failures, list):
            return None
        try:
            built_at = float(payload.get("built_at") or 0)
        except (TypeError, ValueError):
            return None
        if built_at <= 0:
            return None
        return {
            "built_at": built_at,
            "rows": copy.deepcopy(rows),
            "failures": [str(value) for value in failures],
        }

    def build_browse_snapshot(self, progress=None):
        """Build and persist the one browse snapshot used by every browse route."""
        keys = [row["key"] for row in self.definitions(browsable_only=True)]
        rows = []
        failures = []
        if keys:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(keys))) as pool:
                futures = {pool.submit(self._catalog_one, key): key for key in keys}
                completed = 0
                for future in concurrent.futures.as_completed(futures):
                    key = futures[future]
                    try:
                        values = future.result()
                        rows.extend(values)
                        status = "ok"
                    except Exception as error:
                        values = []
                        failures.append(key)
                        status = "error"
                        if self.diagnostics_enabled:
                            log_failure("catalog", error, provider=key)
                    completed += 1
                    if progress:
                        progress(completed, len(keys), key, self.provider_name(key), status, len(values))

        unique = {}
        for row in rows:
            unique[(row.get("provider"), row.get("id"))] = row
        payload = {
            "built_at": time.time(),
            "rows": list(unique.values()),
            "failures": list(dict.fromkeys(failures)),
        }
        self.menu_cache.set(self._browse_snapshot_key(), payload)
        return {
            "built_at": payload["built_at"],
            "rows": copy.deepcopy(payload["rows"]),
            "failures": list(payload["failures"]),
        }

    def browse_snapshot(self, progress=None, force=False):
        if not force:
            cached = self.cached_browse_snapshot()
            if cached is not None:
                return cached
        return self.build_browse_snapshot(progress=progress)

    def invalidate_browse_snapshot(self):
        return self.menu_cache.delete(self._browse_snapshot_key())

    def cached_provider_counts(self, max_age=12 * 60 * 60):
        marker = object()
        counts = {}
        for row in self.definitions(browsable_only=True):
            key = row["key"]
            value = self.menu_cache.get(self._provider_count_key(key), max_age, marker)
            if value is marker:
                value = self.fallback_counts.get(key, marker)
            if value is marker:
                return None
            try:
                counts[key] = int(value)
            except (TypeError, ValueError):
                return None
        return counts

    def provider_counts(self, progress=None, force=False):
        snapshot = self.browse_snapshot(progress=progress, force=force)
        counts = {row["key"]: 0 for row in self.definitions(browsable_only=True)}
        for row in snapshot.get("rows") or []:
            key = str(row.get("provider") or "")
            if key in counts:
                counts[key] += 1
        # A provider that failed this snapshot is intentionally represented by
        # zero, not by a stale bundled/provider-count estimate.  That keeps the
        # provider menu and every aggregate derived from the exact same data.
        return counts, list(snapshot.get("failures") or [])

    def _catalog_one(self, key):
        module = self.modules[key]
        rows = module.channels(self.context())
        output = []
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            value = copy.deepcopy(row)
            value["provider"] = key
            value["provider_name"] = module.NAME
            output.append(value)
        if module.BROWSABLE:
            self.menu_cache.set(self._provider_count_key(key), len(output))
        return output

    def catalogs(self, provider=ALL_PROVIDERS, progress=None):
        """Return rows from the shared timestamped browse snapshot.

        A single-provider browse is still carved out of the same snapshot so
        provider, language, genre, and channel counts cannot drift apart during
        the snapshot lifetime.  Search and tune-time resolution deliberately use
        their own direct provider calls and are not pinned to this browse cache.
        """
        snapshot = self.browse_snapshot(progress=progress)
        rows = list(snapshot.get("rows") or [])
        failures = list(snapshot.get("failures") or [])
        if provider != ALL_PROVIDERS:
            rows = [row for row in rows if str(row.get("provider") or "") == str(provider)]
            failures = [key for key in failures if key == provider]
        return rows, failures

    def filter(self, provider=ALL_PROVIDERS, language=ALL_LANGUAGES, genre=ALL_GENRES):
        rows, failures = self.catalogs(provider)
        output = []
        for row in rows:
            if language != ALL_LANGUAGES and language not in (row.get("languages") or []):
                continue
            if genre != ALL_GENRES and genre not in (row.get("genres") or []):
                continue
            output.append(row)
        output.sort(key=lambda value: (value.get("name", "").casefold(), value.get("provider_name", "").casefold()))
        return output, failures

    def languages(self, provider=ALL_PROVIDERS):
        rows, failures = self.catalogs(provider)
        values = sorted({language for row in rows for language in row.get("languages") or []}, key=str.casefold)
        return values, failures

    def genres(self, provider=ALL_PROVIDERS, language=ALL_LANGUAGES):
        rows, failures = self.filter(provider, language, ALL_GENRES)
        values = {genre for row in rows for genre in row.get("genres") or []}
        ordered = [genre for genre in GENRE_ORDER if genre in values]
        ordered.extend(sorted(values.difference(ordered), key=str.casefold))
        return ordered, failures

    @staticmethod
    def _schedule_index_id(row):
        return "{}|{}".format(str(row.get("provider") or ""), str(row.get("id") or ""))

    def _schedule_index(self):
        now = time.time()
        raw = self.menu_cache.get(_SCHEDULE_INDEX_KEY, None, {})
        raw = raw if isinstance(raw, dict) else {}
        active = {}
        for key, entry in raw.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("schedule"), list):
                continue
            try:
                age = now - float(entry.get("captured_at") or 0)
            except (TypeError, ValueError):
                continue
            if 0 <= age <= _SCHEDULE_INDEX_MAX_AGE:
                active[str(key)] = entry
        if len(active) != len(raw):
            self.menu_cache.set(_SCHEDULE_INDEX_KEY, active)
        return active

    def _apply_schedule_index(self, rows):
        index = self._schedule_index()
        output = [copy.deepcopy(row) for row in rows]
        for row in output:
            if row.get("schedule"):
                continue
            entry = index.get(self._schedule_index_id(row)) or {}
            if isinstance(entry.get("schedule"), list):
                row["schedule"] = copy.deepcopy(entry["schedule"])
        return output

    def _remember_schedules(self, rows):
        updates = [row for row in rows if isinstance(row, dict) and row.get("schedule")]
        if not updates:
            return
        now = time.time()
        index = self._schedule_index()
        for row in updates:
            index[self._schedule_index_id(row)] = {
                "captured_at": now,
                "schedule": copy.deepcopy(row.get("schedule") or []),
            }
        self.menu_cache.set(_SCHEDULE_INDEX_KEY, index)

    def enrich(self, rows, limit=36):
        """Best-effort schedule lookup for a screen-sized subset.

        Pluto already carries a guide in its catalog. Plex requires a separate
        public grid call per channel, so it is bounded to keep navigation fast.
        """
        output = [copy.deepcopy(row) for row in rows]
        targets = []
        for index, row in enumerate(output):
            module = self.modules.get(row.get("provider"))
            if module and hasattr(module, "enrich") and not row.get("schedule"):
                targets.append((index, module, row))
            if len(targets) >= max(0, int(limit)):
                break
        if not targets:
            return output
        # Reuse one HTTP/cache context per provider for this screen. Some
        # providers establish an anonymous session before resolving schedules;
        # sharing that context prevents one bootstrap per visible channel.
        contexts = {}
        for _, module, _ in targets:
            if module not in contexts:
                contexts[module] = self.context()
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            futures = {
                pool.submit(module.enrich, contexts[module], row): index
                for index, module, row in targets
            }
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                try:
                    output[index] = future.result()
                except Exception as error:
                    provider = output[index].get("provider", "")
                    if self.diagnostics_enabled:
                        log_failure(
                            "schedule", error, provider=provider,
                            identifier=output[index].get("id", ""),
                            channel=output[index].get("name", ""),
                        )
        self._remember_schedules(output)
        return output

    def search(self, query, per_provider=30, progress=None):
        keys = [row["key"] for row in self.definitions()]
        rows = []
        failures = []

        def run(key):
            module = self.modules[key]
            ctx = self.context()
            if hasattr(module, "search"):
                values = module.search(ctx, query, per_provider)
                for value in values:
                    value["provider_name"] = module.NAME
                return values

            # Channel-name/metadata matching and schedule-title matching use the
            # same score. Provider catalogs that already contain schedules are
            # searched directly. Schedules previously fetched while browsing
            # are merged from a timestamp-bounded index, and providers with a
            # safe bulk-guide path may fill schedules for the entire catalog in
            # one provider-level operation. This avoids hundreds of tune-like
            # per-channel EPG requests to Roku/Plex on every keyword search.
            values = self._apply_schedule_index(self._catalog_one(key))
            if hasattr(module, "search_enrich"):
                try:
                    values = module.search_enrich(ctx, values)
                except Exception as error:
                    # Schedule acquisition is additive search metadata. A guide
                    # outage must not suppress otherwise valid channel-name
                    # matches from the provider's catalog.
                    if self.diagnostics_enabled:
                        log_failure("search-schedule", error, provider=key)
            scored = [(search_score(value, query), value) for value in values]
            scored = [pair for pair in scored if pair[0] > 0]
            scored.sort(key=lambda pair: (-pair[0], pair[1].get("name", "").casefold()))
            return [pair[1] for pair in scored[:per_provider]]

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(keys) or 1)) as pool:
            futures = {pool.submit(run, key): key for key in keys}
            completed = 0
            for future in concurrent.futures.as_completed(futures):
                key = futures[future]
                try:
                    values = future.result()
                    rows.extend(values)
                    status = "ok"
                except Exception as error:
                    values = []
                    failures.append(key)
                    status = "error"
                    if self.diagnostics_enabled:
                        log_failure("search", error, provider=key)
                completed += 1
                if progress:
                    progress(completed, len(keys), key, self.provider_name(key), status, len(values))
        self._remember_schedules(rows)
        unique = {}
        for value in rows:
            unique[(value.get("provider"), value.get("id"))] = value
        output = list(unique.values())
        output.sort(key=lambda value: (-search_score(value, query), value.get("name", "").casefold()))
        return self.enrich(output, limit=24), failures

    def channel(self, provider, identifier, with_schedule=True):
        rows = self._catalog_one(provider)
        value = next((row for row in rows if str(row.get("id")) == str(identifier)), None)
        if value and with_schedule:
            return self.enrich([value], limit=1)[0]
        return value

    def resolve(self, provider, identifier):
        if provider not in self.enabled or provider not in self.modules:
            raise ValueError("Provider is disabled or unknown")
        return self.modules[provider].resolve(self.context(), identifier)
