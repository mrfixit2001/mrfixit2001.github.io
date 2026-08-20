# -*- coding: utf-8 -*-
import os
import re
from urllib import request, parse

import xbmc
import xbmcgui
import xbmcvfs

from .utils import ADDON, ADDON_NAME, VERSION, human_size, record_download, setting_bool


def _safe_name(name):
    name = re.sub(r'[\\/:*?"<>|]+', '_', str(name or '')).strip(' .')
    return name or 'download'


def _download_root():
    root = ADDON.getSetting('download_path') or 'special://home/../'
    if root in ('special://home/', 'special://home', 'special://home/../', 'special://home/..'):
        try:
            home = xbmcvfs.translatePath('special://home/').rstrip('/\\')
            parent = os.path.dirname(home)
            if parent:
                return parent
        except Exception:
            pass
    return root


def destination_for(media, suggested_filename):
    root = _download_root()
    if not root:
        return None
    folder = root
    if setting_bool('create_media_folders', True):
        title = _safe_name(media.get('title') or 'Media')
        folder = os.path.join(folder, title)
        if media.get('season') is not None:
            folder = os.path.join(folder, 'Season %02d' % int(media['season']))
        if not xbmcvfs.exists(folder):
            xbmcvfs.mkdirs(folder)
    filename = _safe_name(suggested_filename or media.get('label') or media.get('title') or 'video')
    if '.' not in os.path.basename(filename):
        filename += '.mkv'
    return os.path.join(folder, filename)


def download_url(url, media, suggested_filename=None):
    dest = destination_for(media, suggested_filename)
    if not dest:
        return False
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON_NAME, 'Starting download...')
    response = None
    out = None
    try:
        req = request.Request(url, headers={'User-Agent': 'PremiumPlayer/%s' % VERSION})
        response = request.urlopen(req, timeout=30)
        total = int(response.headers.get('Content-Length') or 0)
        disposition = response.headers.get('Content-Disposition') or ''
        m = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)', disposition, re.I)
        if m:
            server_name = parse.unquote(m.group(1)).strip()
            if server_name:
                dest = os.path.join(os.path.dirname(dest), _safe_name(server_name))
        out = xbmcvfs.File(dest, 'wb')
        done = 0
        while True:
            if progress.iscanceled() or xbmc.Monitor().abortRequested():
                return False
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            pct = int(done * 100 / total) if total else 0
            progress.update(min(100, pct), '%s / %s' % (human_size(done), human_size(total) if total else 'unknown size'))
        record_download(dest, os.path.basename(dest), media)
        xbmcgui.Dialog().notification(ADDON_NAME, 'Download complete: %s' % os.path.basename(dest), xbmcgui.NOTIFICATION_INFO, 5000)
        return dest
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Download failed:\n%s' % exc)
        return False
    finally:
        try:
            if out:
                out.close()
        except Exception:
            pass
        try:
            if response:
                response.close()
        except Exception:
            pass
        progress.close()
