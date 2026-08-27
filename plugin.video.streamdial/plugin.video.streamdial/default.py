"""StreamDial TV Kodi plug-in entry point."""

import os
import random
import re
import sys
import time
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs


ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
ADDON_NAME = ADDON.getAddonInfo("name") or "StreamDial TV"
ADDON_PATH = ADDON.getAddonInfo("path")
PROFILE_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
RESOURCES_PATH = os.path.join(ADDON_PATH, "resources")
MEDIA_PATH = os.path.join(RESOURCES_PATH, "media")
FANART = os.path.join(ADDON_PATH, "fanart.jpg")
DEFAULT_ICON = os.path.join(ADDON_PATH, "icon.png")
if RESOURCES_PATH not in sys.path:
    sys.path.insert(0, RESOURCES_PATH)

from lib.models import (  # noqa: E402
    ALL_GENRES, ALL_LANGUAGES, ALL_PROVIDERS, current_program, event_status,
    local_time, schedule_plot, dedupe_across_providers, GENRE_ORDER,
)
from lib.diagnostics import (  # noqa: E402
    log_failure, log_playback_attempt, probe_manifest,
)
from lib.playback import apply_to_listitem  # noqa: E402
from lib.widevine import ensure as ensure_widevine, required as widevine_required  # noqa: E402
from lib.registry import Registry  # noqa: E402
from lib.storage import Storage  # noqa: E402
from lib.viewcache import ChannelViewCache  # noqa: E402


HANDLE = int(sys.argv[1]) if len(sys.argv) > 1 else -1
BASE_URL = sys.argv[0] if sys.argv else "plugin://plugin.video.streamdial/"
PARAMS = {
    key: values[0] for key, values in urllib.parse.parse_qs(
        sys.argv[2][1:] if len(sys.argv) > 2 and sys.argv[2].startswith("?") else "",
        keep_blank_values=True,
    ).items()
}


def setting_bool(key, default=True):
    value = ADDON.getSetting(key)
    if value == "":
        return default
    return str(value).casefold() not in ("false", "0", "no", "off")


def setting_int(key, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(ADDON.getSetting(key) or default)))
    except (TypeError, ValueError):
        return default


DEFAULT_LANGUAGES = (
    "English", "Spanish", "French", "German", "Italian", "Portuguese",
    "Dutch", "Polish", "Russian", "Turkish", "Arabic", "Hindi",
    "Chinese", "Japanese", "Korean", "Filipino",
)


def default_language():
    value = ADDON.getSetting("default_language") or "English"
    return value if value in DEFAULT_LANGUAGES else "English"


def _dedupe_enabled():
    return setting_bool("dedupe_channels", False)


def _maybe_dedupe(rows, multi_provider=False):
    rows = list(rows or [])
    if not multi_provider or not _dedupe_enabled():
        return rows
    before = len(rows)
    output = dedupe_across_providers(rows)
    _diagnostic_log("cross_provider_dedupe before={} after={} removed={}".format(
        before, len(output), before - len(output)))
    return output


def _scope_count(rows, provider):
    """Count rows exactly as the corresponding channel screen will render them."""
    values = list(rows or [])
    if provider == ALL_PROVIDERS and _dedupe_enabled():
        values = dedupe_across_providers(values)
    return len(values)


def _language_values_counts(provider):
    """Build language menu values/counts from one catalog snapshot.

    Counts are post-de-duplication only for the cross-provider scope. Single
    providers are never de-duplicated, matching the channel-list behavior.
    """
    rows, failures = REGISTRY.catalogs(provider)
    languages = sorted(
        {language for row in rows for language in (row.get("languages") or [])},
        key=str.casefold,
    )
    counts = {ALL_LANGUAGES: _scope_count(rows, provider)}
    for language in languages:
        subset = [row for row in rows if language in (row.get("languages") or [])]
        counts[language] = _scope_count(subset, provider)
    return languages, counts, failures


def _genre_values_counts(provider, language):
    """Build genre menu values/counts from one language-filtered snapshot."""
    rows, failures = REGISTRY.filter(provider, language, ALL_GENRES)
    genres = {genre for row in rows for genre in (row.get("genres") or [])}
    ordered = [genre for genre in GENRE_ORDER if genre in genres]
    ordered.extend(sorted(genres.difference(ordered), key=str.casefold))
    counts = {ALL_GENRES: _scope_count(rows, provider)}
    for genre in ordered:
        subset = [row for row in rows if genre in (row.get("genres") or [])]
        counts[genre] = _scope_count(subset, provider)
    return ordered, counts, failures


ENABLED = [
    key for key in (
        "pluto", "plex", "samsung", "pbs", "stirr", "roku", "lg", "xumo",
        "tubi", "tcl", "distro", "whale", "freelivesports", "twitch",
    )
    if setting_bool("enable_" + key)
]
DEBUG_DIAGNOSTICS = setting_bool("debug_diagnostics", False)
REGISTRY = Registry(ADDON_PATH, PROFILE_PATH, ENABLED, diagnostics=DEBUG_DIAGNOSTICS)
STORAGE = Storage(PROFILE_PATH)
CHANNEL_VIEW_CACHE = ChannelViewCache(PROFILE_PATH)


