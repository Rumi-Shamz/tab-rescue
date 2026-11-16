"""Builders for synthetic browser files. Everything here is made up (example.* hosts)."""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

from tab_rescue.lz4 import MOZLZ4_MAGIC, compress_literals
from tab_rescue.model import Item, KIND_TAB, KIND_BOOKMARK


# ------------------------------------------------------------------ LZ4
def lz4_compress(data: bytes) -> bytes:
    """Tiny greedy LZ4 block compressor (4-byte hash matches) used to exercise real matches."""
    out = bytearray()
    n = len(data)
    table: dict[bytes, int] = {}
    i = anchor = 0
    while i + 4 <= n - 5:  # LZ4 requires the last 5 bytes to be literals
        key = data[i:i + 4]
        cand = table.get(key)
        table[key] = i
        if cand is not None and i - cand <= 65535:
            ln = 4
            while i + ln < n - 5 and data[cand + ln] == data[i + ln]:
                ln += 1
            _emit(out, data[anchor:i], i - cand, ln)
            i += ln
            anchor = i
        else:
            i += 1
    _emit(out, data[anchor:], None, 0)
    return bytes(out)


def _emit(out: bytearray, literals: bytes, offset: int | None, match_len: int) -> None:
    ll = len(literals)
    token = (min(ll, 15) << 4)
    ml = match_len - 4 if offset is not None else 0
    if offset is not None:
        token |= min(ml, 15)
    out.append(token)
    if ll >= 15:
        rest = ll - 15
        while rest >= 255:
            out.append(255); rest -= 255
        out.append(rest)
    out += literals
    if offset is not None:
        out += struct.pack("<H", offset)
        if ml >= 15:
            rest = ml - 15
            while rest >= 255:
                out.append(255); rest -= 255
            out.append(rest)


def mozlz4_bytes(obj) -> bytes:
    raw = json.dumps(obj).encode("utf-8")
    return MOZLZ4_MAGIC + struct.pack("<I", len(raw)) + lz4_compress(raw)


def mozlz4_literal_bytes(obj) -> bytes:
    raw = json.dumps(obj).encode("utf-8")
    return MOZLZ4_MAGIC + struct.pack("<I", len(raw)) + compress_literals(raw)


# ------------------------------------------------------------------ Firefox
def firefox_session(now_ms: int) -> dict:
    day = 86_400_000
    def tab(url, title, la, **kw):
        t = {"entries": [{"url": "https://old.example.com/", "title": "old"}, {"url": url, "title": title}],
             "index": 2, "lastAccessed": la, "hidden": False}
        t.update(kw)
        return t
    return {
        "windows": [{
            "tabs": [
                tab("https://docs.example.org/guide", "Guide", now_ms - 1 * day, pinned=True),
                tab("https://www.example.net/a?utm_source=x", "A", now_ms - 200 * day, groupId="g1"),
                tab("https://example.net/a", "A again", now_ms - 2 * day),
                {"entries": [], "userTypedValue": "https://typed.example.com/", "index": 1},
                {"entries": []},  # nothing usable -> skipped
            ],
            "groups": [{"id": "g1", "name": "Research", "color": "blue"}],
        }],
        "_closedWindows": [{"tabs": [tab("https://closed.example.com/", "Closed", now_ms)]}],
    }


