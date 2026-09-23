# -*- coding: utf-8 -*-
import os
import re
import sys
from urllib import parse

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs

from .debrid.realdebrid import RealDebrid
from .debrid.torbox import TorBox
from .debrid.alldebrid import AllDebrid
from .debrid.premiumize import Premiumize
from .debrid.debridlink import DebridLink
from .debrid.linksnappy import LinkSnappy
from .debrid.registry import client_for_account, PROVIDER_CODES
from .downloads import download_url
from .http import ApiError
from .metadata import MetadataClient
from .account_metadata import (clear_account_metadata_cache, looks_like_series_tree,
                               match_entries, media_sort_key, update_account_item_count)
from .playback import PlaybackEngine
from .resolveurl_bridge import ResolveURLBridge
from .sources import TorrentSourceClient
from .theme import color_text, media_text, media_tag, provider_color, provider_text, STATUS_COLORS
from .utils import (ADDON, ADDON_NAME, clean_path, human_size, is_archive, is_video,
                    clear_active_source_session, clear_new_releases_cache, ensure_new_releases_cache_schema, load_active_source_state,
                    load_download_history, load_new_releases_cache, load_pins, load_search_history,
                    load_session, log, plugin_url, remember_search, remove_download_history, remove_pin,
                    mark_new_releases_menu_rendered, prepare_new_releases_child_entry, remove_search_history, save_new_releases_cache, save_pin, save_pins, save_session,
                    set_active_source_focus, set_active_source_session, setting_bool)

BASE = sys.argv[0] if len(sys.argv) > 0 else 'plugin://plugin.video.premiumplayer/'
HANDLE = int(sys.argv[1]) if len(sys.argv) > 1 and str(sys.argv[1]).lstrip('-').isdigit() else -1
ADDON_PATH = ADDON.getAddonInfo('path')
MENU_MEDIA_ROOT = os.path.join(ADDON_PATH, 'resources', 'media', 'menu')
MENU_FANART = os.path.join(ADDON_PATH, 'resources', 'media', 'fanart.jpg')
TERMS_VERSION = '2026-08-25-v2'
NEW_RELEASES_PAGE_SIZE = 100
NEW_RELEASES_MAX_RESULTS = 300


DEFAULT_SETTINGS = {
    'terms_agreed': 'false',
    'terms_version': '',
    'max_quality': '0',
    'max_size_gb': '10',
    'source_limit': '80',
    'metadata_base': 'https://v3-cinemeta.strem.io',
    'source_base': 'https://torrentio.strem.fun',
    'auto_fallback': 'true',
    'wrap_fallback': 'true',
    'startup_timeout': '15',
    'uncached_wait': '120',
    'auto_cleanup_playback': 'true',
    'download_path': 'special://home/../',
    'create_media_folders': 'true',
    'debug': 'false',
    'resolveurl_migration_done': 'false',
    # Legacy Premium Player credential copies are reset too. ResolveURL owns its
    # own settings and authorizations, so this deliberately does not touch them.
    'rd_enabled': 'true',
    'rd_cached_only': 'true',
    'rd_client_id': '',
    'rd_client_secret': '',
    'rd_access_token': '',
    'rd_refresh_token': '',
    'rd_token_expires': '0',
    'rd_username': '',
    'tb_enabled': 'true',
    'tb_cached_only': 'true',
    'tb_token': '',
    'tb_username': '',
}


def params():
    raw = sys.argv[2][1:] if len(sys.argv) > 2 and sys.argv[2].startswith('?') else ''
    return {k: v[-1] for k, v in parse.parse_qs(raw).items()}


def _menu_art(name):
    if not name:
        return None
    path = os.path.join(MENU_MEDIA_ROOT, '%s.png' % str(name))
    # Match the known-working TheSportsSearch ListItem artwork contract.
    return {'icon': path, 'thumb': path, 'fanart': MENU_FANART}


def add_item(label, action=None, folder=True, art=None, info=None, context=None, playable=None,
             properties=None, menu_icon=None, **kwargs):
    # Directory entries are built off-screen, matching Kodi's recommended
    # pattern for plugin listings.  In particular this preserves the supplied
    # ListItem.Icon/Thumb artwork for skins that render generic file menus.
    item = xbmcgui.ListItem(label=label, offscreen=True)
    if properties:
        for key, value in properties.items():
            try:
                item.setProperty(str(key), str(value))
            except Exception:
                pass
    if not art and menu_icon:
        art = _menu_art(menu_icon)
    if art:
        try:
            item.setArt(art)
        except Exception:
            pass
    if info:
        try:
            item.setInfo('video', info)
        except Exception:
            pass
    if playable is None:
        playable = not folder
    if playable:
        item.setProperty('IsPlayable', 'true')
    if context:
        item.addContextMenuItems(context)
    url = plugin_url(BASE, action=action, **kwargs) if action else BASE
    xbmcplugin.addDirectoryItem(HANDLE, url, item, isFolder=folder)


def _metadata_context(media_type='', title='', imdb='', year='', season=''):
    context = {}
    if media_type:
        context['media_type'] = str(media_type)
    if title:
        context['title'] = str(title)
    if imdb:
        context['imdb'] = str(imdb)
    if year not in ('', None):
        try:
            context['year'] = int(year)
        except Exception:
            pass
    if season not in ('', None):
        try:
            context['season'] = int(season)
        except Exception:
            pass
    return context


def _metadata_context_kwargs(context):
    context = context or {}
    return {
        'meta_type': context.get('media_type'),
        'meta_title': context.get('title'),
        'meta_imdb': context.get('imdb'),
        'meta_year': context.get('year'),
        'meta_season': context.get('season'),
    }


def _child_metadata_context(matched, inherited=None):
    return (matched or {}).get('context') or (inherited or {})


def end(content='files'):
    if HANDLE >= 0:
        try:
            xbmcplugin.setContent(HANDLE, content)
        except Exception:
            pass
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def end_menu():
    """Finish a navigation menu without assigning a media content type.

    Kodi skins can then render ListItem.Icon in the actual row instead of
    substituting folder/watched status glyphs. Result directories continue to
    use end() and retain their existing content types.
    """
    if HANDLE >= 0:
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def notify(message, error=False):
    xbmcgui.Dialog().notification(ADDON_NAME, str(message),
                                  xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO, 4500)


def _close_addon():
    if HANDLE >= 0:
        try:
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
        except Exception:
            pass
    try:
        xbmc.executebuiltin('Container.GoBack')
    except Exception:
        pass


def ensure_terms_agreed():
    if (setting_bool('terms_agreed', False) and
            ADDON.getSetting('terms_version') == TERMS_VERSION):
        return True
    message = (
        'Premium Player is software only. It does not host, upload, store, provide, sell, '
        'license, control, or distribute any movies, television programs, streams, '
        'torrents, files, links, or other media.\n\n'
        'All media metadata, artwork, search results, source listings, torrent information, '
        'links, files, streams, and provider data displayed or accessed through this add-on '
        'come from independent third-party internet locations, services, networks, indexes, '
        'or accounts. Those third parties are not owned, operated, controlled, sponsored, '
        'endorsed, or affiliated with Premium Player or its author. Neither the add-on nor '
        'its author verifies or represents that any item is lawful, licensed, authorized, '
        'accurate, safe, or available.\n\n'
        'You are solely responsible for determining and obtaining every right, license, '
        'subscription, permission, and other authorization required for each source or item '
        'you access, stream, download, copy, distribute, or otherwise use. You must comply '
        'with all applicable laws, including copyright and intellectual-property laws, and '
        'all third-party terms. A result\'s appearance or availability is not permission or '
        'proof of legality.\n\n'
        'Your use is entirely at your own risk. To the maximum extent permitted by applicable '
        'law, Premium Player and its author disclaim all warranties and liability for your '
        'use; third-party content, services, or conduct; and any legal claim, account action, '
        'loss, damage, security or privacy issue, or other consequence arising from them. You '
        'agree to defend, indemnify, and hold harmless Premium Player and its author from '
        'claims, liabilities, damages, losses, costs, and expenses arising from your use, '
        'your violation of law or third-party terms, or your infringement of another party\'s '
        'rights.\n\n'
        'By selecting AGREE, you confirm that you have read, understood, and accepted this '
        'notice and assume sole legal responsibility for your use. Select DISAGREE to close '
        'the add-on.'
    )
    agreed = xbmcgui.Dialog().yesno(
        '%s - User Agreement' % ADDON_NAME, message,
        nolabel='DISAGREE', yeslabel='AGREE')
    if agreed:
        ADDON.setSetting('terms_agreed', 'true')
        ADDON.setSetting('terms_version', TERMS_VERSION)
        return True
    _close_addon()
    return False


def reset_defaults():
    confirmed = xbmcgui.Dialog().yesno(
        ADDON_NAME,
        'Reset every Premium Player setting to its default value?\n\n'
        'The user agreement will be shown again the next time the add-on starts. '
        'ResolveURL account authorizations are not changed.',
        nolabel='CANCEL', yeslabel='RESET')
    if not confirmed:
        return
    for setting_id, value in DEFAULT_SETTINGS.items():
        ADDON.setSetting(setting_id, value)
    clear_active_source_session()
    notify('All Premium Player settings were reset to defaults.')


def _active_providers():
    # ResolveURL is the single source of truth for provider authorization and
    # enable/disable state. Native RD/TB clients only reuse those credentials for
    # richer cloud browsing/deletion APIs.
    bridge = ResolveURLBridge()
    rurl = bridge.authorized_providers(torrent_capable_only=True)
    rd = RealDebrid()
    tb = TorBox()
    names = {str(p.get('name') or '').strip().lower() for p in rurl}
    classes = {str(p.get('class') or '') for p in rurl}
    rd_active = 'real-debrid' in names or 'RealDebridResolver' in classes
    tb_active = 'torbox' in names or 'TorBoxResolver' in classes
    return rd, tb, rd_active, tb_active, bridge, rurl


def _require_search_provider(show_dialog=True):
    rd, tb, rd_active, tb_active, bridge, rurl = _active_providers()
    if rd_active or tb_active or rurl:
        return rd, tb, rd_active, tb_active, bridge, rurl
    if show_dialog:
        xbmcgui.Dialog().ok(
            ADDON_NAME,
            'Search requires at least one authorized provider.\n\n'
            'Open Premium Player Settings, then ResolveURL provider authorizations / settings, and authorize at least one torrent-capable service.'
        )
    return rd, tb, False, False, bridge, []


def root():
    # New Releases caches intentionally survive at the Premium Player main menu.
    # Their lifecycle boundary is one level deeper: the shared New Releases menu.
    rd, tb, rd_active, tb_active, bridge, rurl = _active_providers()
    if rurl:
        add_item('Search', 'search_root', True, menu_icon='search')
        add_item('New Releases', 'new_releases_root', True, menu_icon='new_releases', main_entry='1')
    else:
        add_item('Search unavailable - authorize a provider', 'open_resolveurl_settings', False,
                 playable=False, menu_icon='search')
    add_item('Browse Accounts', 'browse_accounts', True, menu_icon='accounts')
    add_item('Pinned', 'pinned_root', True, menu_icon='pinned')
    add_item('Settings', 'open_settings', False, playable=False, menu_icon='settings')
    end_menu()

def _pin_context(action, **kwargs):
    return [('Pin in Premium Player', 'RunPlugin(%s)' % plugin_url(BASE, action=action, **kwargs))]


def _unpin_context(pin_id):
    return [('Unpin from Premium Player', 'RunPlugin(%s)' % plugin_url(BASE, action='unpin_item', pin_id=pin_id))]


def _pin_id(*parts):
    return ':'.join(str(x if x is not None else '') for x in parts)


def pin_meta(kind, imdb_id, title, season=None, episode=None, episode_title=None):
    kind = str(kind or '')
    category = 'movies' if kind == 'movie' else 'tv'
    pin = {
        'id': _pin_id('meta', kind, imdb_id, season, episode),
        'category': category,
        'kind': kind,
        'imdb_id': imdb_id,
        'title': title or imdb_id,
    }
    if season not in (None, ''):
        pin['season'] = int(season)
    if episode not in (None, ''):
        pin['episode'] = int(episode)
    if episode_title:
        pin['episode_title'] = episode_title
    save_pin(pin)
    notify('Pinned in Premium Player.')


def pin_source(session, index):
    data = load_session(session) or {}
    rows = data.get('sources') or []
    try:
        index = int(index)
        source = dict(rows[index])
    except Exception:
        return xbmcgui.Dialog().ok(ADDON_NAME, 'This source list expired. Please search again.')
    media = dict(data.get('media') or {})
    category = 'movies' if media.get('type') == 'movie' else 'tv'
    pin_id = _pin_id('source', media.get('type'), media.get('imdb_id'), media.get('season'),
                     media.get('episode'), source.get('provider'), source.get('hash'), source.get('file_idx'))
    save_pin({
        'id': pin_id,
        'category': category,
        'kind': 'source',
        'media': media,
        'source': source,
        'label': _source_label(None, source),
    })
    notify('Pinned in Premium Player.')


def pin_account(kind, provider, label='', **kwargs):
    payload = {
        'id': _pin_id('account', provider, kind, kwargs.get('item_type'), kwargs.get('item_id'),
                      kwargs.get('torrent_id'), kwargs.get('file_id'), kwargs.get('download_id'), kwargs.get('prefix'),
                      kwargs.get('folder_id'), kwargs.get('entry_type')),
        'category': 'downloads',
        'kind': kind,
        'provider': provider,
        'label': label,
    }
    for key in ('item_type', 'item_id', 'torrent_id', 'file_id', 'download_id', 'prefix', 'filename', 'folder_id', 'entry_type'):
        if kwargs.get(key) not in (None, ''):
            payload[key] = kwargs.get(key)
    save_pin(payload)
    notify('Pinned in Premium Player.')


def unpin_item(pin_id):
    remove_pin(pin_id)
    notify('Removed from Pinned.')
    try:
        xbmc.executebuiltin('Container.Refresh')
    except Exception:
        pass


def browse_accounts():
    bridge = ResolveURLBridge()
    providers = bridge.authorized_providers(torrent_capable_only=False)
    if not providers:
        add_item('No authorized accounts - open ResolveURL settings', 'open_resolveurl_settings', False,
                 playable=False, menu_icon='settings')
        end_menu()
        return
    for provider in providers:
        name = str(provider.get('name') or provider.get('code') or 'ResolveURL Provider')
        lname = name.lower()
        pclass = provider.get('class')
        if lname == 'real-debrid' or pclass == 'RealDebridResolver':
            add_item(provider_text('RD', 'Real-Debrid'), 'rd_root', True, menu_icon='accounts')
        elif lname == 'torbox' or pclass == 'TorBoxResolver':
            add_item(provider_text('TB', 'TorBox'), 'tb_root', True, menu_icon='accounts')
        elif pclass == 'AllDebridResolver':
            add_item(provider_text('AD', 'AllDebrid'), 'cloud_root', True, menu_icon='accounts', provider='ad')
        elif pclass == 'PremiumizeMeResolver':
            add_item(provider_text('PM', 'Premiumize.me'), 'pm_root', True, menu_icon='accounts')
        elif pclass == 'DebridLinkResolver':
            add_item(provider_text('DL', 'Debrid-Link'), 'cloud_root', True, menu_icon='accounts', provider='dl')
        elif pclass == 'LinkSnappyResolver':
            add_item(provider_text('LS', 'LinkSnappy'), 'cloud_root', True, menu_icon='accounts', provider='ls')
        else:
            add_item(provider_text(provider.get('code'), name, name), 'resolveurl_provider_info', False,
                     playable=False, menu_icon='accounts', provider=name)
    end_menu()


