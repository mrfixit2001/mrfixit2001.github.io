# -*- coding: utf-8 -*-
import json
import os
import re
import time
import uuid
from urllib import parse

import xbmc
import xbmcaddon
import xbmcvfs

ADDON_ID = 'plugin.video.premiumplayer'
ADDON = xbmcaddon.Addon(ADDON_ID)
ADDON_NAME = ADDON.getAddonInfo('name')
VERSION = ADDON.getAddonInfo('version')
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
TEMP_ROOT = xbmcvfs.translatePath('special://temp/premiumplayer')
NEW_RELEASES_CACHE_SCHEMA = 5
NEW_RELEASES_NAV_STATE = os.path.join(TEMP_ROOT, 'new_releases_nav_state.json')
NEW_RELEASES_SCHEMA_MARKER = os.path.join(TEMP_ROOT, 'new_releases_cache_schema.json')
VIDEO_EXTS = ('.mkv', '.mp4', '.avi', '.mov', '.m4v', '.ts', '.m2ts', '.webm', '.mpg', '.mpeg', '.wmv')
ARCHIVE_EXTS = ('.zip', '.rar', '.tar', '.7z')


def ensure_profile():
    if not xbmcvfs.exists(PROFILE):
        xbmcvfs.mkdirs(PROFILE)
    return PROFILE


def ensure_session_dir():
    if not xbmcvfs.exists(TEMP_ROOT):
        xbmcvfs.mkdirs(TEMP_ROOT)
    path = os.path.join(TEMP_ROOT, 'sessions')
    if not xbmcvfs.exists(path):
        xbmcvfs.mkdirs(path)
    return path


def setting_bool(key, default=False):
    raw = ADDON.getSetting(key)
    if raw == '':
        return default
    return raw.lower() == 'true'


def setting_int(key, default=0):
    try:
        return int(float(ADDON.getSetting(key)))
    except Exception:
        return default


def log(message, level=xbmc.LOGINFO):
    if level == xbmc.LOGDEBUG and not setting_bool('debug', False):
        return
    xbmc.log('[%s] %s' % (ADDON_NAME, message), level)


def plugin_url(base, **params):
    return '%s?%s' % (base, parse.urlencode({k: v for k, v in params.items() if v is not None}))


def save_session(data):
    """Persist only short-lived cross-invocation state in Kodi's temp area.

    Torrent/search result lists are deliberately not stored in the add-on profile.
    Kodi starts a fresh plugin invocation when a source row is clicked, so a tiny
    temporary hand-off file is necessary; it is automatically aged out.
    """
    folder = ensure_session_dir()
    sid = uuid.uuid4().hex
    path = os.path.join(folder, sid + '.json')
    f = xbmcvfs.File(path, 'w')
    try:
        f.write(json.dumps(data, ensure_ascii=False))
    finally:
        f.close()
    _cleanup_sessions(folder)
    return sid


def load_session(sid):
    path = os.path.join(ensure_session_dir(), sid + '.json')
    if not xbmcvfs.exists(path):
        log('New Releases cache MISS: %s' % os.path.basename(path))
        return None
    f = xbmcvfs.File(path, 'r')
    try:
        raw = f.read()
    finally:
        f.close()
    return json.loads(raw) if raw else None


def delete_session(sid):
    path = os.path.join(ensure_session_dir(), sid + '.json')
    try:
        if xbmcvfs.exists(path):
            xbmcvfs.delete(path)
    except Exception:
        pass



