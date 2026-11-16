import io
import json
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np

from tab_rescue import analyze as an
from tab_rescue import cli, embed, report
from tab_rescue.cluster import cluster_items
from tab_rescue.discover import discover, classify_chromium
from . import fixtures as fx
from .fixtures import make_item


def synthetic_html() -> tuple[str, dict]:
    items = [make_item(u, t) for t, u in fx.TOPIC_A + fx.TOPIC_B]
    items.append(make_item("https://evil.example/</script><script>alert(1)</script>", "</script><img src=https://evil.example/x.png>"))
    items.append(make_item("https://example.com/dup", "Dup"))
    items.append(make_item("https://example.com/dup", "Dup"))
    an.analyze(items)
    clusters, method = cluster_items(items)
    summary = an.summarize(items)
    payload = report.build_payload(items, clusters, summary, method, report.scan_id(items))
    return report.render_html(payload), payload


class HtmlTests(unittest.TestCase):
    def test_no_remote_resource_loads(self):
        full, payload = synthetic_html()
        # check the page code itself; user data lives (escaped) in the JSON literal
        html = full.replace(report.safe_json_for_html(payload), "null")
        self.assertNotRegex(html, r'(?i)\b(src|href|action|poster|data|srcset)\s*=\s*["\']?\s*(https?:)?//')
        self.assertNotRegex(html, r"(?i)\burl\(\s*[\"']?(https?:)?//")
        self.assertNotRegex(html, r"(?i)@import")
        self.assertNotRegex(html, r"(?i)<(link|iframe|embed|object|img|video|audio|source)\b")
        self.assertNotRegex(html, r"(?i)\b(fetch|XMLHttpRequest|WebSocket|sendBeacon|importScripts)\b")
        # ... and no markup tag anywhere (data included) loads a remote src/href
        self.assertNotRegex(full, r'(?i)<[^>]*\b(src|href)\s*=\s*["\']?https?:')
        self.assertIn("default-src 'none'", html)
        # the only script is inline
        self.assertEqual(len(re.findall(r"(?i)<script\b", html)), 1)
        self.assertNotRegex(html, r'(?i)<script[^>]*\bsrc=')

    def test_payload_cannot_break_out_of_script(self):
        html, payload = synthetic_html()
        self.assertEqual(len(re.findall(r"(?i)</script", html)), 1)        # only the real closing tag
        self.assertNotIn("<img src=https://evil.example", html)
        m = re.search(r"const DATA = (.*?);\n\(function", html, re.S)
        data = json.loads(m.group(1))
        self.assertEqual(data["items"][12]["u"], payload["items"][12]["u"])  # round-trips exactly
        self.assertIn("</script>", data["items"][12]["u"])

    def test_payload_shape(self):
        _, payload = synthetic_html()
        self.assertEqual(len(payload["items"]), 15)
        self.assertEqual(sum(c["size"] for c in payload["clusters"]), 15)
        self.assertTrue(all("i" in it and "c" in it and "r" in it for it in payload["items"]))
        self.assertEqual(payload["summary"]["tab_duplicate_extras"], 1)

    def test_js_syntax(self):
        import shutil, subprocess, glob
        node = shutil.which("node") or next(iter(sorted(glob.glob(str(Path.home() / ".local/share/fnm/node-versions/*/installation/bin/node")))), None)
        if not node:
            self.skipTest("node not available")
        html, _ = synthetic_html()
        js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "page.js"
            p.write_text(js, encoding="utf-8")
            r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


