"""Privacy-safe, upload-friendly diagnostics for provider and playback failures."""

import json
import platform
import re
import sys
import traceback
import urllib.parse
import uuid

import xbmc

from .http import HttpClient


PREFIX = "[StreamDial Diagnostics]"
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?\b")
_SECRET_RE = re.compile(
    r"(?i)\b(token|jwt|auth(?:orization)?|signature|sig|session|cookie|license|key|"
    r"client_secret|password)\s*[:=]\s*(?:bearer\s+)?([^&\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_URL_RE = re.compile(r"https?://[^\s<>\"\']+", re.IGNORECASE)
_JSON_SECRET_RE = re.compile(
    r'(?i)(["\'](?:token|jwt|authorization|signature|session|cookie|license|key|csrf|playId)["\']\s*:\s*["\'])([^"\']+)(["\'])'
)


def sanitize_url(value):
    """Keep only safe URL structure and query-key names."""
    text = str(value or "")
    try:
        parsed = urllib.parse.urlsplit(text)
    except (TypeError, ValueError):
        return _redact(text)
    if parsed.scheme not in ("http", "https"):
        return _redact(text)
    keys = [key for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)]
    query = "&".join("{}=<redacted>".format(key) for key in keys)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _redact(value):
    text = _JWT_RE.sub("<redacted-jwt>", str(value or ""))
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _SECRET_RE.sub(lambda match: "{}=<redacted>".format(match.group(1)), text)

    def redact_url(match):
        raw = match.group(0)
        suffix = ""
        while raw and raw[-1] in ".,;:)]}":
            suffix = raw[-1] + suffix
            raw = raw[:-1]
        return sanitize_url(raw) + suffix

    return _URL_RE.sub(redact_url, text)


