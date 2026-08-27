"""Small dependency-free HTTP client for provider APIs."""

import gzip
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class HttpFailure(Exception):
    def __init__(self, status=None, message="", url="", response_body="", content_type=""):
        self.status = status
        self.url = str(url or "")
        # Bounded response metadata is invaluable for provider API diagnostics.
        # Keep it on the exception rather than in the message so normal UI
        # errors remain concise and the structured diagnostics layer can redact it.
        self.response_body = str(response_body or "")[:16384]
        self.content_type = str(content_type or "")[:256]
        text = message or ("HTTP request failed" if status is None else "HTTP {}".format(status))
        super().__init__(text)


class HttpClient:
    def __init__(self, user_agent=DEFAULT_UA, cookies=True):
        self.user_agent = user_agent
        handlers = []
        self.cookie_jar = None
        if cookies:
            self.cookie_jar = http.cookiejar.CookieJar()
            handlers.append(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.opener = urllib.request.build_opener(*handlers)

    @staticmethod
    def _url(url, params):
        if not params:
            return url
        query = urllib.parse.urlencode(params, doseq=True)
        return url + ("&" if "?" in url else "?") + query

    def request_info(self, url, method="GET", params=None, data=None, headers=None,
                     timeout=20, max_bytes=16 * 1024 * 1024, allow_truncated=False):
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip",
        }
        request_headers.update(headers or {})
        target_url = self._url(url, params)
        request = urllib.request.Request(
            target_url, data=data, headers=request_headers, method=method)
        try:
            response = self.opener.open(request, timeout=timeout)
            raw = response.read(max_bytes + 1)
        except urllib.error.HTTPError as error:
            try:
                error_raw = error.read(16 * 1024)
            except (OSError, ValueError, AttributeError):
                error_raw = b""
            try:
                error_text = error_raw.decode("utf-8", "replace")
            except AttributeError:
                error_text = str(error_raw or "")
            error_headers = getattr(error, "headers", None)
            content_type = ""
            if error_headers is not None:
                try:
                    content_type = error_headers.get("Content-Type", "")
                except AttributeError:
                    content_type = ""
            raise HttpFailure(
                error.code, url=getattr(error, "url", target_url),
                response_body=error_text, content_type=content_type)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise HttpFailure(message=str(error), url=target_url)
        truncated = len(raw) > max_bytes
        if truncated:
            if not allow_truncated:
                raise HttpFailure(message="Response exceeded safety limit", url=response.geturl())
            raw = raw[:max_bytes]
        compressed = response.headers.get("Content-Encoding", "").lower() == "gzip" or raw[:2] == b"\x1f\x8b"
        if compressed and truncated and allow_truncated:
            return raw, response.headers, response.geturl(), getattr(response, "status", None)
        if compressed:
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError):
                raise HttpFailure(message="Invalid compressed response", url=response.geturl())
            if len(raw) > max_bytes:
                raise HttpFailure(message="Expanded response exceeded safety limit", url=response.geturl())
        return raw, response.headers, response.geturl(), getattr(response, "status", None)

    def request(self, url, **kwargs):
        raw, headers, final_url, _ = self.request_info(url, **kwargs)
        return raw, headers, final_url

    def get_bytes(self, url, **kwargs):
        return self.request(url, **kwargs)[0]

    def get_text(self, url, **kwargs):
        raw, headers, _ = self.request(url, **kwargs)
        charset = headers.get_content_charset() if hasattr(headers, "get_content_charset") else None
        return raw.decode(charset or "utf-8", "replace")

    def get_json(self, url, **kwargs):
        try:
            return json.loads(self.get_bytes(url, **kwargs).decode("utf-8", "replace"))
        except (TypeError, ValueError):
            raise HttpFailure(message="Invalid JSON response", url=url)

    def post_json(self, url, payload=None, headers=None, **kwargs):
        merged = {"Accept": "application/json", "Content-Type": "application/json"}
        merged.update(headers or {})
        data = json.dumps(payload if payload is not None else {}).encode("utf-8")
        raw, _, _ = self.request(url, method="POST", data=data, headers=merged, **kwargs)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except (TypeError, ValueError):
            raise HttpFailure(message="Invalid JSON response", url=url)

    def cookie_header(self):
        """Return the current session cookies in an HTTP Cookie header."""
        if not self.cookie_jar:
            return ""
        return "; ".join(
            "{}={}".format(cookie.name, cookie.value)
            for cookie in self.cookie_jar
            if cookie.name and cookie.value is not None
        )

    def final_url(self, url, **kwargs):
        """Follow redirects and return the provider's final URL."""
        kwargs.setdefault("max_bytes", 1024)
        kwargs.setdefault("allow_truncated", True)
        return self.request_info(url, **kwargs)[2]


def header_string(headers):
    """Encode headers in Kodi's URL/ISA pipe format."""
    return urllib.parse.urlencode(headers or {})