def plugin_url(**params):
    clean = {key: value for key, value in params.items() if value is not None}
    return BASE_URL + "?" + urllib.parse.urlencode(clean)


def media(*parts):
    return os.path.join(MEDIA_PATH, *parts)


def slug(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_") or "other"


def provider_icon(key):
    path = media("providers", slug(key) + ".png")
    return path if xbmcvfs.exists(path) else media("menu", "providers.png")


def genre_icon(name):
    path = media("genres", slug(name) + ".png")
    return path if xbmcvfs.exists(path) else media("genres", "other.png")


def language_icon(name):
    path = media("flags", slug(name) + ".png")
    return path if xbmcvfs.exists(path) else media("flags", "all_languages.png")


def set_art(item, icon, fanart=""):
    icon = icon or DEFAULT_ICON
    item.setArt({"icon": icon, "thumb": icon, "fanart": fanart or FANART})


def set_video_info(item, title, plot, genres=None, studio=""):
    info = {
        "title": title,
        "plot": plot,
        "mediatype": "video",
        "genre": " / ".join(genres or []),
        "studio": studio,
    }
    item.setInfo("video", info)


def end_directory(content=None, succeeded=True, cache=False):
    if content:
        xbmcplugin.setContent(HANDLE, content)
    xbmcplugin.endOfDirectory(HANDLE, succeeded=succeeded, cacheToDisc=cache)


def add_folder(label, action, icon, params=None, context=None, plot=""):
    item = xbmcgui.ListItem(label=label, offscreen=True)
    set_art(item, icon)
    if plot:
        set_video_info(item, label, plot)
    if context:
        item.addContextMenuItems(context)
    target = {"action": action}
    target.update(params or {})
    xbmcplugin.addDirectoryItem(HANDLE, plugin_url(**target), item, isFolder=True)


def add_action(label, action, icon, params=None, context=None):
    item = xbmcgui.ListItem(label=label, offscreen=True)
    set_art(item, icon)
    if action == "play":
        # Kodi only enters the normal plugin playback/resolve pipeline when the
        # directory item is explicitly marked playable. Without this, pinned
        # channels can be invoked as a bare plugin script (HANDLE == -1), making
        # setResolvedUrl() a no-op even though the provider resolves correctly.
        item.setProperty("IsPlayable", "true")
        set_video_info(item, label, "Pinned live channel")
    if context:
        item.addContextMenuItems(context)
    target = {"action": action}
    target.update(params or {})
    xbmcplugin.addDirectoryItem(HANDLE, plugin_url(**target), item, isFolder=False)


def replace_container(url):
    xbmcplugin.endOfDirectory(HANDLE, succeeded=True, updateListing=False, cacheToDisc=False)
    try:
        xbmc.sleep(50)
    except (AttributeError, TypeError):
        pass
    xbmc.executebuiltin("Container.Update({},replace)".format(url))


_FAILURE_TOAST_PROPERTY = "StreamDial.LastPlaybackFailureToast"
_FAILURE_TOAST_COOLDOWN = 15.0
_RETURN_VIEW_PROPERTY = "StreamDial.ReturnChannelViewId"
_CHANNEL_VIEW_MAX_AGE = 30 * 60


def _home_window():
    try:
        return xbmcgui.Window(10000)
    except Exception:
        return None


def _clear_return_view():
    home = _home_window()
    if home is not None:
        try:
            home.clearProperty(_RETURN_VIEW_PROPERTY)
        except Exception:
            try:
                home.setProperty(_RETURN_VIEW_PROPERTY, "")
            except Exception:
                pass


def _mark_return_view(view_id):
    """Mark a successfully resolved list item as eligible for fast return reloads."""
    view_id = str(view_id or "")
    if not view_id:
        return
    home = _home_window()
    if home is not None:
        try:
            home.setProperty(_RETURN_VIEW_PROPERTY, view_id)
        except Exception:
            pass


def _take_return_view(screen_key):
    """Load the most recent playback-return screen while it remains valid.

    Kodi can request the same directory more than once while unwinding from
    VideoPlayer.  Keep the marker on a successful hit so those repeated reloads
    all reuse the exact final channel list.  Explicit navigation clears the
    marker in dispatch(); a mismatch/expiry clears it here.
    """
    home = _home_window()
    if home is None:
        return None
    try:
        view_id = str(home.getProperty(_RETURN_VIEW_PROPERTY) or "")
    except Exception:
        view_id = ""
    if not view_id:
        return None
    snapshot = CHANNEL_VIEW_CACHE.load(screen_key, view_id, _CHANNEL_VIEW_MAX_AGE)
    if snapshot:
        _diagnostic_log(
            "channel_view_cache hit screen={!r} view_id={} age={:.1f}s rows={}".format(
                screen_key, view_id[:10], max(0.0, time.time() - float(snapshot.get("built_at") or 0)),
                len(snapshot.get("rows") or []),
            )
        )
        return snapshot
    _clear_return_view()
    _diagnostic_log("channel_view_cache miss/expired screen={!r} view_id={}".format(screen_key, view_id[:10]))
    return None


def _store_channel_view(screen_key, rows, show_provider=False):
    # A newly generated final list supersedes any playback-return marker from a
    # previous list.  The marker is set only after one of this list's channels
    # resolves successfully.
    _clear_return_view()
    snapshot = CHANNEL_VIEW_CACHE.store(screen_key, rows, show_provider=show_provider)
    _diagnostic_log(
        "channel_view_cache stored screen={!r} view_id={} rows={}".format(
            screen_key, str(snapshot.get("view_id") or "")[:10], len(rows or []))
    )
    return snapshot


def _diagnostic_log(message, level=None):
    if not DEBUG_DIAGNOSTICS:
        return
    try:
        xbmc.log(
            "[StreamDial Diagnostics] {}".format(str(message or "")),
            level if level is not None else getattr(xbmc, "LOGDEBUG", 0),
        )
    except Exception:
        pass


def _reserve_playback_failure_toast():
    """Rate-limit only StreamDial's own failure toast, never Kodi/global notifications."""
    now = time.time()
    try:
        home = xbmcgui.Window(10000)
        raw = home.getProperty(_FAILURE_TOAST_PROPERTY) or "0"
        try:
            previous = float(raw)
        except (TypeError, ValueError):
            previous = 0.0
        if previous > 0 and now - previous < _FAILURE_TOAST_COOLDOWN:
            _diagnostic_log(
                "failure_toast suppressed cooldown_remaining={:.1f}s".format(
                    max(0.0, _FAILURE_TOAST_COOLDOWN - (now - previous))
                )
            )
            return False
        # Reserve before submission so overlapping short-lived plugin invocations
        # cannot enqueue multiple StreamDial toasts into Kodi's shared queue.
        home.setProperty(_FAILURE_TOAST_PROPERTY, str(now))
    except Exception as error:
        # Notification delivery must not depend on Home-window property support.
        _diagnostic_log(
            "failure_toast cooldown state unavailable: {}: {}".format(
                type(error).__name__, error
            )
        )
    return True


def _settle_notification(milliseconds=350):
    """Give Kodi's GUI thread time to commit a toast before plugin teardown."""
    try:
        xbmc.sleep(int(milliseconds))
    except Exception:
        pass


def notify(message, warning=False, playback_failure=False):
    """Submit one Kodi toast without consulting/suppressing the global notification window."""
    if playback_failure and not _reserve_playback_failure_toast():
        return False

    text = str(message or "")
    icon = xbmcgui.NOTIFICATION_WARNING if warning else xbmcgui.NOTIFICATION_INFO
    submitted = False
    try:
        _diagnostic_log("notification submit playback_failure={} text={!r}".format(
            "yes" if playback_failure else "no", text[:120]))
        xbmcgui.Dialog().notification(ADDON_NAME, text, icon, 3500)
        submitted = True
        _diagnostic_log("notification Dialog().notification returned")
    except Exception as error:
        _diagnostic_log(
            "notification Dialog().notification failed: {}: {}".format(
                type(error).__name__, error
            ),
            getattr(xbmc, "LOGWARNING", 2),
        )

    if not submitted:
        # Keep the fallback deliberately simple and comma-safe; do not submit a
        # second notification when Dialog().notification already accepted it.
        clean_title = str(ADDON_NAME).replace(",", " -").replace("\n", " ")
        clean_text = text.replace(",", " -").replace("\n", " ")
        fallback_icon = "warning" if warning else "info"
        try:
            xbmc.executebuiltin(
                "Notification({},{},{},{})".format(
                    clean_title, clean_text, 3500, fallback_icon
                )
            )
            submitted = True
            _diagnostic_log("notification builtin fallback submitted")
        except Exception as error:
            _diagnostic_log(
                "notification builtin fallback failed: {}: {}".format(
                    type(error).__name__, error
                ),
                getattr(xbmc, "LOGWARNING", 2),
            )
    return submitted


def _pin(kind, label, provider=ALL_PROVIDERS, language=ALL_LANGUAGES,
         genre=ALL_GENRES, channel_id=""):
    return {
        "kind": kind,
        "label": str(label),
        "provider": provider,
        "language": language,
        "genre": genre,
        "channel_id": str(channel_id or ""),
    }


def _pin_params(pin):
    return {
        "p_kind": pin.get("kind", ""),
        "p_label": pin.get("label", ""),
        "p_provider": pin.get("provider", ALL_PROVIDERS),
        "p_language": pin.get("language", ALL_LANGUAGES),
        "p_genre": pin.get("genre", ALL_GENRES),
        "p_channel_id": pin.get("channel_id", ""),
    }


def _pin_from_params():
    return _pin(
        PARAMS.get("p_kind", ""), PARAMS.get("p_label", "Pinned item"),
        PARAMS.get("p_provider", ALL_PROVIDERS),
        PARAMS.get("p_language", ALL_LANGUAGES),
        PARAMS.get("p_genre", ALL_GENRES), PARAMS.get("p_channel_id", ""),
    )


def _pin_context(pin, force_unpin=False):
    params = {"action": "toggle_pin"}
    params.update(_pin_params(pin))
    is_pinned = STORAGE.is_pinned(pin)
    label = "Unpin" if force_unpin or is_pinned else "Pin"
    return [(label, "RunPlugin({})".format(plugin_url(**params)))]


def _random_context(provider=ALL_PROVIDERS, language=ALL_LANGUAGES,
                    genre=ALL_GENRES, pin_scope=""):
    url = plugin_url(
        action="play_random", provider=provider, language=language,
        genre=genre, pin_scope=pin_scope,
    )
    return [("Play Random", "PlayMedia({})".format(url))]


def root():
    add_folder(
        "Live TV Search", "search_menu", media("menu", "search.png"),
        plot="Search channel names, metadata, and available live schedules across every enabled provider.")
    add_folder(
        "Browse Providers", "providers", media("menu", "providers.png"),
        plot="Browse all providers together or open one provider at a time.")
    add_folder(
        "Browse by Genre", "genres", media("menu", "genres.png"),
        params={"provider": ALL_PROVIDERS, "language": ALL_LANGUAGES},
        plot="Browse the complete shared channel catalog from all enabled providers by genre.")
    add_folder(
        "Pinned", "pinned", media("menu", "pinned.png"),
        context=_random_context(pin_scope="all"),
        plot="Quick access to pinned providers, languages, genres, and channels.")
    end_directory(cache=False)


def search_menu():
    add_folder("New Search", "new_search", media("menu", "new_search.png"))
    for query in STORAGE.history():
        remove_url = plugin_url(action="delete_search", query=query)
        context = [("Delete saved search", "RunPlugin({})".format(remove_url))]
        add_folder(query, "search_results", media("menu", "history.png"),
                   params={"query": query}, context=context)
    end_directory(cache=False)


def new_search():
    keyboard = xbmc.Keyboard("", "Search free live TV")
    keyboard.doModal()
    if not keyboard.isConfirmed():
        replace_container(plugin_url(action="search_menu"))
        return
    query = keyboard.getText().strip()
    if not query:
        replace_container(plugin_url(action="search_menu"))
        return
    STORAGE.remember_search(query)
    replace_container(plugin_url(action="search_results", query=query, fresh="1"))


def delete_search():
    query = PARAMS.get("query", "")
    if STORAGE.forget_search(query):
        notify("Deleted saved search: {}".format(query))
    xbmc.executebuiltin("Container.Refresh")


def _search_progress():
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON_NAME, "Searching direct live-TV providers…")
    progress.update(0, "Starting provider search…")

    def update(completed, total, key, name, status, count):
        del key
        percent = int((completed * 100) / max(1, total))
        if status == "error":
            message = "Searched {}/{} · {} unavailable".format(completed, total, name)
        else:
            message = "Searched {}/{} · {} · {} match(es)".format(completed, total, name, count)
        progress.update(percent, message)
    return progress, update