def _safe_http_body(value, limit=800):
    """Redact and bound an HTTP error body for upload-friendly diagnostics."""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return ""

    secret_keys = {
        "token", "jwt", "authorization", "signature", "session", "cookie",
        "license", "license_key", "license_url", "key", "api_key", "csrf", "playid",
        "access_token", "refresh_token", "password", "client_secret",
    }

    def scrub(value, key=""):
        if key.casefold() in secret_keys:
            return "<redacted>"
        if isinstance(value, dict):
            return {str(k): scrub(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value[:50]]
        if isinstance(value, str):
            if value.startswith(("http://", "https://")):
                return sanitize_url(value)
            return _redact(value)
        return value

    try:
        text = json.dumps(scrub(json.loads(text)), separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        text = _JSON_SECRET_RE.sub(lambda match: match.group(1) + "<redacted>" + match.group(3), text)
        text = _redact(text)
    if len(text) > limit:
        text = text[:limit] + "...<truncated>"
    return text


def _kodi_version():
    try:
        return xbmc.getInfoLabel("System.BuildVersion") or "unknown"
    except (AttributeError, TypeError):
        return "unknown"


def probe_manifest(plan):
    """Best-effort manifest check used only for diagnostics, never gating play."""
    plan = plan if isinstance(plan, dict) else {}
    url = str(plan.get("url") or "")
    result = {
        "state": "skipped", "status": "n/a", "url": url,
        "content_type": "n/a", "bytes": 0, "marker": "n/a",
        "content_protection": False, "widevine": False, "pssh": False,
        "hls_apple_fairplay": False, "hls_aes128": False, "hls_sample_aes": False,
        "error_type": "", "error": "",
    }
    if not url.startswith(("http://", "https://")):
        return result
    try:
        raw, headers, final_url, status = HttpClient().request_info(
            url, headers=dict(plan.get("headers") or {}), timeout=18,
            max_bytes=768 * 1024, allow_truncated=True,
        )
        lowered = raw.lower()
        manifest_type = str(plan.get("manifest_type") or "").casefold()
        if manifest_type == "mpd":
            marker = "MPD" if b"<mpd" in lowered else "missing-MPD"
        elif manifest_type == "hls":
            marker = "EXTM3U" if b"#extm3u" in lowered else "missing-EXTM3U"
        else:
            marker = "unknown-type"
        result.update({
            "state": "ok", "status": status if status is not None else 200,
            "url": final_url or url,
            "content_type": str(headers.get("Content-Type", "n/a")).split(";", 1)[0],
            "bytes": len(raw), "marker": marker,
            "content_protection": b"contentprotection" in lowered,
            "widevine": b"widevine" in lowered or b"edef8ba9" in lowered,
            "pssh": b"pssh" in lowered,
            "hls_apple_fairplay": b"com.apple.streamingkeydelivery" in lowered,
            "hls_aes128": b"method=aes-128" in lowered,
            "hls_sample_aes": b"method=sample-aes" in lowered,
        })
    except Exception as error:
        result.update({
            "state": "error", "status": getattr(error, "status", None) or "n/a",
            "url": getattr(error, "url", "") or url,
            "error_type": error.__class__.__name__, "error": str(error),
        })
    return result


def log_playback_attempt(provider, identifier, plan, use_inputstream,
                         inputstream_available, inputstream_version="",
                         widevine_status="", probe=None):
    """Log the handoff Kodi may later fail asynchronously to open."""
    attempt_id = uuid.uuid4().hex[:10]
    plan = plan if isinstance(plan, dict) else {}
    details = plan.get("diagnostics") if isinstance(plan.get("diagnostics"), dict) else {}
    probe = probe if isinstance(probe, dict) else {}
    header_names = sorted(str(key) for key in (plan.get("headers") or {}).keys())
    manifest_type = str(plan.get("manifest_type") or "n/a")
    selected = bool(use_inputstream and inputstream_available and manifest_type in ("hls", "mpd"))
    license_url = str(plan.get("license_key") or "")
    lines = [
        "{} PLAYBACK BEGIN id={}".format(PREFIX, attempt_id),
        "id={} stage=handoff provider={} channel_id={}".format(
            attempt_id, _redact(provider or "unknown"), _redact(identifier or "unknown")),
        "runtime kodi={} python={} platform={}".format(
            _kodi_version(), sys.version.split()[0], platform.platform()),
        "plan url={} manifest={} mime={} header_names={}".format(
            sanitize_url(plan.get("url")) or "n/a", manifest_type,
            _redact(plan.get("mime_type") or "n/a"), ",".join(header_names) or "none"),
        "drm required={} license_type={} license_url={} license_key_mode={} widevine_cdm={}".format(
            "yes" if license_url else "no", _redact(plan.get("license_type") or "n/a"),
            sanitize_url(license_url) if license_url else "n/a",
            "url-only" if license_url else "n/a", _redact(widevine_status or "n/a")),
        "inputstream requested={} available={} selected={} version={}".format(
            "yes" if use_inputstream else "no", "yes" if inputstream_available else "no",
            "yes" if selected else "no", _redact(inputstream_version or "n/a")),
    ]
    if details:
        safe_details = _safe_http_body(json.dumps(details, separators=(",", ":"), ensure_ascii=False))
        if safe_details:
            lines.append("provider_plan_details provider={} data={}".format(
                _redact(provider or details.get("provider") or "unknown"), safe_details))
    if details and provider == "pbs":
        lines.append(
            "pbs callsign={} station={} profile={} selected_source={} "
            "has_hls={} has_dash={} has_widevine={}".format(
                _redact(details.get("station_callsign") or "n/a"),
                _redact(details.get("station_name") or "n/a"),
                _redact(details.get("feed_profile") or "n/a"),
                _redact(details.get("selected_source") or "n/a"),
                "yes" if details.get("feed_has_hls") else "no",
                "yes" if details.get("feed_has_dash") else "no",
                "yes" if details.get("feed_has_widevine") else "no",
            )
        )
    elif details and provider == "roku":
        lines.append(
            "roku selected_source={} resolution={} play_id={} play_id_source={} requested_format={}".format(
                _redact(details.get("selected_source") or "n/a"),
                _redact(details.get("resolution") or "n/a"),
                _redact(details.get("play_id") or "n/a"),
                _redact(details.get("play_id_source") or "n/a"),
                _redact(details.get("requested_format") or "n/a"),
            )
        )
    if probe:
        lines.append(
            "manifest_probe state={} status={} url={} content_type={} bytes={} marker={} "
            "content_protection={} widevine={} pssh={} hls_apple_fairplay={} "
            "hls_aes128={} hls_sample_aes={} error_type={} error={}".format(
                _redact(probe.get("state") or "n/a"), _redact(probe.get("status") or "n/a"),
                sanitize_url(probe.get("url")) or "n/a",
                _redact(probe.get("content_type") or "n/a"), probe.get("bytes") or 0,
                _redact(probe.get("marker") or "n/a"),
                "yes" if probe.get("content_protection") else "no",
                "yes" if probe.get("widevine") else "no",
                "yes" if probe.get("pssh") else "no",
                "yes" if probe.get("hls_apple_fairplay") else "no",
                "yes" if probe.get("hls_aes128") else "no",
                "yes" if probe.get("hls_sample_aes") else "no",
                _redact(probe.get("error_type") or "n/a"),
                _redact(probe.get("error") or "n/a"),
            )
        )
    lines.append("{} PLAYBACK END id={}".format(PREFIX, attempt_id))
    level = getattr(xbmc, "LOGWARNING", getattr(xbmc, "LOGERROR", 4))
    for line in lines:
        xbmc.log(line, level)
    return attempt_id


def log_failure(stage, error, provider="", identifier="", plan=None, channel="",
                correlation_id=""):
    """Write a structured failure block and return its short correlation ID."""
    diagnostic_id = uuid.uuid4().hex[:10]
    plan = plan if isinstance(plan, dict) else {}
    error_url = getattr(error, "url", "") or plan.get("url", "")
    status = getattr(error, "status", None)
    header_names = sorted(str(key) for key in (plan.get("headers") or {}).keys())
    lines = [
        "{} BEGIN id={}".format(PREFIX, diagnostic_id),
        "id={} stage={} provider={} channel_id={} channel={}".format(
            diagnostic_id, _redact(stage or "unknown"), _redact(provider or "unknown"),
            _redact(identifier or "unknown"), _redact(channel or "unknown")),
        "correlation playback_attempt_id={}".format(_redact(correlation_id or "n/a")),
        "runtime kodi={} python={} platform={}".format(
            _kodi_version(), sys.version.split()[0], platform.platform()),
        "error type={} status={} message={}".format(
            error.__class__.__name__, status if status is not None else "n/a", _redact(error)),
        "request url={}".format(sanitize_url(error_url) or "n/a"),
        "plan manifest={} mime={} inputstream_headers={}".format(
            plan.get("manifest_type") or "n/a", plan.get("mime_type") or "n/a",
            ",".join(header_names) or "none"),
    ]
    response_body = _safe_http_body(getattr(error, "response_body", ""))
    response_type = _redact(getattr(error, "content_type", "") or "n/a")
    if response_body:
        lines.append("http_response content_type={} body={}".format(response_type, response_body))

    plan_details = plan.get("diagnostics") if isinstance(plan.get("diagnostics"), dict) else {}
    if plan_details:
        safe_plan_details = _safe_http_body(
            json.dumps(plan_details, separators=(",", ":"), ensure_ascii=False))
        if safe_plan_details:
            lines.append("provider_plan_details provider={} data={}".format(
                _redact(provider or plan_details.get("provider") or "unknown"), safe_plan_details))

    details = getattr(error, "diagnostics", None)
    if isinstance(details, dict):
        attempts = details.get("attempts") if isinstance(details.get("attempts"), list) else []
        lines.append(
            "provider_diag provider={} attempts={} cookie_count={} usn={}".format(
                _redact(details.get("provider") or provider or "unknown"), len(attempts),
                _redact(details.get("cookie_count") if details.get("cookie_count") is not None else "n/a"),
                "yes" if details.get("has_usn") else "no",
            )
        )
        for index, attempt in enumerate(attempts[:30], 1):
            if not isinstance(attempt, dict):
                continue
            lines.append(
                "provider_attempt index={} stage={} play_id_source={} play_id={} media_format={} "
                "result={} status={} reason={} url={} content_type={} cookies={} usn={}".format(
                    index, _redact(attempt.get("stage") or "n/a"),
                    _redact(attempt.get("play_id_source") or "n/a"),
                    _redact(attempt.get("play_id") or "n/a"),
                    _redact(attempt.get("media_format") or "n/a"),
                    _redact(attempt.get("result") or "n/a"),
                    _redact(attempt.get("status") if attempt.get("status") is not None else "n/a"),
                    _redact(attempt.get("reason") or "n/a"),
                    sanitize_url(attempt.get("url")) or "n/a",
                    _redact(attempt.get("content_type") or "n/a"),
                    _redact(attempt.get("cookie_count") if attempt.get("cookie_count") is not None else "n/a"),
                    "yes" if attempt.get("has_usn") else "no",
                )
            )
            body = _safe_http_body(attempt.get("response_body") or "")
            if body:
                lines.append("provider_attempt_http index={} body={}".format(index, body))
        if len(attempts) > 30:
            lines.append("provider_diag attempts_truncated={} total={}".format(len(attempts) - 30, len(attempts)))

    trace = _redact(traceback.format_exc())
    if trace and trace.strip() != "NoneType: None":
        lines.append("traceback:\n{}".format(trace.rstrip()))
    lines.append("{} END id={}".format(PREFIX, diagnostic_id))
    for line in lines:
        xbmc.log(line, xbmc.LOGERROR)
    return diagnostic_id
