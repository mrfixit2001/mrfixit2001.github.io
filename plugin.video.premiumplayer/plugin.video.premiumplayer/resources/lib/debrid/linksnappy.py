# -*- coding: utf-8 -*-
"""LinkSnappy native torrent adapter reusing ResolveURL's authenticated cookie jar."""
import json
import os
import time
from urllib import parse

import xbmc

from ..http import ApiError
from ..utils import clean_path, select_media_file


class LinkSnappy:
    NAME = 'LinkSnappy'
    CODE = 'LS'
    RESOLVER_CLASS = 'LinkSnappyResolver'
    BASE = 'https://linksnappy.com/api/torrents'

    def __init__(self):
        self._resolver = None

    def _get_resolver(self):
        if self._resolver is None:
            try:
                from resolveurl.plugins.linksnappy import LinkSnappyResolver
                self._resolver = LinkSnappyResolver()
            except Exception as exc:
                raise ApiError('Could not initialize LinkSnappy through ResolveURL: %s' % exc)
        return self._resolver

    def _setting(self, key):
        try:
            from resolveurl.plugins.linksnappy import LinkSnappyResolver
            return LinkSnappyResolver.get_setting(key)
        except Exception:
            return ''

    @property
    def resolveurl_authorized(self):
        try:
            from resolveurl.plugins.linksnappy import LinkSnappyResolver
            return bool(LinkSnappyResolver._is_enabled())
        except Exception:
            return False

    @property
    def authorized(self):
        return self.resolveurl_authorized

    @property
    def enabled(self):
        return self.resolveurl_authorized

    @property
    def cached_only(self):
        return self._setting('cached_only') == 'true'

    def _get(self, url):
        resolver = self._get_resolver()
        try:
            raw = resolver.net().http_GET(url).content
            data = json.loads(raw)
        except Exception as exc:
            raise ApiError('LinkSnappy request failed: %s' % exc)
        if isinstance(data, dict) and data.get('status') == 'ERROR':
            raise ApiError('LinkSnappy: %s' % (data.get('error') or 'API request failed'))
        return data

    @staticmethod
    def _unwrap_list(data):
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        value = data.get('return')
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in ('torrents', 'items', 'data'):
                if isinstance(value.get(key), list):
                    return value.get(key)
        for key in ('torrents', 'items', 'data'):
            if isinstance(data.get(key), list):
                return data.get(key)
        return []

    def list_torrents(self):
        return self._unwrap_list(self._get(self.BASE + '/LIST'))

    @staticmethod
    def _flatten_file_tree(node, prefix='', out=None):
        out = out if out is not None else []
        if isinstance(node, list):
            for entry in node:
                LinkSnappy._flatten_file_tree(entry, prefix, out)
            return out
        if not isinstance(node, dict):
            return out
        # A LinkSnappy file object is identified by a direct link or isVideo flag.
        if node.get('downloadLink') or str(node.get('isVideo') or '').lower() in ('y', 'yes', 'true', '1'):
            name = node.get('name') or node.get('text') or node.get('filename') or node.get('fileName') or ''
            path = clean_path('/'.join(x for x in (prefix, name) if x)) or str(node.get('id') or '')
            clone = dict(node)
            clone.update({
                'id': node.get('id') if node.get('id') is not None else path,
                'path': path,
                'name': os.path.basename(path) or name,
                'size': int(node.get('size') or 0),
                'bytes': int(node.get('size') or 0),
                'link': node.get('downloadLink'),
            })
            out.append(clone)
            return out
        # Response trees often use dictionary keys as folder/file names.
        for key, value in node.items():
            if key in ('status', 'error', 'return'):
                if key == 'return':
                    LinkSnappy._flatten_file_tree(value, prefix, out)
                continue
            if isinstance(value, (dict, list)):
                next_prefix = prefix
                if isinstance(value, dict) and not (value.get('downloadLink') or value.get('isVideo')):
                    next_prefix = clean_path('/'.join(x for x in (prefix, str(key)) if x))
                LinkSnappy._flatten_file_tree(value, next_prefix, out)
        return out

    def torrent_info(self, torrent_id):
        status_data = self._get(self.BASE + '/STATUS?tid=%s' % parse.quote(str(torrent_id)))
        info = status_data.get('return') if isinstance(status_data, dict) else {}
        if not isinstance(info, dict):
            info = {}
        files_data = self._get(self.BASE + '/FILES?id=%s' % parse.quote(str(torrent_id)))
        info = dict(info)
        info['files'] = self._flatten_file_tree(files_data)
        info.setdefault('id', torrent_id)
        return info

    def delete_torrent(self, torrent_id):
        self._get(self.BASE + '/DELETETORRENT?tid=%s&delFiles=1' % parse.quote(str(torrent_id)))
        return True

    def check_cached(self, hashes):
        found = set()
        for value in hashes or []:
            if not value:
                continue
            data = self._get(self.BASE + '/HASHCHECK?hash=%s' % parse.quote(str(value)))
            if isinstance(data, dict) and data.get('status') == 'OK' and data.get('return') == 'CACHED':
                found.add(str(value).lower())
        return found

    def _folder_id(self):
        data = self._get(self.BASE + '/FOLDERLIST')
        rows = self._unwrap_list(data)
        downloads = None
        for item in reversed(rows):
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'root' and item.get('text') == 'resolveurl':
                return item.get('id')
            if item.get('type') == 'root' and item.get('text') == 'Downloads':
                downloads = item.get('id')
        if downloads is None:
            return ''
        result = self._get(self.BASE + '/CREATEFOLDER?name=resolveurl&dir=%s' % parse.quote(str(downloads)))
        value = result.get('return') if isinstance(result, dict) else None
        return value.get('id') if isinstance(value, dict) else ''

    def add_magnet(self, magnet):
        data = self._get(self.BASE + '/ADDMAGNET?magnetlinks=%s' % parse.quote(str(magnet), safe=''))
        value = data.get('return') if isinstance(data, dict) else None
        torrent_id = None
        if isinstance(value, dict):
            torrent_id = value.get('torrentid') or value.get('id') or value.get('tid') or value.get('torrent_id')
        elif isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict):
                torrent_id = first.get('torrentid') or first.get('id') or first.get('tid') or first.get('torrent_id')
            else:
                torrent_id = first
        elif value not in (None, ''):
            torrent_id = value
        if not torrent_id:
            raise ApiError('LinkSnappy did not return a torrent ID')
        folder_id = self._folder_id()
        start_url = self.BASE + '/START?tid=%s&fid=%s' % (parse.quote(str(torrent_id)), parse.quote(str(folder_id)))
        for _ in range(10):
            started = self._get(start_url)
            if isinstance(started, dict) and started.get('error') is False:
                return str(torrent_id)
            if not isinstance(started, dict) or started.get('error') != 'Magnet URI processing in progress. Please wait...':
                break
            xbmc.sleep(750)
        return str(torrent_id)

    def _direct(self, target):
        link = target.get('link') or target.get('downloadLink')
        if not link:
            return None
        resolver = self._get_resolver()
        try:
            return resolver.net().http_HEAD(link).get_url()
        except Exception:
            try:
                return resolver.net(ssl_verify=False).http_HEAD(link).get_url()
            except Exception:
                return link

    def resolve_source(self, source, media, wait_seconds=120, track_cleanup=False):
        torrent_id = None
        try:
            hash_value = str(source.get('hash') or '').lower()
            cached = hash_value in self.check_cached([hash_value]) if hash_value else False
            if self.cached_only and not cached:
                raise ApiError('LinkSnappy: not cached')
            torrent_id = self.add_magnet(source.get('magnet'))
            deadline = time.time() + (15 if self.cached_only else max(30, int(wait_seconds)))
            info = {}
            while time.time() < deadline:
                if xbmc.Monitor().abortRequested():
                    raise ApiError('Cancelled')
                info = self.torrent_info(torrent_id)
                state = str(info.get('status') or '').upper()
                if state == 'FINISHED' or info.get('files'):
                    break
                xbmc.sleep(750)
            target = select_media_file(info.get('files') or [], media, source.get('filename'), source.get('file_idx'))
            if not target:
                raise ApiError('Requested video file was not found in the LinkSnappy torrent')
            direct = self._direct(target)
            if not direct:
                raise ApiError('LinkSnappy did not return a playable URL')
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
            raise ApiError('File no longer exists in LinkSnappy')
        direct = self._direct(target)
        if not direct:
            raise ApiError('LinkSnappy did not return a playable URL')
        return direct, target
