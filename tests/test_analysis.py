import unittest

from tab_rescue import analyze as an
from tab_rescue.model import KIND_BOOKMARK, KIND_TAB
from tab_rescue.normalize import normalize_url as n
from .fixtures import make_item

DAY = 86_400_000


class NormalizeTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(n("HTTPS://WWW.Example.COM/Path/?b=2&a=1#frag"), "https://example.com/Path?a=1&b=2")
        self.assertEqual(n("https://example.com/"), "https://example.com")
        self.assertEqual(n("https://example.com"), n("https://www.example.com/"))

    def test_tracking_params(self):
        self.assertEqual(
            n("https://example.com/a?utm_source=x&utm_medium=y&fbclid=1&gclid=2&ref=h&mc_cid=3&mc_eid=4&si=5&keep=1"),
            "https://example.com/a?keep=1")
        self.assertEqual(n("https://example.com/a?UTM_Campaign=x"), "https://example.com/a")

    def test_ports_and_non_web(self):
        self.assertEqual(n("http://example.com:80/x"), "http://example.com/x")
        self.assertEqual(n("https://example.com:8443/x"), "https://example.com:8443/x")
        self.assertEqual(n("about:blank#x"), "about:blank")
        self.assertEqual(n("FILE:///tmp/a/"), "file:///tmp/a")

    def test_path_case_preserved(self):
        self.assertNotEqual(n("https://example.com/A"), n("https://example.com/a"))


class TemporaryTests(unittest.TestCase):
    def reasons(self, url, title="Some title"):
        return an.temporary_reasons(url, title)

    def test_search_pages(self):
        for u in ("https://www.google.com/search?q=cats", "https://www.google.de/search?q=x&hl=de",
                  "https://www.bing.com/search?q=x", "https://duckduckgo.com/?q=x&ia=web",
                  "https://search.brave.com/search?q=x", "https://www.youtube.com/results?search_query=x"):
            self.assertEqual(self.reasons(u), [an.R_SEARCH], u)

    def test_not_search(self):
        for u in ("https://www.google.com/maps", "https://www.youtube.com/watch?v=abc",
                  "https://duckduckgo.com/", "https://example.com/search?q=x"):
            self.assertEqual(self.reasons(u), [], u)

    def test_internal_and_new_tab(self):
        for u in ("about:blank", "about:newtab", "chrome://newtab/", "brave://settings", "chrome://extensions"):
            self.assertEqual(self.reasons(u, ""), [an.R_INTERNAL], u)
        self.assertEqual(self.reasons("about:reader?url=https%3A%2F%2Fexample.com"), [])

    def test_local(self):
        for u in ("http://localhost:3000/", "http://127.0.0.1:8080/x", "file:///home/u/a.html"):
            self.assertEqual(self.reasons(u), [an.R_LOCAL], u)

    def test_auth(self):
        for u in ("https://example.com/login", "https://example.com/users/sign-in?next=/", "https://example.com/oauth/callback?code=1",
                  "https://shop.example.com/checkout/step1", "https://accounts.example.com/x"):
            self.assertEqual(self.reasons(u), [an.R_AUTH], u)
        self.assertEqual(self.reasons("https://example.com/blog/login-tips"), [])

    def test_empty_title(self):
        self.assertEqual(self.reasons("https://example.com/a", "  "), [an.R_EMPTY])
        self.assertEqual(self.reasons("https://example.com/a", "x"), [])


class AnalyzeTests(unittest.TestCase):
    NOW = 1_800_000_000_000

    def run_analysis(self, items):
        an.analyze(items, now_ms=self.NOW)
        return items

    def test_duplicates_and_bookmarked(self):
        a = make_item("https://www.example.com/p?utm_source=a", "P", last_accessed_ms=self.NOW - 5 * DAY)
        b = make_item("https://example.com/p/", "P", last_accessed_ms=self.NOW - 1 * DAY)
        c = make_item("https://example.com/other", "Other", last_accessed_ms=self.NOW)
        bm = make_item("https://example.com/other#frag", "Other", kind=KIND_BOOKMARK, folder="X")
        self.run_analysis([a, b, c, bm])
        self.assertEqual(a.dup_group, b.dup_group)
        self.assertTrue(b.dup_primary and not a.dup_primary)      # most recently used wins
        self.assertEqual((a.rec, b.rec), ("close", "keep"))
        self.assertIn("duplicate", a.reason)
        self.assertTrue(c.bookmarked)
        self.assertEqual(c.rec, "close")
        self.assertIn("already bookmarked", c.reason)
        self.assertEqual(bm.rec, "keep")
        self.assertEqual(c.dup_group, "")                          # tab/bookmark pair is not a duplicate group

    def test_pinned_never_closed(self):
        a = make_item("https://example.com/x", "X", pinned=True)
        b = make_item("https://example.com/x", "X")
        self.run_analysis([a, b])
        self.assertTrue(a.dup_primary)
        self.assertEqual(a.rec, "keep")
        self.assertEqual(b.rec, "close")

    def test_stale(self):
        old = make_item("https://example.com/old", "Old", last_accessed_ms=self.NOW - 120 * DAY)
        fresh = make_item("https://example.com/new", "New", last_accessed_ms=self.NOW - 10 * DAY)
        self.run_analysis([old, fresh])
        self.assertEqual(old.stale_days, 120)
        self.assertEqual((old.rec, old.reason), ("review", "not opened in 120 days"))
        self.assertIsNone(fresh.stale_days)
        self.assertEqual(fresh.rec, "keep")

    def test_temporary_is_close_with_reason(self):
        t = make_item("https://www.google.com/search?q=x", "x - Google Search")
        self.run_analysis([t])
        self.assertEqual(t.rec, "close")
        self.assertIn("search results", t.reason)

    def test_duplicate_bookmarks_flagged_for_review(self):
        a = make_item("https://example.com/b", "B", kind=KIND_BOOKMARK, added_ms=1)
        b = make_item("https://example.com/b/", "B", kind=KIND_BOOKMARK, added_ms=2)
        self.run_analysis([a, b])
        self.assertEqual((a.rec, b.rec), ("keep", "review"))

    def test_ids_are_stable_and_unique(self):
        mk = lambda: [make_item("https://example.com/x"), make_item("https://example.com/x")]
        i1, i2 = self.run_analysis(mk()), self.run_analysis(mk())
        self.assertEqual([i.id for i in i1], [i.id for i in i2])
        self.assertNotEqual(i1[0].id, i1[1].id)

    def test_internal_pages_not_duplicate_groups(self):
        items = self.run_analysis([make_item("about:blank", ""), make_item("about:blank", "")])
        self.assertTrue(all(not i.dup_group for i in items))

    def test_summary(self):
        a = make_item("https://example.com/p", "P")
        b = make_item("https://example.com/p", "P")
        s = an.summarize(self.run_analysis([a, b]))
        self.assertEqual((s["tabs"], s["tab_duplicate_groups"], s["tab_duplicate_extras"]), (2, 1, 1))


if __name__ == "__main__":
    unittest.main()