def build_places(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT);
    CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, type INTEGER, fk INTEGER, parent INTEGER,
                                title TEXT, dateAdded INTEGER, guid TEXT);
    INSERT INTO moz_places VALUES (1, 'https://example.org/one', 'Place one'),
                                  (2, 'https://example.org/two', 'Place two'),
                                  (3, 'place:type=6&sort=14', 'Smart query');
    INSERT INTO moz_bookmarks VALUES
      (1, 2, NULL, 0, '', 0, 'root________'),
      (2, 2, NULL, 1, 'menu', 0, 'menu________'),
      (3, 2, NULL, 1, 'toolbar', 0, 'toolbar_____'),
      (4, 2, NULL, 3, 'Dev', 0, 'devfolder0001'),
      (5, 2, NULL, 4, 'Python', 0, 'pyfolder00001'),
      (6, 1, 1, 5, 'Bookmark one', 1700000000000000, 'bm0000000001'),
      (7, 1, 2, 2, NULL, 1700000001000000, 'bm0000000002'),
      (8, 1, 3, 3, 'Smart', 1700000002000000, 'bm0000000003');
    """)
    con.commit()
    con.close()


# ------------------------------------------------------------------ Chromium
def chromium_bookmarks() -> dict:
    t0 = (1_700_000_000 * 1_000_000) + 11644473600 * 1_000_000
    def url(name, u):
        return {"type": "url", "name": name, "url": u, "date_added": str(t0)}
    return {"roots": {
        "bookmark_bar": {"type": "folder", "name": "Bookmarks bar", "children": [
            url("Top", "https://example.com/top"),
            {"type": "folder", "name": "Recipes", "children": [
                url("Soup", "https://example.com/soup"),
                {"type": "folder", "name": "Deep", "children": [url("Stew", "https://example.com/stew")]},
            ]},
        ]},
        "other": {"type": "folder", "name": "Other bookmarks", "children": [url("Misc", "https://example.com/misc")]},
        "synced": {"type": "folder", "name": "Mobile bookmarks", "children": []},
    }}


def _pad4(b: bytes) -> bytes:
    return b + b"\0" * ((-len(b)) % 4)


def pickle_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<i", len(b)) + _pad4(b)


def pickle_str16(s: str) -> bytes:
    b = s.encode("utf-16-le")
    return struct.pack("<i", len(s)) + _pad4(b)


def snss_record(cmd: int, payload: bytes) -> bytes:
    return struct.pack("<H", len(payload) + 1) + bytes([cmd]) + payload


def nav_record(tab_id: int, index: int, url: str, title: str) -> bytes:
    body = (struct.pack("<ii", tab_id, index) + pickle_str(url) + pickle_str16(title)
            + pickle_str("") + struct.pack("<ii", 0, 0) + pickle_str(""))
    return snss_record(6, struct.pack("<I", len(body)) + body)


def snss_session() -> bytes:
    """Window 1: tabs 10 (2 navs, 2nd selected), 11 (closed), 12 (pinned, grouped).
    Window 2 (closed): tab 13.  Tab 14 has no window.  Tab 15: pruned navigation."""
    rec = [b"SNSS" + struct.pack("<i", 3)]
    rec.append(snss_record(0, struct.pack("<ii", 1, 10)))
    rec.append(snss_record(0, struct.pack("<ii", 1, 11)))
    rec.append(snss_record(0, struct.pack("<ii", 1, 12)))
    rec.append(snss_record(0, struct.pack("<ii", 2, 13)))
    rec.append(snss_record(2, struct.pack("<ii", 10, 0)))
    rec.append(snss_record(2, struct.pack("<ii", 12, 1)))
    rec.append(nav_record(10, 0, "https://first.example.com/", "First"))
    rec.append(nav_record(10, 1, "https://second.example.com/page", "Café ☃ second"))
    rec.append(snss_record(7, struct.pack("<ii", 10, 1)))
    rec.append(nav_record(11, 0, "https://closed.example.com/", "Closed tab"))
    rec.append(snss_record(16, struct.pack("<i4xq", 11, 0)))
    rec.append(nav_record(12, 0, "https://pinned.example.com/", "Pinned"))
    rec.append(snss_record(7, struct.pack("<ii", 12, 0)))
    rec.append(snss_record(12, struct.pack("<ibxxx", 12, 1)))
    hi, lo = 0x1111111111111111, 0x2222222222222222
    rec.append(snss_record(25, struct.pack("<iiQQbxxxxxxx", 12, 0, hi, lo, 1)))
    gmeta = struct.pack("<QQ", hi, lo) + pickle_str16("Reading list") + struct.pack("<i", 3)
    rec.append(snss_record(27, struct.pack("<I", len(gmeta)) + gmeta))
    rec.append(nav_record(13, 0, "https://window2.example.com/", "Closed window tab"))
    rec.append(snss_record(17, struct.pack("<i4xq", 2, 0)))
    rec.append(nav_record(14, 0, "https://orphan.example.com/", "No window"))
    rec.append(snss_record(0, struct.pack("<ii", 1, 15)))
    rec.append(nav_record(15, 0, "https://a.example.com/", "A"))
    rec.append(nav_record(15, 1, "https://b.example.com/", "B"))
    rec.append(nav_record(15, 2, "https://c.example.com/", "C"))
    rec.append(snss_record(24, struct.pack("<iii", 15, 0, 1)))  # prune entry 0 -> B, C shift to 0, 1
    rec.append(snss_record(7, struct.pack("<ii", 15, 1)))       # selected = old C
    rec.append(snss_record(99, b"unknown command is skipped"))
    rec.append(snss_record(21, struct.pack("<i4xq", 10, 13_300_000_000_000_000)))
    return b"".join(rec)


# ------------------------------------------------------------------ analysis items
def make_item(url: str, title: str = "T", kind: str = KIND_TAB, browser: str = "firefox", **kw) -> Item:
    return Item(kind=kind, browser=browser, profile="p", url=url, title=title, **kw)


TOPIC_A = [
    ("Python asyncio tutorial", "https://docs.python.example/asyncio/tutorial"),
    ("Python asyncio event loop guide", "https://docs.python.example/asyncio/event-loop"),
    ("Python asyncio tasks and coroutines", "https://docs.python.example/asyncio/tasks"),
    ("Python coroutine futures reference", "https://docs.python.example/asyncio/futures"),
    ("Python asyncio streams tutorial", "https://blog.python.example/asyncio-streams"),
    ("Python asyncio queues guide", "https://blog.python.example/asyncio-queues"),
]
TOPIC_B = [
    ("Sourdough bread recipe", "https://bakery.example/recipes/sourdough-bread"),
    ("Easy sourdough starter recipe", "https://bakery.example/recipes/sourdough-starter"),
    ("Bread baking flour hydration guide", "https://bakery.example/guides/bread-flour-hydration"),
    ("Sourdough loaf scoring bread tips", "https://kitchen.example/sourdough-scoring-tips"),
    ("Best flour for sourdough bread baking", "https://kitchen.example/flour-for-bread"),
    ("Homemade bread baking oven recipe", "https://kitchen.example/homemade-bread-oven"),
]
