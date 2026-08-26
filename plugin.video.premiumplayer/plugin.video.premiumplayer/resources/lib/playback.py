# -*- coding: utf-8 -*-
import os
import re
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from .debrid.realdebrid import RealDebrid
from .debrid.torbox import TorBox
from .debrid.alldebrid import AllDebrid
from .debrid.premiumize import Premiumize
from .debrid.debridlink import DebridLink
from .debrid.linksnappy import LinkSnappy
from .debrid.registry import delete_playback_item
from .http import ApiError
from .resolveurl_bridge import ResolveURLBridge
from .utils import (ADDON_NAME, fallback_indices, human_size, load_session, log,
                    setting_bool, setting_int)
from .theme import provider_text


class StartupPlayer(xbmc.Player):
    def __init__(self):
        super().__init__()
        self.started = False
        self.failed = False
        self.stopped_before_start = False
        self.ended = False
        self.user_stopped = False

    def onAVStarted(self):
        self.started = True
        log('Player callback: AV started', xbmc.LOGDEBUG)

    def onPlayBackError(self):
        self.failed = True
        log('Player callback: playback error', xbmc.LOGDEBUG)

    def onPlayBackStopped(self):
        if not self.started:
            self.stopped_before_start = True
            log('Player callback: stopped before AV start', xbmc.LOGDEBUG)
        else:
            self.user_stopped = True
            log('Player callback: user stopped playback', xbmc.LOGDEBUG)

    def onPlayBackEnded(self):
        self.ended = True
        log('Player callback: playback ended naturally', xbmc.LOGDEBUG)