def _live_id(value):
    return str(value) if value is not None else ''


def _prune_account_pins():
    """Prune account pins only after successful live provider validation."""
    pins = load_pins()
    account_pins = [p for p in pins if p.get('category') == 'downloads']
    if not account_pins:
        return pins
    stale = set()

    rd_pins = [p for p in account_pins if p.get('provider') == 'rd']
    if rd_pins:
        rd = RealDebrid()
        if rd.authorized and rd.enabled:
            try:
                torrent_ids = {_live_id(x.get('id')) for x in rd.list_torrents()}
                download_ids = {_live_id(x.get('id')) for x in rd.list_downloads()}
                for pin in rd_pins:
                    if pin.get('kind') == 'rd_download':
                        if _live_id(pin.get('download_id')) not in download_ids:
                            stale.add(pin.get('id'))
                    elif pin.get('kind') in ('rd_torrent', 'rd_folder', 'rd_file'):
                        if _live_id(pin.get('torrent_id')) not in torrent_ids:
                            stale.add(pin.get('id'))
            except Exception as exc:
                log('Could not validate Real-Debrid pins: %s' % exc, xbmc.LOGWARNING)

    tb_pins = [p for p in account_pins if p.get('provider') == 'tb']
    if tb_pins:
        tb = TorBox()
        if tb.authorized and tb.enabled:
            try:
                needed = {str(p.get('item_type') or 'torrent') for p in tb_pins}
                live = {}
                if 'torrent' in needed:
                    live['torrent'] = {_live_id(x.get('id') or x.get('torrent_id')) for x in tb.list_torrents(bypass_cache=True)}
                if 'usenet' in needed:
                    live['usenet'] = {_live_id(x.get('id') or x.get('usenet_id')) for x in tb.list_usenet()}
                if 'web' in needed:
                    live['web'] = {_live_id(x.get('id') or x.get('webdownload_id') or x.get('web_id')) for x in tb.list_webdl()}
                for pin in tb_pins:
                    item_type = str(pin.get('item_type') or 'torrent')
                    if _live_id(pin.get('item_id')) not in live.get(item_type, set()):
                        stale.add(pin.get('id'))
            except Exception as exc:
                log('Could not validate TorBox pins: %s' % exc, xbmc.LOGWARNING)

    # Native adapters for other ResolveURL services validate provider-backed
    # pins against the live account. Never prune on a provider/API exception.
    for provider in ('ad', 'dl', 'ls'):
        provider_pins = [p for p in account_pins if p.get('provider') == provider]
        if not provider_pins:
            continue
        try:
            client = _native_cloud_client(provider)
            live_ids = {_live_id(x.get('torrentid') or x.get('id') or x.get('torrent_id') or x.get('tid')) for x in client.list_torrents()}
            for pin in provider_pins:
                if pin.get('kind') in ('cloud_torrent', 'cloud_folder', 'cloud_file'):
                    if _live_id(pin.get('torrent_id')) not in live_ids:
                        stale.add(pin.get('id'))
        except Exception as exc:
            log('Could not validate %s pins: %s' % (provider, exc), xbmc.LOGWARNING)

    pm_pins = [p for p in account_pins if p.get('provider') == 'pm']
    if pm_pins:
        pm = Premiumize()
        if pm.authorized and pm.enabled:
            try:
                live_file_ids = {_live_id(x.get('id')) for x in pm.list_all_files()}
                live_transfer_ids = {_live_id(x.get('id')) for x in pm.list_transfers()}
                for pin in pm_pins:
                    kind = pin.get('kind')
                    if kind == 'pm_file' and _live_id(pin.get('file_id')) not in live_file_ids:
                        stale.add(pin.get('id'))
                    elif kind == 'pm_transfer' and _live_id(pin.get('item_id')) not in live_transfer_ids:
                        stale.add(pin.get('id'))
                # Folder pins require folder-specific lookups; only mark stale if
                # Premiumize positively reports the folder as invalid. Transient
                # failures are deliberately ignored.
                for pin in [x for x in pm_pins if x.get('kind') == 'pm_folder']:
                    try:
                        pm.list_folder(pin.get('folder_id'))
                    except ApiError as exc:
                        payload = getattr(exc, 'payload', None)
                        text = str(payload or exc).lower()
                        if any(word in text for word in ('not found', 'invalid', 'does not exist')):
                            stale.add(pin.get('id'))
            except Exception as exc:
                log('Could not validate Premiumize.me pins: %s' % exc, xbmc.LOGWARNING)

    if stale:
        pins = [p for p in pins if p.get('id') not in stale]
        save_pins(pins)
    return pins


def pinned_root():
    pins = _prune_account_pins()
    counts = {c: len([p for p in pins if p.get('category') == c]) for c in ('movies', 'tv', 'downloads')}
    add_item('%s%s' % (media_text('movie', 'Movies'), (' (%d)' % counts['movies'] if counts['movies'] else '')),
             'pinned_category', True, menu_icon='movie_pinned', category='movies')
    add_item('%s%s' % (media_text('series', 'TV Shows'), (' (%d)' % counts['tv'] if counts['tv'] else '')),
             'pinned_category', True, menu_icon='tv_pinned', category='tv')
    add_item('My Torrents%s' % (' (%d)' % counts['downloads'] if counts['downloads'] else ''),
             'pinned_category', True, menu_icon='torrents', category='downloads')
    downloaded = load_download_history(prune=True)
    if downloaded:
        add_item('Downloaded (%d)' % len(downloaded), 'downloaded_files', True, menu_icon='downloads')
    end_menu()


def downloaded_files():
    rows = load_download_history(prune=True)
    if not rows:
        add_item('No downloaded files', 'file_info', False, playable=False,
                 message='No locally downloaded files are currently available.')
        end('videos')
        return
    for row in rows:
        path = row.get('path') or ''
        label = row.get('label') or os.path.basename(clean_path(path)) or 'Downloaded file'
        info = {'title': row.get('title') or label}
        if row.get('season') not in (None, ''):
            info['season'] = int(row.get('season'))
        if row.get('episode') not in (None, ''):
            info['episode'] = int(row.get('episode'))
        ctx = [('Remove from Downloaded list', 'RunPlugin(%s)' % plugin_url(
            BASE, action='remove_downloaded', path=path))]
        add_item(label, 'play_local_file', False, info=info, context=ctx, path=path, label=label)
    end('videos')


def play_local_file(path, label=''):
    if not xbmcvfs.exists(path):
        remove_download_history(path)
        return _set_failed('This downloaded file no longer exists.')
    _set_resolved(path, label or os.path.basename(clean_path(path)))


def remove_downloaded(path):
    remove_download_history(path)
    xbmc.executebuiltin('Container.Refresh')


def _live_pin_meta(pin):
    kind = pin.get('kind')
    imdb_id = pin.get('imdb_id') or (pin.get('media') or {}).get('imdb_id')
    if not imdb_id or kind == 'source' and not (pin.get('media') or {}).get('type'):
        return {}, {}
    media_type = 'movie' if (kind == 'movie' or (kind == 'source' and (pin.get('media') or {}).get('type') == 'movie')) else 'series'
    try:
        meta = MetadataClient().meta(media_type, imdb_id)
    except Exception:
        return {}, {}
    poster = meta.get('poster') or ''
    background = meta.get('background') or ''
    art = {'poster': poster, 'thumb': poster, 'icon': poster, 'fanart': background, 'landscape': background}
    info = {'title': pin.get('title') or (pin.get('media') or {}).get('title') or meta.get('name') or meta.get('title') or imdb_id,
            'plot': meta.get('description') or ''}
    if kind == 'season':
        season = int(pin.get('season') or 0)
        info.update({'season': season, 'tvshowtitle': pin.get('title') or ''})
    elif kind == 'episode':
        season = int(pin.get('season') or 0); episode = int(pin.get('episode') or 0)
        richer = MetadataClient().tvmaze_episodes(imdb_id).get((season, episode), {})
        if richer.get('thumbnail'):
            art['thumb'] = richer['thumbnail']; art['landscape'] = richer['thumbnail']
        info.update({'season': season, 'episode': episode, 'tvshowtitle': pin.get('title') or '',
                     'title': richer.get('title') or pin.get('episode_title') or info['title'],
                     'plot': richer.get('plot') or info['plot']})
    return art, info


def pinned_category(category):
    pins = _prune_account_pins() if category == 'downloads' else load_pins()
    rows = [p for p in pins if p.get('category') == category]
    if not rows:
        add_item('No pinned items', 'file_info', False, playable=False, message='Nothing has been pinned in this category yet.')
        end('videos' if category != 'downloads' else 'files')
        return
    for pin in rows:
        kind = pin.get('kind')
        label = pin.get('label') or pin.get('title') or 'Pinned item'
        if category == 'downloads' and pin.get('provider'):
            code = PROVIDER_CODES.get(str(pin.get('provider') or '').lower(), str(pin.get('provider') or '').upper())
            label = '%s | %s' % (provider_text(code, code), label)
        elif kind == 'source':
            label = _source_label(None, pin.get('source') or {})
        elif category == 'movies':
            label = media_text('movie', label)
        elif category == 'tv':
            label = media_text('series', label)
        art, info = ({}, {}) if category == 'downloads' else _live_pin_meta(pin)
        ctx = _unpin_context(pin['id'])
        args = {}
        action = 'file_info'; folder = False; playable = False
        if kind == 'movie':
            action='sources'; folder=False; playable=False; args={'media_type':'movie','imdb_id':pin['imdb_id'],'title':pin.get('title','')}
        elif kind == 'tvshow':
            ctx = [('Play All Seasons', 'RunPlugin(%s)' % plugin_url(BASE, action='play_all_tv', imdb_id=pin['imdb_id'], title=pin.get('title','TV Show')))] + ctx
            action='seasons'; folder=True; args={'imdb_id':pin['imdb_id'],'title':pin.get('title','TV Show')}
        elif kind == 'season':
            label = media_text('series', '%s - Season %s' % (pin.get('title','TV Show'), pin.get('season')))
            ctx = [('Play All Starting Here', 'RunPlugin(%s)' % plugin_url(BASE, action='play_all_tv', imdb_id=pin['imdb_id'], title=pin.get('title','TV Show'), start_season=pin.get('season')))] + ctx
            action='episodes'; folder=True; args={'imdb_id':pin['imdb_id'],'title':pin.get('title','TV Show'),'season':pin.get('season')}
        elif kind == 'episode':
            label = media_text('series', '%s - S%02dE%02d%s' % (pin.get('title','TV Show'), int(pin.get('season') or 0), int(pin.get('episode') or 0),
                                            (' - '+pin.get('episode_title')) if pin.get('episode_title') else ''))
            ctx = [('Play All Starting Here', 'RunPlugin(%s)' % plugin_url(BASE, action='play_all_tv', imdb_id=pin['imdb_id'], title=pin.get('title','TV Show'), start_season=pin.get('season'), start_episode=pin.get('episode')))] + ctx
            action='sources'; folder=False; playable=False; args={'media_type':'series','imdb_id':pin['imdb_id'],'title':pin.get('title',''),
                                                 'season':pin.get('season'),'episode':pin.get('episode'),'episode_title':pin.get('episode_title','')}
        elif kind == 'source':
            action='play_pinned_source'; playable=True; args={'pin_id':pin['id']}
        elif kind in ('rd_torrent','rd_folder'):
            action='browse_rd_torrent'; folder=True; args={'torrent_id':pin.get('torrent_id'),'prefix':pin.get('prefix','')}
            ctx = [('Delete torrent', 'RunPlugin(%s)' % plugin_url(BASE, action='delete_rd_torrent', torrent_id=pin.get('torrent_id')))] + ctx
        elif kind == 'rd_file':
            action='play_rd_file'; playable=True; args={'torrent_id':pin.get('torrent_id'),'file_id':pin.get('file_id'),'filename':pin.get('filename','')}
        elif kind == 'rd_download':
            action='play_pinned_rd_download'; playable=True; args={'download_id':pin.get('download_id')}
        elif kind in ('tb_item','tb_folder'):
            action='browse_tb_item'; folder=True; args={'item_type':pin.get('item_type'),'item_id':pin.get('item_id'),'prefix':pin.get('prefix','')}
            if pin.get('item_type') == 'torrent':
                ctx = [('Delete torrent', 'RunPlugin(%s)' % plugin_url(BASE, action='delete_tb_torrent', torrent_id=pin.get('item_id')))] + ctx
        elif kind == 'tb_file':
            action='play_tb_file'; playable=True; args={'item_type':pin.get('item_type'),'item_id':pin.get('item_id'),'file_id':pin.get('file_id'),'filename':pin.get('filename','')}
        elif kind in ('cloud_torrent', 'cloud_folder'):
            action='browse_cloud_torrent'; folder=True; args={'provider':pin.get('provider'),'torrent_id':pin.get('torrent_id'),'prefix':pin.get('prefix','')}
            ctx = [('Delete torrent', 'RunPlugin(%s)' % plugin_url(BASE, action='delete_cloud_torrent', provider=pin.get('provider'), torrent_id=pin.get('torrent_id')))] + ctx
        elif kind == 'cloud_file':
            action='play_cloud_file'; playable=True; args={'provider':pin.get('provider'),'torrent_id':pin.get('torrent_id'),'file_id':pin.get('file_id'),'filename':pin.get('filename','')}
        elif kind == 'pm_folder':
            action='pm_folder'; folder=True; args={'folder_id':pin.get('folder_id')}
            ctx = [('Delete folder', 'RunPlugin(%s)' % plugin_url(BASE, action='delete_pm_item', entry_type='folder', item_id=pin.get('folder_id')))] + ctx
        elif kind == 'pm_file':
            action='play_pm_file'; playable=True; args={'file_id':pin.get('file_id'),'filename':pin.get('filename','')}
            ctx = [('Delete file', 'RunPlugin(%s)' % plugin_url(BASE, action='delete_pm_item', entry_type='file', item_id=pin.get('file_id')))] + ctx
        elif kind == 'pm_transfer':
            if pin.get('folder_id'):
                action='pm_folder'; folder=True; args={'folder_id':pin.get('folder_id')}
            elif pin.get('file_id'):
                action='play_pm_file'; playable=True; args={'file_id':pin.get('file_id'),'filename':pin.get('filename','')}
            ctx = [('Delete transfer', 'RunPlugin(%s)' % plugin_url(BASE, action='delete_pm_transfer', transfer_id=pin.get('item_id')))] + ctx
        add_item(label, action, folder, art=art, info=info, context=ctx, playable=playable, **args)
    end('videos' if category != 'downloads' else 'files')


