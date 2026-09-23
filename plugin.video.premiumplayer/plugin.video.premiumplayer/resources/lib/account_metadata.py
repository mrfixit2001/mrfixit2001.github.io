# -*- coding: utf-8 -*-
"""High-confidence metadata matching for files already present in debrid accounts.

This module is presentation-only: it never changes provider selection or playback URLs.
Matches are intentionally conservative so uncertain filenames remain plain file entries.
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import parse

import xbmcvfs

from .http import request_json
from .metadata import MetadataClient
from .utils import ensure_profile, log

_VIDEO_EXT_RE = re.compile(r'\.(?:mkv|mp4|avi|mov|m4v|wmv|ts|m2ts|mpg|mpeg|webm)$', re.I)
_TV_PATTERNS = [
    re.compile(r'(?i)(?:^|[\s._\-\[(])S(\d{1,2})[ ._\-]*E(\d{1,3})(?:[ ._\-]*E\d{1,3})?'),
    re.compile(r'(?i)(?:^|[\s._\-\[(])(\d{1,2})x(\d{1,3})(?:x\d{1,3})?'),
    re.compile(r'(?i)(?:^|[\s._\-\[(])Season[ ._\-]*(\d{1,2})[ ._\-]*(?:Episode|Ep)[ ._\-]*(\d{1,3})'),
    # Common hand-named form with the E omitted: S1-4 => S01E04.
    # This does not match season ranges such as S01-S08 because the second
    # component there begins with S, not a digit.
    re.compile(r'(?i)(?:^|[\s._\-\[(])S(\d{1,2})[ ._\-]+(\d{1,3})(?=$|[\s._\-\])])'),
]
# Common scene/library form: "Show Name - 101 - Episode Title" => S01E01.
_COMPACT_EP_RE = re.compile(r'(?i)(?:^|[\s._\-\[(])(\d)(\d{2})(?=$|[\s._\-\])])')
_SEASON_RE = re.compile(r'(?i)(?:^|[\s._\-\[(])(?:S|Season[ ._\-]*)(\d{1,2})(?=$|[\s._\-\])])')
_SEASON_ONLY_RE = re.compile(r'(?i)^\s*(?:S|Season[ ._\-]*)(\d{1,2})(?:\s*[-–]\s*(?:S)?\d{1,2})?\s*$')
_EP_ONLY_RE = re.compile(r'(?i)(?:^|[\s._\-\[(])(?:E|Ep(?:isode)?)[ ._\-]*(\d{1,3})(?=$|[\s._\-\])])')
_LEADING_EP_RE = re.compile(r'(?i)^\s*(\d{1,3})(?=$|[\s._\-\])])')
_YEAR_RE = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')
_COMPLETE_SERIES_RE = re.compile(r'(?i)\b(?:the\s+)?complete(?:\s+(?:tv\s+)?)?series\b|\bcomplete\s+collection\b|\bseries\s+collection\b')

# These are release/encode markers, not title words.  We cut only when a marker
# appears after a non-empty title, which keeps the cleanup conservative.
_RELEASE_CUT_RE = re.compile(
    r'(?i)(?:^|\s)(?:'
    r'(?:480|576|720|1080|1440|2160)p|4k|uhd|hdr10\+?|dolby\s*vision|dv|'
    r'blu[ ._-]?ray|b[dr]rip|dvd[ ._-]?rip|web[ ._-]?dl|web[ ._-]?rip|hdtv|pdtv|webrip|'
    r'x26[45]|h\.?26[45]|hevc|avc|av1|xvid|divx|10bit|8bit|'
    r'aac(?:2\.0|5\.1)?|ac3|eac3|ddp?5\.1|dts(?:-hd)?|truehd|atmos|'
    r'\d(?:\.\d)?ch|multi(?:audio)?|proper|repack|remux|internal|limited|'
    r'rartv|yts(?:\s*mx)?'
    r')\b'
)

_CACHE_SCHEMA = 1
_CACHE_FILE = 'account_metadata_cache.json'


def _cache_path():
    return os.path.join(ensure_profile(), _CACHE_FILE)


def _empty_cache():
    return {'schema': _CACHE_SCHEMA, 'providers': {}}


def _load_cache():
    path = _cache_path()
    if not xbmcvfs.exists(path):
        return _empty_cache()
    try:
        f = xbmcvfs.File(path, 'r')
        try:
            raw = f.read()
        finally:
            f.close()
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict) or data.get('schema') != _CACHE_SCHEMA:
            return _empty_cache()
        if not isinstance(data.get('providers'), dict):
            data['providers'] = {}
        return data
    except Exception as exc:
        log('Could not read account metadata cache: %s' % exc)
        return _empty_cache()


def _save_cache(data):
    try:
        f = xbmcvfs.File(_cache_path(), 'w')
        try:
            f.write(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
        finally:
            f.close()
    except Exception as exc:
        log('Could not save account metadata cache: %s' % exc)


def clear_account_metadata_cache(provider=None):
    """Clear all cached metadata, or only one provider's cache."""
    if not provider:
        try:
            path = _cache_path()
            if xbmcvfs.exists(path):
                xbmcvfs.delete(path)
        except Exception:
            pass
        return
    data = _load_cache()
    data.get('providers', {}).pop(str(provider), None)
    _save_cache(data)


