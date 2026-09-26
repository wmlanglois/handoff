"""Credential references for explicitly configured model gateways; never persist key values."""
import os
import re
import urllib.request
from urllib.parse import urlsplit, urlunsplit


def gateway_url(value):
    p = urlsplit(value)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError("gateway must be an http(s) URL without credentials, query or fragment")
    path = p.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((p.scheme, p.netloc, path, "", ""))


def key_value(env_name):
    if not env_name:
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise ValueError("api-key environment reference must be a variable name")
    value = os.environ.get(env_name)
    if not value or any(c in value for c in "\r\n"):
        raise ValueError("gateway credential environment variable is missing or invalid")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("gateway redirects are refused; configure the final URL explicitly")


def open_request(request, timeout, env_name=None):
    key = key_value(env_name)
    if key:
        request.add_header("Authorization", "Bearer " + key)
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)
