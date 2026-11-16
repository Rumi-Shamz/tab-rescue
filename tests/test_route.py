import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tab_rescue import analyze as an
from tab_rescue import apply as ap
from tab_rescue import report
from tab_rescue import route as rt
from .fixtures import make_item
from tab_rescue.model import KIND_BOOKMARK


def ctx(tmp: Path) -> rt.Context:
    repo = tmp / "webapp"
    (repo / ".git/info").mkdir(parents=True, exist_ok=True)
    client = tmp / "client"
    (client / ".git/info").mkdir(parents=True, exist_ok=True)
    return rt.Context(
        projects=[{"id": "webapp", "label": "Example web app", "needs": ["hosting"], "path": repo, "sensitive": False},
                  {"id": "client", "label": "Client ERP", "needs": [], "path": client, "sensitive": True},
                  {"id": "infra", "label": "Home infra", "needs": [], "path": Path.home(), "sensitive": False}],
        modules={"m1": "Business modeling", "m2": "Building", "m3": "Marketing", "m4": "Operating"},
        vault=tmp / "vault", fields=["Psychology", "Systems Thinking"], backlog=[],
        rcfg={"venture_os": str(tmp / "vos"), "forge": str(tmp / "forge")})


def fake_yt(tmp: Path) -> SimpleNamespace:
    q, s = tmp / "ideas-queue.jsonl", tmp / "ideas-sent.jsonl"
    read = lambda p: [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []  # noqa: E731
    return SimpleNamespace(
        render=SimpleNamespace(safe=lambda n: re.sub(r"[\\/:]", "-", n)),
        route=SimpleNamespace(MODULES={"m1": ("Business modeling", "component-1"), "m2": ("Building", None),
                                       "m3": ("Marketing", "component-3"), "m4": ("Operating", "component-4")}),
        ideas=SimpleNamespace(QUEUE=q, SENT=s, _read=read, _key=lambda u, t: f"{u}|{t}"))


class FakeBackend:
    alias, kind = "fake", "claude"

    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def run(self, system, user, schema):
        self.prompts.append(user)
        lines = [json.loads(ln) for ln in user.split("## Items\n", 1)[1].splitlines()]
        return SimpleNamespace(data={"routes": [dict(self.answer(x), i=x["i"]) for x in lines]}, error="", seconds=0.1)


def inventory():
    items = [
        make_item("https://hosting.example/docs", "Hosting docs", group="webapp"),
        make_item("https://hosting.example/docs?utm_source=x", "Hosting docs"),             # duplicate tab -> noise
        make_item("https://duckduckgo.com/?q=x", "x - Search"),
        make_item("https://mail.example.com/inbox/1", "Invoice 42 for a client"),            # private host
        make_item("https://blog.example/attention", "How attention works", kind=KIND_BOOKMARK, folder="Toolbar / read"),
        make_item("https://bank.example/login", "My bank"),                                  # temp (login) -> noise
    ]
    an.analyze(items)
    return [it.to_dict() for it in items]


class CleanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = ctx(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_project_becomes_backlog(self):
        r = rt.clean({"route": "integrate", "target": "nope", "next_step": "x"}, self.c)
        self.assertEqual((r["route"], r["target"]), ("backlog", "nope"))
        self.assertEqual(rt.clean({"route": "integrate", "target": "WebApp"}, self.c)["target"], "webapp")

    def test_learn_modes(self):
        self.assertEqual(rt.clean({"route": "learn", "field": "psychology", "mode": "breadth"}, self.c)["mode"], "depth")
        r = rt.clean({"route": "learn", "field": "Psychology", "mode": "bridge", "bridge_to": "systems thinking"}, self.c)
        self.assertEqual((r["mode"], r["bridge_to"]), ("bridge", "Systems Thinking"))
        r = rt.clean({"route": "learn", "field": "music theory", "mode": "depth"}, self.c)
        self.assertEqual((r["field"], r["mode"]), ("Music Theory", "breadth"))

    def test_bad_values_are_coerced(self):
        self.assertEqual(rt.clean({"route": "teleport"}, self.c)["route"], "drop")
        self.assertEqual(rt.clean({"route": "venture-os", "target": "M9"}, self.c)["target"], "m1")
        r = rt.clean({"route": "reference", "target": "", "next_step": "x", "field": "F"}, self.c)
        self.assertEqual((r["target"], r["next_step"], r["field"]), ("Reference", "", ""))


class RouteItemsTests(unittest.TestCase):
    def test_routes_only_non_noise_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            items = inventory()
            noise = [it["title"] for it in items if rt.noise_reason(it)]
            self.assertIn("x - Search", noise)
            self.assertIn("My bank", noise)
            self.assertEqual(sum(1 for it in items if it["title"] == "Hosting docs" and rt.noise_reason(it)), 1)
            be = FakeBackend(lambda x: {"route": "integrate", "target": "webapp", "next_step": "use it"})
            cache = tmp / "routes.json"
            routed, failed = rt.route_items(items, ctx(tmp), be, cache, batch=2, log=lambda m: None)
            self.assertEqual((routed, failed), (3, 0))
            self.assertEqual(len(be.prompts), 2)
            sent = "\n".join(be.prompts)
            self.assertNotIn("Invoice 42", sent)                 # private title withheld
            self.assertIn("(private)", sent)
            self.assertNotIn("utm_source", sent)                 # never query strings
            self.assertIn('"group": "webapp"', sent)
            self.assertEqual(rt.used({"a": {"route": "learn", "field": "Music Theory", "target": ""},
                                      "b": {"route": "reference", "target": "Accounts"}}),
                             {"fields": ["Music Theory"], "backlog": [], "reference": ["Accounts"]})
            self.assertEqual(rt.route_items(items, ctx(tmp), be, cache, log=lambda m: None), (0, 0))
            self.assertEqual(len(be.prompts), 2)                 # second run is served from the cache

    def test_failed_batch_is_reported_not_cached(self):
        class Broken(FakeBackend):
            def run(self, system, user, schema):
                return SimpleNamespace(data=None, error="boom", seconds=0)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            routed, failed = rt.route_items(inventory(), ctx(tmp), Broken(None), tmp / "r.json", log=lambda m: None)
            self.assertEqual((routed, failed), (0, 3))
            self.assertFalse((tmp / "r.json").exists())


def decision(url, route, title="T", kind="tab", **kw):
    return {"id": "x", "kind": kind, "url": url, "norm": url, "title": title, "group": "", "folder": "",
            "route": route, "target": "", "field": "", "mode": "", "bridge_to": "", "next_step": "", "why": "",
            "one_liner": "", **kw}


class ApplyTests(unittest.TestCase):
    def test_writes_each_destination_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            c, yt, out = ctx(tmp), fake_yt(tmp), tmp / "out"
            (tmp / "vos" / "component-3").mkdir(parents=True)
            decs = [
                decision("https://a.example/", "integrate", "Hosting docs", target="webapp", next_step="add it", why="w"),
                decision("https://b.example/", "integrate", "Client CRM", target="client"),
                decision("https://c.example/", "integrate", "Router", target="infra"),
                decision("https://d.example/", "learn", "Attention", field="Psychology", mode="depth", next_step="read"),
                decision("https://e.example/", "learn", "Scales", field="Music Theory", mode="breadth"),
                decision("https://f.example/", "reference", "Bank", target="Accounts"),
                decision("https://g.example/", "intake", "Shop idea", one_liner="For x, y so that z"),
                decision("https://h.example/", "venture-os", "Funnels", target="m3"),
                decision("https://i.example/", "backlog", "Lib", target="Cli Tool"),
                decision("https://j.example/", "drop", "Gone"),
            ]
            counts = ap.apply(decs, c, yt, out, log=lambda m: None)
            self.assertEqual(sum(counts.values()), 10)
            inbox = (tmp / "webapp" / "TAB-INBOX.md").read_text()
            self.assertIn("[Hosting docs](https://a.example/)", inbox)
            self.assertIn("add it", inbox)
            self.assertIn("TAB-INBOX.md", (tmp / "client/.git/info/exclude").read_text())   # client repo: kept out of git
            self.assertFalse((tmp / "webapp/.git/info/exclude").exists())
            self.assertIn("Router", (c.vault / "Next/Dev - infra.md").read_text())          # home dir is not a repo
            self.assertIn("· depth: read", (c.vault / "Knowledge/Reading/Psychology.md").read_text())
            self.assertIn("type: field", (c.vault / "Knowledge/Music Theory.md").read_text())  # breadth opens a field
            self.assertIn("[[Reading/Music Theory|reading queue]]", (c.vault / "Knowledge/Music Theory.md").read_text())
            self.assertRegex((c.vault / "Next/Dev - Backlog.md").read_text(), r"(?m)^- \[ \] \*\*Cli Tool\*\*: ")
            self.assertIn("Funnels", (tmp / "vos/component-3/TAB-INBOX.md").read_text())
            q = [json.loads(x) for x in yt.ideas.QUEUE.read_text().splitlines()]
            self.assertEqual((q[0]["origin"], q[0]["one_liner"]), ("browser tab", "For x, y so that z"))
            self.assertIn('HREF="https://f.example/"', (out / "reference-bookmarks.html").read_text())

            before = (tmp / "webapp" / "TAB-INBOX.md").read_text()
            self.assertEqual(ap.apply(decs, c, yt, out, log=lambda m: None), {})              # idempotent
            self.assertEqual((tmp / "webapp" / "TAB-INBOX.md").read_text(), before)
            self.assertEqual(len(yt.ideas.QUEUE.read_text().splitlines()), 1)

            moved = [dict(decs[0], route="learn", target="", field="Psychology")]
            self.assertEqual(sum(ap.apply(moved, c, yt, out, log=lambda m: None).values()), 1)  # a move re-applies

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            counts = ap.apply([decision("https://a.example/", "integrate", target="webapp")], ctx(tmp), fake_yt(tmp),
                              tmp / "out", dry=True, log=lambda m: None)
            self.assertEqual(counts, {"webapp repo": 1})
            self.assertFalse((tmp / "webapp/TAB-INBOX.md").exists())
            self.assertFalse((tmp / "out").exists())

    def test_close_plan(self):
        items = inventory()
        applied = {f"{it['kind']}|{it['norm_url']}": {"route": "drop"} for it in items if it["kind"] == "tab"}
        plan = "\n".join(ap.close_plan(items, applied))
        self.assertIn("CLOSE webapp: 1/1 saved", plan)
        self.assertIn("Bookmark folders fully routed", plan)


class PayloadTests(unittest.TestCase):
    def test_payload_carries_routes_and_applied(self):
        items = inventory()
        routes = {rt.key(items[0]): {"route": "integrate", "target": "webapp", "next_step": "go"}}
        applied = {rt.key(items[0]): {"route": "integrate", "target": "webapp"}}
        p = report.build_payload(items, [], {}, "tfidf", "sid", routes, applied, {"projects": [{"id": "webapp", "label": "x"}]})
        first = p["items"][0]
        self.assertEqual(first["s"], {"r": "integrate", "t": "webapp", "x": "go"})
        self.assertEqual(first["ap"]["t"], "webapp")
        self.assertEqual(p["items"][2]["s"]["r"], "drop")       # search page: noise
        self.assertEqual(p["items"][2]["s"]["z"], 1)
        self.assertNotIn("s", p["items"][4])                    # bookmark not routed yet
        self.assertEqual(p["ctx"]["projects"][0]["id"], "webapp")


class StackAndPinnedTests(unittest.TestCase):
    def test_pinned_tab_stays_open_without_model(self):
        items = inventory()
        it = dict(items[0], pinned=True)
        self.assertEqual(rt.suggestion(it, {})["route"], "keep")
        self.assertNotIn(rt.key(it), {rt.key(x) for x in rt.todo([it], {})})

    def test_noise_category(self):
        self.assertEqual(rt.noise_category("duplicate tab (2 open copies); already bookmarked (safe to close)"), "Duplicate tabs")
        self.assertEqual(rt.noise_category("search results page"), "Search results")

    def test_stack_console_is_filed_under_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            items = inventory()
            items[0] = dict(items[0], host="dash.hosting.example", url="https://dash.hosting.example/projects/1")
            be = FakeBackend(lambda x: {"route": "reference", "target": "Hosting"})
            rt.route_items(items, ctx(tmp), be, tmp / "r.json", stack={"Hoster": r"hosting\.example$"}, log=lambda m: None)
            self.assertIn('"stack": "Hoster"', be.prompts[0])
            self.assertEqual(rt.read_json(tmp / "r.json")[rt.key(items[0])]["target"], "Stack / Hoster")

    def test_reference_html_nests_folders(self):
        out = ap.reference_html([{"url": "https://a.example/", "title": "A", "target": "Stack / Vercel"},
                                 {"url": "https://b.example/", "title": "B", "target": "Tools"}])
        self.assertLess(out.index("<H3>Stack</H3>"), out.index("<H3>Vercel</H3>"))
        self.assertIn("<H3>Tools</H3>", out)


def mk(*a, **k) -> dict:
    items = [make_item(*a, **k)]
    an.analyze(items)
    return items[0].to_dict()


class ReaderRulesTests(unittest.TestCase):
    """Rules on top of the model's answer: stack consoles, YouTube, merged fields, the page itself."""

    STACK = {"Hoster": {"host": r"(^|\.)hosting\.example$", "console": "https://dash.hosting.example/"},
             "Code": {"host": r"^code\.example$", "console": "https://code.example/", "path": r"^/?$|^/settings(/|$)"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = ctx(Path(self.tmp.name))
        self.ctx.areas = {"dev": "https://youtube.example/pl1", "knowledge": "", "dump": ""}

    def tearDown(self):
        self.tmp.cleanup()

    def sg(self, items, cache, cfg=None):
        return rt.suggestions(items, cache, cfg or {"stack": self.STACK}, self.ctx)

    def test_docs_are_not_stack_pages(self):
        self.assertEqual(rt.stack_service("hosting.example", self.STACK, "/docs/start"), "")
        self.assertEqual(rt.stack_service("dash.hosting.example", self.STACK, "/project/1/logs"), "Hoster")
        self.assertEqual(rt.stack_service("code.example", self.STACK, "/someone/repo"), "")
        self.assertEqual(rt.stack_service("code.example", self.STACK, "/settings/tokens"), "Code")

    def test_stack_consoles_fold_into_one_link(self):
        a = mk("https://dash.hosting.example/project/1", "Project 1")
        b = mk("https://code.example/someone/repo", "someone/repo", kind=KIND_BOOKMARK)
        cache = {rt.key(a): {"route": "reference", "target": "Stack / Hoster"},
                 rt.key(b): {"route": "reference", "target": "Tools"}}
        sa, sb = self.sg([a, b], cache)
        self.assertEqual((sa["route"], sa["category"]), ("drop", "Stack console pages"))
        self.assertEqual(sb["target"], "Tools")
        links = rt.stack_links({"stack": self.STACK})
        self.assertEqual({x["url"] for x in links}, {"https://dash.hosting.example/", "https://code.example/"})

    def test_same_console_twice(self):
        a = mk("https://console.cloud.example/a", "A")
        b = mk("https://console.cloud.example/b", "B", kind=KIND_BOOKMARK)
        cache = {rt.key(a): {"route": "reference", "target": "Accounts"}, rt.key(b): {"route": "reference", "target": "Accounts"}}
        sa, sb = self.sg([a, b], cache)
        self.assertEqual(sa["route"], "reference")
        self.assertEqual(sb["category"], "Same console twice")

    def test_youtube_goes_to_a_playlist(self):
        v = mk("https://www.youtube.com/watch?v=abc", "A talk")
        w = mk("https://www.youtube.com/watch?v=def", "Noise video")
        cache = {rt.key(v): {"route": "learn", "field": "Psychology", "mode": "depth", "next_step": "watch it"},
                 rt.key(w): {"route": "drop", "why": "no value"}}
        sv, sw = self.sg([v, w], cache)
        self.assertEqual((sv["route"], sv["target"], sv["next_step"]), ("video", "knowledge", "watch it"))
        self.assertEqual(sw["route"], "drop")
        sv = self.sg([v], {rt.key(v): {"route": "venture-os", "target": "m1"}})[0]
        self.assertEqual(sv["target"], "dump")                   # no business playlist configured

    def test_merged_fields(self):
        a = mk("https://x.example/ds", "Raft explained")
        cache = {rt.key(a): {"route": "learn", "field": "Distributed Systems", "mode": "bridge", "bridge_to": "Software Design"}}
        cfg = {"stack": {}, "merge_fields": {"distributed systems": "Software Architecture",
                                             "Software Design": "Software Architecture"}}
        s = self.sg([a], cache, cfg)[0]
        self.assertEqual((s["field"], s["mode"], s["bridge_to"]), ("Software Architecture", "depth", ""))
        self.assertEqual(cache[rt.key(a)]["field"], "Distributed Systems")   # the cache is not rewritten

    def test_review_page_stays_open(self):
        it = mk("file:///home/u/Dev/projects/tab-rescue/out/review.html", "tab-rescue")
        self.assertEqual(rt.suggestion(it, {})["route"], "keep")


class ApplyExtrasTests(unittest.TestCase):
    def test_note_video_and_stack_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            c = ctx(tmp)
            c.areas = {"knowledge": "https://youtube.example/pl"}
            dec = [
                {"kind": "tab", "url": "https://design.example/", "norm": "https://design.example", "title": "Nice site",
                 "route": "integrate", "target": "webapp", "next_step": "", "why": "moved by hand",
                 "note": "inspiration for personal brand pages"},
                {"kind": "tab", "url": "https://www.youtube.com/watch?v=abc", "norm": "https://youtube.com/watch?v=abc",
                 "title": "A talk", "route": "video", "target": "knowledge", "note": "deep work"},
                {"kind": "bookmark", "url": "https://tool.example/", "norm": "https://tool.example", "title": "Tool",
                 "route": "reference", "target": "Tools", "note": "invoice tool"},
            ]
            stack = [{"title": "Hoster", "url": "https://dash.hosting.example/", "target": "Stack"}]
            yt = fake_yt(tmp)
            yt.root = tmp / "yt"
            counts = ap.apply(dec, c, yt, tmp / "out", log=lambda m: None, stack=stack)
            q = (tmp / "yt" / "state" / "queue" / "knowledge.txt").read_text()
            self.assertEqual(q, "https://www.youtube.com/watch?v=abc\tA talk\n")
            ap.apply(dec, c, yt, tmp / "out", log=lambda m: None, stack=stack)          # again: no duplicate
            self.assertEqual((tmp / "yt" / "state" / "queue" / "knowledge.txt").read_text(), q)
            self.assertEqual(counts["youtube · knowledge"], 1)
            inbox = (tmp / "webapp" / "TAB-INBOX.md").read_text()
            self.assertIn("inspiration for personal brand pages", inbox)
            self.assertNotIn("moved by hand", inbox)
            vids = (tmp / "out" / "youtube-playlists.md").read_text()
            self.assertIn("## knowledge · [playlist](https://youtube.example/pl)", vids)
            self.assertIn("deep work", vids)
            ref = (tmp / "out" / "reference-bookmarks.html").read_text()
            self.assertIn("<H3>Stack</H3>", ref)
            self.assertIn("<DD>invoice tool", ref)


class ClosePlanRootsTests(unittest.TestCase):
    def test_root_folders_are_emptied_not_deleted(self):
        items = [{"kind": "bookmark", "norm_url": f"https://x.example/{i}", "folder": f} for i, f in
                 enumerate(["Bookmarks Toolbar", "Bookmarks Toolbar / tools", "Bookmarks Toolbar / tools / cms",
                            "Bookmarks Toolbar / misc"])]
        applied = {f"bookmark|https://x.example/{i}": {} for i in range(3)}
        plan = "\n".join(ap.close_plan(items, applied))
        self.assertIn("EMPTY  Bookmarks Toolbar", plan)
        self.assertIn("DELETE Bookmarks Toolbar / tools  (with its subfolders)", plan)
        self.assertNotIn("DELETE Bookmarks Toolbar / tools / cms", plan)
        self.assertNotIn("Bookmarks Toolbar / misc", plan.split("routed")[1])


class LastExportTests(unittest.TestCase):
    def test_compact_item_carries_last_export(self):
        it = inventory()[0]
        d = report.compact_item(it, {"route": "learn", "field": "Psychology"}, None, {it["id"]: "learn||Psychology"})
        self.assertEqual(d["ex"], "learn||Psychology")
        self.assertEqual(d["s"]["f"], "Psychology")


if __name__ == "__main__":
    unittest.main()