def _delete_new_releases_temp_files(include_nav_state=False, include_schema_marker=False):
    """Delete New Releases temp files from every cache schema used by the addon.

    Historical builds used unversioned names such as ``new_releases_movie.json``
    and ``new_releases_series_action.json``; newer builds use versioned names.
    Enumerating by prefix makes cleanup an active deletion, not merely a cache-key
    change that leaves stale files behind.
    """
    if not xbmcvfs.exists(TEMP_ROOT):
        return 0
    try:
        _dirs, files = xbmcvfs.listdir(TEMP_ROOT)
    except Exception as exc:
        log('Could not enumerate New Releases temp files: %s' % exc, xbmc.LOGWARNING)
        return 0

    nav_name = os.path.basename(NEW_RELEASES_NAV_STATE)
    marker_name = os.path.basename(NEW_RELEASES_SCHEMA_MARKER)
    removed = 0
    for name in files:
        text = str(name)
        if not text.startswith('new_releases_') or not text.endswith('.json'):
            continue
        if text == nav_name and not include_nav_state:
            continue
        if text == marker_name and not include_schema_marker:
            continue
        path = os.path.join(TEMP_ROOT, text)
        try:
            if xbmcvfs.exists(path):
                deleted = xbmcvfs.delete(path)
                if deleted is not False and not xbmcvfs.exists(path):
                    removed += 1
                elif not xbmcvfs.exists(path):
                    removed += 1
                else:
                    log('New Releases temp file still exists after delete attempt: %s' % text,
                        xbmc.LOGWARNING)
        except Exception as exc:
            log('Could not delete New Releases temp file %s: %s' % (text, exc), xbmc.LOGWARNING)
    return removed


def ensure_new_releases_cache_schema():
    """Actively purge old New Releases caches once when the cache schema changes.

    This deliberately deletes the old files themselves. Merely changing the
    filename/schema is insufficient because Kodi upgrades preserve special://temp
    across addon invocations and stale files can otherwise linger indefinitely.
    The stale navigation state is purged at the same time so the first navigation
    after an upgrade starts from a known boundary state.
    """
    if not xbmcvfs.exists(TEMP_ROOT):
        xbmcvfs.mkdirs(TEMP_ROOT)

    current_schema = None
    if xbmcvfs.exists(NEW_RELEASES_SCHEMA_MARKER):
        try:
            f = xbmcvfs.File(NEW_RELEASES_SCHEMA_MARKER, 'r')
            try:
                raw = f.read()
            finally:
                f.close()
            data = json.loads(raw) if raw else {}
            if isinstance(data, dict):
                current_schema = data.get('schema')
        except Exception:
            current_schema = None

    if current_schema == NEW_RELEASES_CACHE_SCHEMA:
        return False

    removed = _delete_new_releases_temp_files(
        include_nav_state=True, include_schema_marker=True)
    log('New Releases cache schema migration %s -> %s; actively removed %d old temp file(s)' %
        (current_schema if current_schema is not None else 'legacy',
         NEW_RELEASES_CACHE_SCHEMA, removed))

    try:
        f = xbmcvfs.File(NEW_RELEASES_SCHEMA_MARKER, 'w')
        try:
            f.write(json.dumps({
                'schema': NEW_RELEASES_CACHE_SCHEMA,
                'addon_version': VERSION,
                'updated': int(time.time()),
            }))
        finally:
            f.close()
    except Exception as exc:
        # If the marker cannot be written, fail safe: the next invocation will
        # attempt the purge again rather than trusting potentially stale data.
        log('Could not write New Releases cache schema marker: %s' % exc, xbmc.LOGWARNING)
    return True

def _new_releases_cache_path(media_type, genre=None):
    if not xbmcvfs.exists(TEMP_ROOT):
        xbmcvfs.mkdirs(TEMP_ROOT)
    kind = 'series' if str(media_type or '') == 'series' else 'movie'
    genre_text = str(genre or '').strip()
    if not genre_text:
        genre_key = 'all'
    else:
        genre_key = re.sub(r'[^a-z0-9]+', '_', genre_text.casefold()).strip('_') or 'all'
    return os.path.join(TEMP_ROOT, 'new_releases_v%d_%s_%s.json' % (NEW_RELEASES_CACHE_SCHEMA, kind, genre_key))


def load_new_releases_cache(media_type, genre=None):
    """Load one temporary, genre-specific New Releases result set.

    Each Movie/TV genre keeps its complete three-page result set under
    special://temp so Kodi page navigation is local and immediate.  The shared
    New Releases menu is the lifecycle boundary that clears these files.
    """
    path = _new_releases_cache_path(media_type, genre)
    if not xbmcvfs.exists(path):
        return None
    try:
        f = xbmcvfs.File(path, 'r')
        try:
            raw = f.read()
        finally:
            f.close()
        rows = json.loads(raw) if raw else None
        if isinstance(rows, list):
            log('New Releases cache HIT: %s (%d rows)' % (os.path.basename(path), len(rows)))
            return rows
        return None
    except Exception as exc:
        log('Could not load New Releases cache: %s' % exc, xbmc.LOGWARNING)
        return None


