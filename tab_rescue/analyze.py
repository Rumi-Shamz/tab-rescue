"""Duplicate detection, temporary-tab heuristics, staleness and recommendations."""

from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict
from urllib.parse import urlsplit, parse_qs

from .model import Item, KIND_TAB, KIND_BOOKMARK, assign_ids, host_of
from .normalize import normalize_url, WEB_SCHEMES

STALE_DAYS_DEFAULT = 90

R_SEARCH = "search results page"
R_INTERNAL = "browser internal or new-tab page"
R_LOCAL = "local development or file page"
R_AUTH = "login / OAuth / callback / checkout page"
R_EMPTY = "empty title"

INTERNAL_SCHEMES = {"about", "chrome", "brave", "edge", "chrome-search", "chrome-untrusted", "view-source"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
AUTH_SEGMENTS = {
    "login", "signin", "sign-in", "log-in", "logon", "oauth", "oauth2", "callback",
    "checkout", "authorize", "sso", "logout", "signout", "sign-out",
}
AUTH_HOST_LABELS = {"accounts", "login", "auth", "sso", "signin", "id"}

_GOOGLE = re.compile(r"^(?:[a-z0-9-]+\.)*google\.[a-z.]{2,6}$")
_YAHOO_LIKE = re.compile(r"^(?:search\.yahoo\.com|(?:[a-z]+\.)?ecosia\.org|(?:www\.)?startpage\.com|kagi\.com|yandex\.[a-z.]+)$")


def _is_search(host: str, path: str, query: dict[str, list[str]]) -> bool:
    path = path.rstrip("/") or "/"
    if _GOOGLE.match(host) and path == "/search" and ("q" in query or "tbm" in query):
        return True
    if host == "bing.com" or host.endswith(".bing.com"):
        return path == "/search" and "q" in query
    if host in ("duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com"):
        return "q" in query and path in ("/", "/html", "/lite")
    if host == "search.brave.com":
        return path.startswith("/search") and "q" in query
    if host.endswith("youtube.com"):
        return path == "/results" and "search_query" in query
    if _YAHOO_LIKE.match(host):
        return "/search" in path and bool(query)
    return False


def temporary_reasons(url: str, title: str) -> list[str]:
    """Human-readable reasons why a *tab* is probably temporary (empty list if none)."""
    reasons: list[str] = []
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        query = parse_qs(parts.query)
    except ValueError:
        parts = None
        host = ""
        query = {}
    scheme = parts.scheme.lower() if parts else ""
    path = parts.path if parts else ""
    if scheme in INTERNAL_SCHEMES:
        if not (scheme == "about" and path.startswith("reader")) and not url.lower().startswith("about:reader"):
            reasons.append(R_INTERNAL)
    elif scheme == "file" or host in LOCAL_HOSTS or host.endswith(".localhost"):
        reasons.append(R_LOCAL)
    elif scheme in WEB_SCHEMES:
        h = host[4:] if host.startswith("www.") else host
        if _is_search(h, path, query):
            reasons.append(R_SEARCH)
        segs = {s for s in path.lower().split("/") if s}
        if segs & AUTH_SEGMENTS or h.split(".")[0] in AUTH_HOST_LABELS and h.count(".") >= 2:
            reasons.append(R_AUTH)
        elif "redirect_uri" in query or ("code" in query and "state" in query):
            reasons.append(R_AUTH)
    if not (title or "").strip() and R_INTERNAL not in reasons:
        reasons.append(R_EMPTY)
    return reasons


def _dup_id(norm: str) -> str:
    return "d" + hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:8]


def _primary_key(it: Item) -> tuple:
    # pinned first, then most recently accessed / oldest bookmark, then original order
    if it.kind == KIND_TAB:
        return (not it.pinned, -(it.last_accessed_ms or 0))
    return (it.added_ms or 0,)


