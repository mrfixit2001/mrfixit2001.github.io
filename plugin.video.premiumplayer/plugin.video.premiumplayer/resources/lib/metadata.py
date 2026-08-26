# -*- coding: utf-8 -*-
import html
import re
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import parse

from .http import request_json
from .utils import ADDON, VERSION, log


_TAG_RE = re.compile(r'<[^>]+>')


def _plain(value):
    if not value:
        return ''
    text = _TAG_RE.sub(' ', str(value))
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


class MetadataClient:
    def __init__(self):
        self.base = (ADDON.getSetting('metadata_base') or 'https://v3-cinemeta.strem.io').rstrip('/')
        self.headers = {
            'User-Agent': 'PremiumPlayer/%s' % VERSION,
            'Cache-Control': 'no-cache, no-store, max-age=0',
            'Pragma': 'no-cache',
        }
        self.tvmaze = 'https://api.tvmaze.com'

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

    def enrich_rows(self, media_type, rows):
        """Enrich only the rows that will be rendered on the current page."""
        rows = [dict(row) for row in list(rows or [])]
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

    def new_tv_releases(self, days=30, limit=80):
        """Return newly aired TV *episodes* from TVmaze, newest first.

        Every schedule entry remains a separate episode.  This intentionally
        does not de-duplicate by show: two newly released episodes of the same
        series are two distinct New Releases results.  Only entries with an
        IMDb series ID and a concrete season/episode number are returned so
        every displayed row can open that exact episode in Premium Player.
        """
        today = datetime.now().date()
        collected = {}
        limit = max(1, int(limit))
        for offset in range(max(1, int(days))):
            day = today - timedelta(days=offset)
            schedule = []
            schedule_requests = (
                ('/schedule', {'country': 'US', 'date': day.isoformat()}),
                ('/schedule/web', {'date': day.isoformat()}),
            )
            for endpoint, request_params in schedule_requests:
                try:
                    payload, _, _ = request_json(
                        self.tvmaze + endpoint, params=request_params,
                        headers=self.headers, timeout=20)
                    if isinstance(payload, list):
                        schedule.extend(payload)
                except Exception as exc:
                    log('TVmaze new-release schedule unavailable for %s%s: %s' %
                        (day, endpoint, exc))
            for episode in schedule:
                show = episode.get('show') or {}
                show_id = show.get('id')
                show_title = show.get('name') or ''
                externals = show.get('externals') or {}
                imdb = str(externals.get('imdb') or '').strip()
                try:
                    season = int(episode.get('season'))
                    number = int(episode.get('number'))
                except (TypeError, ValueError):
                    continue
                if not show_id or not show_title or not imdb.startswith('tt'):
                    continue
                airdate = str(episode.get('airdate') or day.isoformat())[:10]
                episode_id = str(episode.get('id') or
                                 '%s:%s:%s:%s' % (show_id, season, number, airdate))
                if episode_id in collected:
                    continue
                show_image = show.get('image') or {}
                episode_image = episode.get('image') or {}
                poster = show_image.get('original') or show_image.get('medium') or ''
                thumbnail = (episode_image.get('original') or episode_image.get('medium') or
                             poster)
                episode_title = episode.get('name') or 'Episode %d' % number
                collected[episode_id] = {
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
                    'description': (_plain(episode.get('summary')) or
                                    _plain(show.get('summary'))),
                    '_airdate': airdate,
                    '_airstamp': str(episode.get('airstamp') or ''),
                }
                if len(collected) >= limit:
                    break
            if len(collected) >= limit:
                break
        rows = list(collected.values())
        rows.sort(key=lambda row: (row.get('_airdate') or '',
                                   row.get('_airstamp') or ''), reverse=True)
        return rows[:limit]

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
                params={'imdb': imdb_id}, headers=self.headers, timeout=20)
            show_id = show.get('id') if isinstance(show, dict) else None
            if not show_id:
                return {}
            episodes, _, _ = request_json(
                self.tvmaze + '/shows/%s/episodes' % show_id,
                params={'specials': 1}, headers=self.headers, timeout=25)
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