def save_new_releases_cache(media_type, rows, genre=None):
    """Persist one complete (up to 300-row) genre-specific result set."""
    path = _new_releases_cache_path(media_type, genre)
    try:
        f = xbmcvfs.File(path, 'w')
        try:
            f.write(json.dumps(list(rows or []), ensure_ascii=False))
        finally:
            f.close()
        return True
    except Exception as exc:
        log('Could not save New Releases cache: %s' % exc, xbmc.LOGWARNING)
        return False


def clear_new_releases_cache():
    """Actively delete every Movie/TV New Releases result cache, old or current."""
    removed = _delete_new_releases_temp_files(
        include_nav_state=False, include_schema_marker=False)
    log('Cleared %d New Releases result cache file(s)' % removed)
    return removed

def _load_new_releases_nav_state():
    if not xbmcvfs.exists(NEW_RELEASES_NAV_STATE):
        return {}
    try:
        f = xbmcvfs.File(NEW_RELEASES_NAV_STATE, 'r')
        try:
            raw = f.read()
        finally:
            f.close()
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_new_releases_nav_state(descended):
    if not xbmcvfs.exists(TEMP_ROOT):
        xbmcvfs.mkdirs(TEMP_ROOT)
    data = {
        'schema': NEW_RELEASES_CACHE_SCHEMA,
        'descended': bool(descended),
        'updated': int(time.time()),
    }
    f = xbmcvfs.File(NEW_RELEASES_NAV_STATE, 'w')
    try:
        f.write(json.dumps(data))
    finally:
        f.close()


def mark_new_releases_menu_rendered():
    """Record a freshly rendered shared New Releases menu.

    The menu route itself clears all result caches. ``descended=False`` means
    the first Movie/TV child opened from this freshly rendered menu must not
    clear a second time.
    """
    _save_new_releases_nav_state(False)


def prepare_new_releases_child_entry():
    """Honor the shared New Releases menu boundary despite Kodi history.

    Kodi can restore a parent directory from its in-memory navigation history
    without reinvoking that parent's plugin URL. We therefore remember whether
    a Movie/TV child has already been entered from the current shared menu. If a
    top-level child is entered again, the user necessarily returned through the
    New Releases boundary, so all result caches are invalidated before descent.

    A missing/old state also invalidates caches, which prevents caches written
    by older addon builds from surviving an upgrade.
    """
    state = _load_new_releases_nav_state()
    schema_ok = state.get('schema') == NEW_RELEASES_CACHE_SCHEMA
    descended = bool(state.get('descended')) if schema_ok else True
    if descended:
        clear_new_releases_cache()
        log('New Releases boundary detected; result caches invalidated')
    _save_new_releases_nav_state(True)


def _active_source_path():
    if not xbmcvfs.exists(TEMP_ROOT):
        xbmcvfs.mkdirs(TEMP_ROOT)
    return os.path.join(TEMP_ROOT, 'active_sources.json')


def load_active_source_state():
    path = _active_source_path()
    if not xbmcvfs.exists(path):
        return {}
    try:
        f = xbmcvfs.File(path, 'r')
        try:
            raw = f.read()
        finally:
            f.close()
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def set_active_source_session(session_id, focus_index=None):
    """Make one source session the live temporary source-list cache.

    The cache lives only under special://temp. It is intentionally retained until
    Premium Player starts another source/search operation, rather than expiring
    while the user is watching a long video.
    """
    data = {'session': str(session_id or '')}
    if focus_index not in (None, ''):
        try:
            data['focus'] = int(focus_index)
        except Exception:
            pass
    path = _active_source_path()
    f = xbmcvfs.File(path, 'w')
    try:
        f.write(json.dumps(data))
    finally:
        f.close()


def set_active_source_focus(session_id, focus_index):
    state = load_active_source_state()
    if str(state.get('session') or '') != str(session_id or ''):
        return False
    try:
        state['focus'] = int(focus_index)
    except Exception:
        return False
    path = _active_source_path()
    f = xbmcvfs.File(path, 'w')
    try:
        f.write(json.dumps(state))
    finally:
        f.close()
    return True


