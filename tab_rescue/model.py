"""Plain data model shared by the parsers, the analysis and the report."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any
from urllib.parse import urlsplit

KIND_TAB = "tab"
KIND_BOOKMARK = "bookmark"


@dataclass
class Item:
    kind: str                      # "tab" | "bookmark"
    browser: str                   # "firefox" | "brave" | "chromium" | "chrome"
    profile: str                   # profile directory name (not a full path)
    url: str
    title: str = ""
    folder: str = ""               # bookmark folder path ("Toolbar / Dev")
    group: str = ""                # browser tab group name, if any
    window: int = 0
    pinned: bool = False
    hidden: bool = False
    added_ms: int | None = None        # bookmark creation time (ms since Unix epoch)
    last_accessed_ms: int | None = None  # tab last access (ms since Unix epoch)
    # ---- filled by analysis ----
    id: str = ""
    norm_url: str = ""
    host: str = ""
    temp_reasons: list[str] = field(default_factory=list)
    stale_days: int | None = None
    dup_group: str = ""
    dup_primary: bool = False
    dup_size: int = 0
    bookmarked: bool = False
    rec: str = "keep"              # close | keep | review
    reason: str = ""
    cluster: int = -1

    def source_label(self) -> str:
        return f"{self.browser}:{self.kind}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def host_of(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host and parts.scheme in ("about", "chrome", "brave", "edge", "chrome-extension", "moz-extension"):
        return f"{parts.scheme}:"
    return host


def assign_ids(items: list[Item]) -> None:
    """Give every item a stable short id (same input -> same ids)."""
    seen: dict[str, int] = {}
    for it in items:
        base = "|".join((it.kind, it.browser, it.profile, str(it.window), it.url))
        n = seen.get(base, 0)
        seen[base] = n + 1
        it.id = hashlib.sha1(f"{base}|{n}".encode("utf-8", "replace")).hexdigest()[:12]
