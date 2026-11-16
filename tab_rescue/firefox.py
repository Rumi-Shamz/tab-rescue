"""Firefox: open tabs from the session store, bookmarks from places.sqlite."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .lz4 import read_mozlz4, LZ4Error
from .model import Item, KIND_TAB, KIND_BOOKMARK
from .tmpcopy import temp_copy

SESSION_FILES = ("sessionstore-backups/recovery.jsonlz4", "sessionstore.jsonlz4")

ROOT_NAMES = {
    "menu________": "Bookmarks Menu",
    "toolbar_____": "Bookmarks Toolbar",
    "unfiled_____": "Other Bookmarks",
    "mobile______": "Mobile Bookmarks",
}


def parse_session(data: dict, profile: str) -> list[Item]:
    """Turn a decoded session-store JSON document into tab items."""
    items: list[Item] = []
    for w_idx, win in enumerate(data.get("windows", []) or []):
        groups = {g.get("id"): g.get("name") or "" for g in (win.get("groups") or []) if isinstance(g, dict)}
        for tab in win.get("tabs", []) or []:
            entries = tab.get("entries") or []
            url = title = ""
            if entries:
                idx = tab.get("index")
                if not isinstance(idx, int) or not 1 <= idx <= len(entries):
                    idx = len(entries)
                entry = entries[idx - 1] or {}
                url = entry.get("url") or ""
                title = entry.get("title") or ""
            if not url:
                url = tab.get("userTypedValue") or ""
            if not url:
                continue
            la = tab.get("lastAccessed")
            items.append(Item(
                kind=KIND_TAB, browser="firefox", profile=profile, url=url, title=title,
                group=groups.get(tab.get("groupId"), "") if tab.get("groupId") else "",
                window=w_idx, pinned=bool(tab.get("pinned")), hidden=bool(tab.get("hidden")),
                last_accessed_ms=int(la) if isinstance(la, (int, float)) and la > 0 else None,
            ))
    return items


def read_session_file(path: Path) -> dict:
    with temp_copy(path) as (copy,):
        assert copy is not None
        return json.loads(read_mozlz4(copy.read_bytes()))


def load_tabs(profile_dir: Path) -> list[Item]:
    """Open tabs of a Firefox profile (empty list if no readable session store)."""
    for rel in SESSION_FILES:
        path = profile_dir / rel
        if not path.is_file():
            continue
        try:
            return parse_session(read_session_file(path), profile_dir.name)
        except (OSError, LZ4Error, ValueError):
            continue
    return []


def read_bookmarks_db(db_path: Path, profile: str) -> list[Item]:
    """Read bookmarks from a places.sqlite *copy* (db_path must already be a copy)."""
    uri = "file:" + str(db_path).replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        folders: dict[int, tuple[int, str, str]] = {}
        for rid, parent, title, guid in con.execute(
                "SELECT id, parent, title, guid FROM moz_bookmarks WHERE type = 2"):
            folders[rid] = (parent, title or "", guid or "")

        def folder_path(folder_id: int) -> str:
            names: list[str] = []
            seen: set[int] = set()
            cur = folder_id
            while cur in folders and cur not in seen:
                seen.add(cur)
                parent, title, guid = folders[cur]
                if guid == "root________":
                    break
                names.append(ROOT_NAMES.get(guid, title) or "(untitled)")
                cur = parent
            return " / ".join(reversed(names))

        cache: dict[int, str] = {}
        items: list[Item] = []
        rows = con.execute(
            "SELECT b.parent, b.title, b.dateAdded, p.url, p.title FROM moz_bookmarks b "
            "JOIN moz_places p ON p.id = b.fk WHERE b.type = 1 ORDER BY b.id")
        for parent, btitle, added, url, ptitle in rows:
            if not url or url.startswith("place:"):
                continue
            if parent not in cache:
                cache[parent] = folder_path(parent)
            items.append(Item(
                kind=KIND_BOOKMARK, browser="firefox", profile=profile, url=url,
                title=btitle or ptitle or "", folder=cache[parent],
                added_ms=int(added // 1000) if isinstance(added, int) and added > 0 else None,
            ))
        return items
    finally:
        con.close()


def load_bookmarks(profile_dir: Path) -> list[Item]:
    src = profile_dir / "places.sqlite"
    if not src.is_file():
        return []
    try:
        with temp_copy(src, profile_dir / "places.sqlite-wal") as (db, _wal):
            assert db is not None
            return read_bookmarks_db(db, profile_dir.name)
    except sqlite3.Error:
        return []