def _provider_progress():
    progress = xbmcgui.DialogProgress()
    progress.create(ADDON_NAME, "Building shared live-TV catalog…")
    progress.update(0, "Starting provider catalog…")

    def update(completed, total, key, name, status, count):
        del key
        percent = int((completed * 100) / max(1, total))
        if status == "error":
            message = "{}: 0 channels ({}/{})".format(name, completed, total)
        else:
            message = "{}: {} channel(s) ({}/{})".format(name, count, completed, total)
        progress.update(percent, message)
    return progress, update


def _ensure_browse_snapshot():
    """Return the one timestamped browse snapshot, showing progress only on rebuild."""
    snapshot = REGISTRY.cached_browse_snapshot()
    if snapshot is not None:
        _diagnostic_log(
            "browse_catalog_cache hit age={:.1f}s rows={} failures={}".format(
                max(0.0, time.time() - float(snapshot.get("built_at") or 0)),
                len(snapshot.get("rows") or []),
                len(snapshot.get("failures") or []),
            )
        )
        return snapshot

    progress, update = _provider_progress()
    try:
        snapshot = REGISTRY.browse_snapshot(progress=update, force=True)
    finally:
        progress.close()
    _diagnostic_log(
        "browse_catalog_cache rebuilt rows={} failures={}".format(
            len(snapshot.get("rows") or []), len(snapshot.get("failures") or []))
    )
    return snapshot


