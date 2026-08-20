# -*- coding: utf-8 -*-
"""Premiumize.me native cloud/transfer adapter using ResolveURL-managed auth."""
import time

import xbmc
import xbmcaddon

from ..http import ApiError, request_json
from ..utils import select_media_file, VERSION


class Premiumize:
    NAME = 'Premiumize.me'
    CODE = 'PM'
    RESOLVER_CLASS = 'PremiumizeMeResolver'
    API = 'https://www.premiumize.me/api'

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
            raise ApiError('Premiumize.me is not authorized in ResolveURL')
        headers = dict(self.headers)
        headers['Authorization'] = 'Bearer %s' % self.token
        return headers

    def _request(self, path, method='GET', params=None, data=None):
        payload, _, _ = request_json(self.API + path, method=method, params=params, data=data,
                                     headers=self._headers())
        if not isinstance(payload, dict) or payload.get('status') != 'success':
            msg = payload.get('message') if isinstance(payload, dict) else None
            raise ApiError('Premiumize.me: %s' % (msg or 'API request failed'), payload=payload)
        return payload

    def check_cached(self, magnets):
        magnets = [m for m in magnets if m]
        if not magnets:
            return set()
        result = self._request('/cache/check', params={'items[]': magnets})
        flags = result.get('response') or []
        return {magnets[i] for i, value in enumerate(flags) if i < len(magnets) and bool(value)}

    def list_folder(self, folder_id=''):
        params = {'id': folder_id} if folder_id else None
        return self._request('/folder/list', params=params)

    def file_info(self, file_id):
        return self._request('/item/details', params={'id': file_id})

    def list_all_files(self):
        result = self._request('/item/listall')
        return result.get('files') or []

    def list_transfers(self):
        result = self._request('/transfer/list')
        return result.get('transfers') or []

    def delete_transfer(self, transfer_id):
        self._request('/transfer/delete', method='POST', data={'id': transfer_id})
        return True

    def delete_file(self, file_id):
        self._request('/item/delete', method='POST', data={'id': file_id})
        return True

    def delete_folder(self, folder_id):
        self._request('/folder/delete', method='POST', data={'id': folder_id})
        return True

    def create_transfer(self, source):
        result = self._request('/transfer/create', method='POST', data={'src': source})
        return result.get('id')

    def direct_dl(self, source):
        result = self._request('/transfer/directdl', method='POST', data={'src': source})
        return result.get('content') or []

    def resolve_source(self, source, media, wait_seconds=120, track_cleanup=False):
        magnet = source.get('magnet')
        transfer_id = None
        try:
            known = source.get('pm_cached')
            cached = bool(known) if known is not None else bool(self.check_cached([magnet]))
            if self.cached_only and not cached:
                raise ApiError('Premiumize.me: not cached')
            if not cached:
                transfer_id = self.create_transfer(magnet)
                if not transfer_id:
                    raise ApiError('Premiumize.me did not return a transfer ID')
                deadline = time.time() + max(30, int(wait_seconds))
                while time.time() < deadline:
                    if xbmc.Monitor().abortRequested():
                        raise ApiError('Cancelled')
                    item = next((x for x in self.list_transfers() if str(x.get('id')) == str(transfer_id)), None)
                    if item:
                        status = str(item.get('status') or '').lower()
                        if status in ('finished', 'seeding'):
                            break
                        if status == 'error':
                            raise ApiError('Premiumize.me transfer failed: %s' % (item.get('message') or 'error'))
                    xbmc.sleep(750)
                else:
                    raise ApiError('Premiumize.me transfer did not finish before timeout')

            files = []
            for pos, item in enumerate(self.direct_dl(magnet)):
                files.append({
                    'id': str(pos),
                    'path': item.get('path') or '',
                    'name': item.get('path') or '',
                    'size': int(item.get('size') or 0),
                    'bytes': int(item.get('size') or 0),
                    'link': item.get('link'),
                })
            target = select_media_file(files, media, source.get('filename'), source.get('file_idx'))
            if not target or not target.get('link'):
                raise ApiError('Requested video file was not found in the Premiumize.me transfer')
            direct = target.get('link')
            # Cached directdl does not create a cloud transfer, so there is
            # nothing to clean. Only an uncached transfer created above is tracked.
            return (direct, str(transfer_id) if transfer_id else None) if track_cleanup else direct
        except Exception:
            if transfer_id and track_cleanup:
                try:
                    self.delete_transfer(transfer_id)
                except Exception:
                    pass
            raise

    def resolve_cloud_file(self, file_id):
        item = self.file_info(file_id)
        direct = item.get('link')
        if not direct:
            raise ApiError('Premiumize.me file no longer has a download URL')
        target = {'id': item.get('id'), 'path': item.get('name'), 'name': item.get('name'),
                  'size': item.get('size'), 'bytes': item.get('size'), 'link': direct}
        return direct, target
