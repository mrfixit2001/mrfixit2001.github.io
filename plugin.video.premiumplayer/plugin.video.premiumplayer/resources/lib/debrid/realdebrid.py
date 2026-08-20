# -*- coding: utf-8 -*-
import time
from urllib import parse

import xbmc
import xbmcaddon
import xbmcgui

from ..http import request_json, ApiError
from ..utils import ADDON, ADDON_NAME, VERSION, log, select_media_file


class RealDebrid:
    CLIENT_ID = 'X245A4XAIBGVM'
    OAUTH = 'https://api.real-debrid.com/oauth/v2'
    REST = 'https://api.real-debrid.com/rest/1.0'
    GRANT = 'http://oauth.net/grant_type/device/1.0'

    def __init__(self):
        self.headers = {'User-Agent': 'PremiumPlayer/%s' % VERSION}

    def _rurl_setting(self, key):
        try:
            addon = xbmcaddon.Addon('script.module.resolveurl')
            return addon.getSetting('RealDebridResolver_%s' % key)
        except Exception:
            return ''

    @property
    def resolveurl_authorized(self):
        return bool(self._rurl_setting('token') or self._rurl_setting('refresh'))

    @property
    def enabled(self):
        # ResolveURL owns the visible RD enable switch when it is authorized.
        if self.resolveurl_authorized:
            try:
                return self._rurl_setting('enabled') != 'false'
            except Exception:
                return True
        raw = ADDON.getSetting('rd_enabled')
        return raw.lower() != 'false' if raw else True

    @property
    def cached_only(self):
        if self.resolveurl_authorized:
            return self._rurl_setting('cached_only') == 'true'
        raw = ADDON.getSetting('rd_cached_only')
        return raw.lower() == 'true' if raw else True

    @property
    def authorized(self):
        return self.resolveurl_authorized or bool(ADDON.getSetting('rd_access_token') or ADDON.getSetting('rd_refresh_token'))

    def revoke(self):
        for key in ('rd_client_id', 'rd_client_secret', 'rd_access_token', 'rd_refresh_token', 'rd_token_expires', 'rd_username'):
            ADDON.setSetting(key, '')

    def authorize(self):
        payload, _, _ = request_json(
            self.OAUTH + '/device/code',
            params={'client_id': self.CLIENT_ID, 'new_credentials': 'yes'},
            headers=self.headers,
        )
        code = payload.get('user_code')
        device_code = payload.get('device_code')
        verify = payload.get('verification_url', 'https://real-debrid.com/device')
        interval = int(payload.get('interval') or 5)
        expires = int(payload.get('expires_in') or 600)
        progress = xbmcgui.DialogProgress()
        progress.create('%s - Real-Debrid' % ADDON_NAME,
                        'Go to: %s\nEnter code: %s\nWaiting for authorization...' % (verify, code))
        started = time.time()
        credentials = None
        try:
            while time.time() - started < expires and not progress.iscanceled():
                try:
                    credentials, _, _ = request_json(
                        self.OAUTH + '/device/credentials',
                        params={'client_id': self.CLIENT_ID, 'code': device_code},
                        headers=self.headers,
                        timeout=15,
                    )
                    if isinstance(credentials, dict) and credentials.get('client_id') and credentials.get('client_secret'):
                        break
                except ApiError:
                    pass
                elapsed = int(time.time() - started)
                progress.update(min(99, int(elapsed * 100 / max(expires, 1))),
                                'Go to: %s\nEnter code: %s\nWaiting for authorization...' % (verify, code))
                xbmc.sleep(interval * 1000)
        finally:
            progress.close()
        if not credentials or not credentials.get('client_secret'):
            return False
        token = self._token(credentials['client_id'], credentials['client_secret'], device_code)
        if not token:
            return False
        self._save_token(credentials['client_id'], credentials['client_secret'], token)
        try:
            user = self.get('/user')
            ADDON.setSetting('rd_username', user.get('username') or user.get('email') or '')
        except Exception:
            pass
        return True

    def _token(self, client_id, client_secret, code):
        payload, _, _ = request_json(
            self.OAUTH + '/token', method='POST',
            data={'client_id': client_id, 'client_secret': client_secret, 'code': code, 'grant_type': self.GRANT},
            headers=self.headers,
        )
        return payload if isinstance(payload, dict) else None

    def _save_token(self, client_id, client_secret, token):
        ADDON.setSetting('rd_client_id', client_id)
        ADDON.setSetting('rd_client_secret', client_secret)
        ADDON.setSetting('rd_access_token', token.get('access_token', ''))
        ADDON.setSetting('rd_refresh_token', token.get('refresh_token', ''))
        ADDON.setSetting('rd_token_expires', str(int(time.time()) + int(token.get('expires_in') or 3600) - 60))

    def _ensure_token(self):
        # Prefer ResolveURL's globally-managed credentials. This lets Premium
        # Player share the same authorization as The Crew/other ResolveURL apps.
        rurl_token = self._rurl_setting('token')
        if rurl_token:
            return rurl_token
        token = ADDON.getSetting('rd_access_token')
        try:
            expires = int(ADDON.getSetting('rd_token_expires') or 0)
        except Exception:
            expires = 0
        if token and (not expires or time.time() < expires):
            return token
        refresh = ADDON.getSetting('rd_refresh_token')
        client_id = ADDON.getSetting('rd_client_id')
        client_secret = ADDON.getSetting('rd_client_secret')
        if not (refresh and client_id and client_secret):
            return token
        try:
            refreshed = self._token(client_id, client_secret, refresh)
            if refreshed and refreshed.get('access_token'):
                self._save_token(client_id, client_secret, refreshed)
                return refreshed.get('access_token')
        except Exception as exc:
            log('Real-Debrid token refresh failed: %s' % exc, xbmc.LOGWARNING)
        return token

    def _refresh_resolveurl_token(self):
        if not self.resolveurl_authorized:
            return False
        try:
            from resolveurl.plugins.realdebrid import RealDebridResolver
            RealDebridResolver().refresh_token()
            return bool(self._rurl_setting('token'))
        except Exception as exc:
            log('ResolveURL Real-Debrid token refresh failed: %s' % exc, xbmc.LOGWARNING)
            return False

    def _auth_headers(self):
        token = self._ensure_token()
        if not token:
            raise ApiError('Real-Debrid is not authorized')
        headers = dict(self.headers)
        headers['Authorization'] = 'Bearer %s' % token
        return headers

    def _api_request(self, path, method='GET', params=None, data=None, retry=True):
        try:
            payload, _, _ = request_json(self.REST + path, method=method, params=params, data=data,
                                         headers=self._auth_headers())
            return payload
        except ApiError as exc:
            if retry and exc.status == 401 and self._refresh_resolveurl_token():
                return self._api_request(path, method=method, params=params, data=data, retry=False)
            if method == 'DELETE' and exc.status == 204:
                return True
            raise

    def get(self, path, params=None):
        return self._api_request(path, params=params)

    def post(self, path, data=None):
        return self._api_request(path, method='POST', data=data)

    def delete(self, path):
        return self._api_request(path, method='DELETE')

    def list_torrents(self):
        return self.get('/torrents', {'limit': 5000}) or []

    def torrent_info(self, torrent_id):
        return self.get('/torrents/info/%s' % torrent_id) or {}

    def list_downloads(self):
        return self.get('/downloads', {'limit': 5000}) or []

    def cloud_hashes(self):
        result = set()
        try:
            for item in self.list_torrents():
                if item.get('status') == 'downloaded' and item.get('hash'):
                    result.add(str(item['hash']).lower())
        except Exception:
            pass
        return result

    def add_magnet(self, magnet):
        result = self.post('/torrents/addMagnet', {'magnet': magnet}) or {}
        return result.get('id')

    def select_files(self, torrent_id, file_ids):
        if isinstance(file_ids, (list, tuple)):
            file_ids = ','.join(str(x) for x in file_ids)
        self.post('/torrents/selectFiles/%s' % torrent_id, {'files': str(file_ids)})
        return True

    def delete_torrent(self, torrent_id):
        try:
            self.delete('/torrents/delete/%s' % torrent_id)
            return True
        except ApiError as exc:
            if exc.status in (204, 404):
                return True
            raise

    def unrestrict(self, link):
        result = self.post('/unrestrict/link', {'link': link}) or {}
        return result.get('download') or result.get('link')

    def _link_for_selected_file(self, info, selected_file):
        selected = [f for f in info.get('files', []) if f.get('selected')]
        links = info.get('links') or []
        target_id = selected_file.get('id')
        for idx, item in enumerate(selected):
            if item.get('id') == target_id and idx < len(links):
                return links[idx]
        if len(links) == 1:
            return links[0]
        return None

    def resolve_source(self, source, media, wait_seconds=120, track_cleanup=False):
        torrent_id = None
        keep = False
        try:
            torrent_id = self.add_magnet(source['magnet'])
            if not torrent_id:
                raise ApiError('Real-Debrid did not return a torrent ID')
            info = self.torrent_info(torrent_id)
            status = info.get('status')
            if self.cached_only and status not in ('downloaded', 'waiting_files_selection'):
                raise ApiError('Not cached on Real-Debrid')

            target = select_media_file(info.get('files') or [], media, source.get('filename'), source.get('file_idx'))
            if not target:
                raise ApiError('Requested video file was not found in the torrent')

            if status == 'waiting_files_selection':
                self.select_files(torrent_id, target.get('id'))
                info = self.torrent_info(torrent_id)
                status = info.get('status')

            if status != 'downloaded':
                if self.cached_only:
                    raise ApiError('Not cached on Real-Debrid')
                deadline = time.time() + max(30, int(wait_seconds))
                while time.time() < deadline:
                    if xbmc.Monitor().abortRequested():
                        raise ApiError('Cancelled')
                    xbmc.sleep(3000)
                    info = self.torrent_info(torrent_id)
                    status = info.get('status')
                    if status == 'downloaded':
                        break
                    if status in ('magnet_error', 'error', 'virus', 'dead'):
                        raise ApiError('Real-Debrid torrent failed: %s' % status)
                if status != 'downloaded':
                    raise ApiError('Real-Debrid download did not finish before timeout')

            # Refresh target object because selected flags/ordering may have changed.
            target = select_media_file(info.get('files') or [], media, source.get('filename'), source.get('file_idx')) or target
            link = self._link_for_selected_file(info, target)
            if not link:
                # If the torrent was already downloaded with multiple files selected,
                # use the file/path ordering mapping above; never silently pick link[0]
                # unless it is the only link.
                raise ApiError('Could not map the requested file to a Real-Debrid link')
            direct = self.unrestrict(link)
            if not direct:
                raise ApiError('Real-Debrid did not return a playable URL')
            return (direct, None) if track_cleanup else direct
        finally:
            # Search-created RD torrents are temporary, matching ResolveURL's behavior.
            if torrent_id and not keep:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass

    def resolve_cloud_file(self, torrent_id, file_id):
        info = self.torrent_info(torrent_id)
        target = None
        for f in info.get('files') or []:
            if str(f.get('id')) == str(file_id):
                target = f
                break
        if not target:
            raise ApiError('File no longer exists in this Real-Debrid torrent')
        if not target.get('selected'):
            raise ApiError('This file was not selected/downloaded in Real-Debrid')
        link = self._link_for_selected_file(info, target)
        if not link:
            raise ApiError('Could not map this file to a Real-Debrid link')
        direct = self.unrestrict(link)
        if not direct:
            raise ApiError('Real-Debrid did not return a playable URL')
        return direct, target
