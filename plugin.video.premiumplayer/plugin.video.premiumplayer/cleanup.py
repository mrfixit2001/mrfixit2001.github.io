# -*- coding: utf-8 -*-
"""Silent post-playback cleanup worker for temporary provider transfers.

This script is launched asynchronously with Kodi's RunScript built-in after a
source has actually started. It never opens dialogs and never manipulates Kodi
windows or containers; it only waits for playback to stop, then calls the
provider's delete API for the exact temporary item created for that playback.
"""
import sys
import time

import xbmc

from resources.lib.debrid.registry import delete_playback_item
from resources.lib.utils import log, setting_bool


def _is_video_playing(player):
    try:
        if player.isPlayingVideo() or player.isPlaying():
            return True
    except Exception:
        pass
    try:
        return bool(xbmc.getCondVisibility('Player.HasVideo'))
    except Exception:
        return False


def _wait_until_stopped():
    """Wait for the current video stream to stop/end without touching the GUI."""
    monitor = xbmc.Monitor()
    player = xbmc.Player()
    saw_playback = False
    inactive_since = None
    started = time.time()

    while not monitor.abortRequested():
        active = _is_video_playing(player)
        if active:
            saw_playback = True
            inactive_since = None
        else:
            # Normally the worker is launched only after AV start was confirmed.
            # If it starts after an extremely fast Stop, allow a short grace
            # period, then treat the already-inactive player as stopped.
            if saw_playback or (time.time() - started) >= 2.0:
                if inactive_since is None:
                    inactive_since = time.time()
                elif (time.time() - inactive_since) >= 1.25:
                    return True
        if monitor.waitForAbort(0.20):
            return False
    return False


def main():
    if len(sys.argv) < 3:
        return
    provider = str(sys.argv[1] or '').strip().lower()
    item_id = str(sys.argv[2] or '').strip()
    mode = str(sys.argv[3] if len(sys.argv) > 3 else 'wait').strip().lower()
    if not provider or not item_id:
        return

    # Re-check at execution/deletion time so disabling the setting while a video
    # is playing cancels the pending cleanup without any prompt.
    if not setting_bool('auto_cleanup_playback', True):
        log('Playback cleanup disabled; leaving %s item %s' % (provider, item_id), xbmc.LOGDEBUG)
        return

    if mode != 'now' and not _wait_until_stopped():
        return

    if not setting_bool('auto_cleanup_playback', True):
        log('Playback cleanup disabled before deletion; leaving %s item %s' % (provider, item_id), xbmc.LOGDEBUG)
        return

    try:
        if delete_playback_item(provider, item_id):
            log('Removed temporary playback item: %s %s' % (provider, item_id), xbmc.LOGDEBUG)
    except Exception as exc:
        # Cleanup is deliberately best-effort and silent. Playback must never be
        # interrupted or followed by a dialog because a provider delete failed.
        log('Background playback cleanup failed for %s %s: %s' % (provider, item_id, exc), xbmc.LOGWARNING)


if __name__ == '__main__':
    main()
