"""Chromium family (Chromium, Chrome, Brave): bookmarks JSON and SNSS session files."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .model import Item, KIND_TAB, KIND_BOOKMARK
from .tmpcopy import temp_copy

WEBKIT_EPOCH_OFFSET_US = 11644473600 * 1_000_000  # 1601-01-01 -> 1970-01-01

ROOT_LABELS = {"bookmark_bar": "Bookmarks Bar", "other": "Other Bookmarks", "synced": "Mobile Bookmarks"}


def webkit_us_to_unix_ms(value) -> int | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return (v - WEBKIT_EPOCH_OFFSET_US) // 1000


# --------------------------------------------------------------------- bookmarks

def parse_bookmarks(data: dict, browser: str, profile: str) -> list[Item]:
    items: list[Item] = []

    def walk(node: dict, path: list[str]) -> None:
        kind = node.get("type")
        if kind == "url":
            url = node.get("url") or ""
            if url:
                items.append(Item(
                    kind=KIND_BOOKMARK, browser=browser, profile=profile, url=url,
                    title=node.get("name") or "", folder=" / ".join(path),
                    added_ms=webkit_us_to_unix_ms(node.get("date_added")),
                ))
        elif kind == "folder" or "children" in node:
            for child in node.get("children") or []:
                if isinstance(child, dict):
                    walk(child, path)

    def walk_folder(node: dict, path: list[str]) -> None:
        # folders extend the path with their own name
        for child in node.get("children") or []:
            if not isinstance(child, dict):
                continue
            if child.get("type") == "folder" or ("children" in child and child.get("type") != "url"):
                walk_folder(child, path + [child.get("name") or "(untitled)"])
            else:
                walk(child, path)

    roots = data.get("roots") or {}
    for key in ("bookmark_bar", "other", "synced"):
        root = roots.get(key)
        if isinstance(root, dict):
            walk_folder(root, [ROOT_LABELS.get(key, key)])
    return items


def load_bookmarks(profile_dir: Path, browser: str) -> list[Item]:
    src = profile_dir / "Bookmarks"
    if not src.is_file():
        return []
    try:
        with temp_copy(src) as (copy,):
            assert copy is not None
            data = json.loads(copy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return parse_bookmarks(data, browser, profile_dir.name)


# --------------------------------------------------------------------- SNSS sessions
#
# Command ids from Chromium's chrome/browser/sessions/session_service_commands.cc
# (components/sessions). IDs are stable because they are persisted on disk.
CMD_SET_TAB_WINDOW = 0
CMD_SET_TAB_INDEX_IN_WINDOW = 2
CMD_PRUNED_FROM_BACK = 5           # {tab_id, count}: drop entries with index >= count
CMD_UPDATE_TAB_NAVIGATION = 6
CMD_SET_SELECTED_NAVIGATION_INDEX = 7
CMD_PRUNED_FROM_FRONT = 11         # {tab_id, count}: drop first `count` entries
CMD_SET_PINNED_STATE = 12
CMD_TAB_CLOSED = 16
CMD_WINDOW_CLOSED = 17
CMD_LAST_ACTIVE_TIME = 21          # {tab_id, int64 time}
CMD_TAB_NAV_PATH_PRUNED = 24       # {tab_id, index, count}
CMD_SET_TAB_GROUP = 25             # {tab_id, pad, token_high, token_low}
CMD_SET_TAB_GROUP_METADATA2 = 27   # pickle {token_high, token_low, string16 title, int32 color}
CMD_SET_ACTIVE_WINDOW = 20


@dataclass
class _Tab:
    window: int | None = None
    index_in_window: int = 0
    selected_nav: int | None = None
    navs: dict[int, tuple[str, str]] = field(default_factory=dict)
    pinned: bool = False
    group: tuple[int, int] | None = None
    last_active_us: int | None = None


def iter_commands(data: bytes):
    """Yield ``(command_id, payload)`` for each well-formed record; stop at truncation."""
    if len(data) < 8 or data[:4] != b"SNSS":
        return
    pos = 8  # magic + int32 version
    n = len(data)
    while pos + 2 <= n:
        (size,) = struct.unpack_from("<H", data, pos)
        pos += 2
        if size < 1 or pos + size > n:
            return
        cmd = data[pos]
        yield cmd, data[pos + 1:pos + size]
        pos += size


class _Pickle:
    """Reader for Chromium's base::Pickle (4-byte aligned fields)."""

    def __init__(self, payload: bytes):
        if len(payload) < 4:
            raise ValueError("short pickle")
        (length,) = struct.unpack_from("<I", payload, 0)
        self.buf = payload[4:4 + length] if length <= len(payload) - 4 else payload[4:]
        self.pos = 0

    def int32(self) -> int:
        (v,) = struct.unpack_from("<i", self.buf, self.pos)
        self.pos += 4
        return v

    def uint64(self) -> int:
        (v,) = struct.unpack_from("<Q", self.buf, self.pos)
        self.pos += 8
        return v

    def string(self) -> bytes:
        ln = self.int32()
        if ln < 0 or self.pos + ln > len(self.buf):
            raise ValueError("bad string length")
        v = self.buf[self.pos:self.pos + ln]
        self.pos += (ln + 3) & ~3
        return v

    def string16(self) -> str:
        ln = self.int32()
        if ln < 0 or self.pos + ln * 2 > len(self.buf):
            raise ValueError("bad string16 length")
        v = self.buf[self.pos:self.pos + ln * 2].decode("utf-16-le", "replace")
        self.pos += (ln * 2 + 3) & ~3
        return v


