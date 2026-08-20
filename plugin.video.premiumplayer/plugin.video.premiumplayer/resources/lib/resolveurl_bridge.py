# -*- coding: utf-8 -*-
"""Bridge between Premium Player and ResolveURL universal/debrid resolvers.

ResolveURL owns provider enable/disable, cached-only, and authorization settings.
Premium Player uses ResolveURL as the authorization/settings authority. Native
provider adapters reuse those ResolveURL credentials only where Premium Player
needs APIs ResolveURL does not expose generically: account/cloud browsing, exact
file selection, downloads, transfer deletion, stale-pin validation, and controlled
torrent playback for Real-Debrid, TorBox, AllDebrid, Premiumize.me, Debrid-Link,
and LinkSnappy. Other ResolveURL universal providers continue to resolve through
ResolveURL itself.
"""
import inspect

import xbmc
import xbmcaddon

from .http import ApiError
from .utils import ADDON, clean_path, log, select_media_file
from .theme import provider_color


_DUMMY_MAGNET = 'magnet:?xt=urn:btih:' + ('0' * 40)


class ResolveURLBridge:
    def __init__(self):
        self.resolveurl = None
        try:
            import resolveurl
            self.resolveurl = resolveurl
            self._migrate_legacy_credentials_once()
        except Exception as exc:
            log('ResolveURL unavailable: %s' % exc, xbmc.LOGWARNING)

    @property
    def available(self):
        return self.resolveurl is not None

    def _migrate_legacy_credentials_once(self):
        """One-time migration from Premium Player 0.1.0-0.1.3 credentials.

        This prevents users who already authorized RD/TorBox from being forced to
        re-pair after the ResolveURL transition. Legacy credentials are cleared
        after the transfer so a later ResolveURL revoke is not silently undone.
        """
        if ADDON.getSetting('resolveurl_migration_done').lower() == 'true':
            return
        try:
            rurl_addon = xbmcaddon.Addon('script.module.resolveurl')
        except Exception:
            return

        try:
            rd_map = {
                'token': 'rd_access_token',
                'refresh': 'rd_refresh_token',
                'client_id': 'rd_client_id',
                'client_secret': 'rd_client_secret',
            }
            copied_rd = False
            for rkey, pkey in rd_map.items():
                old = ADDON.getSetting(pkey)
                key = 'RealDebridResolver_%s' % rkey
                if old and not rurl_addon.getSetting(key):
                    rurl_addon.setSetting(key, old)
                    copied_rd = True
            if copied_rd or rurl_addon.getSetting('RealDebridResolver_token'):
                rurl_addon.setSetting('RealDebridResolver_enabled', 'true')
                rurl_addon.setSetting('RealDebridResolver_torrents', 'true')
                # Preserve Premium Player's existing cached-only choice when we
                # are the component that migrated the authorization. If RD was
                # already configured in ResolveURL, leave its global choice alone.
                if copied_rd:
                    old_cached = ADDON.getSetting('rd_cached_only')
                    if old_cached:
                        rurl_addon.setSetting('RealDebridResolver_cached_only', old_cached)

            old_tb = ADDON.getSetting('tb_token')
            copied_tb = False
            if old_tb and not rurl_addon.getSetting('TorBoxResolver_apikey'):
                rurl_addon.setSetting('TorBoxResolver_apikey', old_tb)
                copied_tb = True
            if copied_tb or rurl_addon.getSetting('TorBoxResolver_apikey'):
                rurl_addon.setSetting('TorBoxResolver_enabled', 'true')
                rurl_addon.setSetting('TorBoxResolver_torrents', 'true')
                if copied_tb:
                    old_cached = ADDON.getSetting('tb_cached_only')
                    if old_cached:
                        rurl_addon.setSetting('TorBoxResolver_cached_only', old_cached)

            # Remove duplicate credential copies after migration. Settings stay
            # present (hidden) only for upgrade compatibility with older builds.
            for key in ('rd_client_id', 'rd_client_secret', 'rd_access_token',
                        'rd_refresh_token', 'rd_token_expires', 'rd_username',
                        'tb_token', 'tb_username'):
                try:
                    ADDON.setSetting(key, '')
                except Exception:
                    pass
            ADDON.setSetting('resolveurl_migration_done', 'true')
        except Exception as exc:
            log('ResolveURL legacy credential migration failed: %s' % exc, xbmc.LOGWARNING)

    def display_settings(self):
        if not self.available:
            raise ApiError('ResolveURL is not installed or could not be loaded')
        # Use ResolveURL's documented public settings entry point. The settings.xml
        # action used inside Premium Player has option="close" so Kodi closes
        # our settings dialog before opening ResolveURL; calls from the normal
        # Premium Player directory can safely use this public API directly.
        display = getattr(self.resolveurl, 'display_settings', None)
        if not display:
            raise ApiError('This ResolveURL version does not expose display_settings()')
        display()

    def _classes(self, include_disabled=False):
        if not self.available:
            return []
        try:
            classes = self.resolveurl.relevant_resolvers(
                include_universal=True,
                include_popups=True,
                include_disabled=include_disabled,
                order_matters=True,
            )
        except TypeError:
            classes = self.resolveurl.relevant_resolvers(
                include_universal=True,
                include_popups=True,
                include_disabled=include_disabled,
            )
        result = []
        for cls in classes or []:
            try:
                if cls.isUniversal():
                    result.append(cls)
            except Exception:
                continue
        return result

    @staticmethod
    def _code(name):
        known = {
            'real-debrid': 'RD',
            'torbox': 'TB',
            'alldebrid': 'AD',
            'premiumize.me': 'PM',
            'premiumize': 'PM',
            'debrid-link': 'DL',
            'linksnappy': 'LS',
        }
        key = str(name or '').strip().lower()
        if key in known:
            return known[key]
        words = [w for w in str(name or '').replace('-', ' ').replace('.', ' ').split() if w]
        return ''.join(w[0].upper() for w in words)[:3] or 'RU'

    @staticmethod
    def _color(code, name=''):
        return provider_color(code, name)

    def _supports_magnets(self, cls):
        try:
            if cls.get_setting('torrents') == 'false':
                return False
        except Exception:
            pass
        try:
            resolver = cls()
            return bool(resolver.valid_url(_DUMMY_MAGNET, 'magnet'))
        except Exception as exc:
            log('ResolveURL magnet capability probe failed for %s: %s' % (
                getattr(cls, 'name', cls.__name__), exc), xbmc.LOGDEBUG)
            return False

    def authorized_providers(self, torrent_capable_only=True):
        providers = []
        for cls in self._classes(include_disabled=False):
            try:
                if not cls._is_enabled():
                    continue
            except Exception:
                continue
            if torrent_capable_only and not self._supports_magnets(cls):
                continue
            name = str(getattr(cls, 'name', cls.__name__))
            code = self._code(name)
            try:
                cached_only = cls.get_setting('cached_only') == 'true'
            except Exception:
                cached_only = False
            try:
                priority = int(cls._get_priority())
            except Exception:
                priority = 100
            providers.append({
                'id': 'resolveurl:%s' % cls.__name__,
                'class': cls.__name__,
                'name': name,
                'code': code,
                'color': self._color(code, name),
                'cached_only': cached_only,
                'priority': priority,
            })
        providers.sort(key=lambda p: (int(p.get('priority') or 100), p['name'].lower()))
        return providers

    def get_provider(self, provider_id):
        class_name = str(provider_id or '').split(':', 1)[-1]
        for cls in self._classes(include_disabled=False):
            if cls.__name__ == class_name:
                name = str(getattr(cls, 'name', class_name))
                code = self._code(name)
                return cls, {
                    'id': 'resolveurl:%s' % class_name,
                    'class': class_name,
                    'name': name,
                    'code': code,
                    'color': self._color(code, name),
                    'cached_only': cls.get_setting('cached_only') == 'true',
                    'priority': cls._get_priority(),
                }
        return None, None

    def resolve_source(self, provider_id, source, media):
        cls, provider = self.get_provider(provider_id)
        if not cls or not provider:
            raise ApiError('ResolveURL provider is no longer enabled or authorized')
        resolver = cls()
        magnet = source.get('magnet')
        if not magnet:
            raise ApiError('Torrent source has no magnet link')
        try:
            resolver.login()
        except Exception:
            pass
        if not resolver.valid_url(magnet, 'magnet'):
            raise ApiError('%s is not configured for torrents/magnets' % provider['name'])
        host_media = resolver.get_host_and_id(magnet)
        if not host_media or len(host_media) != 2:
            raise ApiError('%s could not parse the magnet link' % provider['name'])
        host, media_id = host_media

        spec = inspect.getfullargspec(resolver.get_media_url)
        kwargs = {}
        if 'cached_only' in spec.args:
            kwargs['cached_only'] = bool(provider.get('cached_only'))
        can_return_all = 'return_all' in spec.args
        if media.get('type') == 'series' and not can_return_all:
            raise ApiError('%s cannot expose torrent files for exact episode selection' % provider['name'])
        if can_return_all:
            kwargs['return_all'] = True

        result = resolver.get_media_url(host, media_id, **kwargs)
        if not result:
            raise ApiError('%s did not return a playable link' % provider['name'])

        if isinstance(result, (list, tuple)):
            candidates = []
            for item in result:
                if isinstance(item, dict):
                    name = clean_path(item.get('name') or item.get('path') or item.get('filename'))
                    link = item.get('link') or item.get('url')
                    if name and link:
                        candidates.append({'path': name, 'name': name, 'link': link,
                                           'bytes': item.get('bytes') or item.get('size') or 0})
            target = select_media_file(candidates, media, source.get('filename'), source.get('file_idx'))
            if not target:
                raise ApiError('Requested video file was not found in the %s torrent' % provider['name'])
            result = target.get('link')
            if not result:
                raise ApiError('%s did not return a link for the selected file' % provider['name'])
            try:
                hmf = self.resolveurl.HostedMediaFile(result)
                if hmf:
                    resolved = hmf.resolve()
                    if resolved:
                        result = resolved
            except Exception as exc:
                log('ResolveURL second-stage resolve failed for %s: %s' % (provider['name'], exc), xbmc.LOGDEBUG)

        if isinstance(result, dict):
            result = result.get('url') or result.get('link')
        if not isinstance(result, str) or not result:
            raise ApiError('%s did not return a playable URL' % provider['name'])
        return result, provider['name']