def play_pinned_source(pin_id):
    pin = next((p for p in load_pins() if p.get('id') == pin_id), None)
    if not pin:
        return _set_failed('This pinned source no longer exists.')
    sid = save_session({'media': pin.get('media') or {}, 'sources': [pin.get('source') or {}]})
    PlaybackEngine().play_session(sid, 0, handle=HANDLE)


def play_pinned_rd_download(download_id):
    try:
        item = next((x for x in RealDebrid().list_downloads() if str(x.get('id')) == str(download_id)), None)
    except Exception as exc:
        return _set_failed('Could not refresh Real-Debrid downloads:\n%s' % exc)
    if not item or not item.get('download'):
        return _set_failed('This Real-Debrid download is no longer available.')
    _set_resolved(item.get('download'), item.get('filename') or 'Real-Debrid')


def search_root():
    state = _require_search_provider(show_dialog=True)
    if not (state[2] or state[3] or state[5]):
        return
    # Search prompt actions are not folders. This keeps the current Search
    # directory as the history parent; results are loaded with Container.Update.
    add_item('Search %s' % media_text('movie', 'Movies'), 'search_prompt', False,
             playable=False, menu_icon='movie_search', media_type='movie')
    add_item('Search %s' % media_text('series', 'TV Shows'), 'search_prompt', False,
             playable=False, menu_icon='tv_search', media_type='series')
    for row in load_search_history():
        media_type = row.get('media_type')
        query = row.get('query') or ''
        prefix = media_tag(media_type)
        label = '%s - %s' % (prefix, query)
        context = [('Remove search', 'RunPlugin(%s)' % plugin_url(
            BASE, action='remove_search', media_type=media_type, query=query))]
        add_item(label, 'search_results', True, context=context,
                 menu_icon=('movie_search' if media_type == 'movie' else 'tv_search'),
                 media_type=media_type, query=query)
    end_menu()


def search_prompt(media_type):
    state = _require_search_provider(show_dialog=True)
    if not (state[2] or state[3] or state[5]):
        return
    query = xbmcgui.Dialog().input(
        'Search %s' % ('Movies' if media_type == 'movie' else 'TV Shows'),
        type=xbmcgui.INPUT_ALPHANUM)
    if not query:
        return
    # A new search invalidates the previous temporary torrent/source list.
    clear_active_source_session()
    remember_search(media_type, query)
    url = plugin_url(BASE, action='search_results', media_type=media_type, query=query)
    # Called from a non-folder item, so the Search menu remains the parent and
    # Back from results does not invoke the keyboard again.
    xbmc.executebuiltin('Container.Update(%s)' % url)


def search_results(media_type, query):
    # Loading a new metadata search invalidates any prior torrent-source cache.
    clear_active_source_session()
    state = _require_search_provider(show_dialog=True)
    if not (state[2] or state[3] or state[5]):
        return
    remember_search(media_type, query)
    _show_search_results(media_type, query)


def remove_search(media_type, query):
    remove_search_history(media_type, query)
    try:
        xbmc.executebuiltin('Container.Refresh')
    except Exception:
        pass


def _render_search_results(media_type, results):
    for meta in results:
        imdb = meta.get('imdb_id') or meta.get('id')
        if not imdb or not str(imdb).startswith('tt'):
            continue
        title = meta.get('name') or meta.get('title') or imdb
        year = meta.get('releaseInfo') or meta.get('year') or ''
        label = media_text(media_type, '%s%s' % (title, ' (%s)' % year if year else ''))
        poster = meta.get('poster') or ''
        background = meta.get('background') or ''
        art = {
            'poster': poster,
            'thumb': poster,
            'icon': poster,
            'fanart': background,
            'landscape': background,
        }
        plot = meta.get('description') or meta.get('plot') or ''
        info = {
            'title': title,
            'year': int(str(year)[:4]) if str(year)[:4].isdigit() else 0,
            'plot': plot,
        }
        genres = meta.get('genres') or []
        if genres:
            info['genre'] = ' / '.join(str(g) for g in genres) if isinstance(genres, (list, tuple)) else str(genres)
        if media_type == 'movie':
            ctx = _pin_context('pin_meta', kind='movie', imdb_id=imdb, title=title)
            add_item(label, 'sources', False, art=art, info=info, context=ctx, playable=False,
                     media_type='movie', imdb_id=imdb, title=title)
        else:
            ctx = [('Play All Seasons', 'RunPlugin(%s)' % plugin_url(BASE, action='play_all_tv', imdb_id=imdb, title=title))]
            ctx += _pin_context('pin_meta', kind='tvshow', imdb_id=imdb, title=title)
            add_item(label, 'seasons', True, art=art, info=info, context=ctx, imdb_id=imdb, title=title)
    # Search result directories are always live; never let Kodi persist them.
    end('movies' if media_type == 'movie' else 'tvshows')


def _show_search_results(media_type, query):
    try:
        results = MetadataClient().search(media_type, query)
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Metadata search failed:\n%s' % exc)
        return
    _render_search_results(media_type, results)


def _clean_episode_search_part(value):
    """Return only letters/numbers separated by single spaces for episode search."""
    value = str(value or '').strip().replace("'", '').replace('’', '')
    cleaned = ''.join(ch if ch.isalnum() else ' ' for ch in value)
    return ' '.join(cleaned.split())


def _episode_search_queries(title, season, episode, episode_title):
    """Build the two exact, label-independent queries used by New TV Episodes."""
    show = _clean_episode_search_part(title)
    episode_name = _clean_episode_search_part(episode_title)
    try:
        code = 'S%02dE%02d' % (int(season), int(episode))
    except (TypeError, ValueError):
        return []

    queries = []
    for query in ('%s %s' % (show, code), '%s %s' % (show, episode_name)):
        query = ' '.join(query.split()).strip()
        if query and query.casefold() not in {q.casefold() for q in queries}:
            queries.append(query)
    return queries


def episode_search_results(title, season, episode, episode_title=''):
    """Search a New Releases TV episode two ways and merge the visible results.

    Never derive either query from the decorated Kodi ListItem label.  That label
    can contain media tags, punctuation and an air date; the search contract is
    intentionally limited to ``Show Name SXXEXX`` and ``Show Name Episode Name``.
    """
    clear_active_source_session()
    state = _require_search_provider(show_dialog=True)
    if not (state[2] or state[3] or state[5]):
        return

    queries = _episode_search_queries(title, season, episode, episode_title)
    if not queries:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not build a valid TV episode search.')
        return

    client = MetadataClient()
    merged = []
    seen = set()
    errors = []
    for query in queries:
        try:
            rows = client.search('series', query)
        except Exception as exc:
            errors.append('%s: %s' % (query, exc))
            log('TV episode metadata search failed for %r: %s' % (query, exc), xbmc.LOGWARNING)
            continue
        for meta in rows or []:
            imdb = str(meta.get('imdb_id') or meta.get('id') or '').strip()
            key = imdb.casefold() if imdb else (
                _clean_episode_search_part(meta.get('name') or meta.get('title')).casefold(),
                str(meta.get('releaseInfo') or meta.get('year') or '')[:4],
            )
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(meta)

    if not merged and errors:
        xbmcgui.Dialog().ok(ADDON_NAME, 'TV episode search failed:\n%s' % '\n'.join(errors))
        return
    _render_search_results('series', merged)


def _new_release_tv_target(imdb_id, title, season, episode, episode_title=''):
    """Open the deepest New Releases TV location that actually exists.

    New TV Releases is sourced independently from the series metadata used by
    the normal TV browser.  Very recent schedule entries can therefore refer to
    a season/episode that has not propagated into that browser yet.  Resolve the
    target progressively so Kodi never opens an empty directory:

      * show + season + episode -> open the season and focus that episode
      * show + season           -> open the season's episode list
      * show only               -> open the show's season list

    This is a direct IMDb metadata lookup, not a title search; selecting a New
    Releases row never starts a source search or presents metadata search
    results.
    """
    try:
        wanted_season = int(season)
        wanted_episode = int(episode)
    except (TypeError, ValueError):
        xbmcgui.Dialog().ok(ADDON_NAME, 'This new-release entry does not contain a valid season and episode.')
        _close_addon()
        return

    try:
        meta = MetadataClient().meta('series', imdb_id)
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not load series metadata:\n%s' % exc)
        _close_addon()
        return

    if not isinstance(meta, dict) or not meta:
        xbmcgui.Dialog().ok(ADDON_NAME, 'This TV show is not currently available in the TV browser.')
        _close_addon()
        return

    resolved_title = meta.get('name') or meta.get('title') or title or 'TV Show'
    videos = list(meta.get('videos') or [])
    seasons_available = set()
    episodes_by_season = {}
    for video in videos:
        try:
            video_season = int(video.get('season'))
            video_episode = int(video.get('episode'))
        except (TypeError, ValueError):
            continue
        seasons_available.add(video_season)
        episodes_by_season.setdefault(video_season, set()).add(video_episode)

    if wanted_season in seasons_available:
        if wanted_episode in episodes_by_season.get(wanted_season, set()):
            log('New Releases TV matched show/season/episode: %s S%02dE%02d' %
                (resolved_title, wanted_season, wanted_episode))
            episodes(imdb_id, resolved_title, wanted_season, focus_episode=wanted_episode)
        else:
            log('New Releases TV matched show/season only: %s S%02d; episode E%02d unavailable' %
                (resolved_title, wanted_season, wanted_episode))
            episodes(imdb_id, resolved_title, wanted_season)
        return

    if seasons_available:
        log('New Releases TV matched show only: %s; season S%02d unavailable' %
            (resolved_title, wanted_season))
        seasons(imdb_id, resolved_title)
        return

    log('New Releases TV matched show but no seasons exist: %s; using plain-text torrent fallback' % resolved_title)
    _new_release_episode_text_sources(
        imdb_id, resolved_title, wanted_season, wanted_episode, episode_title)


def _new_release_episode_text_sources(imdb_id, title, season, episode, episode_title=''):
    """Fallback source discovery for a release absent from series metadata.

    No metadata search is performed and these queries are never written to the
    add-on's recent-search history.  Search the torrent index directly using the
    two sanitized forms requested for New Releases, merge/dedupe the raw hits,
    then reuse the normal quality/size/debrid source pipeline.
    """
    clear_active_source_session()
    queries = _episode_search_queries(title, season, episode, episode_title)
    if not queries:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not build a valid TV episode torrent search.')
        return

    state = _require_search_provider(show_dialog=True)
    if not (state[2] or state[3] or state[5]):
        return

    media = {
        'type': 'series',
        'imdb_id': imdb_id,
        'title': title,
        'season': int(season),
        'episode': int(episode),
        'episode_title': episode_title or '',
        'label': '%s - S%02dE%02d%s' % (
            title, int(season), int(episode),
            (' - ' + episode_title) if episode_title else ''),
    }
    session_id = _discover_source_session(
        media, show_dialog=True, provider_state=state,
        text_queries=queries, progress_title='Fallback Torrent Search')
    if not session_id:
        return

    # This fallback is reached from a New Releases item declared as a folder.
    # Kodi therefore expects this *same plugin invocation* to populate and finish
    # the child directory.  Calling Container.Update() here races/abandons the
    # pending folder request and Kodi restores the New TV Releases parent list.
    # Render the saved source session directly instead. Back then naturally
    # returns to New TV Releases, while playback continues to use the exact same
    # temporary session/cache as the normal source-results route.
    set_active_source_session(session_id)
    _render_source_session(session_id)


def seasons(imdb_id, title):
    try:
        meta = MetadataClient().meta('series', imdb_id)
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not load series metadata:\n%s' % exc)
        return
    videos = meta.get('videos') or []
    season_numbers = sorted({int(v.get('season')) for v in videos if str(v.get('season', '')).isdigit()})
    show_poster = meta.get('poster') or ''
    show_background = meta.get('background') or ''
    show_plot = meta.get('description') or ''
    for season in season_numbers:
        label = media_text('series', 'Specials' if season == 0 else 'Season %d' % season)
        season_videos = [v for v in videos if int(v.get('season') or -1) == season]
        season_videos.sort(key=lambda v: int(v.get('episode') or 0))
        episode_thumb = next((v.get('thumbnail') for v in season_videos if v.get('thumbnail')), '')
        art = {
            'poster': show_poster,
            'thumb': episode_thumb or show_poster,
            'icon': show_poster,
            'fanart': show_background,
            'landscape': episode_thumb or show_background,
        }
        if season == 0:
            season_intro = 'Specials for %s.' % title
        else:
            season_intro = 'Season %d of %s.' % (season, title)
        season_plot = season_intro + (('\n\n' + show_plot) if show_plot else '')
        info = {
            'title': label,
            'tvshowtitle': title,
            'season': season,
            'plot': season_plot,
        }
        ctx = [('Play All Starting Here', 'RunPlugin(%s)' % plugin_url(BASE, action='play_all_tv', imdb_id=imdb_id, title=title, start_season=season))]
        ctx += _pin_context('pin_meta', kind='season', imdb_id=imdb_id, title=title, season=season)
        add_item(label, 'episodes', True, art=art, info=info, context=ctx,
                 imdb_id=imdb_id, title=title, season=season)
    end('seasons')


def episodes(imdb_id, title, season, focus_episode=None):
    client = MetadataClient()
    try:
        meta = client.meta('series', imdb_id)
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not load episode metadata:\n%s' % exc)
        return
    tvmaze = client.tvmaze_episodes(imdb_id)
    season = int(season)
    videos = [v for v in (meta.get('videos') or []) if int(v.get('season') or -1) == season]
    videos.sort(key=lambda v: int(v.get('episode') or 0))
    for video in videos:
        ep = int(video.get('episode') or 0)
        richer = tvmaze.get((season, ep), {})
        ep_title = richer.get('title') or video.get('title') or video.get('name') or 'Episode %d' % ep
        label = media_text('series', '%02d. %s' % (ep, ep_title))
        episode_thumb = richer.get('thumbnail') or video.get('thumbnail') or ''
        poster = meta.get('poster') or ''
        background = meta.get('background') or ''
        art = {
            'thumb': episode_thumb or poster,
            'poster': poster,
            'icon': episode_thumb or poster,
            'fanart': background,
            'landscape': episode_thumb or background,
        }
        episode_plot = (richer.get('plot') or video.get('overview') or
                        video.get('description') or '')
        if not episode_plot:
            episode_plot = '%s - Season %d, Episode %d.' % (title, season, ep)
        info = {
            'title': ep_title,
            'tvshowtitle': title,
            'season': season,
            'episode': ep,
            'plot': episode_plot,
        }
        released = richer.get('premiered') or str(video.get('released') or '')[:10]
        if released:
            info['premiered'] = released
        ctx = [('Play All Starting Here', 'RunPlugin(%s)' % plugin_url(BASE, action='play_all_tv', imdb_id=imdb_id, title=title, start_season=season, start_episode=ep))]
        ctx += _pin_context('pin_meta', kind='episode', imdb_id=imdb_id, title=title,
                           season=season, episode=ep, episode_title=ep_title)
        add_item(label, 'sources', False, art=art, info=info, context=ctx, playable=False, media_type='series', imdb_id=imdb_id,
                 title=title, season=season, episode=ep, episode_title=ep_title,
                 properties={'PremiumPlayerEpisode': ep})
    end('episodes')
    if focus_episode not in (None, ''):
        _focus_episode_after_render(focus_episode)



