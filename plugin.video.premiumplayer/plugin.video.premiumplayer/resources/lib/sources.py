# -*- coding: utf-8 -*-
import re
from urllib import parse

from .http import request_json
from .utils import ADDON, VERSION


_SIZE_RE = re.compile(r'(?i)(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB)')
_SEED_RE = re.compile(r'(?:👤|seeders?\s*[:=]?)\s*(\d+)', re.I)
_QUALITY_RE = re.compile(r'(?i)\b(2160p|1080p|720p|576p|480p|4k)\b')


class TorrentSourceClient:
    """Fetch info-hash sources from a Stremio-compatible stream endpoint."""
    def __init__(self):
        self.base = (ADDON.getSetting('source_base') or 'https://torrentio.strem.fun').rstrip('/')
        self.headers = {
            'User-Agent': 'PremiumPlayer/%s' % VERSION,
            'Cache-Control': 'no-cache, no-store, max-age=0',
            'Pragma': 'no-cache',
        }

    def search_text(self, queries):
        """Search a public magnet index by plain text and normalize the hits.

        This is intentionally separate from ``get()``: Torrentio's Stremio
        stream resource is ID-based and cannot perform a free-text torrent
        lookup. New Releases uses this only as a fallback when the show was
        identified but the normal series metadata has no seasons to navigate.
        Every supplied query is attempted and the results are merged by hash.
        """
        queries = [str(q or '').strip() for q in (queries or []) if str(q or '').strip()]
        if not queries:
            return []

        try:
            limit = max(1, int(float(ADDON.getSetting('source_limit') or 80)))
        except Exception:
            limit = 80

        results = []
        seen = set()
        errors = []
        successful_queries = 0
        for query in queries:
            # Magnetz exposes a no-auth JSON keyword search and returns normalized
            # magnet URIs/info-hashes. One page per query keeps this narrow
            # recent-release fallback responsive while still running both forms.
            try:
                payload, _, _ = request_json(
                    'https://magnetz.eu/api/magnets/search',
                    params={'query': query, 'page': 1},
                    headers=self.headers, timeout=35)
            except Exception as exc:
                errors.append(exc)
                continue
            successful_queries += 1
            rows = payload.get('data', []) if isinstance(payload, dict) else []
            for row in rows or []:
                info_hash = str(row.get('info_hash') or '').strip().lower()
                if not re.fullmatch(r'[0-9a-f]{40}', info_hash) or info_hash in seen:
                    continue
                seen.add(info_hash)
                name = str(row.get('name') or row.get('largest_file') or info_hash).strip()
                try:
                    size = int(row.get('size') or 0)
                except Exception:
                    size = 0
                try:
                    seeders = int(row.get('seeders')) if row.get('seeders') not in (None, '') else None
                except Exception:
                    seeders = None
                magnet = str(row.get('magnet_link') or '').strip()
                if not magnet:
                    magnet = self._magnet(info_hash, name, [])
                quality = self._quality(name)
                results.append({
                    'hash': info_hash,
                    'file_idx': None,
                    'filename': str(row.get('largest_file') or name).strip(),
                    'title': name,
                    'name': name,
                    'quality': quality,
                    'quality_rank': self.quality_rank(quality),
                    'size_bytes': size,
                    'seeders': seeders,
                    'magnet': magnet,
                    'tb_cached': None,
                    'rd_cloud': False,
                    'text_query': query,
                })

        if not successful_queries and errors:
            raise errors[-1]
        results.sort(key=lambda row: (
            -int(row.get('quality_rank') or 1),
            -int(row.get('seeders') or 0),
            -int(row.get('size_bytes') or 0),
            str(row.get('title') or '').lower(),
        ))
        return results[:limit]

    def get(self, media):
        if media.get('type') == 'movie':
            video_id = media['imdb_id']
            kind = 'movie'
        else:
            video_id = '%s:%s:%s' % (media['imdb_id'], int(media['season']), int(media['episode']))
            kind = 'series'
        url = '%s/stream/%s/%s.json' % (self.base, kind, parse.quote(video_id, safe=''))
        payload, _, _ = request_json(url, headers=self.headers, timeout=35)
        streams = (payload or {}).get('streams', []) if isinstance(payload, dict) else []
        limit = int(float(ADDON.getSetting('source_limit') or 80))
        results = []
        seen = set()
        for stream in streams:
            info_hash = (stream.get('infoHash') or stream.get('info_hash') or '').strip().lower()
            if not re.fullmatch(r'[0-9a-f]{40}', info_hash):
                continue
            hints = stream.get('behaviorHints') or {}
            filename = hints.get('filename') or self._filename_from_title(stream.get('title'))
            key = (info_hash, stream.get('fileIdx'), filename or '')
            if key in seen:
                continue
            seen.add(key)
            title = stream.get('title') or stream.get('name') or filename or info_hash
            source_text = '%s\n%s' % (stream.get('name') or '', title)
            size = self._hint_size(hints.get('videoSize')) or self._size(source_text)
            quality = self._quality(source_text)
            results.append({
                'hash': info_hash,
                'file_idx': stream.get('fileIdx'),
                'filename': filename or '',
                'title': title,
                'name': stream.get('name') or '',
                'quality': quality,
                'quality_rank': self.quality_rank(quality),
                'size_bytes': size,
                'seeders': self._seeders(source_text),
                'magnet': self._magnet(info_hash, filename, stream.get('sources') or []),
                'tb_cached': None,
                'rd_cloud': False,
            })
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def quality_rank(value):
        return {'4K': 4, '1080P': 3, '720P': 2, 'SD': 1}.get(str(value or '').upper(), 1)

    @staticmethod
    def _filename_from_title(title):
        if not title:
            return ''
        lines = [x.strip() for x in str(title).splitlines() if x.strip()]
        for line in lines:
            if any(ext in line.lower() for ext in ('.mkv', '.mp4', '.avi', '.m2ts', '.ts')):
                return line
        # Torrentio normally puts the release name on the first line; strip only
        # obvious status glyph lines when present.
        for line in lines:
            if not line.startswith(('👤', '💾', '⚙️', '🔗')):
                return line
        return lines[0] if lines else ''

    @staticmethod
    def _quality(text):
        m = _QUALITY_RE.search(text or '')
        if not m:
            return 'SD'
        value = m.group(1).upper()
        if value in ('2160P', '4K'):
            return '4K'
        if value == '1080P':
            return '1080P'
        if value == '720P':
            return '720P'
        return 'SD'

    @staticmethod
    def _seeders(text):
        m = _SEED_RE.search(text or '')
        return int(m.group(1)) if m else None

    @staticmethod
    def _hint_size(value):
        try:
            value = int(value or 0)
            return value if value > 0 else 0
        except Exception:
            return 0

    @staticmethod
    def _size(text):
        matches = _SIZE_RE.findall(text or '')
        if not matches:
            return 0
        value, unit = matches[-1]
        n = float(value)
        mult = {'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}[unit.upper()]
        return int(n * mult)

    @staticmethod
    def _magnet(info_hash, filename, sources):
        params = [('xt', 'urn:btih:%s' % info_hash)]
        if filename:
            params.append(('dn', filename))
        for source in sources or []:
            value = str(source)
            if value.startswith('tracker:'):
                params.append(('tr', value.split(':', 1)[1]))
        return 'magnet:?' + parse.urlencode(params)