def search_results():
    query = PARAMS.get("query", "").strip()
    if not query:
        replace_container(plugin_url(action="search_menu"))
        return
    screen_key = "search|{}".format(query.casefold())
    if PARAMS.get("fresh"):
        _clear_return_view()
        snapshot = None
    else:
        snapshot = _take_return_view(screen_key)
    if snapshot:
        render_channels(
            snapshot.get("rows") or [],
            show_provider=bool(snapshot.get("show_provider", True)),
            view_id=snapshot.get("view_id") or "",
        )
        return

    progress, update = _search_progress()
    try:
        rows, failures = REGISTRY.search(
            query, per_provider=setting_int("search_limit", 30, 5, 100), progress=update)
    finally:
        progress.close()
    rows = _maybe_dedupe(rows, multi_provider=True)
    snapshot = _store_channel_view(screen_key, rows, show_provider=True)
    render_channels(rows, show_provider=True, view_id=snapshot.get("view_id") or "")
    if not rows:
        notify("No live channels matched ‘{}’.".format(query))
    elif failures:
        notify("{} provider(s) were temporarily unavailable.".format(len(failures)), warning=True)


def providers():
    definitions = REGISTRY.definitions(browsable_only=True)
    snapshot = _ensure_browse_snapshot()
    catalog_rows = list(snapshot.get("rows") or [])

    # Every provider count and the All Providers aggregate comes from this exact
    # same timestamped raw catalog. A provider that failed the snapshot is shown
    # as zero; no stale count estimate or offline toast is substituted.
    counts = {row["key"]: 0 for row in definitions}
    for value in catalog_rows:
        key = str(value.get("provider") or "")
        if key in counts:
            counts[key] += 1
    total = _scope_count(catalog_rows, ALL_PROVIDERS)

    all_pin = _pin("provider", "All Providers", provider=ALL_PROVIDERS)
    add_folder(
        "All Providers ({:,})".format(total), "provider_entry", media("menu", "all_providers.png"),
        params={"provider": ALL_PROVIDERS}, context=_pin_context(all_pin))
    for row in definitions:
        pin = _pin("provider", row["name"], provider=row["key"])
        count = int(counts.get(row["key"], 0))
        add_folder(
            "{} ({:,})".format(row["name"], count), "provider_entry", provider_icon(row["key"]),
            params={"provider": row["key"]}, context=_pin_context(pin))
    end_directory(cache=False)