def _focus_episode_after_render(episode):
    try:
        wanted = str(int(episode))
    except Exception:
        return
    monitor = xbmc.Monitor()
    for _ in range(20):
        if monitor.abortRequested():
            return
        for control_id in (50, 51, 52, 53, 54, 55):
            try:
                if not xbmc.getCondVisibility('Control.IsVisible(%d)' % control_id):
                    continue
                count = int(xbmc.getInfoLabel('Container(%d).NumAllItems' % control_id) or 0)
                for position in range(max(0, count)):
                    prop = xbmc.getInfoLabel(
                        'Container(%d).ListItemAbsolute(%d).Property(PremiumPlayerEpisode)' % (control_id, position))
                    if str(prop) == wanted:
                        xbmc.executebuiltin('Control.SetFocus(%d,%d,absolute)' % (control_id, position))
                        return
            except Exception:
                continue
        xbmc.sleep(100)

def _max_quality_rank():
    raw = str(ADDON.getSetting('max_quality') or '0')
    mapping = {'0': 4, '1': 3, '2': 2, '3': 1,
               '4K': 4, '1080p': 3, '1080P': 3, '720p': 2, '720P': 2, 'SD': 1}
    return mapping.get(raw, 4)


def _max_size_bytes():
    raw = str(ADDON.getSetting('max_size_gb') or '10')
    if raw.lower() == 'unlimited' or raw == '10':
        return None
    if raw.endswith(' GB') and raw[:-3].strip().isdigit():
        return int(raw[:-3].strip()) * 1024**3
    try:
        idx = int(raw)
        if 0 <= idx <= 9:
            return (idx + 1) * 1024**3
    except Exception:
        pass
    return None


def _source_allowed(source):
    if int(source.get('quality_rank') or 1) > _max_quality_rank():
        return False
    max_bytes = _max_size_bytes()
    size = int(source.get('size_bytes') or 0)
    if max_bytes is not None and size > 0 and size > max_bytes:
        return False
    return True


def _source_full_name(source):
    value = source.get('filename') or source.get('title') or source.get('name') or source.get('hash') or ''
    value = str(value).replace('\r', ' ').replace('\n', ' ')
    return re.sub(r'\s+', ' ', value).strip()


def _source_label(index, row):
    provider_code = row.get('provider_code') or ('RD' if row.get('provider') == 'rd' else 'TB')
    pcolor = provider_color(provider_code, row.get('provider_name') or '')
    status = row.get('cache_status') or 'UNKNOWN'
    status_color = STATUS_COLORS.get(status, STATUS_COLORS['UNKNOWN'])
    quality = row.get('quality') or 'SD'
    quality = {'1080P': '1080p', '720P': '720p'}.get(quality, quality)
    size = human_size(row.get('size_bytes')) if row.get('size_bytes') else 'SIZE ?'
    name = _source_full_name(row)
    prefix = ('%d. ' % (int(index) + 1)) if index is not None else ''
    return ('%s[COLOR %s]%s[/COLOR] | [COLOR %s]%s[/COLOR] | '
            '[COLOR FFE0E0E0]%s[/COLOR] | [COLOR FFB0BEC5]%s[/COLOR] | %s' %
            (prefix, pcolor, provider_code, status_color, status,
             quality, size, name))

def _expand_provider_rows(source_list, rd, tb, rd_active, tb_active, bridge, rurl_providers,
                          rd_cloud_override=None, progress_cb=None):
    hashes = [s['hash'] for s in source_list]

    # TorBox has a real arbitrary-hash cache API, so its rows can be labeled and
    # filtered before the user clicks them. This native API call reuses the token
    # stored by ResolveURL; ResolveURL still owns authorization and playback.
    tb_cached = set()
    tb_cache_ok = not tb_active
    if tb_active and hashes:
        if progress_cb:
            progress_cb(62, 'Checking TorBox cache...')
        try:
            tb_cached = tb.check_cached(hashes)
            tb_cache_ok = True
            log('TorBox cache check: %d/%d cached' % (len(tb_cached), len(hashes)), xbmc.LOGINFO)
        except Exception as exc:
            tb_cache_ok = False
            log('TorBox cache check failed: %s' % exc, xbmc.LOGWARNING)

    # Premiumize exposes a bulk cache-check endpoint. Use the magnet strings
    # themselves so cached-only rows can be labeled/filterable before playback.
    pm_provider = next((p for p in rurl_providers if str(p.get('class') or '') == 'PremiumizeMeResolver'), None)
    pm_cached = set()
    pm_cache_ok = pm_provider is None
    if pm_provider and source_list:
        if progress_cb:
            progress_cb(69, 'Checking Premiumize.me cache...')
        try:
            pm_client = Premiumize()
            pm_cached = pm_client.check_cached([src.get('magnet') for src in source_list])
            pm_cache_ok = True
        except Exception as exc:
            pm_cache_ok = False
            log('Premiumize cache check failed: %s' % exc, xbmc.LOGWARNING)

    # Real-Debrid no longer exposes the old arbitrary-hash instant availability
    # endpoint. A hash already present/downloaded in the user's cloud is known
    # cached; other hashes remain VERIFY and ResolveURL applies its cached-only
    # add/check/delete behavior at resolution time. During Play All this cloud
    # snapshot is collected once and reused for every episode. It is only a label/
    # verification hint; every actual source is still resolved live.
    if rd_cloud_override is not None:
        rd_cloud = set(rd_cloud_override)
    else:
        rd_cloud = set()
        if rd_active:
            if progress_cb:
                progress_cb(76, 'Checking Real-Debrid account...')
            try:
                rd_cloud = rd.cloud_hashes()
            except Exception as exc:
                log('RD cloud hash lookup failed: %s' % exc, xbmc.LOGWARNING)

    rows = []
    for source in source_list:
        for provider in rurl_providers:
            pname = str(provider.get('name') or '').strip().lower()
            pclass = str(provider.get('class') or '')
            is_rd = pname == 'real-debrid' or pclass == 'RealDebridResolver'
            is_tb = pname == 'torbox' or pclass == 'TorBoxResolver'
            is_pm = pclass == 'PremiumizeMeResolver'

            if is_tb:
                cached = source['hash'] in tb_cached
                # If TorBox cached-only is enabled, never show a row whose hash
                # cannot be positively established as cached. ResolveURL will
                # also enforce cached-only when resolving as a second safeguard.
                if provider.get('cached_only') and (not tb_cache_ok or not cached):
                    continue
                cache_status = ('CACHED' if cached else 'UNCACHED') if tb_cache_ok else 'UNKNOWN'
            elif is_rd:
                cache_status = 'CACHED' if source['hash'] in rd_cloud else 'VERIFY'
            elif is_pm:
                cached = source.get('magnet') in pm_cached
                if provider.get('cached_only') and pm_cache_ok and not cached:
                    continue
                cache_status = ('CACHED' if cached else 'UNCACHED') if pm_cache_ok else 'UNKNOWN'
            else:
                # ResolveURL's other universal services do not expose one common
                # cache-check interface. Their resolver owns the final decision.
                cache_status = 'VERIFY' if provider.get('cached_only') else 'UNKNOWN'

            row = dict(source)
            row.update({
                'provider': provider.get('id'),
                'provider_name': provider.get('name'),
                'provider_code': provider.get('code'),
                'provider_color': provider.get('color'),
                'provider_priority': int(provider.get('priority') or 100),
                'resolveurl_cached_only': bool(provider.get('cached_only')),
                'rd_cloud': is_rd and source['hash'] in rd_cloud,
                'tb_cached': (source['hash'] in tb_cached) if is_tb and tb_cache_ok else None,
                'pm_cached': (source.get('magnet') in pm_cached) if is_pm and pm_cache_ok else None,
                'cache_status': cache_status,
            })
            rows.append(row)

    # The visible list order is also the fallback playback order. Quality is
    # primary, torrent size secondary; provider priority only breaks exact ties.
    rows.sort(key=lambda r: (
        -int(r.get('quality_rank') or 1),
        -int(r.get('size_bytes') or 0),
        int(r.get('provider_priority') or 100),
        str(r.get('provider_name') or '').lower(),
        _source_full_name(r).lower(),
    ))
    return rows

def _discover_source_session(media, show_dialog=True, provider_state=None, rd_cloud_override=None,
                             progress_title=None, text_queries=None):
    """Discover, filter and expand sources with a visible, cancellable progress UI.

    provider_state/rd_cloud_override let Play All reuse invariant account state
    between episodes. Torrent search results themselves are always fetched fresh.
    """
    progress = xbmcgui.DialogProgress()
    label = media.get('label') or media.get('title') or 'Media'
    heading = progress_title or 'Acquiring Sources'

    def update(percent, message):
        try:
            progress.update(int(percent), '%s\n%s' % (label, message))
        except Exception:
            pass

    try:
        progress.create('%s - %s' % (ADDON_NAME, heading), '%s\nPreparing providers...' % label)
        update(5, 'Preparing providers...')
        if provider_state is None:
            provider_state = _require_search_provider(show_dialog=show_dialog)
        rd, tb, rd_active, tb_active, bridge, rurl_providers = provider_state
        if not (rd_active or tb_active or rurl_providers):
            return None
        if progress.iscanceled():
            return None

        update(18, 'Searching torrent sources...')
        try:
            source_client = TorrentSourceClient()
            if text_queries:
                source_list = source_client.search_text(text_queries)
            else:
                source_list = source_client.get(media)
        except Exception as exc:
            if show_dialog:
                try:
                    progress.close()
                except Exception:
                    pass
                xbmcgui.Dialog().ok(ADDON_NAME, 'Torrent source search failed:\n%s' % exc)
            else:
                log('Torrent source search failed during Play All: %s' % exc, xbmc.LOGWARNING)
            return None
        if progress.iscanceled():
            return None

        # Distinguish an upstream search that returned no streams at all from
        # one that returned streams which were subsequently excluded by the
        # user's configured quality/size filters.
        if not source_list:
            if show_dialog:
                try:
                    progress.close()
                except Exception:
                    pass
                xbmcgui.Dialog().ok(ADDON_NAME, 'No streams for this selection are currently available')
            return None

        update(48, 'Applying quality and size filters...')
        source_list = [row for row in source_list if _source_allowed(row)]
        if not source_list:
            if show_dialog:
                try:
                    progress.close()
                except Exception:
                    pass
                xbmcgui.Dialog().ok(ADDON_NAME, 'No available streams were within the quality/size filters')
            return None

        update(56, 'Checking enabled debrid services...')
        rows = _expand_provider_rows(
            source_list, rd, tb, rd_active, tb_active, bridge, rurl_providers,
            rd_cloud_override=rd_cloud_override, progress_cb=update)
        if progress.iscanceled():
            return None
        if not rows:
            if show_dialog:
                try:
                    progress.close()
                except Exception:
                    pass
                xbmcgui.Dialog().ok(ADDON_NAME, 'No torrents passed the enabled provider/cache filters.')
            return None
        try:
            rows = rows[:max(1, int(float(ADDON.getSetting('source_limit') or 80)))]
        except Exception:
            pass
        update(100, 'Sources ready.')
        xbmc.sleep(100)
        return save_session({'media': media, 'sources': rows})
    finally:
        try:
            progress.close()
        except Exception:
            pass


def sources(media_type, imdb_id, title, season=None, episode=None, episode_title=None):
    # This is the *start* of a new torrent-source search. The previous source
    # cache remains valid until this point, then is explicitly discarded.
    clear_active_source_session()
    media = {'type': media_type, 'imdb_id': imdb_id, 'title': title, 'label': title}
    if media_type == 'series':
        media.update({'season': int(season), 'episode': int(episode),
                      'label': '%s - S%02dE%02d%s' % (title, int(season), int(episode),
                                                      (' - ' + episode_title) if episode_title else '')})
    session_id = _discover_source_session(media, show_dialog=True)
    if not session_id:
        return

    # Once discovery is complete, make this exact ordered list the active
    # temporary cache and move the visible Kodi directory to a stable cache-only
    # route. If Kodi reconstructs the directory after playback, it reads this
    # session instead of invoking source discovery again.
    set_active_source_session(session_id)
    url = plugin_url(BASE, action='source_results_current')
    xbmc.executebuiltin('Container.Update(%s)' % url)

def _focus_source_after_render(source_index):
    """Focus the exact source row after Kodi has rebuilt the directory.

    The source index is stored as a ListItem property so this works even when
    the skin inserts a parent-folder row or uses a non-default list control.
    """
    try:
        wanted = str(int(source_index))
    except Exception:
        return
    # endOfDirectory() returns before every skin has completed laying out the
    # container. Give Kodi a short bounded window to expose the rebuilt rows.
    monitor = xbmc.Monitor()
    for _ in range(20):
        if monitor.abortRequested():
            return
        for control_id in (50, 51, 52, 53, 54, 55):
            try:
                if not xbmc.getCondVisibility('Control.IsVisible(%d)' % control_id):
                    continue
                raw_count = xbmc.getInfoLabel('Container(%d).NumAllItems' % control_id)
                count = int(raw_count or 0)
                # NumAllItems can include the parent item. Scan the live list and
                # match our explicit property rather than assuming an offset.
                for position in range(max(0, count)):
                    prop = xbmc.getInfoLabel(
                        'Container(%d).ListItemAbsolute(%d).Property(PremiumPlayerSourceIndex)'
                        % (control_id, position))
                    if str(prop) == wanted:
                        xbmc.executebuiltin('Control.SetFocus(%d,%d,absolute)' % (control_id, position))
                        return
            except Exception:
                continue
        xbmc.sleep(100)


