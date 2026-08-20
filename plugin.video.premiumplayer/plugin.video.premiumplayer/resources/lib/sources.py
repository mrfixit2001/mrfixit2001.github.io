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