def provider_entry():
    _ensure_browse_snapshot()
    provider = PARAMS.get("provider", ALL_PROVIDERS)
    languages, language_counts, failures = _language_values_counts(provider)
    if provider == ALL_PROVIDERS or len(languages) > 1:
        _render_languages(provider, languages, failures, counts=language_counts)
        return
    if failures and not languages:
        _render_provider_unavailable(provider)
        return
    language = languages[0] if languages else ALL_LANGUAGES
    values, genre_counts, genre_failures = _genre_values_counts(provider, language)
    if genre_failures and not values:
        _render_provider_unavailable(provider)
        return
    if len(values) == 1:
        _render_channel_scope(provider, language, values[0], genre_failures)
        return
    _render_genres(provider, language, values, genre_failures, counts=genre_counts)


def _render_provider_unavailable(provider):
    name = REGISTRY.provider_name(provider)
    item = xbmcgui.ListItem(label="{} currently has 0 available channels".format(name), offscreen=True)
    set_art(item, provider_icon(provider))
    set_video_info(
        item, name,
        "This provider returned no channel catalog in the current shared browse snapshot. "
        "StreamDial will try it again automatically after the browse cache expires.",
    )
    xbmcplugin.addDirectoryItem(HANDLE, plugin_url(action="provider_entry", provider=provider), item, isFolder=False)
    end_directory(cache=False)


def _render_languages(provider, values, failures, counts=None):
    values = values or []
    if failures and not values:
        _render_provider_unavailable(provider)
        return
    counts = counts or {}
    provider_name = "All Providers" if provider == ALL_PROVIDERS else REGISTRY.provider_name(provider)
    all_pin = _pin("language", "All Languages · {}".format(provider_name), provider, ALL_LANGUAGES)
    context = _pin_context(all_pin) + _random_context(provider, ALL_LANGUAGES, ALL_GENRES)
    all_count = counts.get(ALL_LANGUAGES)
    all_label = "All Languages ({:,})".format(all_count) if all_count is not None else "All Languages"
    add_folder(
        all_label, "genres", media("flags", "all_languages.png"),
        params={"provider": provider, "language": ALL_LANGUAGES}, context=context)
    for language in values:
        pin = _pin("language", "{} · {}".format(language, provider_name), provider, language)
        context = _pin_context(pin) + _random_context(provider, language, ALL_GENRES)
        count = counts.get(language)
        label = "{} ({:,})".format(language, count) if count is not None else language
        add_folder(
            label, "genres", language_icon(language),
            params={"provider": provider, "language": language}, context=context)
    end_directory(cache=False)