def update_account_item_count(provider, scope, count):
    """Invalidate one provider's metadata matches when a browsed account count changes.

    Counts are tracked by collection (for example TorBox torrents vs usenet), so
    opening another collection for the first time does not invalidate already
    cached successful matches. Once a collection has been observed, any count
    change clears that provider's successful-match cache as requested.
    """
    if not provider:
        return False
    provider = str(provider)
    scope = str(scope or 'items')
    try:
        count = int(count)
    except Exception:
        return False
    data = _load_cache()
    providers = data.setdefault('providers', {})
    state = providers.setdefault(provider, {'counts': {}, 'matches': {}})
    counts = state.setdefault('counts', {})
    previous = counts.get(scope)
    changed = previous is not None and int(previous) != count
    counts[scope] = count
    if changed:
        state['matches'] = {}
        log('Account metadata cache invalidated for %s: %s count changed %s -> %s' %
            (provider, scope, previous, count))
    _save_cache(data)
    return changed


def _cache_key(kind, title, year=None):
    return '%s|%s|%s' % (str(kind or ''), _norm(title), '' if year is None else int(year))


def _encode_resolution(value):
    if not value:
        return None
    encoded = dict(value)
    episodes = {}
    for key, row in (value.get('episodes') or {}).items():
        if isinstance(key, (tuple, list)) and len(key) == 2:
            episodes['%s:%s' % (int(key[0]), int(key[1]))] = row
        else:
            episodes[str(key)] = row
    encoded['episodes'] = episodes
    return encoded


def _decode_resolution(value):
    if not isinstance(value, dict):
        return None
    decoded = dict(value)
    episodes = {}
    for key, row in (value.get('episodes') or {}).items():
        try:
            season, episode = str(key).split(':', 1)
            episodes[(int(season), int(episode))] = row
        except Exception:
            continue
    decoded['episodes'] = episodes
    return decoded


def _display_text(value):
    value = os.path.basename(str(value or '').strip())
    value = _VIDEO_EXT_RE.sub('', value)
    value = value.replace('.', ' ').replace('_', ' ')
    value = re.sub(r'\s+', ' ', value).strip(' -_.[](){}')
    return value


def _norm(value):
    return re.sub(r'[^a-z0-9]+', '', str(value or '').casefold())


def _title_before(text, start):
    value = text[:start].strip(' -_.[](){}')
    return re.sub(r'\s+', ' ', value).strip()


def _release_year(value):
    if isinstance(value, dict):
        value = value.get('releaseInfo') or value.get('year') or value.get('released') or ''
    match = _YEAR_RE.search(str(value or ''))
    return int(match.group(1)) if match else None


def _series_year_hint(*values):
    """Use a year from a container/folder name to disambiguate same-title shows."""
    for value in values:
        text = _display_text(value)
        match = _YEAR_RE.search(text)
        if match:
            return int(match.group(1))
    return None


def _strip_release_noise(value):
    """Return a conservative title guess with common release suffixes removed."""
    text = _display_text(value)
    if not text:
        return ''

    # A year in parentheses/range is metadata, not part of a conventional release title.
    year = _YEAR_RE.search(text)
    year_pos = year.start() if year else None

    complete = _COMPLETE_SERIES_RE.search(text)
    cut_positions = []
    if complete and _title_before(text, complete.start()):
        cut_positions.append(complete.start())
    release = _RELEASE_CUT_RE.search(text)
    if release and _title_before(text, release.start()):
        cut_positions.append(release.start())
    if year_pos is not None and _title_before(text, year_pos):
        cut_positions.append(year_pos)

    if cut_positions:
        text = text[:min(cut_positions)]

    # Trim a trailing scene group after a clear separator only; do not guess at
    # arbitrary hyphenated words inside real titles.
    text = re.sub(r'\s+-\s+[A-Za-z0-9][A-Za-z0-9._-]{1,20}$', '', text)
    return re.sub(r'\s+', ' ', text).strip(' -_.[](){}')


