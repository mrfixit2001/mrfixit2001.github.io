# -*- coding: utf-8 -*-
import html
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import parse

import xbmc
import xbmcvfs

from .http import ApiError, request_json
from .utils import ADDON, TEMP_ROOT, VERSION, log


_TAG_RE = re.compile(r'<[^>]+>')


def _plain(value):
    if not value:
        return ''
    text = _TAG_RE.sub(' ', str(value))
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


_FALLBACK_GENRES = {
    'movie': [
        'Action', 'Adventure', 'Animation', 'Biography', 'Comedy', 'Crime',
        'Documentary', 'Drama', 'Family', 'Fantasy', 'History', 'Horror',
        'Mystery', 'Romance', 'Sci-Fi', 'Sport', 'Thriller', 'War', 'Western',
    ],
    'series': [
        'Action', 'Adventure', 'Animation', 'Biography', 'Comedy', 'Crime',
        'Documentary', 'Drama', 'Family', 'Fantasy', 'History', 'Horror',
        'Mystery', 'Romance', 'Sci-Fi', 'Sport', 'Thriller', 'War', 'Western',
        'Reality-TV', 'Talk-Show', 'Game-Show',
    ],
}

# TVmaze publishes this fixed show-genre vocabulary. Keeping the provider's
# taxonomy locally makes the New TV Episodes genre menu immediate and avoids
# fetching release data merely to discover category names.
_TVMAZE_GENRES = [
    'Action', 'Adult', 'Adventure', 'Anime', 'Children', 'Comedy', 'Crime',
    'DIY', 'Drama', 'Espionage', 'Family', 'Fantasy', 'Food', 'History',
    'Horror', 'Legal', 'Medical', 'Music', 'Mystery', 'Nature', 'Romance',
    'Science-Fiction', 'Sports', 'Supernatural', 'Thriller', 'Travel', 'War',
    'Western',
]

_GENRE_ALIASES = {
    'scifi': {'scifi', 'sciencefiction'},
    'sport': {'sport', 'sports'},
    'realitytv': {'realitytv', 'reality'},
    'talkshow': {'talkshow', 'talk'},
    'gameshow': {'gameshow', 'game'},
    'animation': {'animation', 'anime'},
}


def _genre_key(value):
    return re.sub(r'[^a-z0-9]+', '', str(value or '').casefold())


def _genre_matches(values, wanted):
    target = _genre_key(wanted)
    if not target:
        return True
    accepted = {target}
    for canonical, aliases in _GENRE_ALIASES.items():
        group = set(aliases) | {canonical}
        if target in group:
            accepted.update(group)
    for value in values or []:
        key = _genre_key(value)
        if key in accepted:
            return True
    return False


