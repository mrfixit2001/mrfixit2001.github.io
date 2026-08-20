# -*- coding: utf-8 -*-
"""AllDebrid native account/torrent adapter using ResolveURL-managed auth."""
import os
import time

import xbmc
import xbmcaddon

from ..http import ApiError, request_json
from ..utils import clean_path, select_media_file, VERSION


class AllDebrid:
    NAME = 'AllDebrid'
    CODE = 'AD'
    RESOLVER_CLASS = 'AllDebridResolver'
    API = 'https://api.alldebrid.com'

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
            raise ApiError('AllDebrid is not authorized in ResolveURL')
        headers = dict(self.headers)
        headers['Authorization'] = 'Bearer %s' % self.token
        return headers

    def _request(self, path, method='POST', data=None, params=None):
        payload, _, _ = request_json(self.API + path, method=method, data=data, params=params,
                                     headers=self._headers())
        if not isinstance(payload, dict):
            raise ApiError('AllDebrid returned an invalid response')
        if payload.get('status') != 'success':
            err = payload.get('error') or {}
            if isinstance(err, dict):
                msg = err.get('message') or err.get('code')
            else:
                msg = err
            raise ApiError('AllDebrid: %s' % (msg or 'API request failed'), payload=payload)
        return payload.get('data') or {}

    def list_torrents(self):
        data = self._request('/v4.1/magnet/status', data={})
        magnets = data.get('magnets') or []
        return magnets if isinstance(magnets, list) else ([magnets] if magnets else [])

    def _status(self, torrent_id):
        data = self._request('/v4.1/magnet/status', data={'id': torrent_id})
        magnets = data.get('magnets') or []
        if isinstance(magnets, list):
            return next((m for m in magnets if str(m.get('id')) == str(torrent_id)), magnets[0] if magnets else {})
        return magnets if isinstance(magnets, dict) else {}

    @staticmethod
    def _flatten(nodes, prefix=''):
        out = []
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            name = str(node.get('n') or '')
            path = clean_path('/'.join(x for x in (prefix, name) if x))
            children = node.get('e')
            if isinstance(children, list):
                out.extend(AllDebrid._flatten(children, path))
            elif node.get('l'):
                out.append({
                    'id': path,
                    'path': path,
                    'name': os.path.basename(path),
                    'size': int(node.get('s') or 0),
                    'bytes': int(node.get('s') or 0),
                    'link': node.get('l'),
                })
        return out

    def torrent_info(self, torrent_id):
        status = self._status(torrent_id)
        data = self._request('/v4/magnet/files', data={'id': [torrent_id]})
        magnets = data.get('magnets') or []
        entry = next((m for m in magnets if str(m.get('id')) == str(torrent_id)), {}) if isinstance(magnets, list) else {}
        info = dict(status or {})
        info['files'] = self._flatten(entry.get('files') or [])
        return info

    def delete_torrent(self, torrent_id):
        self._request('/v4/magnet/delete', data={'id': torrent_id})
        return True

    def add_magnet(self, magnet):
        data = self._request('/v4/magnet/upload', data={'magnets': magnet})
        magnets = data.get('magnets') or []
        if not isinstance(magnets, list):
            return None, {}
        for item in magnets:
            if isinstance(item, dict) and item.get('id'):
                return item.get('id'), item
        return None, {}

    def unlock(self, link):
        data = self._request('/v4/link/unlock', data={'link': link})
        return data.get('link')

    def resolve_cloud_file(self, torrent_id, file_id):
        info = self.torrent_info(torrent_id)
        target = next((f for f in info.get('files') or [] if str(f.get('id')) == str(file_id)), None)
        if not target:
            raise ApiError('File no longer exists in AllDebrid')
        direct = self.unlock(target.get('link'))
        if not direct:
            raise ApiError('AllDebrid did not return a playable URL')
        return direct, target

    def resolve_source(self, source, media, wait_seconds=120, track_cleanup=False):
        torrent_id = None
        try:
            torrent_id, created = self.add_magnet(source.get('magnet'))
            if not torrent_id:
                raise ApiError('AllDebrid did not return a magnet ID')
            if self.cached_only and not bool(created.get('ready')):
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass
                torrent_id = None
                raise ApiError('AllDebrid: not cached')

            deadline = time.time() + (15 if self.cached_only else max(30, int(wait_seconds)))
            info = {}
            while time.time() < deadline:
                if xbmc.Monitor().abortRequested():
                    raise ApiError('Cancelled')
                info = self._status(torrent_id)
                if int(info.get('statusCode') or 0) == 4:
                    break
                code = int(info.get('statusCode') or 0)
                if code >= 5:
                    raise ApiError('AllDebrid transfer failed: %s' % (info.get('status') or code))
                xbmc.sleep(750)
            if int(info.get('statusCode') or 0) != 4:
                raise ApiError('AllDebrid transfer did not finish before timeout')

            info = self.torrent_info(torrent_id)
            target = select_media_file(info.get('files') or [], media, source.get('filename'), source.get('file_idx'))
            if not target:
                raise ApiError('Requested video file was not found in the AllDebrid torrent')
            direct = self.unlock(target.get('link'))
            if not direct:
                raise ApiError('AllDebrid did not return a playable URL')
            return (direct, str(torrent_id)) if track_cleanup else direct
        except Exception:
            if torrent_id and track_cleanup:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass
            raise