def _series_title_guess(value):
    """Extract a likely series title from a container/folder label, without lookup."""
    text = _display_text(value)
    if not text or _SEASON_ONLY_RE.match(text):
        return ''
    for pattern in _TV_PATTERNS:
        match = pattern.search(text)
        if match:
            return _title_before(text, match.start())
    match = _SEASON_RE.search(text)
    if match:
        title = _title_before(text, match.start())
        if title:
            return title
    complete = _COMPLETE_SERIES_RE.search(text)
    if complete:
        title = _title_before(text, complete.start())
        if title:
            return title
    return _strip_release_noise(text)


def _series_title_from_context(parent_name='', root_name=''):
    # The provider's root torrent/download name is the strongest title context.
    for value in (root_name, parent_name):
        if not value:
            continue
        # Preserve hierarchy long enough to inspect useful outer path segments.
        parts = [p for p in re.split(r'[\\/]+', str(value)) if p]
        for part in parts:
            title = _series_title_guess(part)
            if title and len(_norm(title)) >= 2:
                return title
    return ''


def _season_from_context(parent_name='', root_name=''):
    # Prefer the nearest/current folder, then fall back to a root season pack.
    for value in (parent_name, root_name):
        if not value:
            continue
        parts = [p for p in re.split(r'[\\/]+', str(value)) if p]
        for part in reversed(parts):
            text = _display_text(part)
            match = _SEASON_ONLY_RE.match(text) or _SEASON_RE.search(text)
            if match:
                try:
                    return int(match.group(1))
                except (TypeError, ValueError):
                    pass
    return None


def looks_like_series_tree(entries):
    """Return True when provider file paths contain strong TV structure evidence."""
    for entry in entries or []:
        if isinstance(entry, dict):
            path = entry.get('path') or entry.get('name') or entry.get('short_name') or ''
        else:
            path = entry or ''
        path = str(path)
        if not path:
            continue
        for part in re.split(r'[\\/]+', path):
            text = _display_text(part)
            if _SEASON_ONLY_RE.match(text):
                return True
            if any(pattern.search(text) for pattern in _TV_PATTERNS):
                return True
            # Compact numbering is only structural evidence when there is some
            # surrounding title text, not for a random 3-digit filename.
            m = _COMPACT_EP_RE.search(text)
            if m and (_title_before(text, m.start()) or text[m.end():].strip(' -_.[](){}')):
                return True
    return False