class MetadataClient:
    def __init__(self):
        self.base = (ADDON.getSetting('metadata_base') or 'https://v3-cinemeta.strem.io').rstrip('/')
        self.headers = {
            'User-Agent': 'PremiumPlayer/%s' % VERSION,
            'Cache-Control': 'no-cache, no-store, max-age=0',
            'Pragma': 'no-cache',
        }
        self.tvmaze = 'https://api.tvmaze.com'
        # Do not send Cinemeta's no-cache directives to TVmaze. TVmaze's own
        # load balancers cache public API output for 60 minutes; allowing those
        # edge hits reduces latency and avoids unnecessary backend rate-limit use.
        self.tvmaze_headers = {
            'User-Agent': 'PremiumPlayer/%s' % VERSION,
        }
        self._tvmaze_rate_lock = threading.Lock()
        self._tvmaze_backoff_until = 0.0

    def tv_genres(self):
        """Return TVmaze's documented show-genre taxonomy locally."""
        return list(_TVMAZE_GENRES)

    def genres(self, media_type):
        """Return the current Cinemeta content genres for Movie/Series."""
        kind = 'series' if str(media_type or '') == 'series' else 'movie'
        try:
            payload, _, _ = request_json(self.base + '/manifest.json', headers=self.headers)
            catalogs = (payload or {}).get('catalogs', []) if isinstance(payload, dict) else []
            for catalog in catalogs:
                if catalog.get('type') == kind and catalog.get('id') == 'top':
                    genres = [str(x).strip() for x in (catalog.get('genres') or []) if str(x).strip()]
                    if genres:
                        return genres
        except Exception as exc:
            log('Could not load Cinemeta genre list: %s' % exc)
        return list(_FALLBACK_GENRES[kind])

    def search(self, media_type, query):
        kind = 'movie' if media_type == 'movie' else 'series'
        encoded = parse.quote(query, safe='')
        url = '%s/catalog/%s/top/search=%s.json' % (self.base, kind, encoded)
        payload, _, _ = request_json(url, headers=self.headers)
        rows = (payload or {}).get('metas', []) if isinstance(payload, dict) else []
        rows = list(rows or [])[:25]

        # Cinemeta catalog rows commonly omit plot/detail fields. Fetch the full
        # metadata object for visible search results, but never persist it locally.
        missing = []
        for idx, row in enumerate(rows):
            imdb = row.get('imdb_id') or row.get('id')
            if imdb and str(imdb).startswith('tt') and not row.get('description'):
                missing.append((idx, str(imdb)))
        if missing:
            with ThreadPoolExecutor(max_workers=min(6, len(missing))) as pool:
                futures = {pool.submit(self.meta, media_type, imdb): idx for idx, imdb in missing}
                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        full = future.result() or {}
                    except Exception as exc:
                        log('Metadata enrichment failed: %s' % exc)
                        continue
                    merged = dict(rows[idx])
                    for key, value in full.items():
                        if value not in (None, '', [], {}):
                            merged[key] = value
                    rows[idx] = merged
        return rows

    def enrich_rows(self, media_type, rows, require_genres=False):
        """Enrich rows, optionally requiring full genre metadata."""
        rows = [dict(row) for row in list(rows or [])]
        missing = []
        for idx, row in enumerate(rows):
            imdb = row.get('imdb_id') or row.get('id')
            needs_detail = not row.get('description')
            if require_genres and not row.get('genres'):
                needs_detail = True
            if imdb and str(imdb).startswith('tt') and needs_detail:
                missing.append((idx, str(imdb)))
        if missing:
            with ThreadPoolExecutor(max_workers=min(6, len(missing))) as pool:
                futures = {pool.submit(self.meta, media_type, imdb): idx for idx, imdb in missing}
                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        full = future.result() or {}
                    except Exception as exc:
                        log('New-release metadata enrichment failed: %s' % exc)
                        continue
                    merged = dict(rows[idx])
                    for key, value in full.items():
                        if value not in (None, '', [], {}):
                            merged[key] = value
                    rows[idx] = merged
        return rows

    def new_releases(self, media_type, year=None, limit=80, enrich=True):
        """Return an expanded, paginated Cinemeta year catalog live.

        Cinemeta catalog responses are paginated through Stremio's ``skip``
        catalog extra. Continue requesting pages until the requested visible
        limit is reached, de-duplicating rows and falling back to the prior year
        only when the current-year catalog is genuinely short.
        """
        kind = 'movie' if media_type == 'movie' else 'series'
        year = int(year or datetime.now().year)
        limit = max(1, int(limit))
        rows = []
        seen = set()
        for catalog_year in (year, year - 1):
            skip = 0
            while len(rows) < limit:
                extra = 'genre=%d' % catalog_year
                if skip:
                    extra += '&skip=%d' % skip
                url = '%s/catalog/%s/year/%s.json' % (self.base, kind, extra)
                payload, _, _ = request_json(url, headers=self.headers)
                page = (payload or {}).get('metas', []) if isinstance(payload, dict) else []
                page = list(page or [])
                if not page:
                    break
                added = 0
                for row in page:
                    key = str(row.get('imdb_id') or row.get('id') or '').strip().casefold()
                    if not key:
                        key = '%s|%s' % (
                            str(row.get('name') or row.get('title') or '').strip().casefold(),
                            str(row.get('releaseInfo') or row.get('year') or '').strip())
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    rows.append(row)
                    added += 1
                    if len(rows) >= limit:
                        break
                if len(rows) >= limit or added == 0:
                    break
                skip += len(page)
            if len(rows) >= limit:
                break
        rows = rows[:limit]

        # Pagination callers collect up to 300 lightweight catalog rows, then
        # enrich only the 100 rows visible on the requested page.
        return self.enrich_rows(media_type, rows) if enrich else rows

    def new_releases_by_genre(self, media_type, genre, year=None, limit=300, max_years=2):
        """Return the newest year-catalog rows matching a content genre.

        Cinemeta's dedicated genre catalog is popularity-ranked, so it cannot
        satisfy a New Releases request.  Walk the year-sorted catalog newest to
        oldest within the same current/prior-year New Releases scope, enrich
        only the pages needed for genre metadata, and stop once the requested
        number of matching releases has been collected.
        """
        kind = 'movie' if media_type == 'movie' else 'series'
        wanted = str(genre or '').strip()
        if not wanted:
            return self.new_releases(media_type, year=year, limit=limit, enrich=True)
        start_year = int(year or datetime.now().year)
        limit = max(1, int(limit))
        max_years = max(1, int(max_years))
        rows = []
        seen = set()
        for catalog_year in range(start_year, start_year - max_years, -1):
            skip = 0
            while len(rows) < limit:
                extra = 'genre=%d' % catalog_year
                if skip:
                    extra += '&skip=%d' % skip
                url = '%s/catalog/%s/year/%s.json' % (self.base, kind, extra)
                payload, _, _ = request_json(url, headers=self.headers)
                page = (payload or {}).get('metas', []) if isinstance(payload, dict) else []
                page = list(page or [])
                if not page:
                    break

                unique_page = []
                for row in page:
                    key = str(row.get('imdb_id') or row.get('id') or '').strip().casefold()
                    if not key:
                        key = '%s|%s' % (
                            str(row.get('name') or row.get('title') or '').strip().casefold(),
                            str(row.get('releaseInfo') or row.get('year') or '').strip())
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    unique_page.append(row)

                if not unique_page:
                    break
                enriched = self.enrich_rows(media_type, unique_page, require_genres=True)
                for row in enriched:
                    values = row.get('genres') or []
                    if isinstance(values, str):
                        values = [values]
                    if _genre_matches(values, wanted):
                        rows.append(row)
                        if len(rows) >= limit:
                            break
                if len(rows) >= limit:
                    break
                skip += len(page)
        return rows[:limit]

    @staticmethod
    def filter_tv_release_rows(rows, genre=None, limit=300):
        """Filter TV release rows using many-to-many show genres.

        A show can belong to several TVmaze genres and therefore qualifies for
        every matching genre view. ``Other`` is strictly reserved for rows
        whose resolved parent-show metadata has no usable genre values.
        """
        wanted = str(genre or '').strip()
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 300
        result = []
        for row in list(rows or []):
            values = row.get('genres') or []
            if isinstance(values, str):
                values = [values]
            values = [str(value).strip() for value in values if str(value or '').strip()]
            if wanted:
                if _genre_key(wanted) == 'other':
                    if values or row.get('_genres_resolved') is False:
                        continue
                elif not _genre_matches(values, wanted):
                    continue
            result.append(row)
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _schedule_show(episode):
        """Return the parent show from either TVmaze schedule response shape."""
        show = episode.get('show') if isinstance(episode, dict) else None
        if isinstance(show, dict) and show:
            return dict(show)
        embedded = episode.get('_embedded') if isinstance(episode, dict) else None
        show = embedded.get('show') if isinstance(embedded, dict) else None
        return dict(show) if isinstance(show, dict) else {}

    @staticmethod
    def _show_genres(show):
        raw = (show or {}).get('genres') or []
        if isinstance(raw, str):
            raw = [raw]
        result = []
        seen = set()
        for value in raw:
            text = str(value or '').strip()
            key = _genre_key(text)
            if not text or not key or key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result

    @staticmethod
    def _show_imdb(show):
        externals = (show or {}).get('externals') or {}
        return str(externals.get('imdb') or '').strip() if isinstance(externals, dict) else ''

    _TVMAZE_RAW_CACHE_TTL = 60 * 60
    _TVMAZE_SNAPSHOT_SCHEMA = 2
    _TVMAZE_SCHEDULE_WORKERS = 10
    _TVMAZE_SHOW_WORKERS = 4  # retained for legacy helper; release-pool build does not use it
    # The rolling snapshot itself is considered fresh for 15 minutes.  Older
    # snapshots are still useful as a base: we refresh only newly-needed/stale
    # dates instead of rebuilding the complete 30-day window.
    _TVMAZE_SNAPSHOT_FRESH_TTL = 15 * 60
    _TVMAZE_SNAPSHOT_MAX_STALE = 35 * 24 * 60 * 60
    # Historical premiere schedules change far less often than today's. Reuse
    # those raw provider responses aggressively so a rolling refresh normally
    # costs only the current date's two schedule calls.
    _TVMAZE_SCHEDULE_TODAY_TTL = 15 * 60
    _TVMAZE_SCHEDULE_YESTERDAY_TTL = 6 * 60 * 60
    _TVMAZE_SCHEDULE_RECENT_TTL = 24 * 60 * 60
    _TVMAZE_SCHEDULE_OLD_TTL = 7 * 24 * 60 * 60

    def _tvmaze_cache_path(self, name):
        if not xbmcvfs.exists(TEMP_ROOT):
            xbmcvfs.mkdirs(TEMP_ROOT)
        safe = re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(name or '')).strip('_')
        return os.path.join(TEMP_ROOT, 'tvmaze_%s.json' % safe)

    def _load_tvmaze_cache(self, name, max_age=None):
        """Load a short-lived provider cache without treating it as a New Releases result cache.

        These files mirror TVmaze data and deliberately survive the shared New
        Releases menu boundary. Derived Movie/TV result caches still clear at
        that boundary; provider payloads are reusable for no longer than the
        same one-hour interval TVmaze normally caches public API responses.
        """
        path = self._tvmaze_cache_path(name)
        if not xbmcvfs.exists(path):
            return None
        try:
            f = xbmcvfs.File(path, 'r')
            try:
                raw = f.read()
            finally:
                f.close()
            wrapper = json.loads(raw) if raw else {}
            if not isinstance(wrapper, dict) or 'data' not in wrapper:
                return None
            stored = float(wrapper.get('stored') or 0)
            ttl = self._TVMAZE_RAW_CACHE_TTL if max_age is None else max(0, int(max_age))
            if not stored or time.time() - stored > ttl:
                try:
                    xbmcvfs.delete(path)
                except Exception:
                    pass
                return None
            return wrapper.get('data')
        except Exception as exc:
            log('Could not load TVmaze provider cache %s: %s' % (name, exc), xbmc.LOGWARNING)
            return None

    def _save_tvmaze_cache(self, name, data):
        path = self._tvmaze_cache_path(name)
        try:
            f = xbmcvfs.File(path, 'w')
            try:
                f.write(json.dumps({'stored': int(time.time()), 'data': data}, ensure_ascii=False))
            finally:
                f.close()
            return True
        except Exception as exc:
            log('Could not save TVmaze provider cache %s: %s' % (name, exc), xbmc.LOGWARNING)
            return False

    def _tvmaze_wait_for_backoff(self):
        lock = getattr(self, '_tvmaze_rate_lock', None)
        if lock is None:
            lock = threading.Lock()
            self._tvmaze_rate_lock = lock
            self._tvmaze_backoff_until = 0.0
        while True:
            with lock:
                delay = max(0.0, float(getattr(self, '_tvmaze_backoff_until', 0.0)) - time.monotonic())
            if delay <= 0:
                return
            time.sleep(min(delay, 2.0))

    def _tvmaze_backoff(self, seconds):
        lock = getattr(self, '_tvmaze_rate_lock', None)
        if lock is None:
            lock = threading.Lock()
            self._tvmaze_rate_lock = lock
            self._tvmaze_backoff_until = 0.0
        with lock:
            self._tvmaze_backoff_until = max(
                float(getattr(self, '_tvmaze_backoff_until', 0.0)),
                time.monotonic() + max(0.0, float(seconds)))

    def _tvmaze_request_cached(self, cache_name, url, params=None, timeout=12, max_age=None):
        cached = self._load_tvmaze_cache(cache_name, max_age=max_age)
        if cached is not None:
            return cached

        # TVmaze documents a public rate limit of at least 20 calls/10 seconds
        # and recommends bounded parallelism with backoff on HTTP 429. We use a
        # small worker pool for latency hiding, then coordinate all workers on a
        # shared backoff window if the API tells us to slow down.
        for attempt in range(3):
            self._tvmaze_wait_for_backoff()
            try:
                payload, _, _ = request_json(
                    url, params=params, headers=self.tvmaze_headers, timeout=timeout)
                self._save_tvmaze_cache(cache_name, payload)
                return payload
            except ApiError as exc:
                if getattr(exc, 'status', None) == 429 and attempt < 2:
                    delay = 2.0 * (attempt + 1)
                    self._tvmaze_backoff(delay)
                    log('TVmaze rate limit reached; backing off %.0fs before retry' % delay,
                        xbmc.LOGWARNING)
                    continue
                raise
        return None

    def _tvmaze_show(self, show_id):
        if not show_id:
            return {}
        cache_name = 'show_v1_%s' % int(show_id)
        try:
            payload = self._tvmaze_request_cached(
                cache_name, self.tvmaze + '/shows/%s' % int(show_id), timeout=12)
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:
            log('TVmaze show metadata unavailable for %s: %s' % (show_id, exc))
            return {}

    def _tvmaze_schedule_ttl(self, day):
        """Return an age-appropriate TTL for one historical schedule day."""
        age = max(0, (datetime.now().date() - day).days)
        if age <= 0:
            return self._TVMAZE_SCHEDULE_TODAY_TTL
        if age == 1:
            return self._TVMAZE_SCHEDULE_YESTERDAY_TTL
        if age <= 7:
            return self._TVMAZE_SCHEDULE_RECENT_TTL
        return self._TVMAZE_SCHEDULE_OLD_TTL

    def _tvmaze_schedule_request(self, day, kind):
        day_text = day.isoformat()
        if kind == 'web':
            endpoint = '/schedule/web'
            params = {'date': day_text}
        else:
            endpoint = '/schedule'
            params = {'country': 'US', 'date': day_text}
        # Keep the historical v1 cache key so 0.2.27 provider responses can be
        # reused immediately after upgrade; only the acceptance TTL changed.
        cache_name = 'schedule_v1_%s_%s' % (kind, day_text)
        try:
            payload = self._tvmaze_request_cached(
                cache_name, self.tvmaze + endpoint, params=params, timeout=12,
                max_age=self._tvmaze_schedule_ttl(day))
            return list(payload or []) if isinstance(payload, list) else []
        except Exception as exc:
            log('TVmaze new-release schedule unavailable for %s%s: %s' %
                (day_text, endpoint, exc), xbmc.LOGWARNING)
            return []

    def _tvmaze_schedule_dates(self, dates):
        """Fetch only the requested broadcast + streaming schedule dates.

        The rolling 30-day cache calls this with all dates only on a true cold
        start. Thereafter it normally contains just today's newly-added date (or
        a few missing dates after a long gap). Raw daily responses have their own
        age-aware provider cache, so even rebuilding the aggregate snapshot does
        not imply redownloading all 60 endpoints.
        """
        dates = list(dict.fromkeys(dates or []))
        by_day = {day.isoformat(): [] for day in dates}
        jobs = [(day, kind) for day in dates for kind in ('broadcast', 'web')]
        workers = min(self._TVMAZE_SCHEDULE_WORKERS, len(jobs))
        if not jobs:
            return []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._tvmaze_schedule_request, day, kind): (day, kind)
                for day, kind in jobs
            }
            for future in as_completed(futures):
                day, kind = futures[future]
                try:
                    by_day[day.isoformat()].extend(future.result() or [])
                except Exception as exc:
                    log('TVmaze schedule worker failed for %s/%s: %s' %
                        (day, kind, exc), xbmc.LOGWARNING)

        return [(day, by_day.get(day.isoformat(), [])) for day in dates]

    def _tvmaze_schedule_range(self, days):
        today = datetime.now().date()
        dates = [today - timedelta(days=offset) for offset in range(days)]
        return self._tvmaze_schedule_dates(dates)

    def _resolve_schedule_shows(self, episodes, show_cache):
        """Resolve only genuinely incomplete parent-show metadata.

        A present ``genres`` field is authoritative even when it is an empty
        list; that represents a genuinely unclassified TVmaze show and must not
        trigger another /shows/:id call. Canonical show requests are required
        only when the field itself is absent or the IMDb external ID is absent.
        """
        episode_shows = []
        needs = set()
        for episode in episodes:
            show = self._schedule_show(episode)
            genres_present = 'genres' in show
            show['_genres_resolved'] = genres_present
            show_id = show.get('id')
            if show_id in show_cache:
                detail = show_cache.get(show_id) or {}
                merged = dict(show)
                merged.update(detail)
                if 'genres' in detail:
                    merged['_genres_resolved'] = True
                show = merged
            else:
                imdb = self._show_imdb(show)
                if show_id and (not genres_present or not imdb.startswith('tt')):
                    needs.add(show_id)
            episode_shows.append(show)

        if needs:
            with ThreadPoolExecutor(max_workers=min(self._TVMAZE_SHOW_WORKERS, len(needs))) as pool:
                futures = {pool.submit(self._tvmaze_show, show_id): show_id for show_id in needs}
                for future in as_completed(futures):
                    show_id = futures[future]
                    try:
                        detail = future.result() or {}
                    except Exception as exc:
                        log('TVmaze show metadata worker failed for %s: %s' % (show_id, exc))
                        detail = {}
                    show_cache[show_id] = detail if isinstance(detail, dict) else {}

            for index, show in enumerate(episode_shows):
                show_id = show.get('id')
                detail = show_cache.get(show_id) if show_id else None
                if detail:
                    merged = dict(show)
                    merged.update(detail)
                    if 'genres' in detail:
                        merged['_genres_resolved'] = True
                    episode_shows[index] = merged
        return episode_shows

    def _tv_release_row(self, episode, show, fallback_day):
        show_id = show.get('id')
        show_title = show.get('name') or ''
        imdb = self._show_imdb(show)
        try:
            season = int(episode.get('season'))
            number = int(episode.get('number'))
        except (TypeError, ValueError):
            return None
        if not show_id or not show_title or not imdb.startswith('tt'):
            return None

        airdate = str(episode.get('airdate') or fallback_day.isoformat())[:10]
        episode_id = str(episode.get('id') or
                         '%s:%s:%s:%s' % (show_id, season, number, airdate))
        show_image = show.get('image') or {}
        episode_image = episode.get('image') or {}
        poster = show_image.get('original') or show_image.get('medium') or ''
        thumbnail = (episode_image.get('original') or episode_image.get('medium') or poster)
        episode_title = episode.get('name') or 'Episode %d' % number
        return {
            'id': episode_id,
            'imdb_id': imdb,
            'name': show_title,
            'title': show_title,
            'episode_title': episode_title,
            'season': season,
            'episode': number,
            'year': airdate[:4],
            'releaseInfo': airdate,
            'poster': poster,
            'thumbnail': thumbnail,
            'background': poster,
            'description': (_plain(episode.get('summary')) or _plain(show.get('summary'))),
            'genres': self._show_genres(show),
            '_genres_resolved': bool(show.get('_genres_resolved', 'genres' in show)),
            '_airdate': airdate,
            '_airstamp': str(episode.get('airstamp') or ''),
        }

    def _tv_release_snapshot_name(self, days):
        return 'release_snapshot_v%d_%dd' % (self._TVMAZE_SNAPSHOT_SCHEMA, int(days))

    def _schedule_days_to_release_rows(self, schedule_days):
        collected = {}
        for fallback_day, schedule in list(schedule_days or []):
            for episode in list(schedule or []):
                if not isinstance(episode, dict):
                    continue
                show = self._schedule_show(episode)
                if not show:
                    continue
                # A present genres field, including [], is authoritative.
                show['_genres_resolved'] = 'genres' in show
                row = self._tv_release_row(episode, show, fallback_day)
                if not row:
                    continue
                key = str(row.get('id') or '').strip()
                if key and key not in collected:
                    collected[key] = row
        rows = list(collected.values())
        rows.sort(key=lambda row: (row.get('_airdate') or '', row.get('_airstamp') or ''),
                  reverse=True)
        return rows

    def _load_tv_release_snapshot(self, days):
        payload = self._load_tvmaze_cache(
            self._tv_release_snapshot_name(days), max_age=self._TVMAZE_SNAPSHOT_MAX_STALE)
        if not isinstance(payload, dict):
            # 0.2.27 already built a complete v1 30-day shared snapshot. Reuse
            # it on upgrade instead of forcing a cold rebuild merely because the
            # rolling snapshot gained new bookkeeping fields in v2.
            legacy_name = 'release_snapshot_v1_%dd' % int(days)
            legacy = self._load_tvmaze_cache(legacy_name, max_age=self._TVMAZE_SNAPSHOT_MAX_STALE)
            if isinstance(legacy, dict) and isinstance(legacy.get('rows'), list):
                try:
                    anchor = datetime.strptime(
                        str(legacy.get('anchor_date') or ''), '%Y-%m-%d').date()
                except Exception:
                    anchor = None
                if anchor is not None and int(legacy.get('days') or 0) == int(days):
                    covered = [(anchor - timedelta(days=offset)).isoformat()
                               for offset in range(int(days))]
                    payload = self._save_tv_release_snapshot(days, legacy.get('rows') or [], covered)
                    log('Migrated TVmaze v1 release snapshot into rolling v2 All Genres pool')
        if not isinstance(payload, dict):
            return None
        rows = payload.get('rows')
        covered = payload.get('covered_dates')
        if not isinstance(rows, list) or not isinstance(covered, list):
            return None
        try:
            payload['built_at'] = float(payload.get('built_at') or 0)
        except (TypeError, ValueError):
            payload['built_at'] = 0.0
        return payload

    def _save_tv_release_snapshot(self, days, rows, covered_dates):
        payload = {
            'anchor_date': datetime.now().date().isoformat(),
            'days': int(days),
            'built_at': time.time(),
            'covered_dates': sorted(set(covered_dates or []), reverse=True),
            'rows': list(rows or []),
        }
        self._save_tvmaze_cache(self._tv_release_snapshot_name(days), payload)
        return payload

    def _refresh_tv_release_snapshot(self, days, snapshot=None):
        """Maintain one rolling raw 30-day All Genres episode cache.

        Cold start: fetch the requested 30 dates once. Warm start: retain every
        overlapping historical date already in the snapshot, fetch only missing
        dates (normally just today's new date after midnight), and occasionally
        refresh today's schedule. No genre-specific network acquisition occurs.
        """
        today = datetime.now().date()
        wanted_dates = [today - timedelta(days=offset) for offset in range(days)]
        wanted_text = [day.isoformat() for day in wanted_dates]
        wanted_set = set(wanted_text)

        old_rows = []
        old_covered = set()
        built_at = 0.0
        if isinstance(snapshot, dict):
            old_rows = [row for row in (snapshot.get('rows') or []) if isinstance(row, dict)]
            old_covered = {str(value) for value in (snapshot.get('covered_dates') or [])}
            try:
                built_at = float(snapshot.get('built_at') or 0)
            except (TypeError, ValueError):
                built_at = 0.0

        missing = [day for day in wanted_dates if day.isoformat() not in old_covered]
        # Today's schedule can gain episodes during the day. Refresh just that
        # date after the short freshness window; historical dates stay local.
        if (today.isoformat() in old_covered and
                (not built_at or time.time() - built_at > self._TVMAZE_SNAPSHOT_FRESH_TTL)):
            if today not in missing:
                missing.append(today)

        if not missing and old_rows:
            rows = [row for row in old_rows if str(row.get('_airdate') or '')[:10] in wanted_set]
            rows.sort(key=lambda row: (row.get('_airdate') or '', row.get('_airstamp') or ''),
                      reverse=True)
            return snapshot, rows

        started = time.monotonic()
        schedule_days = self._tvmaze_schedule_dates(missing)
        refreshed_dates = {day.isoformat() for day, _schedule in schedule_days}
        new_rows = self._schedule_days_to_release_rows(schedule_days)

        # Replace rows for refreshed dates, preserve all other overlapping days.
        preserved = [
            row for row in old_rows
            if str(row.get('_airdate') or '')[:10] in wanted_set
            and str(row.get('_airdate') or '')[:10] not in refreshed_dates
        ]
        combined = {}
        for row in preserved + new_rows:
            key = str(row.get('id') or '').strip()
            if key and key not in combined:
                combined[key] = row
        rows = list(combined.values())
        rows.sort(key=lambda row: (row.get('_airdate') or '', row.get('_airstamp') or ''),
                  reverse=True)

        covered = (old_covered & wanted_set) | refreshed_dates
        payload = self._save_tv_release_snapshot(days, rows, covered)
        log('Refreshed rolling TVmaze %d-day All Genres pool: %d episode(s), %d day(s) fetched in %.2fs' %
            (days, len(rows), len(missing), time.monotonic() - started))
        return payload, rows

    def new_tv_releases(self, days=30, limit=300, genre=None):
        """Filter one genre locally from a rolling raw 30-day TV episode pool.

        Network acquisition is never performed per genre. The shared pool holds
        all usable episodes from the last 30 days; selecting Action, Thriller,
        Drama, Other, or All Genres only applies an in-memory many-to-many filter
        and then caps the visible result set at the requested limit.
        """
        wanted = str(genre or '').strip()
        try:
            days = max(1, int(days))
        except (TypeError, ValueError):
            days = 30
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 300

        snapshot = self._load_tv_release_snapshot(days)
        snapshot, rows = self._refresh_tv_release_snapshot(days, snapshot=snapshot)
        log('Filtering %s from shared TVmaze %d-day All Genres pool (%d episodes)' %
            (wanted or 'All Genres', days, len(rows)))
        return self.filter_tv_release_rows(rows, genre=wanted or None, limit=limit)

    def meta(self, media_type, imdb_id):
        kind = 'movie' if media_type == 'movie' else 'series'
        url = '%s/meta/%s/%s.json' % (self.base, kind, parse.quote(imdb_id, safe=''))
        payload, _, _ = request_json(url, headers=self.headers)
        if not isinstance(payload, dict):
            return {}
        return payload.get('meta') or {}

    def tvmaze_episodes(self, imdb_id):
        """Return {(season, episode): episode-metadata} from TVmaze, live-only."""
        try:
            show, _, _ = request_json(
                self.tvmaze + '/lookup/shows',
                params={'imdb': imdb_id}, headers=self.tvmaze_headers, timeout=20)
            show_id = show.get('id') if isinstance(show, dict) else None
            if not show_id:
                return {}
            episodes, _, _ = request_json(
                self.tvmaze + '/shows/%s/episodes' % show_id,
                params={'specials': 1}, headers=self.tvmaze_headers, timeout=25)
        except Exception as exc:
            log('TVmaze episode metadata unavailable for %s: %s' % (imdb_id, exc))
            return {}
        result = {}
        for ep in episodes if isinstance(episodes, list) else []:
            season = ep.get('season')
            number = ep.get('number')
            if season is None or number is None:
                continue
            image = ep.get('image') or {}
            result[(int(season), int(number))] = {
                'title': ep.get('name') or '',
                'plot': _plain(ep.get('summary')),
                'thumbnail': image.get('original') or image.get('medium') or '',
                'premiered': str(ep.get('airdate') or '')[:10],
            }
        return result