def clear_active_source_session(delete_cached_session=True):
    state = load_active_source_state()
    sid = str(state.get('session') or '')
    path = _active_source_path()
    try:
        if xbmcvfs.exists(path):
            xbmcvfs.delete(path)
    except Exception:
        pass
    if delete_cached_session and sid:
        delete_session(sid)


def _cleanup_sessions(folder, max_age=7200):
    try:
        _, files = xbmcvfs.listdir(folder)
        now = time.time()
        active_sid = str(load_active_source_state().get('session') or '')
        for name in files:
            if not name.endswith('.json'):
                continue
            if active_sid and name == active_sid + '.json':
                continue
            p = os.path.join(folder, name)
            try:
                st = os.stat(p)
                if now - st.st_mtime > max_age:
                    xbmcvfs.delete(p)
            except Exception:
                pass
    except Exception:
        pass


def _history_path():
    return os.path.join(ensure_profile(), 'search_history.json')


def load_search_history():
    path = _history_path()
    if not xbmcvfs.exists(path):
        return []
    f = xbmcvfs.File(path, 'r')
    try:
        raw = f.read()
    finally:
        f.close()
    try:
        data = json.loads(raw or '[]')
    except Exception:
        return []
    result = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        media_type = row.get('media_type')
        query = str(row.get('query') or '').strip()
        if media_type in ('movie', 'series') and query:
            result.append({'media_type': media_type, 'query': query})
    return result[:20]


def save_search_history(rows):
    path = _history_path()
    f = xbmcvfs.File(path, 'w')
    try:
        f.write(json.dumps(list(rows or [])[:20], ensure_ascii=False))
    finally:
        f.close()


def remember_search(media_type, query):
    query = str(query or '').strip()
    if media_type not in ('movie', 'series') or not query:
        return
    old = load_search_history()
    key = (media_type, query.casefold())
    rows = [{'media_type': media_type, 'query': query}]
    rows.extend(r for r in old if (r.get('media_type'), str(r.get('query') or '').casefold()) != key)
    save_search_history(rows)


def remove_search_history(media_type, query):
    key = (media_type, str(query or '').casefold())
    rows = [r for r in load_search_history()
            if (r.get('media_type'), str(r.get('query') or '').casefold()) != key]
    save_search_history(rows)



def _pins_path():
    return os.path.join(ensure_profile(), 'pins.json')


def load_pins():
    path = _pins_path()
    if not xbmcvfs.exists(path):
        return []
    f = xbmcvfs.File(path, 'r')
    try:
        raw = f.read()
    finally:
        f.close()
    try:
        data = json.loads(raw or '[]')
    except Exception:
        return []
    rows = []
    for row in data if isinstance(data, list) else []:
        if isinstance(row, dict) and row.get('id') and row.get('category') and row.get('kind'):
            rows.append(row)
    return rows


def save_pins(rows):
    path = _pins_path()
    f = xbmcvfs.File(path, 'w')
    try:
        f.write(json.dumps(list(rows or []), ensure_ascii=False, separators=(',', ':')))
    finally:
        f.close()


def save_pin(pin):
    if not isinstance(pin, dict):
        return None
    pin = dict(pin)
    pin_id = str(pin.get('id') or '').strip()
    if not pin_id:
        return None
    old = load_pins()
    rows = [pin]
    rows.extend(row for row in old if str(row.get('id')) != pin_id)
    save_pins(rows)
    return pin_id


def remove_pin(pin_id):
    pin_id = str(pin_id or '')
    save_pins([row for row in load_pins() if str(row.get('id')) != pin_id])



def _downloads_history_path():
    return os.path.join(ensure_profile(), 'downloaded.json')


def save_download_history(rows):
    path = _downloads_history_path()
    f = xbmcvfs.File(path, 'w')
    try:
        f.write(json.dumps(list(rows or []), ensure_ascii=False, separators=(',', ':')))
    finally:
        f.close()


def load_download_history(prune=True):
    path = _downloads_history_path()
    if not xbmcvfs.exists(path):
        return []
    f = xbmcvfs.File(path, 'r')
    try:
        raw = f.read()
    finally:
        f.close()
    try:
        data = json.loads(raw or '[]')
    except Exception:
        data = []
    rows = []
    changed = False
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            changed = True
            continue
        file_path = str(row.get('path') or '')
        if not file_path:
            changed = True
            continue
        if prune and not xbmcvfs.exists(file_path):
            changed = True
            continue
        rows.append(row)
    if prune and changed:
        save_download_history(rows)
    return rows