def analyze(items: list[Item], now_ms: int | None = None, stale_days: int = STALE_DAYS_DEFAULT) -> None:
    """Fill in every analysis field of ``items`` in place (except ``cluster``)."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    assign_ids(items)
    for it in items:
        it.norm_url = normalize_url(it.url)
        it.host = host_of(it.url)
        it.temp_reasons = temporary_reasons(it.url, it.title) if it.kind == KIND_TAB else []
        it.stale_days = None
        if it.kind == KIND_TAB and it.last_accessed_ms:
            days = (now_ms - it.last_accessed_ms) // 86_400_000
            if days > stale_days:
                it.stale_days = int(days)

    # duplicate groups: open tabs among themselves, bookmarks among themselves
    groups: dict[tuple[str, str], list[Item]] = defaultdict(list)
    for it in items:
        scheme = it.norm_url.split(":", 1)[0]
        if scheme in WEB_SCHEMES or scheme in ("file", "ftp"):
            groups[(it.kind, it.norm_url)].append(it)
    for (kind, norm), members in groups.items():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=_primary_key)  # stable: ties keep scan order
        gid = _dup_id(kind + norm)
        for rank, it in enumerate(ordered):
            it.dup_group = gid
            it.dup_primary = rank == 0
            it.dup_size = len(members)

    bookmarked = {it.norm_url for it in items if it.kind == KIND_BOOKMARK}
    for it in items:
        it.bookmarked = it.kind == KIND_TAB and it.norm_url in bookmarked
        it.rec, it.reason = recommend(it)


def recommend(it: Item) -> tuple[str, str]:
    """Return ``(close|keep|review, reason)`` for one analysed item."""
    if it.kind == KIND_BOOKMARK:
        if it.dup_group and not it.dup_primary:
            return "review", f"duplicate bookmark ({it.dup_size} copies)"
        return "keep", "bookmark"
    if it.pinned:
        return "keep", "pinned tab"
    close: list[str] = []
    close.extend(it.temp_reasons)
    if it.dup_group and not it.dup_primary:
        close.append(f"duplicate tab ({it.dup_size} open copies)")
    if it.bookmarked:
        close.append("already bookmarked (safe to close)")
    if close:
        return "close", "; ".join(close)
    if it.stale_days is not None:
        return "review", f"not opened in {it.stale_days} days"
    return "keep", "no issues found"


def mark_unsorted_for_review(items: list[Item], unsorted_id: int | None) -> None:
    """Open tabs that fit no topic are 'uncertain' -> review (unless already flagged)."""
    if unsorted_id is None:
        return
    for it in items:
        if it.kind == KIND_TAB and it.cluster == unsorted_id and it.rec == "keep" and not it.pinned:
            it.rec, it.reason = "review", "doesn't group with any other item (unsorted)"


def summarize(items: list[Item]) -> dict:
    per_source: dict[str, int] = defaultdict(int)
    per_profile: dict[str, int] = defaultdict(int)
    for it in items:
        per_source[it.source_label()] += 1
        per_profile[f"{it.browser}/{it.profile}:{it.kind}"] += 1
    dup_groups = {it.dup_group for it in items if it.dup_group and it.kind == KIND_TAB}
    bm_dup_groups = {it.dup_group for it in items if it.dup_group and it.kind == KIND_BOOKMARK}
    return {
        "per_source": dict(sorted(per_source.items())),
        "per_profile": dict(sorted(per_profile.items())),
        "tabs": sum(1 for it in items if it.kind == KIND_TAB),
        "bookmarks": sum(1 for it in items if it.kind == KIND_BOOKMARK),
        "tab_duplicate_groups": len(dup_groups),
        "tab_duplicate_extras": sum(1 for it in items if it.kind == KIND_TAB and it.dup_group and not it.dup_primary),
        "bookmark_duplicate_groups": len(bm_dup_groups),
        "temporary": sum(1 for it in items if it.temp_reasons),
        "stale": sum(1 for it in items if it.stale_days is not None),
        "already_bookmarked": sum(1 for it in items if it.bookmarked),
        "rec_close": sum(1 for it in items if it.rec == "close"),
        "rec_keep": sum(1 for it in items if it.rec == "keep"),
        "rec_review": sum(1 for it in items if it.rec == "review"),
    }