class PlaybackEngine:
    def __init__(self):
        self.rd = RealDebrid()
        self.tb = TorBox()
        self.ad = AllDebrid()
        self.pm = Premiumize()
        self.dl = DebridLink()
        self.ls = LinkSnappy()
        self.resolveurl = ResolveURLBridge()

    @staticmethod
    def _source_name(source):
        value = source.get('filename') or source.get('title') or source.get('name') or source.get('hash') or ''
        return re.sub(r'\s+', ' ', str(value).replace('\r', ' ').replace('\n', ' ')).strip()

    def _attempt_text(self, source_index, source):
        provider = source.get('provider_code') or source.get('provider_name') or source.get('provider') or '?'
        provider_display = provider_text(provider, provider, source.get('provider_name') or '')
        quality = source.get('quality') or 'SD'
        quality = {'1080P': '1080p', '720P': '720p'}.get(quality, quality)
        size = human_size(source.get('size_bytes')) if source.get('size_bytes') else 'SIZE ?'
        return 'Source %d - Trying Playback | %s | %s | %s | %s' % (
            source_index + 1, provider_display, quality, size, self._source_name(source))

    @staticmethod
    def _unpack_native_resolution(result, provider_key, provider_name, track_cleanup):
        cleanup = None
        direct = result
        if track_cleanup and isinstance(result, tuple) and len(result) == 2:
            direct, item_id = result
            if item_id not in (None, ''):
                cleanup = {'provider': provider_key, 'item_id': str(item_id)}
        return direct, provider_name, cleanup

    @staticmethod
    def _schedule_cleanup(cleanup, wait_for_playback=True):
        """Start a silent independent cleanup worker.

        Do not keep the playable-plugin invocation alive just to delete a provider
        transfer after Stop; that would re-couple cleanup to Kodi's player teardown.
        The worker performs no GUI/container operations.
        """
        if not cleanup or not setting_bool('auto_cleanup_playback', True):
            return False
        provider = str(cleanup.get('provider') or '').strip().lower()
        item_id = str(cleanup.get('item_id') or '').strip()
        if not provider or not item_id:
            return False
        try:
            addon_path = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('path'))
            script = os.path.join(addon_path, 'cleanup.py')
            mode = 'wait' if wait_for_playback else 'now'
            def quote(value):
                return str(value).replace('\\', '\\\\').replace('"', '\\"')
            command = 'RunScript("%s","%s","%s","%s")' % (
                quote(script), quote(provider), quote(item_id), mode)
            xbmc.executebuiltin(command)
            log('Scheduled background playback cleanup: %s %s (%s)' % (provider, item_id, mode), xbmc.LOGDEBUG)
            return True
        except Exception as exc:
            log('Could not schedule playback cleanup for %s %s: %s' % (provider, item_id, exc), xbmc.LOGWARNING)
            return False

    @staticmethod
    def _cleanup_now(cleanup):
        """Synchronously remove a temporary item when no playback is active."""
        if not cleanup or not setting_bool('auto_cleanup_playback', True):
            return False
        provider = str(cleanup.get('provider') or '').strip().lower()
        item_id = str(cleanup.get('item_id') or '').strip()
        if not provider or not item_id:
            return False
        try:
            deleted = delete_playback_item(provider, item_id)
            if deleted:
                log('Removed unused temporary item: %s %s' % (provider, item_id), xbmc.LOGDEBUG)
            return deleted
        except Exception as exc:
            # Deletion may involve a short provider API round trip, but doing it
            # here makes cancellation deterministic. Fall back to the silent
            # worker so a transient API failure still gets another attempt.
            log('Immediate cleanup failed for %s %s: %s' % (provider, item_id, exc), xbmc.LOGWARNING)
            return PlaybackEngine._schedule_cleanup(cleanup, wait_for_playback=False)

    def resolve_source(self, source, media, track_cleanup=False, cancel_cb=None):
        wait = setting_int('uncached_wait', 120)
        requested = str(source.get('provider') or '')
        # ResolveURL owns authorization/settings. Native clients are used where
        # Premium Player needs exact-file selection and provider lifecycle control.
        if requested == 'resolveurl:RealDebridResolver':
            if not (self.rd.enabled and self.rd.authorized):
                raise ApiError('Real-Debrid is not enabled and authorized in ResolveURL')
            result = self.rd.resolve_source(source, media, wait_seconds=wait, track_cleanup=track_cleanup)
            return self._unpack_native_resolution(result, 'rd', 'Real-Debrid', track_cleanup)
        if requested == 'resolveurl:TorBoxResolver':
            if not (self.tb.enabled and self.tb.authorized):
                raise ApiError('TorBox is not enabled and authorized in ResolveURL')
            if self.tb.cached_only and source.get('tb_cached') is False:
                raise ApiError('TorBox: not cached')
            result = self.tb.resolve_source(
                source, media, wait_seconds=wait, track_cleanup=track_cleanup,
                cancel_cb=cancel_cb)
            return self._unpack_native_resolution(result, 'tb', 'TorBox', track_cleanup)
        if requested == 'resolveurl:AllDebridResolver':
            if not (self.ad.enabled and self.ad.authorized):
                raise ApiError('AllDebrid is not enabled and authorized in ResolveURL')
            result = self.ad.resolve_source(source, media, wait_seconds=wait, track_cleanup=track_cleanup)
            return self._unpack_native_resolution(result, 'ad', 'AllDebrid', track_cleanup)
        if requested == 'resolveurl:PremiumizeMeResolver':
            if not (self.pm.enabled and self.pm.authorized):
                raise ApiError('Premiumize.me is not enabled and authorized in ResolveURL')
            if self.pm.cached_only and source.get('pm_cached') is False:
                raise ApiError('Premiumize.me: not cached')
            result = self.pm.resolve_source(source, media, wait_seconds=wait, track_cleanup=track_cleanup)
            return self._unpack_native_resolution(result, 'pm', 'Premiumize.me', track_cleanup)
        if requested == 'resolveurl:DebridLinkResolver':
            if not (self.dl.enabled and self.dl.authorized):
                raise ApiError('Debrid-Link is not enabled and authorized in ResolveURL')
            result = self.dl.resolve_source(source, media, wait_seconds=wait, track_cleanup=track_cleanup)
            return self._unpack_native_resolution(result, 'dl', 'Debrid-Link', track_cleanup)
        if requested == 'resolveurl:LinkSnappyResolver':
            if not (self.ls.enabled and self.ls.authorized):
                raise ApiError('LinkSnappy is not enabled and authorized in ResolveURL')
            if self.ls.cached_only and source.get('ls_cached') is False:
                raise ApiError('LinkSnappy: not cached')
            result = self.ls.resolve_source(source, media, wait_seconds=wait, track_cleanup=track_cleanup)
            return self._unpack_native_resolution(result, 'ls', 'LinkSnappy', track_cleanup)
        if requested.startswith('resolveurl:'):
            direct, provider = self.resolveurl.resolve_source(requested, source, media)
            # ResolveURL's public interface does not expose a universal transfer ID
            # that Premium Player can safely delete, so generic providers are not
            # auto-cleaned unless/until a native lifecycle adapter exists.
            return direct, provider, None
        if requested == 'rd':
            if not (self.rd.enabled and self.rd.authorized):
                raise ApiError('Real-Debrid is not enabled and authorized')
            result = self.rd.resolve_source(source, media, wait_seconds=wait, track_cleanup=track_cleanup)
            return self._unpack_native_resolution(result, 'rd', 'Real-Debrid', track_cleanup)
        if requested == 'tb':
            if not (self.tb.enabled and self.tb.authorized):
                raise ApiError('TorBox is not enabled and authorized')
            if self.tb.cached_only and source.get('tb_cached') is False:
                raise ApiError('TorBox: not cached')
            result = self.tb.resolve_source(
                source, media, wait_seconds=wait, track_cleanup=track_cleanup,
                cancel_cb=cancel_cb)
            return self._unpack_native_resolution(result, 'tb', 'TorBox', track_cleanup)
        raise ApiError('No enabled, authorized provider can resolve this source')

    def play_session(self, session_id, selected_index, handle=-1, force_auto=None, force_wrap=None, return_outcome=False):
        session = load_session(session_id)
        if not session:
            xbmcgui.Dialog().ok(ADDON_NAME, 'This source list expired. Please search again.')
            return False
        sources = session.get('sources') or []
        media = session.get('media') or {}
        if not sources:
            xbmcgui.Dialog().ok(ADDON_NAME, 'No torrent sources are available.')
            return False
        selected_index = max(0, min(int(selected_index), len(sources) - 1))
        auto = setting_bool('auto_fallback', True) if force_auto is None else bool(force_auto)
        wrap = setting_bool('wrap_fallback', True) if force_wrap is None else bool(force_wrap)
        order = fallback_indices(len(sources), selected_index, wrap=wrap) if auto else [selected_index]

        progress = xbmcgui.DialogProgress()
        progress.create(ADDON_NAME, self._attempt_text(selected_index, sources[selected_index]))
        resolved_once = False
        cancelled = False
        success_index = None
        success_player = None
        cleanup_enabled = setting_bool('auto_cleanup_playback', True)
        try:
            for attempt_num, source_index in enumerate(order):
                if progress.iscanceled():
                    cancelled = True
                    break
                source = sources[source_index]
                pct = int((attempt_num * 100) / max(len(order), 1))
                progress.update(pct, self._attempt_text(source_index, source))
                cleanup = None
                try:
                    direct, provider, cleanup = self.resolve_source(
                        source, media, track_cleanup=cleanup_enabled,
                        cancel_cb=progress.iscanceled)
                except Exception as exc:
                    log('Source #%d resolution failed: %s' % (source_index + 1, exc), xbmc.LOGWARNING)
                    if not auto:
                        break
                    continue

                # Cancellation can land after the provider returns a URL but
                # before playback starts. Remove any newly-created item now;
                # an existing account torrent never produces a cleanup token.
                if progress.iscanceled():
                    cancelled = True
                    if cleanup:
                        self._cleanup_now(cleanup)
                    break

                use_resolved_url = handle >= 0 and not resolved_once
                if use_resolved_url:
                    resolved_once = True
                try:
                    started, player = self._play_and_confirm(
                        direct, media, source, provider, handle=handle,
                        use_resolved_url=use_resolved_url)
                except Exception:
                    if cleanup:
                        self._cleanup_now(cleanup)
                    raise
                if started:
                    # The provider-side item must remain alive while streaming.
                    # A separate silent worker waits for Stop/End and deletes it.
                    if cleanup:
                        self._schedule_cleanup(cleanup, wait_for_playback=True)
                    success_index = source_index
                    success_player = player
                    break
                if cleanup:
                    # No stream started, so the temporary item can be removed now
                    # without waiting on player state.
                    self._cleanup_now(cleanup)
                log('Source #%d playback did not start (%s)' % (source_index + 1, provider), xbmc.LOGWARNING)
                if not auto:
                    break
        finally:
            try:
                progress.close()
            except Exception:
                pass

        if success_index is not None:
            # Play All needs the natural-end-vs-user-stop result, so it must stay
            # alive for the duration of the episode. Ordinary playback does not.
            if return_outcome:
                end_reason = self._wait_for_playback_end(success_player)
                return {'index': success_index, 'end_reason': end_reason}

            # Common case: the exact row the user selected started successfully.
            # Hand control back to Kodi immediately. In particular, do not keep a
            # playable-plugin invocation alive until the later Stop event merely
            # to restore a cursor that is already on the correct row.
            if success_index == selected_index:
                return success_index

            # Fallback succeeded on a different displayed row. Keep this call
            # alive only long enough to know playback has ended; app.py then does
            # a best-effort focus move in the existing source list, never a
            # Container.Update/Refresh.
            self._wait_for_playback_end(success_player)
            return success_index

        if not cancelled:
            xbmcgui.Dialog().ok(ADDON_NAME, 'No sources were able to be played')
        return None

    @staticmethod
    def _wait_for_playback_end(player):
        monitor = xbmc.Monitor()
        # Wait for the player callback, then require the core player/fullscreen
        # state to remain inactive before returning control to navigation code.
        reason = None
        while not monitor.abortRequested():
            if getattr(player, 'ended', False):
                reason = 'ended'
                break
            if getattr(player, 'user_stopped', False):
                reason = 'stopped'
                break
            try:
                if not player.isPlayingVideo() and not player.isPlaying():
                    if monitor.waitForAbort(0.25):
                        return 'stopped'
                    if getattr(player, 'ended', False):
                        reason = 'ended'
                    elif getattr(player, 'user_stopped', False):
                        reason = 'stopped'
                    else:
                        reason = 'stopped'
                    break
            except Exception:
                reason = 'stopped'
                break
            if monitor.waitForAbort(0.25):
                return 'stopped'

        if reason is None:
            return 'stopped'

        # Kodi can fire onPlayBackStopped before FullscreenVideo and the player
        # have completely torn down. Do not let caller manipulate GUI/container
        # state until this has remained stable for ~800 ms.
        stable = 0
        for _ in range(80):
            if monitor.abortRequested():
                break
            try:
                playing = player.isPlaying() or player.isPlayingVideo() or xbmc.getCondVisibility('Player.HasVideo')
            except Exception:
                playing = False
            try:
                fullscreen = xbmc.getCondVisibility('Window.IsVisible(fullscreenvideo)')
            except Exception:
                fullscreen = False
            if not playing and not fullscreen:
                stable += 1
                if stable >= 8:
                    break
            else:
                stable = 0
            if monitor.waitForAbort(0.1):
                break
        return reason

    def _play_and_confirm(self, direct_url, media, source, provider, handle=-1, use_resolved_url=False):
        player = StartupPlayer()
        item = xbmcgui.ListItem(label=media.get('label') or media.get('title') or source.get('filename') or 'Premium Player')
        item.setPath(direct_url)
        item.setProperty('IsPlayable', 'true')
        info = {'title': media.get('title') or source.get('filename') or 'Premium Player'}
        if media.get('season') is not None:
            info['season'] = int(media['season'])
        if media.get('episode') is not None:
            info['episode'] = int(media['episode'])
        try:
            item.setInfo('video', info)
        except Exception:
            pass
        timeout = max(5, setting_int('startup_timeout', 15))
        log('Playing selected torrent via %s: %s' % (provider, source.get('hash')), xbmc.LOGINFO)
        if use_resolved_url and handle >= 0:
            xbmcplugin.setResolvedUrl(handle, True, item)
        else:
            player.play(direct_url, item)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if xbmc.Monitor().abortRequested():
                return False, player
            if player.started:
                return True, player
            if player.failed:
                try:
                    player.stop()
                except Exception:
                    pass
                return False, player
            try:
                if player.isPlayingVideo():
                    xbmc.sleep(1200)
                    if player.isPlayingVideo():
                        return True, player
            except Exception:
                pass
            if player.stopped_before_start:
                return False, player
            xbmc.sleep(200)
        try:
            player.stop()
        except Exception:
            pass
        return False, player
