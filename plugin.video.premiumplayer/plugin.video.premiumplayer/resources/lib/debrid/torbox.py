# -*- coding: utf-8 -*-
import base64
import datetime
import re
import time

import xbmc
import xbmcaddon
import xbmcgui

from ..http import request_json, unwrap, ApiError
from ..utils import ADDON, ADDON_NAME, VERSION, log, select_media_file


class TorBox:
    BASE = 'https://api.torbox.app/v1/api'

    def __init__(self):
        self.headers = {'User-Agent': 'PremiumPlayer/%s' % VERSION}

    def _rurl_setting(self, key):
        try:
            addon = xbmcaddon.Addon('script.module.resolveurl')
            return addon.getSetting('TorBoxResolver_%s' % key)
        except Exception:
            return ''

    @property
    def resolveurl_authorized(self):
        return bool(self._rurl_setting('apikey'))

    @property
    def enabled(self):
        if self.resolveurl_authorized:
            return self._rurl_setting('enabled') != 'false'
        raw = ADDON.getSetting('tb_enabled')
        return raw.lower() != 'false' if raw else True

    @property
    def cached_only(self):
        if self.resolveurl_authorized:
            return self._rurl_setting('cached_only') == 'true'
        raw = ADDON.getSetting('tb_cached_only')
        return raw.lower() == 'true' if raw else True

    @property
    def token(self):
        return self._rurl_setting('apikey') or ADDON.getSetting('tb_token')

    @property
    def authorized(self):
        return bool(self.token)

    def revoke(self):
        ADDON.setSetting('tb_token', '')
        ADDON.setSetting('tb_username', '')

    def authorize(self):
        payload, _, _ = request_json(
            self.BASE + '/user/auth/device/start',
            params={'app': ADDON_NAME}, headers=self.headers,
        )
        data = unwrap(payload) or {}
        device_code = data.get('device_code')
        user_code = data.get('code') or data.get('user_code')
        verify = data.get('friendly_verification_url') or data.get('verification_url') or 'https://torbox.app/oauth/device'
        interval = int(data.get('interval') or 5)
        expires = self._ttl(data.get('expires_at'), int(data.get('expires_in') or 600))
        if not device_code or not user_code:
            raise ApiError('TorBox did not return a device authorization code')

        progress = xbmcgui.DialogProgress()
        progress.create('%s - TorBox' % ADDON_NAME,
                        'Go to: %s\nEnter code: %s\nWaiting for authorization...' % (verify, user_code))
        started = time.time()
        token = None
        try:
            while time.time() - started < expires and not progress.iscanceled():
                try:
                    response, _, _ = request_json(
                        self.BASE + '/user/auth/device/token', method='POST',
                        json_data={'device_code': device_code}, headers=self.headers, timeout=15,
                    )
                    token_data = unwrap(response)
                    if isinstance(token_data, str):
                        token = token_data
                    elif isinstance(token_data, dict):
                        token = (token_data.get('access_token') or token_data.get('token') or
                                 token_data.get('api_token') or token_data.get('auth_token'))
                    if token:
                        break
                except ApiError:
                    pass
                elapsed = int(time.time() - started)
                progress.update(min(99, int(elapsed * 100 / max(expires, 1))),
                                'Go to: %s\nEnter code: %s\nWaiting for authorization...' % (verify, user_code))
                xbmc.sleep(interval * 1000)
        finally:
            progress.close()
        if not token:
            return False
        ADDON.setSetting('tb_token', token)
        try:
            me = self.get('/user/me') or {}
            ADDON.setSetting('tb_username', me.get('email') or me.get('username') or '')
        except Exception:
            pass
        return True

    @staticmethod
    def _ttl(expires_at, default=600):
        if not expires_at:
            return default
        try:
            dt = datetime.datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
            now = datetime.datetime.now(datetime.timezone.utc)
            return max(60, int((dt - now).total_seconds()))
        except Exception:
            return default

    def _auth_headers(self):
        if not self.token:
            raise ApiError('TorBox is not authorized')
        result = dict(self.headers)
        result['Authorization'] = 'Bearer %s' % self.token
        return result

    def get(self, path, params=None, auth=True):
        payload, _, _ = request_json(self.BASE + path, params=params,
                                     headers=self._auth_headers() if auth else self.headers)
        return unwrap(payload)

    def post(self, path, params=None, data=None, json_data=None, multipart=None, auth=True):
        payload, _, _ = request_json(
            self.BASE + path, method='POST', params=params, data=data, json_data=json_data,
            multipart=multipart, headers=self._auth_headers() if auth else self.headers,
        )
        return unwrap(payload)

    def check_cached(self, hashes):
        hashes = [str(h).lower() for h in hashes if h]
        if not hashes:
            return set()
        # Object format is less ambiguous than list format and lets us support
        # both current and older TorBox response shapes.
        def parse_result(result, requested):
            found = set()
            if isinstance(result, dict):
                for key, value in result.items():
                    key_l = str(key).lower()
                    if isinstance(value, dict):
                        item_hash = str(value.get('hash') or key_l).lower()
                        if value.get('cached') is False:
                            continue
                        if value and item_hash in requested:
                            found.add(item_hash)
                    elif value and key_l in requested:
                        found.add(key_l)
            elif isinstance(result, list):
                for idx, item in enumerate(result):
                    if isinstance(item, str) and item.lower() in requested:
                        found.add(item.lower())
                    elif isinstance(item, dict):
                        item_hash = str(item.get('hash') or '').lower()
                        if item_hash in requested and item.get('cached', True) is not False:
                            found.add(item_hash)
                    elif isinstance(item, bool) and item and idx < len(requested):
                        found.add(requested[idx])
            return found

        cached = set()
        request_succeeded = False
        last_error = None
        try:
            result = self.post('/torrents/checkcached', params={'format': 'object'}, json_data={'hashes': hashes})
            request_succeeded = True
            cached.update(parse_result(result, hashes))
        except Exception as exc:
            last_error = exc
            log('TorBox POST cache check failed, trying GET: %s' % exc, xbmc.LOGWARNING)

        # GET is an intentional compatibility fallback. TorBox documents both
        # GET and POST cache endpoints, and the GET form is limited to ~100 hashes.
        # If POST returned no matches, retry in chunks to avoid silently dropping
        # every TorBox row because of a response-format/API-version mismatch.
        if not cached:
            for pos in range(0, len(hashes), 100):
                chunk = hashes[pos:pos + 100]
                try:
                    result = self.get('/torrents/checkcached', {
                        'hash': chunk, 'format': 'object', 'list_files': 'false'})
                    request_succeeded = True
                    cached.update(parse_result(result, chunk))
                except Exception as exc:
                    last_error = exc
                    log('TorBox GET cache check failed: %s' % exc, xbmc.LOGWARNING)
                    break
        if not request_succeeded and last_error is not None:
            raise last_error
        return cached

    def create_torrent(self, magnet, cached_only=False):
        result = self.post('/torrents/createtorrent', multipart={
            'magnet': magnet,
            'seed': 3,
            'allow_zip': 'true',
            'as_queued': 'false',
            'add_only_if_cached': 'true' if cached_only else 'false',
        }) or {}
        if isinstance(result, (int, str)):
            return result
        return result.get('torrent_id') or result.get('id')

    @staticmethod
    def _normalize_hash(value):
        """Normalize v1/v2 info hashes from raw values or magnet links."""
        value = str(value or '').strip()
        if not value:
            return ''
        match = re.search(r'(?i)(?:urn:btih:|btih:)([a-z0-9]+)', value)
        if match:
            value = match.group(1)
        elif value.lower().startswith('magnet:'):
            # Never treat an entire malformed magnet URI as an ownership key.
            # Without a valid hash we cannot prove whether a TorBox item existed
            # before this add-on invocation, so fail safe instead of adding it.
            return ''
        value = value.strip().lower()
        if re.fullmatch(r'[a-z2-7]{32}', value):
            try:
                return base64.b32decode(value.upper()).hex()
            except Exception:
                return value
        if re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', value):
            return value
        return ''

    @classmethod
    def _source_hash(cls, source):
        source = source or {}
        for value in (source.get('hash'), source.get('info_hash'), source.get('torrent_hash'),
                      source.get('magnet')):
            normalized = cls._normalize_hash(value)
            if normalized:
                return normalized
        return ''

    @classmethod
    def _item_hash(cls, item):
        item = item or {}
        for value in (item.get('hash'), item.get('info_hash'), item.get('torrent_hash'), item.get('magnet')):
            normalized = cls._normalize_hash(value)
            if normalized:
                return normalized
        return ''

    @staticmethod
    def _item_id(item):
        item = item or {}
        value = item.get('id')
        if value is None:
            value = item.get('torrent_id')
        return value

    @staticmethod
    def _cancelled(cancel_cb=None):
        if xbmc.Monitor().abortRequested():
            return True
        if cancel_cb:
            try:
                return bool(cancel_cb())
            except Exception:
                return False
        return False

    def torrent_info(self, torrent_id, bypass_cache=True):
        params = {'id': torrent_id}
        # TorBox caches /mylist for up to 600 seconds. Account browsing and
        # post-delete refreshes need authoritative state from the torrent client.
        if bypass_cache:
            params['bypass_cache'] = 'true'
        result = self.get('/torrents/mylist', params)
        if isinstance(result, list):
            return result[0] if result else {}
        return result or {}

    def list_torrents(self, bypass_cache=True):
        # TorBox documents /mylist as cached for up to 600 seconds. Using the
        # cached list after a delete makes a successfully removed torrent appear
        # to come back. Cloud/account browsing should reflect the live account.
        # Page through the account so ownership checks cannot miss an existing
        # torrent merely because it fell beyond the first 1,000 rows.
        rows = []
        offset = 0
        page_size = 1000
        for _ in range(20):
            params = {'limit': page_size, 'offset': offset}
            if bypass_cache:
                params['bypass_cache'] = 'true'
            result = self.get('/torrents/mylist', params)
            page = result if isinstance(result, list) else ([] if not result else [result])
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        return rows

    def delete_torrent(self, torrent_id):
        # Current TorBox control API: POST JSON {torrent_id, operation: delete}.
        # Keep the actual account ID intact; coerce numeric IDs only when safe.
        try:
            request_id = int(torrent_id)
        except (TypeError, ValueError):
            request_id = torrent_id
        result = self.post('/torrents/controltorrent', json_data={
            'torrent_id': request_id,
            'operation': 'delete',
            'all': False,
        })

        # The control request can complete just before the torrent client's list
        # reflects the removal. Force a fresh /mylist read so Kodi's subsequent
        # Container.Refresh cannot repopulate the deleted item from TorBox's
        # normal 600-second API cache. This is deliberately short and bounded.
        target = str(torrent_id)
        for _ in range(6):
            xbmc.sleep(250)
            try:
                items = self.list_torrents(bypass_cache=True)
                present = False
                for item in items:
                    item_id = item.get('id') or item.get('torrent_id')
                    if item_id is not None and str(item_id) == target:
                        present = True
                        break
                if not present:
                    break
            except Exception:
                # The delete request itself already succeeded; do not turn a
                # transient verification/read failure into a false delete error.
                break
        return result

    def list_usenet(self):
        result = self.get('/usenet/mylist', {'limit': 1000})
        return result if isinstance(result, list) else ([] if not result else [result])

    def list_webdl(self):
        result = self.get('/webdl/mylist', {'limit': 1000})
        return result if isinstance(result, list) else ([] if not result else [result])

    def request_file(self, item_type, item_id, file_id):
        endpoint = {'torrent': '/torrents/requestdl', 'usenet': '/usenet/requestdl', 'web': '/webdl/requestdl'}[item_type]
        id_key = {'torrent': 'torrent_id', 'usenet': 'usenet_id', 'web': 'web_id'}[item_type]
        params = {'token': self.token, id_key: int(item_id), 'file_id': int(file_id), 'redirect': 'false', 'append_name': 'true'}
        result = self.get(endpoint, params=params)
        if isinstance(result, dict):
            return result.get('url') or result.get('download') or result.get('link')
        return result

    def resolve_source(self, source, media, wait_seconds=120, track_cleanup=False, cancel_cb=None):
        torrent_id = None
        temporary = False
        try:
            if self._cancelled(cancel_cb):
                raise ApiError('Cancelled')

            # Establish account ownership before creating anything. TorBox may
            # return the ID of an account torrent that already has the same hash;
            # such an item belongs to the user and must never enter the temporary
            # cleanup lifecycle.
            source_hash = self._source_hash(source)
            if not source_hash:
                raise ApiError('Could not determine the torrent hash safely')
            before = self.list_torrents(bypass_cache=True)
            before_ids = {
                str(item_id) for item_id in (self._item_id(item) for item in before)
                if item_id not in (None, '')
            }
            existing = next((item for item in before if self._item_hash(item) == source_hash), None)
            if existing is not None:
                torrent_id = self._item_id(existing)
                if torrent_id in (None, ''):
                    raise ApiError('Existing TorBox torrent did not include an ID')
                log('Using existing TorBox account torrent %s; cleanup disabled' % torrent_id, xbmc.LOGDEBUG)
            else:
                if self._cancelled(cancel_cb):
                    raise ApiError('Cancelled')
                torrent_id = self.create_torrent(source['magnet'], cached_only=self.cached_only)
                # ID comparison catches an existing account item even if an API
                # response omitted or changed the hash field used above.
                temporary = bool(torrent_id not in (None, '') and str(torrent_id) not in before_ids)
            if not torrent_id:
                raise ApiError('TorBox did not return a torrent ID')
            deadline = time.time() + (20 if self.cached_only else max(30, int(wait_seconds)))
            info = {}
            while time.time() < deadline:
                if self._cancelled(cancel_cb):
                    raise ApiError('Cancelled')
                info = self.torrent_info(torrent_id)
                files = info.get('files') or []
                finished = bool(info.get('download_finished')) or info.get('download_state') in ('completed', 'cached', 'seeding')
                if files and finished:
                    break
                if self.cached_only and files and info.get('download_state') in ('downloading', 'queued'):
                    raise ApiError('Not cached on TorBox')
                xbmc.sleep(750)
            files = info.get('files') or []
            if not files:
                raise ApiError('TorBox did not return torrent contents')
            finished = bool(info.get('download_finished')) or info.get('download_state') in ('completed', 'cached', 'seeding')
            if not finished:
                if self.cached_only:
                    raise ApiError('Not cached on TorBox')
                raise ApiError('TorBox download did not finish before timeout')
            target = select_media_file(files, media, source.get('filename'), source.get('file_idx'))
            if not target:
                raise ApiError('Requested video file was not found in the TorBox torrent')
            direct = self.request_file('torrent', torrent_id, target.get('id'))
            if not direct:
                raise ApiError('TorBox did not return a playable URL')
            return (direct, str(torrent_id)) if track_cleanup and temporary else direct
        except Exception:
            # A playback-only transfer that never reached playback is safe to
            # remove immediately. Successful streams are cleaned by cleanup.py
            # only after Kodi reports that playback has stopped.
            if torrent_id and track_cleanup and temporary:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass
            raise

    def resolve_cloud_file(self, item_type, item_id, file_id):
        if item_type == 'torrent':
            info = self.torrent_info(item_id)
        elif item_type == 'usenet':
            result = self.get('/usenet/mylist', {'id': item_id})
            info = result[0] if isinstance(result, list) and result else (result or {})
        elif item_type == 'web':
            result = self.get('/webdl/mylist', {'id': item_id})
            info = result[0] if isinstance(result, list) and result else (result or {})
        else:
            raise ApiError('Unknown TorBox item type')
        target = None
        for f in info.get('files') or []:
            if str(f.get('id')) == str(file_id):
                target = f
                break
        if not target:
            raise ApiError('File no longer exists in TorBox')
        direct = self.request_file(item_type, item_id, file_id)
        if not direct:
            raise ApiError('TorBox did not return a playable URL')
        return direct, target