class DiscoverTests(unittest.TestCase):
    def test_discover_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".config/mozilla/firefox/abc.default").mkdir(parents=True)
            (home / ".config/mozilla/firefox/abc.default/places.sqlite").write_bytes(b"")
            (home / ".config/mozilla/firefox/Crash Reports").mkdir()
            (home / ".config/chromium/Default").mkdir(parents=True)
            (home / ".config/chromium/Default/Bookmarks").write_text("{}")
            (home / ".config/chromium/Profile 1/Sessions").mkdir(parents=True)
            (home / ".config/chromium/Cache").mkdir()
            (home / ".config/BraveSoftware/Brave-Browser/Default/Sessions").mkdir(parents=True)
            (home / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Bookmarks").parent.mkdir(parents=True)
            (home / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Bookmarks").write_text("{}")
            ff, ch = discover(home)
        self.assertEqual([p.name for p in ff], ["abc.default"])
        self.assertEqual(sorted((b, p.name) for b, p in ch),
                         [("brave", "Default"), ("brave", "Default"), ("chromium", "Default"), ("chromium", "Profile 1")])

    def test_classify(self):
        self.assertEqual(classify_chromium(Path("/h/.config/google-chrome/Default")), "chrome")
        self.assertEqual(classify_chromium(Path("/h/.config/chromium/Default")), "chromium")


class EndToEndTests(unittest.TestCase):
    def test_scan_cli_writes_outputs_and_never_touches_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ff = tmp / "ff" / "x.default"
            (ff / "sessionstore-backups").mkdir(parents=True)
            (ff / "sessionstore-backups" / "recovery.jsonlz4").write_bytes(fx.mozlz4_bytes(fx.firefox_session(1_800_000_000_000)))
            fx.build_places(ff / "places.sqlite")
            ch = tmp / "chromium" / "Default"
            (ch / "Sessions").mkdir(parents=True)
            (ch / "Sessions" / "Session_1").write_bytes(fx.snss_session())
            (ch / "Bookmarks").write_text(json.dumps(fx.chromium_bookmarks()))
            snapshot = {p: p.read_bytes() for p in tmp.rglob("*") if p.is_file()}
            out = tmp / "out"
            buf = io.StringIO()
            args = cli.build_parser().parse_args(["scan", "--firefox", str(ff), "--chromium", str(ch), "--no-embed", "--out", str(out)])
            self.assertEqual(cli.run_scan(args, out=buf), 0)
            after = {p: p.read_bytes() for p in tmp.rglob("*") if p.is_file() and out not in p.parents}
            self.assertEqual(snapshot, after)                      # sources byte-identical, nothing added
            self.assertEqual(sorted(p.name for p in ch.parent.iterdir()), ["Default"])
            inv = json.loads((out / "inventory.json").read_text())
            self.assertEqual(inv["summary"]["tabs"], 4 + 3)
            self.assertEqual(inv["summary"]["bookmarks"], 2 + 4)
            self.assertTrue((out / "review.html").stat().st_size > 1000)
            text = buf.getvalue()
            self.assertIn("duplicates:", text)
            self.assertIn("clusters:", text)
            self.assertNotIn("example", text)                      # summary prints counts, never URLs/titles

    def test_no_profiles_is_an_error(self):
        args = cli.build_parser().parse_args(["scan", "--out", "unused"])
        self.assertEqual(cli.run_scan(args), 2)


class _FakeOllama(BaseHTTPRequestHandler):
    calls = 0
    def log_message(self, *a):
        pass
    def do_GET(self):
        body = json.dumps({"models": [{"name": "nomic-embed-text:latest"}]}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers["Content-Length"])
        req = json.loads(self.rfile.read(n))
        type(self).calls += 1
        assert req["model"] == "nomic-embed-text"
        assert all(s.startswith("clustering: ") for s in req["input"])
        vecs = [[float(len(s) % 7), float(len(s) % 3), 1.0] for s in req["input"]]
        body = json.dumps({"embeddings": vecs}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)


class EmbedTests(unittest.TestCase):
    def test_cache_and_prefix(self):
        srv = HTTPServer(("127.0.0.1", 0), _FakeOllama)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        try:
            self.assertTrue(embed.model_available(base))
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp) / "embed-cache.json"
                texts = ["alpha", "beta beta", "alpha", "gamma gamma gamma"]
                _FakeOllama.calls = 0
                m1 = embed.embed_texts(texts, cache, base, batch=2)
                first_calls = _FakeOllama.calls
                self.assertEqual(m1.shape, (4, 3))
                self.assertTrue(np.allclose(np.linalg.norm(m1, axis=1), 1, atol=1e-3))
                self.assertTrue(np.allclose(m1[0], m1[2]))
                m2 = embed.embed_texts(texts, cache, base, batch=2)
                self.assertEqual(_FakeOllama.calls, first_calls)    # second run is served from the cache
                self.assertTrue(np.allclose(m1, m2, atol=1e-3))
                data = json.loads(cache.read_text())
                self.assertEqual(len(data["vectors"]), 3)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_refuses_remote_endpoint(self):
        with self.assertRaises(ValueError):
            embed.model_available("http://example.com:11434")
        with self.assertRaises(ValueError):
            embed.embed_texts(["x"], None, "https://api.example.com")


if __name__ == "__main__":
    unittest.main()