def _candidate(name, parent_name='', root_name='', container=False, series_hint=False,
               known_context=None):
    """Return a conservative candidate dict or None."""
    text = _display_text(name)
    if not text:
        return None

    known_context = known_context or {}
    known_series = str(known_context.get('media_type') or '') == 'series'
    context_title = (known_context.get('title') if known_series else '') or _series_title_from_context(parent_name, root_name)
    context_year = known_context.get('year') if known_series else None
    if context_year in ('', None):
        context_year = _series_year_hint(root_name, parent_name)
    try:
        context_year = int(context_year) if context_year not in ('', None) else None
    except Exception:
        context_year = None
    context_season = known_context.get('season') if known_series else None
    try:
        context_season = int(context_season) if context_season not in ('', None) else None
    except Exception:
        context_season = None
    if context_season is None:
        context_season = _season_from_context(parent_name, root_name)

    for pattern in _TV_PATTERNS:
        match = pattern.search(text)
        if match:
            title = _title_before(text, match.start()) or context_title
            if title:
                return {'kind': 'episode', 'title': title,
                        'season': int(match.group(1)), 'episode': int(match.group(2)),
                        'year': context_year, 'imdb': known_context.get('imdb') if known_series else None}

    # Only accept compact NNN numbering when delimiters isolate it.  Root/folder
    # context may supply the show title when the file itself is just "101 - Pilot".
    match = _COMPACT_EP_RE.search(text)
    if match:
        title = _title_before(text, match.start()) or context_title
        if title and len(_norm(title)) >= 2:
            return {'kind': 'episode', 'title': title,
                    'season': int(match.group(1)), 'episode': int(match.group(2)),
                    'year': context_year, 'imdb': known_context.get('imdb') if known_series else None}

    # Season folders often contain files named only "E01 - ..." or "01 - ...".
    # Accept those only when the hierarchy supplies both an unambiguous show title
    # and a season number.
    if context_title and context_season is not None:
        match = _EP_ONLY_RE.search(text)
        if match:
            return {'kind': 'episode', 'title': context_title,
                    'season': int(context_season), 'episode': int(match.group(1)),
                    'year': context_year, 'imdb': known_context.get('imdb') if known_series else None}
        match = _LEADING_EP_RE.search(text)
        if match:
            episode = int(match.group(1))
            if 0 < episode <= 300:
                return {'kind': 'episode', 'title': context_title,
                        'season': int(context_season), 'episode': episode,
                        'year': context_year, 'imdb': known_context.get('imdb') if known_series else None}

    # Season-pack/folder names may safely receive show-level metadata.  A bare
    # "Season 1" inherits the show from its root torrent/folder context.
    match = _SEASON_RE.search(text)
    if match:
        title = _title_before(text, match.start()) or context_title
        if title and len(_norm(title)) >= 2:
            return {'kind': 'series', 'title': title, 'season': int(match.group(1)),
                    'year': context_year or _series_year_hint(text, root_name, parent_name),
                    'imdb': known_context.get('imdb') if known_series else None}

    # Movie matching deliberately requires a four-digit year and an exact
    # title/year metadata hit.  Container series years are handled below first
    # when strong TV evidence exists.
    year_matches = list(_YEAR_RE.finditer(text))
    if year_matches and not series_hint and not _COMPLETE_SERIES_RE.search(text):
        match = year_matches[-1]
        title = _title_before(text, match.start())
        if title and len(_norm(title)) >= 2:
            return {'kind': 'movie', 'title': title, 'year': int(match.group(1))}

    if container:
        title = _series_title_guess(text)
        if title and len(_norm(title)) >= 2:
            # Explicit TV structure/wording is sufficient to constrain the lookup
            # to series. Otherwise search both movie and series and accept only a
            # single exact title match across both media types.
            strong_series = bool(series_hint or _COMPLETE_SERIES_RE.search(text))
            return {'kind': 'series' if (strong_series or known_series) else 'unknown',
                    'title': title,
                    'season': context_season,
                    'year': context_year or _series_year_hint(text, root_name, parent_name),
                    'imdb': known_context.get('imdb') if known_series else None}
    return None


def _basic_search(client, media_type, query):
    kind = 'movie' if media_type == 'movie' else 'series'
    encoded = parse.quote(str(query or ''), safe='')
    url = '%s/catalog/%s/top/search=%s.json' % (client.base, kind, encoded)
    payload, _, _ = request_json(url, headers=client.headers)
    if not isinstance(payload, dict):
        return []
    return list(payload.get('metas') or [])[:25]


def _exact_rows(rows, wanted_title, wanted_year=None):
    wanted = _norm(wanted_title)
    exact = [row for row in rows if _norm(row.get('name') or row.get('title')) == wanted]
    if wanted_year is not None:
        exact = [row for row in exact if _release_year(row) == int(wanted_year)]
    return exact


def _exact_result(rows, wanted_title, wanted_year=None):
    exact = _exact_rows(rows, wanted_title, wanted_year)
    # Multiple exact candidates are ambiguous and intentionally rejected unless
    # a year discriminator narrowed them to one.
    if len(exact) != 1:
        return None
    return exact[0]


def _art(meta, episode=None):
    episode = episode or {}
    poster = meta.get('poster') or ''
    background = meta.get('background') or ''
    thumb = episode.get('thumbnail') or poster
    return {
        'poster': poster,
        'thumb': thumb,
        'icon': thumb or poster,
        'fanart': background,
        'landscape': episode.get('thumbnail') or background,
    }