def parse_session_bytes(data: bytes) -> list[dict]:
    """Reconstruct open tabs from SNSS bytes.

    Returns dicts: ``{url, title, window, pinned, group, last_accessed_ms, order}``.
    """
    tabs: dict[int, _Tab] = {}
    closed_tabs: set[int] = set()
    closed_windows: set[int] = set()
    group_names: dict[tuple[int, int], str] = {}

    def tab(tid: int) -> _Tab:
        t = tabs.get(tid)
        if t is None:
            t = tabs[tid] = _Tab()
        return t

    for cmd, p in iter_commands(data):
        try:
            if cmd == CMD_SET_TAB_WINDOW and len(p) >= 8:
                window_id, tab_id = struct.unpack_from("<ii", p, 0)
                tab(tab_id).window = window_id
            elif cmd == CMD_SET_TAB_INDEX_IN_WINDOW and len(p) >= 8:
                tab_id, index = struct.unpack_from("<ii", p, 0)
                tab(tab_id).index_in_window = index
            elif cmd == CMD_UPDATE_TAB_NAVIGATION:
                pk = _Pickle(p)
                tab_id = pk.int32()
                nav_index = pk.int32()
                url = pk.string().decode("utf-8", "replace")
                title = pk.string16()
                tab(tab_id).navs[nav_index] = (url, title)
            elif cmd == CMD_SET_SELECTED_NAVIGATION_INDEX and len(p) >= 8:
                tab_id, index = struct.unpack_from("<ii", p, 0)
                tab(tab_id).selected_nav = index
            elif cmd == CMD_PRUNED_FROM_BACK and len(p) >= 8:
                tab_id, count = struct.unpack_from("<ii", p, 0)
                t = tab(tab_id)
                t.navs = {i: v for i, v in t.navs.items() if i < count}
            elif cmd == CMD_PRUNED_FROM_FRONT and len(p) >= 8:
                tab_id, count = struct.unpack_from("<ii", p, 0)
                t = tab(tab_id)
                t.navs = {i - count: v for i, v in t.navs.items() if i >= count}
                if t.selected_nav is not None:
                    t.selected_nav -= count
            elif cmd == CMD_TAB_NAV_PATH_PRUNED and len(p) >= 12:
                tab_id, index, count = struct.unpack_from("<iii", p, 0)
                t = tab(tab_id)
                shifted: dict[int, tuple[str, str]] = {}
                for i, v in t.navs.items():
                    if i < index:
                        shifted[i] = v
                    elif i >= index + count:
                        shifted[i - count] = v
                t.navs = shifted
            elif cmd == CMD_SET_PINNED_STATE and len(p) >= 5:
                tab_id = struct.unpack_from("<i", p, 0)[0]
                tab(tab_id).pinned = bool(p[4])
            elif cmd == CMD_TAB_CLOSED and len(p) >= 4:
                closed_tabs.add(struct.unpack_from("<i", p, 0)[0])
            elif cmd == CMD_WINDOW_CLOSED and len(p) >= 4:
                closed_windows.add(struct.unpack_from("<i", p, 0)[0])
            elif cmd == CMD_LAST_ACTIVE_TIME and len(p) >= 12:
                tab_id = struct.unpack_from("<i", p, 0)[0]
                us = struct.unpack_from("<q", p, len(p) - 8)[0]
                tab(tab_id).last_active_us = us
            elif cmd == CMD_SET_TAB_GROUP and len(p) >= 24:
                # {int32 tab_id, 4 pad, uint64 token_high, uint64 token_low, bool has_group}
                tab_id = struct.unpack_from("<i", p, 0)[0]
                hi, lo = struct.unpack_from("<QQ", p, 8)
                has_group = p[24] != 0 if len(p) > 24 else True
                tab(tab_id).group = (hi, lo) if (has_group and (hi or lo)) else None
            elif cmd == CMD_SET_TAB_GROUP_METADATA2:
                pk = _Pickle(p)
                hi, lo = pk.uint64(), pk.uint64()
                group_names[(hi, lo)] = pk.string16()
        except (ValueError, struct.error):
            continue  # a malformed record must not abort the whole parse

    result: list[dict] = []
    for tid, t in tabs.items():
        if tid in closed_tabs or t.window is None or t.window in closed_windows or not t.navs:
            continue
        nav = t.navs.get(t.selected_nav) if t.selected_nav is not None else None
        if nav is None:
            nav = t.navs[max(t.navs)]
        url, title = nav
        if not url:
            continue
        la = None
        if t.last_active_us:
            la = webkit_us_to_unix_ms(t.last_active_us)
        result.append({
            "url": url, "title": title, "window": t.window, "pinned": t.pinned,
            "group": group_names.get(t.group, "") if t.group else "",
            "last_accessed_ms": la, "order": (t.window, t.index_in_window, tid),
        })
    result.sort(key=lambda d: d["order"])
    return result


def newest_session_files(profile_dir: Path) -> list[Path]:
    sdir = profile_dir / "Sessions"
    if not sdir.is_dir():
        return []
    files = [p for p in sdir.glob("Session_*") if p.is_file()]
    files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return files


def load_tabs(profile_dir: Path, browser: str) -> list[Item]:
    """Open tabs from the newest Session_* file that yields any tab."""
    for path in newest_session_files(profile_dir):
        try:
            with temp_copy(path) as (copy,):
                assert copy is not None
                raw = copy.read_bytes()
        except OSError:
            continue
        tabs = parse_session_bytes(raw)
        if not tabs:
            continue
        return [
            Item(kind=KIND_TAB, browser=browser, profile=profile_dir.name, url=t["url"],
                 title=t["title"], group=t["group"], window=t["window"], pinned=t["pinned"],
                 last_accessed_ms=t["last_accessed_ms"])
            for t in tabs
        ]
    return []
