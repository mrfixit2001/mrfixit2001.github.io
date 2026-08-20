# -*- coding: utf-8 -*-
import json
import uuid
from urllib import parse, request, error


class ApiError(Exception):
    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _decode(raw):
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    return raw.decode('utf-8', errors='replace')


def _json_or_text(raw):
    text = _decode(raw)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return text


def request_json(url, method='GET', params=None, data=None, json_data=None,
                 headers=None, timeout=25, multipart=None):
    headers = dict(headers or {})
    if params:
        query = parse.urlencode(params, doseq=True)
        url += ('&' if '?' in url else '?') + query

    body = None
    if multipart is not None:
        boundary = '----PremiumPlayer%s' % uuid.uuid4().hex
        chunks = []
        for key, value in multipart.items():
            if value is None:
                continue
            chunks.append(('--%s\r\n' % boundary).encode('utf-8'))
            chunks.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % key).encode('utf-8'))
            chunks.append(str(value).encode('utf-8'))
            chunks.append(b'\r\n')
        chunks.append(('--%s--\r\n' % boundary).encode('utf-8'))
        body = b''.join(chunks)
        headers['Content-Type'] = 'multipart/form-data; boundary=%s' % boundary
    elif json_data is not None:
        body = json.dumps(json_data).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    elif data is not None:
        body = parse.urlencode(data, doseq=True).encode('utf-8')
        headers['Content-Type'] = 'application/x-www-form-urlencoded'

    req = request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            parsed = _json_or_text(raw)
            return parsed, response.getcode(), dict(response.headers.items())
    except error.HTTPError as exc:
        raw = exc.read()
        payload = _json_or_text(raw)
        message = None
        if isinstance(payload, dict):
            message = payload.get('error') or payload.get('detail') or payload.get('message')
        raise ApiError(message or ('HTTP %s' % exc.code), status=exc.code, payload=payload)
    except error.URLError as exc:
        raise ApiError(str(exc.reason) if getattr(exc, 'reason', None) else str(exc))


def unwrap(payload):
    if isinstance(payload, dict) and 'success' in payload and 'data' in payload:
        if payload.get('success') is False:
            raise ApiError(payload.get('detail') or payload.get('error') or 'API request failed', payload=payload)
        return payload.get('data')
    return payload