def _render_source_session(session_id, focus_index=None):
    data = load_session(session_id)
    if not data:
        xbmcgui.Dialog().ok(ADDON_NAME, 'This source list expired. Please search again.')
        return
    rows = data.get('sources') or []
    if not rows:
        xbmcgui.Dialog().ok(ADDON_NAME, 'No torrent sources are available.')
        return
    for index, source in enumerate(rows):
        label = _source_label(index, source)
        ctx = [('Download this torrent video', 'RunPlugin(%s)' % plugin_url(
            BASE, action='download_source', session=session_id, index=index))]
        ctx += _pin_context('pin_source', session=session_id, index=index)
        add_item(label, 'play_source', False, context=ctx, session=session_id, index=index,
                 properties={'PremiumPlayerSourceIndex': index})
    # Source results are never stored in Kodi's directory cache.
    end('videos')
    if focus_index not in (None, ''):
        _focus_source_after_render(focus_index)


def source_results(session, focus=None):
    # Legacy/session-specific route retained for pinned/backward-compatible URLs.
    _render_source_session(session, focus_index=focus)


def source_results_current():
    state = load_active_source_state()
    session_id = str(state.get('session') or '')
    if not session_id or not load_session(session_id):
        xbmcgui.Dialog().ok(ADDON_NAME, 'This source list expired. Please search again.')
        return
    _render_source_session(session_id, focus_index=state.get('focus'))


def _wait_for_video_window(timeout_ms=6000):
    """Wait until Kodi has fully returned from fullscreen playback.

    This function never opens a dialog and never rebuilds a plugin directory.
    It exists only to make a best-effort cursor move safe after fallback playback.
    """
    monitor = xbmc.Monitor()
    stable = 0
    steps = max(10, int(timeout_ms / 100))
    for _ in range(steps):
        if monitor.abortRequested():
            return False
        try:
            playing = xbmc.Player().isPlaying() or xbmc.getCondVisibility('Player.HasVideo')
        except Exception:
            playing = False
        try:
            fullscreen = xbmc.getCondVisibility('Window.IsVisible(fullscreenvideo)')
        except Exception:
            fullscreen = False
        try:
            videos = xbmc.getCondVisibility('Window.IsActive(videos)')
        except Exception:
            videos = False
        if not playing and not fullscreen and videos:
            stable += 1
            if stable >= 5:  # 500 ms continuously stable in the Videos window
                return True
        else:
            stable = 0
        if monitor.waitForAbort(0.1):
            return False
    log('Post-playback focus skipped: Videos window did not become stable', xbmc.LOGWARNING)
    return False


def _focus_existing_source_after_playback(source_index):
    """Move focus within the already-existing source list only.

    Never call Container.Update/Refresh here. Rebuilding a plugin directory while
    Kodi is unwinding playback can race the GUI/player shutdown path. The source
    result directory is still underneath fullscreen video, so all we need after a
    fallback source succeeds is to select that existing row.
    """
    if not _wait_for_video_window():
        return False
    try:
        wanted = str(int(source_index))
    except Exception:
        return False
    for control_id in (50, 51, 52, 53, 54, 55):
        try:
            if not xbmc.getCondVisibility('Control.IsVisible(%d)' % control_id):
                continue
            count = int(xbmc.getInfoLabel('Container(%d).NumAllItems' % control_id) or 0)
            for position in range(max(0, count)):
                prop = xbmc.getInfoLabel(
                    'Container(%d).ListItemAbsolute(%d).Property(PremiumPlayerSourceIndex)'
                    % (control_id, position))
                if str(prop) == wanted:
                    xbmc.executebuiltin('Control.SetFocus(%d,%d,absolute)' % (control_id, position))
                    log('Focused successful fallback source #%d without rebuilding directory' % (int(source_index) + 1), xbmc.LOGDEBUG)
                    return True
        except Exception:
            continue
    log('Post-playback focus skipped: successful source row not found in existing container', xbmc.LOGWARNING)
    return False


def _focus_existing_episode_after_playback(episode):
    """Best-effort Play-All manual-stop focus with no directory rebuild.

    Only focuses an episode row if that row already exists in the underlying
    Kodi container. Never opens a dialog and never calls Container.Update.
    """
    if not _wait_for_video_window():
        return False
    try:
        wanted = str(int(episode))
    except Exception:
        return False
    for control_id in (50, 51, 52, 53, 54, 55):
        try:
            if not xbmc.getCondVisibility('Control.IsVisible(%d)' % control_id):
                continue
            count = int(xbmc.getInfoLabel('Container(%d).NumAllItems' % control_id) or 0)
            for position in range(max(0, count)):
                prop = xbmc.getInfoLabel(
                    'Container(%d).ListItemAbsolute(%d).Property(PremiumPlayerEpisode)'
                    % (control_id, position))
                if str(prop) == wanted:
                    xbmc.executebuiltin('Control.SetFocus(%d,%d,absolute)' % (control_id, position))
                    log('Play All manual stop: focused existing episode %s without directory rebuild' % wanted,
                        xbmc.LOGDEBUG)
                    return True
        except Exception:
            continue
    log('Play All manual stop: episode row not present; skipped post-stop navigation for safety',
        xbmc.LOGDEBUG)
    return False


def _safe_container_return(url, timeout_ms=8000):
    """Used only when navigation to a different directory is truly required.

    Normal single-source playback must not use this function; its source list is
    already present underneath fullscreen video. Play All may need to navigate to
    a different season/episode directory after the queue finishes.
    """
    monitor = xbmc.Monitor()
    if not _wait_for_video_window(timeout_ms=timeout_ms):
        return False
    # Give Kodi one additional event-loop turn before changing directories.
    if monitor.waitForAbort(0.25):
        return False
    try:
        xbmc.executebuiltin('Container.Update(%s,replace)' % url)
        return True
    except Exception as exc:
        log('Post-playback navigation failed: %s' % exc, xbmc.LOGWARNING)
        return False


def play_source(session, index):
    selected_index = int(index)
    success_index = PlaybackEngine().play_session(session, selected_index, handle=HANDLE)
    if success_index is None:
        return

    # Persist the source that actually started. If Kodi reconstructs the cached
    # result directory after playback, that same row is focused without a new
    # torrent search.
    set_active_source_focus(session, success_index)

    # If the exact source the user clicked played, Kodi already has the correct
    # row selected. Do absolutely no GUI work after Stop. This is the common path
    # and avoids the crash-prone post-playback directory rebuild entirely.
    if int(success_index) == selected_index:
        log('Selected source #%d played; no post-playback UI action required' % (selected_index + 1), xbmc.LOGDEBUG)
        return

    # If fallback succeeded on a later source, only move focus in the existing
    # list. Do not reload the source directory and do not display any modal.
    _focus_existing_source_after_playback(success_index)


def download_source(session, index):
    data = load_session(session)
    if not data:
        xbmcgui.Dialog().ok(ADDON_NAME, 'This source list expired. Please search again.')
        return
    sources_list = data.get('sources') or []
    index = int(index)
    if index < 0 or index >= len(sources_list):
        return
    source = sources_list[index]
    media = data.get('media') or {}
    engine = PlaybackEngine()
    cleanup = None
    try:
        cleanup_enabled = setting_bool('auto_cleanup_playback', True)
        direct, provider, cleanup = engine.resolve_source(
            source, media, track_cleanup=cleanup_enabled)
        suggested = source.get('filename') or media.get('label') or media.get('title')
        download_url(direct, media, suggested)
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not resolve this torrent for download:\n%s' % exc)
    finally:
        # A temporary provider item must outlive the file transfer, then be
        # removed regardless of success, failure, or user cancellation.
        if cleanup:
            engine._cleanup_now(cleanup)



def new_releases_root(main_entry=False):
    # This shared menu is the New Releases cache boundary.  Clear every Movie
    # and TV genre result set whenever this route is actually rendered.  Kodi
    # can restore this directory from navigation history without reinvoking the
    # plugin; child genre routes detect when this menu is their currently visible
    # parent and perform the same invalidation before descending again.
    clear_new_releases_cache()
    mark_new_releases_menu_rendered()
    state = _require_search_provider(show_dialog=True)
    if not (state[2] or state[3] or state[5]):
        return
    add_item(media_text('movie', 'New Movie Releases'), 'new_release_genres', True,
             menu_icon='movie_new', media_type='movie')
    add_item(media_text('series', 'New TV Episodes'), 'new_release_genres', True,
             menu_icon='tv_new', media_type='series')
    end_menu()


def _visible_container_action():
    try:
        current = str(xbmc.getInfoLabel('Container.FolderPath') or '')
        query = parse.urlsplit(current).query
        values = parse.parse_qs(query).get('action') or []
        return str(values[-1] if values else '')
    except Exception:
        return ''


def new_release_genres(media_type):
    # If Kodi restored the shared New Releases parent from in-memory history,
    # its Python route did not run on Back.  Detect that exact visible parent
    # before descending and clear here.  Re-rendering this genre menu itself
    # does not clear anything, so cached pages remain fast below the boundary.
    prepare_new_releases_child_entry()

    client = MetadataClient()
    page_icon = 'movie_new' if media_type == 'movie' else 'tv_new'
    add_item('All Genres', 'new_releases', True, menu_icon=page_icon,
             media_type=media_type)
    try:
        # Genre-menu construction must be immediate. Do not crawl the TVmaze
        # 30-day schedule here; that work belongs only to the selected genre.
        # TVmaze publishes a fixed genre vocabulary, so series categories are
        # available locally with no network request. Movies retain Cinemeta's
        # manifest-driven taxonomy.
        if media_type == 'series':
            genres = client.tv_genres() + ['Other']
        else:
            genres = client.genres(media_type)
    except Exception as exc:
        log('Could not load New Releases genres: %s' % exc, xbmc.LOGWARNING)
        genres = []
    seen = set()
    for genre in genres:
        genre = str(genre or '').strip()
        key = genre.casefold()
        if not genre or key in seen:
            continue
        seen.add(key)
        add_item(genre, 'new_releases', True, menu_icon=page_icon,
                 media_type=media_type, genre=genre)
    end_menu()

def _release_date_key(meta):
    """Return a sortable (year, month, day) tuple, newest first when reversed."""
    for key in ('_airdate', 'lastAired', 'last_air_date', 'released',
                'releaseDate', 'release_date', 'premiered', 'firstAired',
                'releaseInfo', 'year'):
        value = meta.get(key)
        if value in (None, ''):
            continue
        match = re.search(r'(\d{4})(?:[-/.](\d{1,2}))?(?:[-/.](\d{1,2}))?', str(value))
        if not match:
            continue
        year = int(match.group(1))
        month = int(match.group(2) or 1)
        day = int(match.group(3) or 1)
        if 1 <= month <= 12 and 1 <= day <= 31:
            return year, month, day
    return 0, 0, 0


