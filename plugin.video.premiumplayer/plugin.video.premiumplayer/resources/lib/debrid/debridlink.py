# -*- coding: utf-8 -*-
"""Debrid-Link native seedbox adapter using ResolveURL-managed OAuth tokens."""
import time

import xbmc
import xbmcaddon

from ..http import ApiError, request_json
from ..utils import select_media_file, VERSION


class DebridLink:
    NAME = 'Debrid-Link'
    CODE = 'DL'
    RESOLVER_CLASS = 'DebridLinkResolver'
    API = 'https://debrid-link.fr/api/v2'

    def __init__(self):
        self.headers = {'User-Agent': 'PremiumPlayer/%s' % VERSION}

    def _setting(self, key):
        try:
            return xbmcaddon.Addon('script.module.resolveurl').getSetting('%s_%s' % (self.RESOLVER_CLASS, key))
        except Exception:
            return ''

    @property
    def token(self):
        return self._setting('token')

    @property
    def resolveurl_authorized(self):
        return bool(self.token)

    @property
    def authorized(self):
        return self.resolveurl_authorized

    @property
    def enabled(self):
        return self.resolveurl_authorized and self._setting('enabled') != 'false'

    @property
    def cached_only(self):
        return self._setting('cached_only') == 'true'

    def _headers(self):
        if not self.token:
            raise ApiError('Debrid-Link is not authorized in ResolveURL')
        h = dict(self.headers)
        h['Authorization'] = 'Bearer %s' % self.token
        return h

    def _refresh(self):
        try:
            from resolveurl.plugins.debrid_link import DebridLinkResolver
            return bool(DebridLinkResolver().refresh_token())
        except Exception:
            return False

    def _request(self, path, method='GET', data=None, params=None, retry=True):
        try:
            payload, _, _ = request_json(self.API + path, method=method, data=data, params=params, headers=self._headers())
        except ApiError as exc:
            if retry and exc.status == 401 and self._refresh():
                return self._request(path, method=method, data=data, params=params, retry=False)
            raise
        if not isinstance(payload, dict) or payload.get('success') is not True:
            msg = payload.get('ERR') if isinstance(payload, dict) else None
            raise ApiError('Debrid-Link: %s' % (msg or 'API request failed'), payload=payload)
        return payload.get('value')

    def list_torrents(self):
        # Seedbox list endpoint; value is the user's current torrent list.
        result = self._request('/seedbox/list', params={'page': 0, 'perPage': 100})
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ('seedbox', 'torrents', 'items'):
                if isinstance(result.get(key), list):
                    return result.get(key)
        return []

    def check_cached(self, hashes):
        values = [str(x).lower() for x in (hashes or []) if x]
        if not values:
            return set()
        result = self._request('/seedbox/cached', params={'url': ','.join(values)})
        found = set()

        def walk(node):
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if not isinstance(node, dict):
                return
            # API versions have returned either per-hash objects or a mapping.
            for key, value in node.items():
                key_l = str(key).lower()
                if key_l in values and bool(value):
                    found.add(key_l)
            h = str(node.get('hash') or node.get('infoHash') or node.get('url') or '').lower()
            cached = node.get('cached')
            if h in values and (cached is True or cached == 1 or str(cached).lower() == 'true'):
                found.add(h)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)

        walk(result)
        return found

    def torrent_info(self, torrent_id):
        result = self._request('/seedbox/%s/infos' % torrent_id) or {}
        files = []
        for pos, item in enumerate(result.get('files') or []):
            if not isinstance(item, dict):
                continue
            name = item.get('name') or item.get('path') or ''
            files.append({
                'id': item.get('id') if item.get('id') is not None else name or str(pos),
                'path': item.get('path') or name,
                'name': name,
                'size': int(item.get('size') or 0),
                'bytes': int(item.get('size') or 0),
                'link': item.get('downloadUrl'),
            })
        info = dict(result)
        info['files'] = files
        return info

    def delete_torrent(self, torrent_id):
        self._request('/seedbox/%s/remove' % torrent_id, method='DELETE')
        return True

    def add_magnet(self, magnet):
        return self._request('/seedbox/add', method='POST', data={'url': magnet, 'async': 'true'}) or {}

    def resolve_source(self, source, media, wait_seconds=120, track_cleanup=False):
        torrent_id = None
        try:
            created = self.add_magnet(source.get('magnet'))
            torrent_id = created.get('id') if isinstance(created, dict) else None
            if not torrent_id:
                raise ApiError('Debrid-Link did not return a seedbox ID')
            pct = float(created.get('downloadPercent') or 0)
            if self.cached_only and pct < 100:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass
                torrent_id = None
                raise ApiError('Debrid-Link: not cached')
            deadline = time.time() + (15 if self.cached_only else max(30, int(wait_seconds)))
            info = {}
            while time.time() < deadline:
                if xbmc.Monitor().abortRequested():
                    raise ApiError('Cancelled')
                info = self.torrent_info(torrent_id)
                if float(info.get('downloadPercent') or 0) >= 100:
                    break
                xbmc.sleep(750)
            if float(info.get('downloadPercent') or 0) < 100:
                raise ApiError('Debrid-Link transfer did not finish before timeout')
            target = select_media_file(info.get('files') or [], media, source.get('filename'), source.get('file_idx'))
            if not target or not target.get('link'):
                raise ApiError('Requested video file was not found in the Debrid-Link torrent')
            direct = target.get('link')
            return (direct, str(torrent_id)) if track_cleanup else direct
        except Exception:
            if torrent_id and track_cleanup:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass
            raise

    def resolve_cloud_file(self, torrent_id, file_id):
        info = self.torrent_info(torrent_id)
        target = next((f for f in info.get('files') or [] if str(f.get('id')) == str(file_id)), None)
        if not target:
            raise ApiError('File no longer exists in Debrid-Link')
        if not target.get('link'):
            raise ApiError('Debrid-Link did not return a playable URL')
        return target.get('link'), target