def _resolve_group(key, candidates):
    """Resolve one unique title/year group and return metadata shared by candidates."""
    kind, title, year = key
    client = MetadataClient()
    known_imdb = next((str(c.get('imdb')) for c in candidates if c.get('imdb')), '')

    if known_imdb.startswith('tt') and kind != 'movie':
        media_type = 'series'
        meta = client.meta(media_type, known_imdb) or {}
        if _norm(meta.get('name') or meta.get('title')) != _norm(title):
            return None
        if year is not None:
            resolved_year = _release_year(meta)
            if resolved_year and resolved_year != int(year):
                return None
        result = {'meta': meta, 'imdb': known_imdb, 'episodes': {}, 'episodes_loaded': False,
                  'media_type': media_type}
        if any(c.get('kind') == 'episode' for c in candidates):
            result['episodes'] = client.tvmaze_episodes(known_imdb)
            result['episodes_loaded'] = True
        return result

    if kind == 'unknown':
        exact = []
        for media_type in ('series', 'movie'):
            rows = _basic_search(client, media_type, title)
            for row in _exact_rows(rows, title, year):
                exact.append((media_type, row))
        if len(exact) != 1:
            return None
        media_type, row = exact[0]
    else:
        media_type = 'movie' if kind == 'movie' else 'series'
        rows = _basic_search(client, media_type, title)
        row = _exact_result(rows, title, year)
        if not row:
            return None

    imdb = row.get('imdb_id') or row.get('id')
    if not imdb or not str(imdb).startswith('tt'):
        return None
    meta = client.meta(media_type, str(imdb)) or {}
    if _norm(meta.get('name') or meta.get('title')) != _norm(title):
        return None
    if year is not None:
        resolved_year = _release_year(meta) or _release_year(row)
        if resolved_year != int(year):
            return None

    result = {'meta': meta, 'imdb': str(imdb), 'episodes': {}, 'episodes_loaded': False,
              'media_type': media_type}
    if media_type == 'series' and any(c.get('kind') == 'episode' for c in candidates):
        # One TVmaze lookup supplies richer summaries/thumbnails for every episode
        # in this show and doubles as an independent exact S/E confirmation source.
        result['episodes'] = client.tvmaze_episodes(str(imdb))
        result['episodes_loaded'] = True
    return result


