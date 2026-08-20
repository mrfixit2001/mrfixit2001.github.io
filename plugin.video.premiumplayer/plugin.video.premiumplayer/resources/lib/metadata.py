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

    def new_releases(self, media_type, year=None, limit=40):
        """Return Cinemeta's official New/year catalog without local caching."""
        kind = 'movie' if media_type == 'movie' else 'series'
        year = int(year or datetime.now().year)
        url = '%s/catalog/%s/year/genre=%d.json' % (self.base, kind, year)
        payload, _, _ = request_json(url, headers=self.headers)
        rows = (payload or {}).get('metas', []) if isinstance(payload, dict) else []
        rows = list(rows or [])[:max(1, int(limit))]

        # As with search, catalog rows may be sparse. Enrich visible entries live
        # from Cinemeta's meta endpoint, but never persist the result ourselves.
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

    def new_tv_releases(self, days=7, limit=40):
        """Return recently airing TV shows from TVmaze, live-only.

        De-duplicate by show ID and keep the most recent airing first. The row
        shape mirrors Cinemeta enough for Premium Player's list renderer.
        """
        today = datetime.now().date()
        collected = {}
        for offset in range(max(1, int(days))):
            day = today - timedelta(days=offset)
            try:
                schedule, _, _ = request_json(
                    self.tvmaze + '/schedule',
                    params={'country': 'US', 'date': day.isoformat()},
                    headers=self.headers, timeout=20)
            except Exception as exc:
                log('TVmaze new-release schedule unavailable for %s: %s' % (day, exc))
                continue
            for episode in schedule if isinstance(schedule, list) else []:
                show = episode.get('show') or {}
                show_id = show.get('id')
                title = show.get('name') or ''
                if not show_id or not title or show_id in collected:
                    continue
                image = show.get('image') or {}
                premiered = str(show.get('premiered') or '')[:4]
                externals = show.get('externals') or {}
                imdb = externals.get('imdb') or ''
                collected[show_id] = {
                    'id': imdb or ('tvmaze:%s' % show_id),
                    'imdb_id': imdb,
                    'name': title,
                    'title': title,
                    'year': premiered,
                    'releaseInfo': premiered,
                    'poster': image.get('original') or image.get('medium') or '',
                    'background': image.get('original') or image.get('medium') or '',
                    'description': _plain(show.get('summary')),
                    '_airdate': str(episode.get('airdate') or day.isoformat()),
                }
                if len(collected) >= int(limit):
                    break
            if len(collected) >= int(limit):
                break
        rows = list(collected.values())
        rows.sort(key=lambda row: row.get('_airdate') or '', reverse=True)
        return rows[:max(1, int(limit))]

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
