import json
import struct
import tempfile
import time
import unittest
from pathlib import Path

from tab_rescue import chromium, firefox
from tab_rescue.model import KIND_BOOKMARK, KIND_TAB
from . import fixtures as fx


class FirefoxTests(unittest.TestCase):
    def test_mozlz4_session_file(self):
        now = int(time.time() * 1000)
        with tempfile.TemporaryDirectory() as tmp:
            prof = Path(tmp) / "abc.default"
            (prof / "sessionstore-backups").mkdir(parents=True)
            (prof / "sessionstore-backups" / "recovery.jsonlz4").write_bytes(fx.mozlz4_bytes(fx.firefox_session(now)))
            tabs = firefox.load_tabs(prof)
        self.assertEqual([t.url for t in tabs], [
            "https://docs.example.org/guide", "https://www.example.net/a?utm_source=x",
            "https://example.net/a", "https://typed.example.com/"])
        self.assertTrue(all(t.kind == KIND_TAB and t.browser == "firefox" for t in tabs))
        self.assertTrue(tabs[0].pinned)
        self.assertEqual(tabs[1].group, "Research")
        self.assertEqual(tabs[0].title, "Guide")      # current entry = entries[index-1]
        self.assertAlmostEqual(tabs[1].last_accessed_ms, now - 200 * 86_400_000, delta=1)
        self.assertNotIn("https://closed.example.com/", [t.url for t in tabs])  # _closedWindows ignored

    def test_fallback_to_sessionstore(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = Path(tmp)
            (prof / "sessionstore.jsonlz4").write_bytes(fx.mozlz4_literal_bytes(fx.firefox_session(1_700_000_000_000)))
            self.assertEqual(len(firefox.load_tabs(prof)), 4)

    def test_corrupt_session_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = Path(tmp)
            (prof / "sessionstore.jsonlz4").write_bytes(b"mozLz40\0" + b"\x05\0\0\0" + b"\x40zz")
            self.assertEqual(firefox.load_tabs(prof), [])

    def test_places_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = Path(tmp)
            fx.build_places(prof / "places.sqlite")
            before = (prof / "places.sqlite").read_bytes()
            bms = firefox.load_bookmarks(prof)
            self.assertEqual((prof / "places.sqlite").read_bytes(), before)  # source untouched
            self.assertEqual(sorted(p.name for p in prof.iterdir()), ["places.sqlite"])  # no stray files
        by_url = {b.url: b for b in bms}
        self.assertEqual(set(by_url), {"https://example.org/one", "https://example.org/two"})  # place: skipped
        one = by_url["https://example.org/one"]
        self.assertEqual(one.kind, KIND_BOOKMARK)
        self.assertEqual(one.folder, "Bookmarks Toolbar / Dev / Python")
        self.assertEqual(one.title, "Bookmark one")
        self.assertEqual(one.added_ms, 1_700_000_000_000)
        two = by_url["https://example.org/two"]
        self.assertEqual(two.title, "Place two")          # falls back to moz_places.title
        self.assertEqual(two.folder, "Bookmarks Menu")


class ChromiumBookmarkTests(unittest.TestCase):
    def test_parse_nested(self):
        bms = chromium.parse_bookmarks(fx.chromium_bookmarks(), "brave", "Default")
        folders = {b.title: b.folder for b in bms}
        self.assertEqual(folders["Top"], "Bookmarks Bar")
        self.assertEqual(folders["Soup"], "Bookmarks Bar / Recipes")
        self.assertEqual(folders["Stew"], "Bookmarks Bar / Recipes / Deep")
        self.assertEqual(folders["Misc"], "Other Bookmarks")
        self.assertEqual(len(bms), 4)
        self.assertTrue(all(b.browser == "brave" for b in bms))
        self.assertEqual(bms[0].added_ms, 1_700_000_000_000)   # WebKit epoch converted

    def test_load_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = Path(tmp) / "Default"
            prof.mkdir()
            (prof / "Bookmarks").write_text(json.dumps(fx.chromium_bookmarks()))
            self.assertEqual(len(chromium.load_bookmarks(prof, "chromium")), 4)
            (prof / "Bookmarks").write_text("{not json")
            self.assertEqual(chromium.load_bookmarks(prof, "chromium"), [])


class SnssTests(unittest.TestCase):
    def test_reconstruct_open_tabs(self):
        tabs = chromium.parse_session_bytes(fx.snss_session())
        urls = [t["url"] for t in tabs]
        # ordered by (window, index in window, tab id)
        self.assertEqual(urls, ["https://second.example.com/page", "https://c.example.com/",
                                "https://pinned.example.com/"])
        self.assertIn("https://second.example.com/page", urls)         # selected nav, not the first one
        self.assertNotIn("https://first.example.com/", urls)
        self.assertNotIn("https://closed.example.com/", urls)           # TabClosed
        self.assertNotIn("https://window2.example.com/", urls)          # WindowClosed
        self.assertNotIn("https://orphan.example.com/", urls)           # never attached to a window
        self.assertEqual(sorted(urls), ["https://c.example.com/", "https://pinned.example.com/",
                                        "https://second.example.com/page"])
        by = {t["url"]: t for t in tabs}
        self.assertEqual(by["https://second.example.com/page"]["title"], "Café ☃ second")
        self.assertTrue(by["https://pinned.example.com/"]["pinned"])
        self.assertEqual(by["https://pinned.example.com/"]["group"], "Reading list")
        self.assertFalse(by["https://second.example.com/page"]["pinned"])
        self.assertIsNotNone(by["https://second.example.com/page"]["last_accessed_ms"])
        self.assertEqual(by["https://second.example.com/page"]["last_accessed_ms"],
                         (13_300_000_000_000_000 - chromium.WEBKIT_EPOCH_OFFSET_US) // 1000)

    def test_truncated_file_is_graceful(self):
        data = fx.snss_session()
        full = chromium.parse_session_bytes(data)
        for cut in (len(data) - 3, len(data) // 2, 9, 8, 5, 0):
            part = chromium.parse_session_bytes(data[:cut])   # must not raise
            self.assertLessEqual(len(part), len(full) + 1)
        self.assertEqual(chromium.parse_session_bytes(b"NOPE" + data[4:]), [])

    def test_garbage_records_are_skipped(self):
        data = (b"SNSS" + struct.pack("<i", 1) + fx.snss_record(6, b"\x01\x02")  # malformed nav pickle
                + fx.snss_record(0, struct.pack("<ii", 1, 5)) + fx.nav_record(5, 0, "https://ok.example.com/", "ok"))
        tabs = chromium.parse_session_bytes(data)
        self.assertEqual([t["url"] for t in tabs], ["https://ok.example.com/"])

    def test_newest_session_file_is_used(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            prof = Path(tmp) / "Default"
            (prof / "Sessions").mkdir(parents=True)
            old = prof / "Sessions" / "Session_1"
            new = prof / "Sessions" / "Session_2"
            tabrestore = prof / "Sessions" / "Tabs_3"
            old.write_bytes(fx.snss_session())
            empty = b"SNSS" + struct.pack("<i", 3)
            new.write_bytes(empty)
            tabrestore.write_bytes(fx.snss_session())
            os.utime(old, (1000, 1000)); os.utime(new, (2000, 2000)); os.utime(tabrestore, (3000, 3000))
            tabs = chromium.load_tabs(prof, "chromium")
            # newest Session_* is empty -> falls back to the older one that has tabs; Tabs_* is never read
            self.assertEqual(len(tabs), 3)
            new.write_bytes(fx.snss_session().replace(b"second.example.com", b"newest.example.com"))
            os.utime(new, (2000, 2000))
            self.assertIn("https://newest.example.com/page", [t.url for t in chromium.load_tabs(prof, "chromium")])


if __name__ == "__main__":
    unittest.main()