def languages():
    _ensure_browse_snapshot()
    provider = PARAMS.get("provider", ALL_PROVIDERS)
    values, counts, failures = _language_values_counts(provider)
    _render_languages(provider, values, failures, counts=counts)


def _render_genres(provider, language, values, failures, counts=None):
    values = values or []
    counts = counts or {}
    if failures and not values:
        _render_provider_unavailable(provider)
        return
    scope = REGISTRY.provider_name(provider) if provider != ALL_PROVIDERS else "All Providers"
    language_label = language if language != ALL_LANGUAGES else "All Languages"
    all_pin = _pin("genre", "All Genres · {} · {}".format(language_label, scope), provider, language, ALL_GENRES)
    context = _pin_context(all_pin) + _random_context(provider, language, ALL_GENRES)
    all_count = counts.get(ALL_GENRES)
    all_label = "All Genres ({:,})".format(all_count) if all_count is not None else "All Genres"
    add_folder(
        all_label, "channels", media("genres", "all_genres.png"),
        params={"provider": provider, "language": language, "genre": ALL_GENRES},
        context=context)
    for genre in values:
        pin = _pin("genre", "{} · {} · {}".format(genre, language_label, scope), provider, language, genre)
        context = _pin_context(pin) + _random_context(provider, language, genre)
        count = counts.get(genre)
        label = "{} ({:,})".format(genre, count) if count is not None else genre
        add_folder(
            label, "channels", genre_icon(genre),
            params={"provider": provider, "language": language, "genre": genre},
            context=context)
    end_directory(cache=False)


def genres():
    _ensure_browse_snapshot()
    provider = PARAMS.get("provider", ALL_PROVIDERS)
    language = PARAMS.get("language", ALL_LANGUAGES)
    values, counts, failures = _genre_values_counts(provider, language)
    if failures and not values:
        _render_provider_unavailable(provider)
        return
    if len(values) == 1:
        _render_channel_scope(provider, language, values[0], failures)
        return
    _render_genres(provider, language, values, failures, counts=counts)


def _channel_screen_key(provider, language, genre):
    return "channels|{}|{}|{}".format(
        str(provider or ALL_PROVIDERS), str(language or ALL_LANGUAGES), str(genre or ALL_GENRES))


def _render_channel_scope(provider, language, genre, known_failures=None):
    _ensure_browse_snapshot()
    screen_key = _channel_screen_key(provider, language, genre)
    snapshot = _take_return_view(screen_key)
    if snapshot:
        render_channels(
            snapshot.get("rows") or [],
            show_provider=bool(snapshot.get("show_provider", provider == ALL_PROVIDERS)),
            view_id=snapshot.get("view_id") or "",
        )
        return

    rows, failures = REGISTRY.filter(provider, language, genre)
    rows = _maybe_dedupe(rows, multi_provider=(provider == ALL_PROVIDERS))
    rows = REGISTRY.enrich(rows, limit=36)
    snapshot = _store_channel_view(screen_key, rows, show_provider=provider == ALL_PROVIDERS)
    render_channels(
        rows, show_provider=provider == ALL_PROVIDERS,
        view_id=snapshot.get("view_id") or "")
    failures = list(dict.fromkeys(list(known_failures or []) + list(failures or [])))
    if not rows and provider != ALL_PROVIDERS:
        # Provider entries already advertise zero from this same snapshot; do
        # not add a second offline toast when the user opens that zero-count row.
        return
    if not rows:
        notify("No channels are available in this selection.")


def channels():
    provider = PARAMS.get("provider", ALL_PROVIDERS)
    language = PARAMS.get("language", ALL_LANGUAGES)
    genre = PARAMS.get("genre", ALL_GENRES)
    _render_channel_scope(provider, language, genre)


def _channel_pin(row):
    return _pin(
        "channel", "{} · {}".format(row.get("name"), row.get("provider_name")),
        row.get("provider", ""), ALL_LANGUAGES, ALL_GENRES, row.get("id", ""))


def render_channels(rows, show_provider=False, view_id=""):
    hours = setting_int("schedule_hours", 8, 2, 24)
    for row in rows:
        current = current_program(row.get("schedule") or [])
        label = row.get("name") or "Live channel"
        if show_provider:
            label += "  [COLOR grey][{}][/COLOR]".format(row.get("provider_name", "Live TV"))
        if current:
            status = "NOW" if event_status(current) == "now" else local_time(current.get("start"))
            label += "  [COLOR deepskyblue]{}: {}[/COLOR]".format(status or "LIVE", current.get("title", ""))
        item = xbmcgui.ListItem(label=label, offscreen=True)
        logo = row.get("logo") or provider_icon(row.get("provider"))
        program_art = (current or {}).get("image") if current else ""
        set_art(item, logo, program_art or row.get("fanart") or FANART)
        set_video_info(
            item, row.get("name") or "Live channel", schedule_plot(row, hours=hours),
            row.get("genres") or [], row.get("provider_name") or "")
        item.setProperty("IsPlayable", "true")
        pin = _channel_pin(row)
        item.addContextMenuItems(_pin_context(pin))
        url = plugin_url(action="play", provider=row.get("provider"), id=row.get("id"), view_id=view_id or None)
        xbmcplugin.addDirectoryItem(HANDLE, url, item, isFolder=False)
    end_directory(content="videos", cache=False)


