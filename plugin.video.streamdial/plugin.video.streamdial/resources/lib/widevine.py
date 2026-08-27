"""Optional Widevine readiness integration.

StreamDial declares InputStream Helper 0.8.5+ as an addon dependency. When a
stream requires Widevine, this module detects an already-installed CDM first;
otherwise it loads the dependency and lets its normal check/install workflow
run before the stream is handed to InputStream Adaptive.
"""

import importlib
import os
import sys

import xbmc
import xbmcaddon
import xbmcvfs


HELPER_ID = "script.module.inputstreamhelper"
WIDEVINE_NAMES = ("libwidevinecdm.so", "libwidevinecdm.dylib", "widevinecdm.dll")


def required(plan):
    """Return True when the provider playback plan declares Widevine DRM."""
    plan = plan if isinstance(plan, dict) else {}
    license_type = str(plan.get("license_type") or "").casefold()
    return bool(plan.get("license_key")) and ("widevine" in license_type or license_type == "com.widevine.alpha")


def platform_managed():
    return bool(xbmc.getCondVisibility("System.Platform.Android"))


def cdm_present():
    """Check the Kodi CDM location used by InputStream Adaptive."""
    if platform_managed():
        return True
    home = xbmcvfs.translatePath("special://home/cdm/")
    return any(xbmcvfs.exists(os.path.join(home, name)) for name in WIDEVINE_NAMES)


def helper_installed():
    try:
        return bool(xbmc.getCondVisibility("System.HasAddon({})".format(HELPER_ID)))
    except (AttributeError, TypeError):
        try:
            xbmcaddon.Addon(HELPER_ID)
            return True
        except Exception:
            return False


def _load_helper_module():
    """Load the declared InputStream Helper dependency from its addon path."""
    addon = xbmcaddon.Addon(HELPER_ID)
    addon_path = xbmcvfs.translatePath(addon.getAddonInfo("path"))
    candidates = [os.path.join(addon_path, "lib"), addon_path]
    for path in candidates:
        if path and path not in sys.path:
            sys.path.insert(0, path)
    return importlib.import_module("inputstreamhelper"), addon


def ensure(plan):
    """Ensure a Widevine stream can be opened.

    Returns (ready, status, helper_version).  If InputStream Helper is present,
    Helper.check_inputstream() owns the user-facing install/update prompts.
    """
    if not required(plan):
        return True, "not-required", ""
    if platform_managed():
        return True, "platform-managed", ""
    if cdm_present():
        return True, "cdm-file-present", ""
    if not helper_installed():
        return False, "helper-not-installed", ""

    try:
        module, addon = _load_helper_module()
        version = addon.getAddonInfo("version") or "unknown"
        protocol = str((plan or {}).get("manifest_type") or "mpd").casefold()
        if protocol not in ("mpd", "ism", "hls", "rtmp"):
            protocol = "mpd"
        helper = module.Helper(protocol, drm="com.widevine.alpha")
        if not helper.check_inputstream():
            return False, "helper-check-failed", version
        # check_inputstream() may just have installed the CDM.  Trust its return
        # value, but retain a more specific status when the expected file is now
        # visible to Kodi.
        return True, "helper-ready-cdm-present" if cdm_present() else "helper-ready", version
    except Exception as error:
        return False, "helper-error:{}".format(error.__class__.__name__), ""
