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