def match_entries(names, parent_name='', root_name='', container=False, series_hint=False,
                  provider='', known_context=None):
    """Return {original_name: {'info': ..., 'art': ...}} for confident matches only.

    ``root_name`` preserves the outer torrent/folder title while ``parent_name``
    describes the current folder.  This lets a file such as ``01 - Pilot.mkv``
    inherit both its show and season from ``Show Name/Season 1``.

    Network work is grouped by unique title and performed concurrently, so a
    season directory produces one show metadata lookup rather than one search per
    episode.
    """
    parsed = {}
    groups = {}
    for name in names or []:
        candidate = _candidate(name, parent_name=parent_name, root_name=root_name,
                               container=container, series_hint=series_hint,
                               known_context=known_context)
        if not candidate:
            continue
        lookup_kind = candidate['kind'] if candidate['kind'] in ('movie', 'unknown') else 'series'
        key = (lookup_kind, candidate['title'], candidate.get('year'))
        parsed[name] = (candidate, key)
        groups.setdefault(key, []).append(candidate)
    if not groups:
        return {}

    resolved = {}
    unresolved = dict(groups)
    cache_data = None
    cache_state = None
    cache_dirty = False
    if provider:
        cache_data = _load_cache()
        cache_state = cache_data.setdefault('providers', {}).setdefault(
            str(provider), {'counts': {}, 'matches': {}})
        matches = cache_state.setdefault('matches', {})
        for key in list(unresolved):
            cached = _decode_resolution(matches.get(_cache_key(*key)))
            if cached:
                if (any(c.get('kind') == 'episode' for c in unresolved[key]) and
                        not cached.get('episodes_loaded')):
                    try:
                        imdb = str(cached.get('imdb') or '')
                        if imdb.startswith('tt'):
                            cached['episodes'] = MetadataClient().tvmaze_episodes(imdb)
                            cached['episodes_loaded'] = True
                            matches[_cache_key(*key)] = _encode_resolution(cached)
                            cache_dirty = True
                    except Exception as exc:
                        log('Could not enrich cached episode metadata for %s: %s' % (key[1], exc))
                resolved[key] = cached
                unresolved.pop(key, None)

    if not unresolved:
        workers = 0
    else:
        workers = min(6, len(unresolved))
    try:
        if workers:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_resolve_group, key, values): key for key, values in unresolved.items()}
                for future in as_completed(futures):
                    key = futures[future]
                    try:
                        value = future.result()
                    except Exception as exc:
                        log('Account metadata match failed for %s: %s' % (key[1], exc))
                        value = None
                    if value:
                        resolved[key] = value
                        if cache_state is not None:
                            cache_state.setdefault('matches', {})[_cache_key(*key)] = _encode_resolution(value)
                            resolved_kind = 'movie' if value.get('media_type') == 'movie' else 'series'
                            alias_years = {key[2], _release_year(value.get('meta') or {})}
                            for alias_year in alias_years:
                                cache_state.setdefault('matches', {})[
                                    _cache_key(resolved_kind, key[1], alias_year)] = _encode_resolution(value)
                            cache_dirty = True
    except Exception as exc:
        log('Account metadata matching failed: %s' % exc)
        return {}

    if cache_data is not None and cache_state is not None and cache_dirty:
        _save_cache(cache_data)

    output = {}
    for original, (candidate, key) in parsed.items():
        data = resolved.get(key)
        if not data:
            continue
        meta = data['meta']
        canonical = meta.get('name') or meta.get('title') or candidate['title']
        genres = meta.get('genres') or []
        resolved_type = data.get('media_type') or ('movie' if candidate['kind'] == 'movie' else 'series')

        if candidate['kind'] == 'movie' or (candidate['kind'] == 'unknown' and resolved_type == 'movie'):
            info = {
                'title': canonical,
                'plot': meta.get('description') or meta.get('plot') or '',
                'mediatype': 'movie',
            }
            year = candidate.get('year') or _release_year(meta)
            if year:
                info['year'] = int(year)
            if genres:
                info['genre'] = ' / '.join(str(x) for x in genres) if isinstance(genres, (list, tuple)) else str(genres)
            output[original] = {'info': info, 'art': _art(meta),
                                'context': {'media_type': 'movie', 'title': canonical,
                                            'imdb': data.get('imdb'), 'year': info.get('year')}}
            continue

        if candidate['kind'] in ('series', 'unknown'):
            info = {
                'title': canonical,
                'tvshowtitle': canonical,
                'plot': meta.get('description') or meta.get('plot') or '',
                'mediatype': 'tvshow',
            }
            if candidate.get('season') is not None:
                info['season'] = int(candidate.get('season'))
            year = candidate.get('year') or _release_year(meta)
            if year:
                info['year'] = int(year)
            if genres:
                info['genre'] = ' / '.join(str(x) for x in genres) if isinstance(genres, (list, tuple)) else str(genres)
            output[original] = {'info': info, 'art': _art(meta),
                                'context': {'media_type': 'series', 'title': canonical,
                                            'imdb': data.get('imdb'), 'year': info.get('year'),
                                            'season': info.get('season')}}
            continue

        season = int(candidate['season'])
        episode = int(candidate['episode'])
        video = next((v for v in (meta.get('videos') or [])
                      if str(v.get('season', '')).isdigit() and str(v.get('episode', '')).isdigit()
                      and int(v.get('season')) == season and int(v.get('episode')) == episode), None)
        richer = data.get('episodes', {}).get((season, episode), {})
        # High confidence does not require both providers to enumerate the episode.
        # Either Cinemeta or TVmaze confirming the exact S/E pair is sufficient.
        if not video and not richer:
            continue
        source_ep = richer or video or {}
        ep_title = richer.get('title') or (video or {}).get('title') or (video or {}).get('name') or 'Episode %d' % episode
        plot = richer.get('plot') or (video or {}).get('overview') or (video or {}).get('description') or ''
        info = {
            'title': ep_title,
            'tvshowtitle': canonical,
            'season': season,
            'episode': episode,
            'plot': plot,
            'mediatype': 'episode',
        }
        premiered = richer.get('premiered') or str((video or {}).get('released') or '')[:10]
        if premiered:
            info['premiered'] = premiered
        output[original] = {'info': info, 'art': _art(meta, source_ep),
                            'context': {'media_type': 'series', 'title': canonical,
                                        'imdb': data.get('imdb'), 'year': _release_year(meta),
                                        'season': season}}
    return output


def media_sort_key(name, parent_name='', root_name='', known_context=None):
    """Sort media-like names numerically by S/E when possible, then naturally.

    This deliberately understands the same imperfect episode forms as matching,
    including S1-4 and S1-E10, so source spelling does not force lexical order.
    """
    candidate = _candidate(name, parent_name=parent_name, root_name=root_name,
                           container=True, series_hint=True, known_context=known_context)
    if candidate:
        if candidate.get('kind') == 'episode':
            return (0, int(candidate.get('season') or 0), int(candidate.get('episode') or 0), _norm(name))
        if candidate.get('kind') == 'series' and candidate.get('season') is not None:
            return (1, int(candidate.get('season') or 0), 0, _norm(name))
    text = _display_text(name).casefold()
    natural = tuple((0, int(part)) if part.isdigit() else (1, part)
                    for part in re.split(r'(\d+)', text))
    return (2, 0, 0, natural)
