"""URL normalisation used for duplicate detection."""

from __future__ import annotations

from urllib.parse import urlsplit, parse_qsl, urlencode

TRACKING_EXACT = {"fbclid", "gclid", "ref", "si"}
TRACKING_PREFIXES = ("utm_", "mc_")
WEB_SCHEMES = {"http", "https"}


def is_tracking_param(name: str) -> bool:
    n = name.lower()
    return n in TRACKING_EXACT or n.startswith(TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    """Lower-case scheme/host, drop ``www.``, fragment, trailing ``/``, tracking params.

    Remaining query parameters are sorted so that ``?a=1&b=2`` equals ``?b=2&a=1``.
    Unparseable input is returned stripped and lower-cased.
    """
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()
    scheme = parts.scheme.lower()
    if scheme not in WEB_SCHEMES:
        # about:, chrome://, file://, ... : only lower-case the scheme, drop fragment
        rest = url[len(parts.scheme) + 1:] if parts.scheme else url
        rest = rest.split("#", 1)[0]
        if rest.endswith("/") and len(rest) > 1:
            rest = rest.rstrip("/")
        return f"{scheme}:{rest}" if scheme else rest
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    userinfo = ""
    if parts.username:
        userinfo = parts.username + (":" + parts.password if parts.password else "") + "@"
    path = parts.path
    if path.endswith("/"):
        path = path.rstrip("/")
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not is_tracking_param(k)]
    pairs.sort()
    query = urlencode(pairs)
    out = f"{scheme}://{userinfo}{host}{path}"
    if query:
        out += "?" + query
    return out