def toggle_pin():
    pin = _pin_from_params()
    if STORAGE.is_pinned(pin):
        STORAGE.remove_pin(pin)
        notify("Unpinned: {}".format(pin.get("label")))
    else:
        STORAGE.add_pin(pin)
        notify("Pinned: {}".format(pin.get("label")))
    xbmc.executebuiltin("Container.Refresh")


def _pinned_scope_count(pin):
    kind = pin.get("kind")
    provider = pin.get("provider", ALL_PROVIDERS)
    language = pin.get("language", ALL_LANGUAGES)
    genre = pin.get("genre", ALL_GENRES)
    if kind == "provider":
        language, genre = ALL_LANGUAGES, ALL_GENRES
    elif kind == "language":
        genre = ALL_GENRES
    rows, _ = REGISTRY.filter(provider, language, genre)
    return _scope_count(rows, provider)


def pinned():
    pins = STORAGE.pins()
    if any(pin.get("kind") in ("provider", "language", "genre") for pin in pins):
        _ensure_browse_snapshot()
    for pin in pins:
        kind = pin.get("kind")
        label = pin.get("label") or "Pinned item"
        if kind in ("provider", "language", "genre"):
            label = "{} ({:,})".format(label, _pinned_scope_count(pin))
        context = _pin_context(pin, force_unpin=True)
        if kind == "provider":
            add_folder(label, "provider_entry", provider_icon(pin.get("provider")),
                       params={"provider": pin.get("provider", ALL_PROVIDERS)}, context=context)
        elif kind == "language":
            context += _random_context(pin.get("provider"), pin.get("language"), ALL_GENRES)
            add_folder(label, "genres", language_icon(pin.get("language")), params={
                "provider": pin.get("provider", ALL_PROVIDERS),
                "language": pin.get("language", ALL_LANGUAGES),
            }, context=context)
        elif kind == "genre":
            context += _random_context(pin.get("provider"), pin.get("language"), pin.get("genre"))
            add_folder(label, "channels", genre_icon(pin.get("genre")), params={
                "provider": pin.get("provider", ALL_PROVIDERS),
                "language": pin.get("language", ALL_LANGUAGES),
                "genre": pin.get("genre", ALL_GENRES),
            }, context=context)
        elif kind == "channel":
            add_action(label, "play", provider_icon(pin.get("provider")), params={
                "provider": pin.get("provider"), "id": pin.get("channel_id"),
            }, context=context)
    if not pins:
        item = xbmcgui.ListItem(label="Nothing pinned yet", offscreen=True)
        set_art(item, media("menu", "pinned_empty.png"))
        set_video_info(item, "Nothing pinned yet", "Long-press a provider, language, genre, or channel and choose Pin.")
        xbmcplugin.addDirectoryItem(HANDLE, plugin_url(action="pinned"), item, isFolder=False)
    end_directory(cache=False)


def _random_candidates_from_pins():
    unique = {}
    for pin in STORAGE.pins():
        if pin.get("kind") == "channel":
            value = {
                "provider": pin.get("provider"), "id": pin.get("channel_id"),
                "name": pin.get("label") or "Pinned channel",
            }
            unique[(value["provider"], value["id"])] = value
            continue
        provider = pin.get("provider", ALL_PROVIDERS)
        language = pin.get("language", ALL_LANGUAGES)
        genre = pin.get("genre", ALL_GENRES)
        if pin.get("kind") == "provider":
            language, genre = ALL_LANGUAGES, ALL_GENRES
        elif pin.get("kind") == "language":
            genre = ALL_GENRES
        rows, _ = REGISTRY.filter(provider, language, genre)
        for value in rows:
            unique[(value.get("provider"), value.get("id"))] = value
    return list(unique.values())


def play_random():
    _ensure_browse_snapshot()
    if PARAMS.get("pin_scope") == "all":
        rows = _random_candidates_from_pins()
    else:
        rows, _ = REGISTRY.filter(
            PARAMS.get("provider", ALL_PROVIDERS),
            PARAMS.get("language", ALL_LANGUAGES),
            PARAMS.get("genre", ALL_GENRES),
        )
    if not rows:
        notify("No playable channels are available in this selection.", warning=True)
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem(offscreen=True))
        return
    selected = random.choice(rows)
    resolve_playback(selected.get("provider"), selected.get("id"))