def record_download(file_path, label='', media=None):
    file_path = str(file_path or '')
    if not file_path:
        return
    media = dict(media or {})
    row = {
        'path': file_path,
        'label': str(label or os.path.basename(clean_path(file_path)) or 'Downloaded file'),
        'title': str(media.get('title') or ''),
        'media_type': str(media.get('type') or ''),
        'season': media.get('season'),
        'episode': media.get('episode'),
        'added': int(time.time()),
    }
    old = load_download_history(prune=True)
    key = file_path.casefold()
    rows = [row]
    rows.extend(r for r in old if str(r.get('path') or '').casefold() != key)
    save_download_history(rows[:100])


def remove_download_history(file_path):
    key = str(file_path or '').casefold()
    save_download_history([r for r in load_download_history(prune=True)
                           if str(r.get('path') or '').casefold() != key])


def is_video(path):
    return str(path or '').lower().endswith(VIDEO_EXTS)


def is_archive(path):
    return str(path or '').lower().endswith(ARCHIVE_EXTS)


def clean_path(path):
    return str(path or '').replace('\\', '/').lstrip('/')


def human_size(value):
    try:
        n = float(value or 0)
    except Exception:
        return ''
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024.0 or unit == 'TB':
            if unit in ('GB', 'TB'):
                return '%.2f %s' % (n, unit)
            if unit == 'MB':
                return '%.1f %s' % (n, unit)
            return '%d %s' % (n, unit)
        n /= 1024.0
    return ''


def episode_patterns(season, episode):
    s = int(season)
    e = int(episode)
    return [
        re.compile(r'(?i)(?:^|[^a-z0-9])s0?%d[ ._\-]*e0?%d(?:[^0-9]|$)' % (s, e)),
        re.compile(r'(?i)(?:^|[^0-9])0?%dx0?%d(?:[^0-9]|$)' % (s, e)),
        re.compile(r'(?i)season[ ._\-]*0?%d.*episode[ ._\-]*0?%d' % (s, e)),
    ]


def select_media_file(files, media=None, source_filename=None, file_idx=None):
    files = list(files or [])
    videos = []
    for pos, item in enumerate(files):
        path = clean_path(item.get('path') or item.get('name') or item.get('short_name'))
        if not is_video(path):
            continue
        if re.search(r'(?i)(?:^|[ ._\-])sample(?:[ ._\-]|$)', path):
            continue
        clone = dict(item)
        clone['_path'] = path
        clone['_pos'] = pos
        videos.append(clone)
    if not videos:
        return None

    if file_idx is not None:
        try:
            idx = int(file_idx)
            if 0 <= idx < len(files):
                target = files[idx]
                target_path = clean_path(target.get('path') or target.get('name') or target.get('short_name'))
                if is_video(target_path):
                    clone = dict(target)
                    clone['_path'] = target_path
                    clone['_pos'] = idx
                    return clone
        except Exception:
            pass

    if media and media.get('season') is not None and media.get('episode') is not None:
        patterns = episode_patterns(media['season'], media['episode'])
        matches = [v for v in videos if any(p.search(v['_path']) for p in patterns)]
        if matches:
            return max(matches, key=lambda x: int(x.get('bytes') or x.get('size') or 0))

    if source_filename:
        wanted = os.path.basename(clean_path(source_filename)).lower()
        exact = [v for v in videos if os.path.basename(v['_path']).lower() == wanted]
        if exact:
            return exact[0]
        stem = re.sub(r'\.[^.]+$', '', wanted)
        fuzzy = [v for v in videos if stem and stem in os.path.basename(v['_path']).lower()]
        if fuzzy:
            return max(fuzzy, key=lambda x: int(x.get('bytes') or x.get('size') or 0))

    return max(videos, key=lambda x: int(x.get('bytes') or x.get('size') or 0))


def fallback_indices(count, selected_index, wrap=True):
    if count <= 0:
        return []
    selected_index = max(0, min(int(selected_index), count - 1))
    order = list(range(selected_index, count))
    if wrap:
        order += list(range(0, selected_index))
    return order