def new_releases(media_type, page=1, genre=None):
    client = MetadataClient()
    genre = str(genre or '').strip()
    try:
        page = max(1, min(3, int(page or 1)))
    except (TypeError, ValueError):
        page = 1

    # Cache the complete finalized result set independently for each genre.
    # Pages 2/3 are therefore local-only; every Movie/TV genre cache survives
    # until the user returns to the shared New Releases menu.
    rows = load_new_releases_cache(media_type, genre)
    if rows is None:
        try:
            if media_type == 'series':
                # TV acquisition is genre-agnostic: maintain one rolling raw
                # 30-day All Genres TVmaze pool, then filter the selected genre
                # locally. The 300-row cap applies only after local filtering;
                # there is no 300-results-per-genre provider crawl.
                rows = client.new_tv_releases(
                    days=30, limit=NEW_RELEASES_MAX_RESULTS, genre=genre or None)
            elif genre:
                # Cinemeta's genre catalog is popularity-ranked. Walk its
                # year-sorted New catalog instead so these are genuinely the
                # newest releases for the selected genre.
                rows = client.new_releases_by_genre(
                    media_type, genre, limit=NEW_RELEASES_MAX_RESULTS)
            else:
                # All Genres deliberately retains the pre-genre behavior.
                rows = client.new_releases(
                    media_type, limit=NEW_RELEASES_MAX_RESULTS, enrich=False)
        except Exception as exc:
            xbmcgui.Dialog().ok(ADDON_NAME, 'Could not load new releases:\n%s' % exc)
            return

        rows = [meta for meta in list(rows or [])
                if str(meta.get('name') or meta.get('title') or '').strip()]
        rows = rows[:NEW_RELEASES_MAX_RESULTS]

        # Finalize all three Movie pages up front. Previously each 100-row page
        # performed its own metadata enrichment, which made first navigation to
        # pages 2/3 slow even though the 300-row catalog had already been read.
        if media_type != 'series' and rows and not genre:
            rows = client.enrich_rows(media_type, rows)

        # Python's sort is stable, so equal/unknown dates retain the catalog's
        # original ordering while every known date is strictly newest-first.
        rows.sort(key=_release_date_key, reverse=True)
        save_new_releases_cache(media_type, rows, genre)
        log('Cached %d %s New Releases rows for genre=%s' %
            (len(rows), media_type, genre or 'All Genres'))
    else:
        # Defensive normalization protects rendering if a cache file was
        # partially written or created by an older build.
        rows = [meta for meta in list(rows or []) if isinstance(meta, dict)
                and str(meta.get('name') or meta.get('title') or '').strip()]
        rows = rows[:NEW_RELEASES_MAX_RESULTS]
        log('Using cached %d %s New Releases rows for genre=%s' %
            (len(rows), media_type, genre or 'All Genres'))

    page_count = max(1, min(3, (len(rows) + NEW_RELEASES_PAGE_SIZE - 1) // NEW_RELEASES_PAGE_SIZE))
    page = min(page, page_count)
    start = (page - 1) * NEW_RELEASES_PAGE_SIZE
    visible_rows = rows[start:start + NEW_RELEASES_PAGE_SIZE]
    page_icon = 'movie_new' if media_type == 'movie' else 'tv_new'

    for meta in visible_rows:
        title = meta.get('name') or meta.get('title') or ''
        if not title:
            continue
        if media_type == 'series':
            try:
                season = int(meta.get('season'))
                episode = int(meta.get('episode'))
            except (TypeError, ValueError):
                continue
            episode_title = meta.get('episode_title') or 'Episode %d' % episode
            released = str(meta.get('_airdate') or meta.get('releaseInfo') or '')[:10]
            label = media_text(
                'series', '%s - S%02dE%02d - %s%s' %
                (title, season, episode, episode_title,
                 ' (%s)' % released if released else ''))
        else:
            year = meta.get('releaseInfo') or meta.get('year') or ''
            label = media_text('movie', '%s%s' %
                               (title, ' (%s)' % str(year)[:4] if year else ''))
        poster = meta.get('poster') or ''
        thumbnail = meta.get('thumbnail') or poster
        background = meta.get('background') or ''
        art = {'poster': poster, 'thumb': thumbnail, 'icon': thumbnail,
               'fanart': background, 'landscape': thumbnail or background}
        info = {'title': title, 'plot': meta.get('description') or meta.get('plot') or ''}
        imdb = meta.get('imdb_id') or meta.get('id')
        if media_type == 'series':
            info.update({'title': episode_title, 'tvshowtitle': title,
                         'season': season, 'episode': episode})
            if released:
                info['premiered'] = released
            ctx = _pin_context('pin_meta', kind='episode', imdb_id=imdb, title=title,
                               season=season, episode=episode,
                               episode_title=episode_title)
            # Resolve the deepest location that exists in the normal TV
            # browser. Very recent schedule data can arrive before Cinemeta has
            # the corresponding episode (or even season), so never jump blindly
            # into an empty episode directory.
            add_item(label, 'new_release_tv_target', True, art=art, info=info, context=ctx,
                     playable=False, imdb_id=imdb, title=title, season=season,
                     episode=episode, episode_title=episode_title)
        else:
            if str(year)[:4].isdigit():
                info['year'] = int(str(year)[:4])
            ctx = []
            if imdb and str(imdb).startswith('tt'):
                ctx = _pin_context('pin_meta', kind='movie', imdb_id=imdb, title=title)
            add_item(label, 'search_results', True, art=art, info=info, context=ctx,
                     media_type='movie', query=title)

    if page < page_count:
        add_item('Next Page (%d/%d)' % (page + 1, page_count), 'new_releases', True,
                 menu_icon=page_icon, media_type=media_type, page=page + 1,
                 genre=genre or None)
    end('movies' if media_type == 'movie' else 'episodes')


def _next_episode_countdown():
    """Stable five-second next-episode countdown.

    Kodi GUI controls are intentionally updated only from this plugin thread.
    The previous custom WindowDialog used a Python worker thread to mutate GUI
    controls, which can destabilize Kodi on some platforms.
    """
    progress = xbmcgui.DialogProgress()
    try:
        progress.create('Playing Next Episode...', 'Starting in 5 seconds...\nPress STOP/Cancel to stop.')
        monitor = xbmc.Monitor()
        for elapsed in range(5):
            remaining = 5 - elapsed
            progress.update(elapsed * 20, 'Starting in %d second%s...\nPress STOP/Cancel to stop.' % (
                remaining, '' if remaining == 1 else 's'))
            # Sleep in short intervals so cancellation is observed promptly.
            for _ in range(10):
                if monitor.abortRequested() or progress.iscanceled():
                    return False
                xbmc.sleep(100)
        progress.update(100, 'Acquiring sources for next episode...')
        return not progress.iscanceled()
    finally:
        try:
            progress.close()
        except Exception:
            pass


def _episode_queue(imdb_id, start_season=None, start_episode=None):
    client = MetadataClient()
    meta = client.meta('series', imdb_id)
    richer = client.tvmaze_episodes(imdb_id)
    rows = []
    for video in meta.get('videos') or []:
        try:
            season = int(video.get('season'))
            episode = int(video.get('episode'))
        except Exception:
            continue
        if start_season in (None, '') and season < 1:
            continue
        if start_season not in (None, ''):
            ss = int(start_season)
            se = int(start_episode) if start_episode not in (None, '') else 1
            if (season, episode) < (ss, se):
                continue
        detail = richer.get((season, episode), {})
        rows.append({'season': season, 'episode': episode,
                     'title': detail.get('title') or video.get('title') or video.get('name') or 'Episode %d' % episode})
    rows.sort(key=lambda row: (row['season'], row['episode']))
    return rows


def play_all_tv(imdb_id, title, start_season=None, start_episode=None):
    # Play All starts its own sequence of fresh per-episode source searches.
    clear_active_source_session()
    try:
        queue = _episode_queue(imdb_id, start_season, start_episode)
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not build episode queue:\n%s' % exc)
        return
    if not queue:
        xbmcgui.Dialog().ok(ADDON_NAME, 'No episodes are available from this starting point.')
        return

    # ResolveURL/provider state is invariant for this queue. Discover it once
    # instead of rebuilding every resolver for every episode.
    provider_state = _require_search_provider(show_dialog=True)
    rd, tb, rd_active, tb_active, bridge, rurl_providers = provider_state
    if not (rd_active or tb_active or rurl_providers):
        return

    # RD cloud enumeration can contain thousands of torrents and was previously
    # repeated for every episode. Snapshot it once for source labels/cache hints.
    # Actual source resolution is still performed live for each episode.
    rd_cloud_snapshot = set()
    if rd_active:
        prep = xbmcgui.DialogProgress()
        try:
            prep.create('%s - Preparing Play All' % ADDON_NAME,
                        'Loading Real-Debrid account state...')
            prep.update(25, 'Loading Real-Debrid account state...')
            try:
                rd_cloud_snapshot = rd.cloud_hashes()
            except Exception as exc:
                log('Play All RD cloud snapshot failed: %s' % exc, xbmc.LOGWARNING)
                rd_cloud_snapshot = set()
            prep.update(100, 'Ready.')
        finally:
            try:
                prep.close()
            except Exception:
                pass

    last_played = None
    final_reason = None
    engine = PlaybackEngine()
    for pos, ep in enumerate(queue):
        media = {'type': 'series', 'imdb_id': imdb_id, 'title': title,
                 'season': ep['season'], 'episode': ep['episode'],
                 'label': '%s - S%02dE%02d - %s' % (title, ep['season'], ep['episode'], ep['title'])}
        session_id = _discover_source_session(
            media, show_dialog=False, provider_state=provider_state,
            rd_cloud_override=rd_cloud_snapshot, progress_title='Acquiring Sources')
        if not session_id:
            xbmcgui.Dialog().ok(ADDON_NAME, 'No sources were able to be played')
            break
        outcome = engine.play_session(session_id, 0, handle=-1,
                                      force_auto=True, force_wrap=False, return_outcome=True)
        if not outcome:
            final_reason = 'no_playable_source'
            break
        last_played = ep
        final_reason = outcome.get('end_reason') or 'stopped'
        if final_reason != 'ended' or pos >= len(queue) - 1:
            break
        if not _next_episode_countdown():
            final_reason = 'cancelled_countdown'
            break

    if not last_played:
        return

    # CRITICAL: a user-initiated Stop must not rebuild/navigate the Kodi plugin
    # directory. Kodi is still unwinding the player invocation at this point and
    # Container.Update here has produced intermittent full-process crashes on
    # real devices. If the episode row is already present underneath playback
    # (the common "Play All Starting Here" case), simply focus it in place. If
    # it is not present (for example after crossing into another season), leave
    # the existing navigation untouched rather than risking Kodi stability.
    if final_reason == 'stopped':
        _focus_existing_episode_after_playback(last_played['episode'])
        return

    # For non-manual termination (natural end of final episode, countdown
    # cancellation, or failure to find a source for the following episode),
    # playback teardown is no longer user-driven. A guarded directory return is
    # still allowed so the last successfully played episode can be shown.
    url = plugin_url(BASE, action='episodes', imdb_id=imdb_id, title=title,
                     season=last_played['season'], focus_episode=last_played['episode'])
    _safe_container_return(url)

def rd_root():
    rd = RealDebrid()
    if not rd.resolveurl_authorized:
        add_item('Configure Real-Debrid in ResolveURL', 'open_resolveurl_settings', False,
                 playable=False, menu_icon='settings')
    else:
        add_item('Torrents', 'rd_torrents', True, menu_icon='torrents')
        add_item('Downloads', 'rd_downloads', True, menu_icon='downloads')
    end_menu()


def rd_torrents():
    rd = RealDebrid()
    try:
        items = rd.list_torrents()
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not load Real-Debrid torrents:\n%s' % exc)
        return
    update_account_item_count('rd', 'torrents', len(items))
    names = [item.get('filename') or '' for item in items]
    series_names = [item.get('filename') or '' for item in items
                    if looks_like_series_tree(item.get('files') or [])]
    series_set = set(series_names)
    metadata = match_entries([name for name in names if name not in series_set],
                             container=True, provider='rd')
    if series_names:
        metadata.update(match_entries(series_names, container=True, series_hint=True, provider='rd'))
    for item in items:
        raw_name = item.get('filename') or ''
        matched = metadata.get(raw_name, {})
        label = raw_name or item.get('hash') or str(item.get('id'))
        status = item.get('status') or ''
        if status:
            label += '  [%s]' % status
        if item.get('bytes'):
            label += '  %s' % human_size(item.get('bytes'))
        context = [('Delete torrent', 'RunPlugin(%s)' % plugin_url(
            BASE, action='delete_rd_torrent', torrent_id=item.get('id')))]
        context += _pin_context('pin_account', kind='rd_torrent', provider='rd', label=label, torrent_id=item.get('id'))
        kwargs = _metadata_context_kwargs(matched.get('context'))
        add_item(label, 'browse_rd_torrent', True, context=context, art=matched.get('art'), info=matched.get('info'),
                 torrent_id=item.get('id'), prefix='', **kwargs)
    end('files')


def _folder_entries(files, prefix):
    prefix = clean_path(prefix).rstrip('/')
    folders = {}
    direct = []
    for f in files or []:
        path = clean_path(f.get('path') or f.get('name') or f.get('short_name'))
        if prefix:
            if path == prefix:
                rel = os.path.basename(path)
            elif path.startswith(prefix + '/'):
                rel = path[len(prefix) + 1:]
            else:
                continue
        else:
            rel = path
        if '/' in rel:
            first = rel.split('/', 1)[0]
            folders[first] = (prefix + '/' + first).strip('/')
        else:
            clone = dict(f)
            clone['_path'] = path
            direct.append(clone)
    return folders, direct


def browse_rd_torrent(torrent_id, prefix='', meta_type='', meta_title='', meta_imdb='', meta_year='', meta_season=''):
    rd = RealDebrid()
    try:
        info = rd.torrent_info(torrent_id)
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not open Real-Debrid torrent:\n%s' % exc)
        return
    all_files = info.get('files') or []
    folders, files = _folder_entries(all_files, prefix)
    parent_name = info.get('filename') or info.get('name') or ''
    metadata_parent = os.path.basename(clean_path(prefix)) if prefix else parent_name
    series_hint = looks_like_series_tree(all_files)
    known_context = _metadata_context(meta_type, meta_title, meta_imdb, meta_year, meta_season)
    folder_metadata = match_entries(list(folders.keys()), parent_name=metadata_parent,
                                    root_name=parent_name, container=True,
                                    series_hint=series_hint, provider='rd',
                                    known_context=known_context)
    file_metadata = match_entries([os.path.basename(f['_path']) for f in files if is_video(os.path.basename(f['_path']))],
                                  parent_name=metadata_parent, root_name=parent_name,
                                  series_hint=series_hint, provider='rd',
                                  known_context=known_context)
    sort_key = lambda value: media_sort_key(value, parent_name=metadata_parent,
                                             root_name=parent_name, known_context=known_context)
    for name in sorted(folders, key=sort_key):
        folder_prefix = folders[name]
        matched = folder_metadata.get(name, {})
        child_context = _child_metadata_context(matched, known_context)
        ctx = _pin_context('pin_account', kind='rd_folder', provider='rd', label=name + '/',
                           torrent_id=torrent_id, prefix=folder_prefix)
        kwargs = _metadata_context_kwargs(child_context)
        add_item(name + '/', 'browse_rd_torrent', True, context=ctx,
                 art=matched.get('art'), info=matched.get('info'),
                 torrent_id=torrent_id, prefix=folder_prefix, **kwargs)
    for f in sorted(files, key=lambda x: sort_key(os.path.basename(x['_path']))):
        name = os.path.basename(f['_path'])
        label = name + ('  %s' % human_size(f.get('bytes')) if f.get('bytes') else '')
        if not f.get('selected'):
            label += '  [not downloaded]'
        ctx = []
        if f.get('selected'):
            ctx.append(('Download', 'RunPlugin(%s)' % plugin_url(BASE, action='download_rd_file', torrent_id=torrent_id, file_id=f.get('id'))))
        ctx += _pin_context('pin_account', kind='rd_file', provider='rd', label=label, torrent_id=torrent_id,
                            file_id=f.get('id'), filename=name)
        action = 'play_rd_file' if is_video(name) and f.get('selected') else 'file_info'
        matched = file_metadata.get(name, {})
        add_item(label, action, False, context=ctx, art=matched.get('art'), info=matched.get('info'),
                 torrent_id=torrent_id, file_id=f.get('id'), filename=name,
                 message=('This file was not selected/downloaded in Real-Debrid.' if not f.get('selected') else 'Use the context menu to download this non-video file.'))
    end('files')


def play_rd_file(torrent_id, file_id, filename=''):
    rd = RealDebrid()
    try:
        direct, target = rd.resolve_cloud_file(torrent_id, file_id)
        _set_resolved(direct, target.get('path') or filename)
    except Exception as exc:
        _set_failed('Real-Debrid file playback failed:\n%s' % exc)


def download_rd_file(torrent_id, file_id):
    rd = RealDebrid()
    try:
        direct, target = rd.resolve_cloud_file(torrent_id, file_id)
        media = {'title': os.path.basename(clean_path(target.get('path') or target.get('name') or 'Real-Debrid'))}
        download_url(direct, media, os.path.basename(clean_path(target.get('path') or target.get('name'))))
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Real-Debrid download failed:\n%s' % exc)


def rd_downloads():
    rd = RealDebrid()
    try:
        downloads = rd.list_downloads()
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not load Real-Debrid downloads:\n%s' % exc)
        return
    update_account_item_count('rd', 'downloads', len(downloads))
    sid = save_session({'rd_downloads': downloads})
    metadata = match_entries([item.get('filename') or '' for item in downloads if is_video(item.get('filename') or '')],
                             provider='rd')
    for idx, item in enumerate(downloads):
        raw_name = item.get('filename') or ''
        matched = metadata.get(raw_name, {})
        name = raw_name or item.get('host') or str(item.get('id'))
        if item.get('filesize'):
            name += '  %s' % human_size(item.get('filesize'))
        ctx = [('Download', 'RunPlugin(%s)' % plugin_url(BASE, action='download_rd_download', session=sid, index=idx))]
        ctx += _pin_context('pin_account', kind='rd_download', provider='rd', label=name,
                            download_id=item.get('id'), filename=item.get('filename') or '')
        add_item(name, 'play_rd_download', False, context=ctx, art=matched.get('art'), info=matched.get('info'),
                 session=sid, index=idx)
    end('files')


def _rd_download_entry(session, index):
    data = load_session(session) or {}
    items = data.get('rd_downloads') or []
    index = int(index)
    return items[index] if 0 <= index < len(items) else None


def play_rd_download(session, index):
    item = _rd_download_entry(session, index)
    if not item:
        return _set_failed('This Real-Debrid download entry expired.')
    direct = item.get('download')
    if not direct:
        return _set_failed('Real-Debrid did not provide a direct download URL.')
    _set_resolved(direct, item.get('filename'))


def download_rd_download(session, index):
    item = _rd_download_entry(session, index)
    if not item:
        return
    media = {'title': item.get('filename') or 'Real-Debrid'}
    download_url(item.get('download'), media, item.get('filename'))


def tb_root():
    tb = TorBox()
    if not tb.resolveurl_authorized:
        add_item('Configure TorBox in ResolveURL', 'open_resolveurl_settings', False,
                 playable=False, menu_icon='settings')
    else:
        add_item('Torrents', 'tb_items', True, menu_icon='torrents', item_type='torrent')
        add_item('Usenet Downloads', 'tb_items', True, menu_icon='usenet', item_type='usenet')
        add_item('Web Downloads', 'tb_items', True, menu_icon='web_downloads', item_type='web')
    end_menu()


def _tb_list(item_type):
    tb = TorBox()
    if item_type == 'torrent':
        return tb.list_torrents()
    if item_type == 'usenet':
        return tb.list_usenet()
    return tb.list_webdl()


def tb_items(item_type):
    try:
        items = _tb_list(item_type)
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not load TorBox %s items:\n%s' % (item_type, exc))
        return
    update_account_item_count('tb', item_type, len(items))
    names = [item.get('name') or item.get('torrent_name') or '' for item in items]
    series_names = [item.get('name') or item.get('torrent_name') or '' for item in items
                    if looks_like_series_tree(item.get('files') or [])]
    series_set = set(series_names)
    metadata = match_entries([name for name in names if name not in series_set],
                             container=True, provider='tb')
    if series_names:
        metadata.update(match_entries(series_names, container=True, series_hint=True, provider='tb'))
    for item in items:
        item_id = item.get('id') or item.get('%s_id' % item_type) or item.get('torrent_id') or item.get('usenet_id') or item.get('webdownload_id')
        raw_name = item.get('name') or item.get('torrent_name') or ''
        matched = metadata.get(raw_name, {})
        name = raw_name or item.get('hash') or str(item_id)
        state = item.get('download_state') or ''
        if state:
            name += '  [%s]' % state
        size = item.get('size') or item.get('bytes')
        if size:
            name += '  %s' % human_size(size)
        context = []
        if item_type == 'torrent':
            context.append(('Delete torrent', 'RunPlugin(%s)' % plugin_url(
                BASE, action='delete_tb_torrent', torrent_id=item_id)))
        context += _pin_context('pin_account', kind='tb_item', provider='tb', label=name,
                                item_type=item_type, item_id=item_id)
        kwargs = _metadata_context_kwargs(matched.get('context'))
        add_item(name, 'browse_tb_item', True, context=context, art=matched.get('art'), info=matched.get('info'),
                 item_type=item_type, item_id=item_id, prefix='', **kwargs)
    end('files')


def _tb_info(tb, item_type, item_id):
    if item_type == 'torrent':
        return tb.torrent_info(item_id)
    endpoint = '/usenet/mylist' if item_type == 'usenet' else '/webdl/mylist'
    result = tb.get(endpoint, {'id': item_id})
    if isinstance(result, list):
        return result[0] if result else {}
    return result or {}


def browse_tb_item(item_type, item_id, prefix='', meta_type='', meta_title='', meta_imdb='', meta_year='', meta_season=''):
    tb = TorBox()
    try:
        info = _tb_info(tb, item_type, item_id)
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not open TorBox item:\n%s' % exc)
        return
    all_files = info.get('files') or []
    folders, files = _folder_entries(all_files, prefix)
    parent_name = info.get('name') or info.get('torrent_name') or info.get('filename') or ''
    metadata_parent = os.path.basename(clean_path(prefix)) if prefix else parent_name
    series_hint = looks_like_series_tree(all_files)
    known_context = _metadata_context(meta_type, meta_title, meta_imdb, meta_year, meta_season)
    folder_metadata = match_entries(list(folders.keys()), parent_name=metadata_parent,
                                    root_name=parent_name, container=True,
                                    series_hint=series_hint, provider='tb',
                                    known_context=known_context)
    file_metadata = match_entries([os.path.basename(f['_path']) for f in files if is_video(os.path.basename(f['_path']))],
                                  parent_name=metadata_parent, root_name=parent_name,
                                  series_hint=series_hint, provider='tb',
                                  known_context=known_context)
    sort_key = lambda value: media_sort_key(value, parent_name=metadata_parent,
                                             root_name=parent_name, known_context=known_context)
    for name in sorted(folders, key=sort_key):
        folder_prefix = folders[name]
        matched = folder_metadata.get(name, {})
        child_context = _child_metadata_context(matched, known_context)
        ctx = _pin_context('pin_account', kind='tb_folder', provider='tb', label=name + '/', item_type=item_type,
                           item_id=item_id, prefix=folder_prefix)
        kwargs = _metadata_context_kwargs(child_context)
        add_item(name + '/', 'browse_tb_item', True, context=ctx,
                 art=matched.get('art'), info=matched.get('info'),
                 item_type=item_type, item_id=item_id, prefix=folder_prefix, **kwargs)
    for f in sorted(files, key=lambda x: sort_key(os.path.basename(x['_path']))):
        name = os.path.basename(f['_path'])
        size = f.get('size') or f.get('bytes')
        label = name + ('  %s' % human_size(size) if size else '')
        ctx = [('Download', 'RunPlugin(%s)' % plugin_url(BASE, action='download_tb_file', item_type=item_type,
                                                          item_id=item_id, file_id=f.get('id')))]
        ctx += _pin_context('pin_account', kind='tb_file', provider='tb', label=label, item_type=item_type,
                            item_id=item_id, file_id=f.get('id'), filename=name)
        action = 'play_tb_file' if is_video(name) else 'file_info'
        matched = file_metadata.get(name, {})
        add_item(label, action, False, context=ctx, art=matched.get('art'), info=matched.get('info'),
                 item_type=item_type, item_id=item_id, file_id=f.get('id'), filename=name,
                 message=('Archive file: use Download from the context menu.' if is_archive(name) else 'Use the context menu to download this non-video file.'))
    end('files')


def play_tb_file(item_type, item_id, file_id, filename=''):
    tb = TorBox()
    try:
        direct, target = tb.resolve_cloud_file(item_type, item_id, file_id)
        _set_resolved(direct, target.get('name') or target.get('short_name') or filename)
    except Exception as exc:
        _set_failed('TorBox file playback failed:\n%s' % exc)


def download_tb_file(item_type, item_id, file_id):
    tb = TorBox()
    try:
        direct, target = tb.resolve_cloud_file(item_type, item_id, file_id)
        name = target.get('name') or target.get('short_name') or 'TorBox file'
        media = {'title': os.path.basename(clean_path(name))}
        download_url(direct, media, os.path.basename(clean_path(name)))
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'TorBox download failed:\n%s' % exc)



def _native_cloud_client(provider):
    client = client_for_account(provider)
    if not client:
        raise ApiError('Unsupported native provider: %s' % provider)
    if not (client.enabled and client.authorized):
        raise ApiError('%s is not enabled and authorized in ResolveURL' % client.NAME)
    return client


def cloud_root(provider):
    try:
        client = _native_cloud_client(provider)
    except Exception as exc:
        add_item('Configure provider in ResolveURL', 'open_resolveurl_settings', False,
                 playable=False, menu_icon='settings')
        end_menu()
        return
    add_item('Torrents', 'cloud_torrents', True, menu_icon='torrents', provider=provider)
    end_menu()


def cloud_torrents(provider):
    try:
        client = _native_cloud_client(provider)
        items = client.list_torrents()
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not load provider torrents:\n%s' % exc)
        return
    update_account_item_count(provider, 'torrents', len(items))
    names = [item.get('filename') or item.get('name') or '' for item in items]
    series_names = [item.get('filename') or item.get('name') or '' for item in items
                    if looks_like_series_tree(item.get('files') or [])]
    series_set = set(series_names)
    metadata = match_entries([name for name in names if name not in series_set],
                             container=True, provider=provider)
    if series_names:
        metadata.update(match_entries(series_names, container=True, series_hint=True, provider=provider))
    for item in items:
        item_id = item.get('torrentid') or item.get('id') or item.get('torrent_id') or item.get('tid')
        if item_id is None:
            continue
        raw_name = item.get('filename') or item.get('name') or ''
        matched = metadata.get(raw_name, {})
        label = raw_name or item.get('hash') or str(item_id)
        status = item.get('status') or item.get('state') or ''
        if status:
            label += '  [%s]' % status
        size = item.get('size') or item.get('totalSize') or item.get('bytes')
        if size:
            label += '  %s' % human_size(size)
        ctx = [('Delete torrent', 'RunPlugin(%s)' % plugin_url(
            BASE, action='delete_cloud_torrent', provider=provider, torrent_id=item_id))]
        ctx += _pin_context('pin_account', kind='cloud_torrent', provider=provider,
                            label=label, torrent_id=item_id)
        kwargs = _metadata_context_kwargs(matched.get('context'))
        add_item(label, 'browse_cloud_torrent', True, context=ctx, art=matched.get('art'), info=matched.get('info'),
                 provider=provider, torrent_id=item_id, prefix='', **kwargs)
    end('files')


def browse_cloud_torrent(provider, torrent_id, prefix='', meta_type='', meta_title='', meta_imdb='', meta_year='', meta_season=''):
    try:
        client = _native_cloud_client(provider)
        info = client.torrent_info(torrent_id)
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not open provider torrent:\n%s' % exc)
        return
    all_files = info.get('files') or []
    folders, files = _folder_entries(all_files, prefix)
    parent_name = info.get('filename') or info.get('name') or ''
    metadata_parent = os.path.basename(clean_path(prefix)) if prefix else parent_name
    series_hint = looks_like_series_tree(all_files)
    known_context = _metadata_context(meta_type, meta_title, meta_imdb, meta_year, meta_season)
    folder_metadata = match_entries(list(folders.keys()), parent_name=metadata_parent,
                                    root_name=parent_name, container=True,
                                    series_hint=series_hint, provider=provider,
                                    known_context=known_context)
    file_metadata = match_entries([os.path.basename(f['_path']) for f in files if is_video(os.path.basename(f['_path']))],
                                  parent_name=metadata_parent, root_name=parent_name,
                                  series_hint=series_hint, provider=provider,
                                  known_context=known_context)
    sort_key = lambda value: media_sort_key(value, parent_name=metadata_parent,
                                             root_name=parent_name, known_context=known_context)
    for name in sorted(folders, key=sort_key):
        folder_prefix = folders[name]
        matched = folder_metadata.get(name, {})
        child_context = _child_metadata_context(matched, known_context)
        ctx = _pin_context('pin_account', kind='cloud_folder', provider=provider,
                           label=name + '/', torrent_id=torrent_id, prefix=folder_prefix)
        kwargs = _metadata_context_kwargs(child_context)
        add_item(name + '/', 'browse_cloud_torrent', True, context=ctx,
                 art=matched.get('art'), info=matched.get('info'),
                 provider=provider, torrent_id=torrent_id, prefix=folder_prefix, **kwargs)
    for f in sorted(files, key=lambda x: sort_key(os.path.basename(x['_path']))):
        name = os.path.basename(f['_path'])
        size = f.get('size') or f.get('bytes')
        label = name + ('  %s' % human_size(size) if size else '')
        file_id = f.get('id')
        ctx = [('Download', 'RunPlugin(%s)' % plugin_url(
            BASE, action='download_cloud_file', provider=provider,
            torrent_id=torrent_id, file_id=file_id))]
        ctx += _pin_context('pin_account', kind='cloud_file', provider=provider,
                            label=label, torrent_id=torrent_id, file_id=file_id, filename=name)
        action = 'play_cloud_file' if is_video(name) else 'file_info'
        matched = file_metadata.get(name, {})
        add_item(label, action, False, context=ctx, art=matched.get('art'), info=matched.get('info'), provider=provider,
                 torrent_id=torrent_id, file_id=file_id, filename=name,
                 message=('Archive file: use Download from the context menu.' if is_archive(name)
                          else 'Use the context menu to download this non-video file.'))
    end('files')


def play_cloud_file(provider, torrent_id, file_id, filename=''):
    try:
        client = _native_cloud_client(provider)
        direct, target = client.resolve_cloud_file(torrent_id, file_id)
        _set_resolved(direct, target.get('path') or target.get('name') or filename)
    except Exception as exc:
        _set_failed('Provider file playback failed:\n%s' % exc)


def download_cloud_file(provider, torrent_id, file_id):
    try:
        client = _native_cloud_client(provider)
        direct, target = client.resolve_cloud_file(torrent_id, file_id)
        name = os.path.basename(clean_path(target.get('path') or target.get('name') or 'download'))
        download_url(direct, {'title': name}, name)
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Provider download failed:\n%s' % exc)


def delete_cloud_torrent(provider, torrent_id):
    if not xbmcgui.Dialog().yesno(ADDON_NAME, 'Are you sure you want to remove this?'):
        return
    try:
        _native_cloud_client(provider).delete_torrent(torrent_id)
        xbmc.executebuiltin('Container.Refresh')
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not delete torrent:\n%s' % exc)


def pm_root():
    pm = Premiumize()
    if not (pm.enabled and pm.authorized):
        add_item('Configure Premiumize.me in ResolveURL', 'open_resolveurl_settings', False,
                 playable=False, menu_icon='settings')
    else:
        add_item('Cloud Files', 'pm_folder', True, menu_icon='cloud_files', folder_id='')
        add_item('Transfers', 'pm_transfers', True, menu_icon='transfers')
    end_menu()


def pm_folder(folder_id='', root_name='', parent_name='', meta_type='', meta_title='', meta_imdb='', meta_year='', meta_season=''):
    pm = Premiumize()
    try:
        data = pm.list_folder(folder_id)
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not load Premiumize.me cloud files:\n%s' % exc)
        return
    content = list(data.get('content') or [])
    if not folder_id:
        update_account_item_count('pm', 'cloud', len(content))
    series_hint = looks_like_series_tree(content)
    known_context = _metadata_context(meta_type, meta_title, meta_imdb, meta_year, meta_season)
    folder_names = [item.get('name') or '' for item in content if item.get('type') == 'folder']
    folder_metadata = match_entries(folder_names, parent_name=parent_name or root_name,
                                    root_name=root_name, container=True,
                                    series_hint=series_hint, provider='pm',
                                    known_context=known_context)
    file_metadata = match_entries([item.get('name') or '' for item in content
                                   if item.get('type') != 'folder' and is_video(item.get('name') or '')],
                                  parent_name=parent_name or root_name,
                                  root_name=root_name, series_hint=series_hint,
                                  provider='pm', known_context=known_context)
    sort_key = lambda item: media_sort_key(item.get('name') or '', parent_name=parent_name or root_name,
                                            root_name=root_name, known_context=known_context)
    for item in sorted(content, key=sort_key):
        item_id = item.get('id')
        name = item.get('name') or str(item_id)
        if item.get('type') == 'folder':
            matched = folder_metadata.get(name, {})
            child_context = _child_metadata_context(matched, known_context)
            ctx = [('Delete folder', 'RunPlugin(%s)' % plugin_url(
                BASE, action='delete_pm_item', entry_type='folder', item_id=item_id))]
            ctx += _pin_context('pin_account', kind='pm_folder', provider='pm', label=name + '/', folder_id=item_id)
            kwargs = _metadata_context_kwargs(child_context)
            add_item(name + '/', 'pm_folder', True, context=ctx,
                     art=matched.get('art'), info=matched.get('info'),
                     folder_id=item_id, root_name=(root_name or name), parent_name=name, **kwargs)
        else:
            label = name + ('  %s' % human_size(item.get('size')) if item.get('size') else '')
            ctx = [('Download', 'RunPlugin(%s)' % plugin_url(BASE, action='download_pm_file', file_id=item_id)),
                   ('Delete file', 'RunPlugin(%s)' % plugin_url(BASE, action='delete_pm_item', entry_type='file', item_id=item_id))]
            ctx += _pin_context('pin_account', kind='pm_file', provider='pm', label=label,
                                file_id=item_id, filename=name)
            action = 'play_pm_file' if is_video(name) else 'file_info'
            matched = file_metadata.get(name, {})
            add_item(label, action, False, context=ctx, art=matched.get('art'), info=matched.get('info'),
                     file_id=item_id, filename=name,
                     message=('Archive file: use Download from the context menu.' if is_archive(name)
                              else 'Use the context menu to download this non-video file.'))
    end('files')


def pm_transfers():
    pm = Premiumize()
    try:
        rows = pm.list_transfers()
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not load Premiumize.me transfers:\n%s' % exc)
        return
    update_account_item_count('pm', 'transfers', len(rows))
    metadata = match_entries([item.get('name') or '' for item in rows if item.get('file_id') and is_video(item.get('name') or '')],
                             provider='pm')
    for item in rows:
        transfer_id = item.get('id')
        matched = metadata.get(item.get('name') or '', {})
        label = item.get('name') or str(transfer_id)
        status = item.get('status') or ''
        if status:
            label += '  [%s]' % status
        if item.get('progress') not in (None, '') and status not in ('finished', 'seeding'):
            try:
                label += '  %d%%' % int(float(item.get('progress')) * 100)
            except Exception:
                pass
        ctx = [('Delete transfer', 'RunPlugin(%s)' % plugin_url(
            BASE, action='delete_pm_transfer', transfer_id=transfer_id))]
        ctx += _pin_context('pin_account', kind='pm_transfer', provider='pm', label=label,
                            item_id=transfer_id, folder_id=item.get('folder_id'), file_id=item.get('file_id'))
        if item.get('folder_id'):
            kwargs = _metadata_context_kwargs(matched.get('context'))
            add_item(label, 'pm_folder', True, context=ctx, folder_id=item.get('folder_id'),
                     root_name=item.get('name') or '', parent_name=item.get('name') or '', **kwargs)
        elif item.get('file_id'):
            add_item(label, 'play_pm_file', False, context=ctx, art=matched.get('art'), info=matched.get('info'),
                     file_id=item.get('file_id'), filename=item.get('name') or '')
        else:
            add_item(label, 'file_info', False, context=ctx, playable=False,
                     message=item.get('message') or 'Transfer is not yet available as a cloud file.')
    end('files')


def play_pm_file(file_id, filename=''):
    try:
        direct, target = Premiumize().resolve_cloud_file(file_id)
        _set_resolved(direct, target.get('name') or filename)
    except Exception as exc:
        _set_failed('Premiumize.me file playback failed:\n%s' % exc)


def download_pm_file(file_id):
    try:
        direct, target = Premiumize().resolve_cloud_file(file_id)
        name = target.get('name') or 'Premiumize file'
        download_url(direct, {'title': name}, name)
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Premiumize.me download failed:\n%s' % exc)


def delete_pm_item(entry_type, item_id):
    if not xbmcgui.Dialog().yesno(ADDON_NAME, 'Are you sure you want to remove this?'):
        return
    try:
        pm = Premiumize()
        if entry_type == 'folder':
            pm.delete_folder(item_id)
        else:
            pm.delete_file(item_id)
        xbmc.executebuiltin('Container.Refresh')
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not delete Premiumize.me item:\n%s' % exc)


def delete_pm_transfer(transfer_id):
    if not xbmcgui.Dialog().yesno(ADDON_NAME, 'Are you sure you want to remove this?'):
        return
    try:
        Premiumize().delete_transfer(transfer_id)
        xbmc.executebuiltin('Container.Refresh')
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not delete Premiumize.me transfer:\n%s' % exc)

def authorize_rd():
    open_resolveurl_settings()

def authorize_tb():
    open_resolveurl_settings()

def revoke_rd():
    open_resolveurl_settings()

def revoke_tb():
    open_resolveurl_settings()

def delete_rd_torrent(torrent_id):
    if not xbmcgui.Dialog().yesno(ADDON_NAME, 'Are you sure you want to remove this?'):
        return
    try:
        RealDebrid().delete_torrent(torrent_id)
        notify('Torrent removed from Real-Debrid.')
        xbmc.executebuiltin('Container.Refresh')
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not remove Real-Debrid torrent:\n%s' % exc)


def delete_tb_torrent(torrent_id):
    if not xbmcgui.Dialog().yesno(ADDON_NAME, 'Are you sure you want to remove this?'):
        return
    try:
        TorBox().delete_torrent(torrent_id)
        notify('Torrent removed from TorBox.')
        xbmc.executebuiltin('Container.Refresh')
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not remove TorBox torrent:\n%s' % exc)


def open_resolveurl_settings():
    try:
        ResolveURLBridge().display_settings()
    except Exception as exc:
        xbmcgui.Dialog().ok(ADDON_NAME, 'Could not open ResolveURL settings:\n%s' % exc)


def clear_metadata_cache():
    clear_account_metadata_cache()
    notify('Account metadata cache cleared.')


def resolveurl_provider_info(provider='ResolveURL provider'):
    xbmcgui.Dialog().ok(
        ADDON_NAME,
        '%s is authorized through ResolveURL and is available for Premium Player source playback.\n\n'
        'ResolveURL does not provide a generic cloud-file browser API for every service, so account browsing is shown only for providers Premium Player can browse reliably.' % provider
    )


def file_info(message='This file is not directly playable.'):
    xbmcgui.Dialog().ok(ADDON_NAME, message)


def _set_resolved(url, filename=''):
    item = xbmcgui.ListItem(label=filename or 'Premium Player')
    item.setPath(url)
    item.setProperty('IsPlayable', 'true')
    if HANDLE >= 0:
        xbmcplugin.setResolvedUrl(HANDLE, True, item)
    else:
        xbmc.Player().play(url, item)


def _set_failed(message):
    xbmcgui.Dialog().ok(ADDON_NAME, message)
    if HANDLE >= 0:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


def run():
    # Cache migrations are performed before routing so an addon upgrade actively
    # deletes every stale New Releases result file before any route can read it.
    ensure_new_releases_cache_schema()
    p = params()
    action = p.get('action', 'root')
    if not ensure_terms_agreed():
        return
    try:
        if action == 'root':
            root()
        elif action == 'browse_accounts':
            browse_accounts()
        elif action == 'pinned_root':
            pinned_root()
        elif action == 'pinned_category':
            pinned_category(p.get('category', 'movies'))
        elif action == 'downloaded_files':
            downloaded_files()
        elif action == 'play_local_file':
            play_local_file(p.get('path', ''), p.get('label', ''))
        elif action == 'remove_downloaded':
            remove_downloaded(p.get('path', ''))
        elif action == 'pin_meta':
            pin_meta(p.get('kind'), p.get('imdb_id'), p.get('title',''), p.get('season'), p.get('episode'), p.get('episode_title'))
        elif action == 'pin_source':
            pin_source(p.get('session'), p.get('index'))
        elif action == 'pin_account':
            pin_account(p.get('kind'), p.get('provider'), p.get('label',''), item_type=p.get('item_type'),
                        item_id=p.get('item_id'), torrent_id=p.get('torrent_id'), file_id=p.get('file_id'),
                        download_id=p.get('download_id'), prefix=p.get('prefix'), filename=p.get('filename'),
                        folder_id=p.get('folder_id'), entry_type=p.get('entry_type'))
        elif action == 'unpin_item':
            unpin_item(p.get('pin_id'))
        elif action == 'play_pinned_source':
            play_pinned_source(p.get('pin_id'))
        elif action == 'play_pinned_rd_download':
            play_pinned_rd_download(p.get('download_id'))
        elif action == 'search_root':
            search_root()
        elif action == 'new_releases_root':
            new_releases_root(p.get('main_entry') == '1')
        elif action == 'new_release_genres':
            new_release_genres(p.get('media_type', 'movie'))
        elif action == 'new_releases':
            new_releases(p.get('media_type', 'movie'), p.get('page', '1'), p.get('genre', ''))
        elif action == 'new_release_tv_target':
            _new_release_tv_target(p.get('imdb_id', ''), p.get('title', 'TV Show'),
                                   p.get('season'), p.get('episode'), p.get('episode_title', ''))
        elif action == 'search_prompt':
            search_prompt(p.get('media_type', 'movie'))
        elif action == 'search_results':
            search_results(p.get('media_type', 'movie'), p.get('query', ''))
        elif action == 'episode_search_results':
            episode_search_results(p.get('title', ''), p.get('season'), p.get('episode'),
                                   p.get('episode_title', ''))
        elif action == 'remove_search':
            remove_search(p.get('media_type', 'movie'), p.get('query', ''))
        elif action == 'seasons':
            seasons(p['imdb_id'], p.get('title', 'TV Show'))
        elif action == 'episodes':
            episodes(p['imdb_id'], p.get('title', 'TV Show'), p['season'], p.get('focus_episode'))
        elif action == 'play_all_tv':
            play_all_tv(p['imdb_id'], p.get('title', 'TV Show'), p.get('start_season'), p.get('start_episode'))
        elif action == 'sources':
            sources(p.get('media_type', 'movie'), p['imdb_id'], p.get('title', ''), p.get('season'), p.get('episode'), p.get('episode_title'))
        elif action == 'source_results':
            source_results(p['session'], p.get('focus'))
        elif action == 'source_results_current':
            source_results_current()
        elif action == 'play_source':
            play_source(p['session'], p['index'])
        elif action == 'download_source':
            download_source(p['session'], p['index'])
        elif action == 'rd_root':
            rd_root()
        elif action == 'rd_torrents':
            rd_torrents()
        elif action == 'browse_rd_torrent':
            browse_rd_torrent(p['torrent_id'], p.get('prefix', ''),
                              p.get('meta_type', ''), p.get('meta_title', ''), p.get('meta_imdb', ''),
                              p.get('meta_year', ''), p.get('meta_season', ''))
        elif action == 'play_rd_file':
            play_rd_file(p['torrent_id'], p['file_id'], p.get('filename', ''))
        elif action == 'download_rd_file':
            download_rd_file(p['torrent_id'], p['file_id'])
        elif action == 'rd_downloads':
            rd_downloads()
        elif action == 'play_rd_download':
            play_rd_download(p['session'], p['index'])
        elif action == 'download_rd_download':
            download_rd_download(p['session'], p['index'])
        elif action == 'tb_root':
            tb_root()
        elif action == 'tb_items':
            tb_items(p['item_type'])
        elif action == 'browse_tb_item':
            browse_tb_item(p['item_type'], p['item_id'], p.get('prefix', ''),
                           p.get('meta_type', ''), p.get('meta_title', ''), p.get('meta_imdb', ''),
                           p.get('meta_year', ''), p.get('meta_season', ''))
        elif action == 'play_tb_file':
            play_tb_file(p['item_type'], p['item_id'], p['file_id'], p.get('filename', ''))
        elif action == 'download_tb_file':
            download_tb_file(p['item_type'], p['item_id'], p['file_id'])
        elif action == 'delete_rd_torrent':
            delete_rd_torrent(p['torrent_id'])
        elif action == 'delete_tb_torrent':
            delete_tb_torrent(p['torrent_id'])
        elif action == 'cloud_root':
            cloud_root(p['provider'])
        elif action == 'cloud_torrents':
            cloud_torrents(p['provider'])
        elif action == 'browse_cloud_torrent':
            browse_cloud_torrent(p['provider'], p['torrent_id'], p.get('prefix', ''),
                                 p.get('meta_type', ''), p.get('meta_title', ''), p.get('meta_imdb', ''),
                                 p.get('meta_year', ''), p.get('meta_season', ''))
        elif action == 'play_cloud_file':
            play_cloud_file(p['provider'], p['torrent_id'], p['file_id'], p.get('filename', ''))
        elif action == 'download_cloud_file':
            download_cloud_file(p['provider'], p['torrent_id'], p['file_id'])
        elif action == 'delete_cloud_torrent':
            delete_cloud_torrent(p['provider'], p['torrent_id'])
        elif action == 'pm_root':
            pm_root()
        elif action == 'pm_folder':
            pm_folder(p.get('folder_id', ''), p.get('root_name', ''), p.get('parent_name', ''),
                      p.get('meta_type', ''), p.get('meta_title', ''), p.get('meta_imdb', ''),
                      p.get('meta_year', ''), p.get('meta_season', ''))
        elif action == 'pm_transfers':
            pm_transfers()
        elif action == 'play_pm_file':
            play_pm_file(p['file_id'], p.get('filename', ''))
        elif action == 'download_pm_file':
            download_pm_file(p['file_id'])
        elif action == 'delete_pm_item':
            delete_pm_item(p.get('entry_type', 'file'), p['item_id'])
        elif action == 'delete_pm_transfer':
            delete_pm_transfer(p['transfer_id'])
        elif action == 'open_resolveurl_settings':
            open_resolveurl_settings()
        elif action == 'resolveurl_provider_info':
            resolveurl_provider_info(p.get('provider', 'ResolveURL provider'))
        elif action == 'authorize_rd':
            authorize_rd()
        elif action == 'authorize_tb':
            authorize_tb()
        elif action == 'revoke_rd':
            revoke_rd()
        elif action == 'revoke_tb':
            revoke_tb()
        elif action == 'file_info':
            file_info(p.get('message', 'This file is not directly playable.'))
        elif action == 'open_settings':
            ADDON.openSettings()
        elif action == 'clear_metadata_cache':
            clear_metadata_cache()
        elif action == 'reset_defaults':
            reset_defaults()
        else:
            root()
    except Exception as exc:
        log('Unhandled error in action %s: %s' % (action, exc), xbmc.LOGERROR)
        xbmcgui.Dialog().ok(ADDON_NAME, 'Unexpected error:\n%s' % exc)
        if HANDLE >= 0 and action.startswith('play_'):
            try:
                xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            except Exception:
                pass