def resolve_playback(provider, identifier):
    plan = {}
    stage = "resolve"
    attempt_id = ""
    try:
        plan = REGISTRY.resolve(provider, identifier)
        stage = "widevine-preflight"
        widevine_ready, widevine_status, helper_version = ensure_widevine(plan)
        if not widevine_ready:
            if DEBUG_DIAGNOSTICS:
                preflight_error = RuntimeError(
                    "Widevine preflight blocked: status={} helper_version={}".format(
                        widevine_status or "unknown", helper_version or "n/a"
                    )
                )
                log_failure(
                    stage, preflight_error, provider=provider, identifier=identifier, plan=plan
                )
            if widevine_status == "helper-not-installed":
                xbmcgui.Dialog().ok(
                    ADDON_NAME,
                    "This stream requires Widevine DRM, but libwidevinecdm is not installed.\n\n"
                    "InputStream Helper (script.module.inputstreamhelper) is a required StreamDial dependency, "
                    "but Kodi does not currently report it as installed. Reinstall/enable the dependency and try again."
                )
            else:
                xbmcgui.Dialog().ok(
                    ADDON_NAME,
                    "Widevine setup did not complete, so this stream cannot be played yet.\n\n"
                    "Run InputStream Helper and complete its Widevine installation, then try again."
                )
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem(offscreen=True))
            return

        stage = "apply"
        item = xbmcgui.ListItem(path=plan.get("url", ""), offscreen=True)
        use_inputstream = setting_bool("use_inputstream", True)
        # InputStream Helper may have just installed/enabled InputStream Adaptive.
        available = bool(xbmc.getCondVisibility("System.HasAddon(inputstream.adaptive)"))
        if widevine_required(plan) and not available:
            raise RuntimeError("Widevine stream requires inputstream.adaptive")
        apply_to_listitem(item, plan, use_inputstream=use_inputstream, inputstream_available=available)

        # Detailed handoff/probe diagnostics are intentionally opt-in.  When
        # enabled they apply uniformly to every provider so failures occurring
        # later inside Kodi/InputStream still have a correlated source record.
        if DEBUG_DIAGNOSTICS:
            try:
                inputstream_version = (
                    xbmcaddon.Addon("inputstream.adaptive").getAddonInfo("version")
                    if available else "not-installed"
                ) or "unknown"
            except Exception:
                inputstream_version = "unknown"
            manifest_type = str(plan.get("manifest_type") or "").casefold()
            probe = probe_manifest(plan) if manifest_type in ("hls", "mpd") else None
            attempt_id = log_playback_attempt(
                provider, identifier, plan, use_inputstream, available,
                inputstream_version=inputstream_version, widevine_status=widevine_status,
                probe=probe,
            )

        stage = "setResolvedUrl"
        _mark_return_view(PARAMS.get("view_id", ""))
        xbmcplugin.setResolvedUrl(HANDLE, True, item)
    except Exception as error:
        failed_view_id = PARAMS.get("view_id", "")
        if failed_view_id:
            # A resolver failure can mean the row has become stale. Do not reuse
            # that exact screen snapshot on return; rebuild it from provider data.
            CHANNEL_VIEW_CACHE.invalidate(failed_view_id)
            _clear_return_view()
        diagnostic_id = ""
        if DEBUG_DIAGNOSTICS:
            diagnostic_id = log_failure(
                stage, error, provider=provider, identifier=identifier, plan=plan,
                correlation_id=attempt_id)
        message = (
            "Playback failed (diagnostic {}). Upload the Kodi log if this persists.".format(diagnostic_id)
            if diagnostic_id else
            "Playback failed. Try another channel; enable Debug log diagnostics in StreamDial settings for troubleshooting."
        )
        submitted = notify(message, warning=True, playback_failure=True)
        if submitted:
            _settle_notification()
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem(offscreen=True))


def play():
    resolve_playback(PARAMS.get("provider", ""), PARAMS.get("id", ""))


def open_settings():
    ADDON.openSettings()


ROUTES = {
    "root": root,
    "search_menu": search_menu,
    "new_search": new_search,
    "delete_search": delete_search,
    "search_results": search_results,
    "providers": providers,
    "provider_entry": provider_entry,
    "languages": languages,
    "genres": genres,
    "channels": channels,
    "toggle_pin": toggle_pin,
    "pinned": pinned,
    "play_random": play_random,
    "play": play,
    "settings": open_settings,
}


def dispatch():
    action = PARAMS.get("action", "root")
    # Only the two final channel-list routes may reuse a playback-return marker.
    # Any other navigation makes a later visit explicit and therefore forces a
    # fresh final list instead of reviving the prior screen snapshot.
    if action not in ("play", "channels", "search_results"):
        _clear_return_view()
    handler = ROUTES.get(action)
    if not handler:
        notify("Unknown StreamDial TV action.", warning=True)
        end_directory(succeeded=False)
        return
    handler()


if __name__ == "__main__":
    dispatch()
