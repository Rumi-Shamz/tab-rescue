"""Command line interface: ``python3 -m tab_rescue scan ...``."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from . import analyze as an
from . import chromium, firefox, embed as embed_mod
from .cluster import cluster_items, item_text
from .discover import discover, classify_chromium
from .model import Item
from . import report
from .report import write_outputs


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tab_rescue", description="Read-only inventory of browser tabs and bookmarks.")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan", help="scan browsers and write inventory.json + review.html")
    s.add_argument("--firefox", metavar="PROFILE_DIR", nargs="+", action="extend", default=[],
                   help="Firefox profile directory (repeatable)")
    s.add_argument("--chromium", metavar="PROFILE_DIR", nargs="+", action="extend", default=[],
                   help="Chromium/Chrome/Brave profile directory (repeatable)")
    s.add_argument("--auto", action="store_true", help="discover profiles in the usual locations")
    s.add_argument("--embed", action=argparse.BooleanOptionalAction, default=None,
                   help="cluster with Ollama nomic-embed-text embeddings (default: auto-detect)")
    s.add_argument("--out", default="out", help="output directory (default: out/)")
    s.add_argument("--stale-days", type=int, default=an.STALE_DAYS_DEFAULT,
                   help="tabs not opened for more than N days are 'stale' (default 90)")
    s.add_argument("--ollama", default=embed_mod.DEFAULT_BASE, help=argparse.SUPPRESS)

    r = sub.add_parser("route", help="propose a destination for every item (projects, Venture OS, vault, reference, drop)")
    r.add_argument("--out", default="out", help="folder of a previous scan (default: out/)")
    r.add_argument("--model", help="yt-extract model alias (Claude or local only)")
    r.add_argument("--limit", type=int, help="route at most N new items (try it on a few first)")
    r.add_argument("--dry-run", action="store_true", help="count what would be routed, no model call")
    r.add_argument("--redo", metavar="REGEX", help="route again the items whose host or URL matches (e.g. 'vercel|supabase')")

    a = sub.add_parser("apply", help="write the decisions exported from review.html to their destinations")
    a.add_argument("decisions", nargs="?", help="decisions file (default: newest ~/Downloads/tab-rescue-decisions*.json)")
    a.add_argument("--out", default="out", help="folder of a previous scan (default: out/)")
    a.add_argument("--dry-run", action="store_true", help="show what would be written, write nothing")
    pg = sub.add_parser("page", help="rebuild review.html from the last scan, routes and applied state (no model call)")
    pg.add_argument("--out", default="out", help="folder of a previous scan (default: out/)")
    return p


def _context(required: bool):
    """yt-extract's destinations, or None when it isn't available and not required."""
    from . import route as rt
    cfg = rt.load_config()
    try:
        yt = rt.yt_extract(cfg)
        return cfg, yt, rt.context(yt)
    except SystemExit:
        if required:
            raise
        return cfg, None, None


def _out(arg: str) -> Path:
    """A relative --out is relative to the tab-rescue folder, so the commands work from anywhere."""
    from .route import REPO
    p = Path(arg).expanduser()
    return p if p.is_absolute() or (Path.cwd() / p / "inventory.json").exists() else REPO / p


def _inventory(out_dir: Path) -> dict:
    p = out_dir / "inventory.json"
    if not p.exists():
        raise SystemExit(f"{p} not found: run `python3 -m tab_rescue scan --auto --out {out_dir}` first")
    return json.loads(p.read_text(encoding="utf-8"))


def run_route(args: argparse.Namespace, out=sys.stdout) -> int:
    from . import route as rt
    out_dir = _out(args.out)
    inv = _inventory(out_dir)
    cfg, yt, ctx = _context(required=True)
    cache_path = out_dir / "routes.json"
    cached = rt.read_json(cache_path)
    if args.redo:
        cached = {k: v for k, v in cached.items()
                  if not any(rt.key(it) == k and (re.search(args.redo, it["host"]) or re.search(args.redo, it["url"]))
                             for it in inv["items"])}
    pending = rt.todo(inv["items"], cached)
    if args.dry_run:
        noise = sum(1 for it in inv["items"] if rt.noise_reason(it))
        print(f"{len(pending)} items to route ({noise} noise items are proposed as drop without a model call); "
              f"{len(ctx.projects)} projects, {len(ctx.fields)} fields", file=out)
        return 0
    be = rt.backend(yt, cfg, args.model)
    log = lambda m: print(m, file=out, flush=True)  # noqa: E731
    routed, failed = rt.route_items(inv["items"], ctx, be, cache_path, batch=int(cfg["batch"]),
                                    private=cfg["private_hosts"], limit=args.limit,
                                    stack=cfg["stack"], redo=args.redo, log=log)
    html = report.rebuild(out_dir, ctx, inv)
    counts: dict[str, int] = {}
    cache = rt.read_json(cache_path)
    for sg in rt.suggestions(inv["items"], cache, cfg, ctx):
        lab = "not routed" if not sg else sg["route"] + (f" {sg['target']}" if sg.get("target") else "")
        counts[lab] = counts.get(lab, 0) + 1
    print("proposed destinations:", file=out)
    for lab, n in sorted(counts.items(), key=lambda x: -x[1])[:25]:
        print(f"  {n:4d}  {lab}", file=out)
    print(f"routed {routed}, failed {failed}\nwrote {html}\n"
          f"Next: open {html}, accept or move each destination, then Export and run "
          f"`{report.apply_command()}`.", file=out)
    return 0 if not failed else 1


def _newest_decisions() -> Path | None:
    files = sorted((Path.home() / "Downloads").glob("tab-rescue-decisions*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def run_apply(args: argparse.Namespace, out=sys.stdout) -> int:
    from . import apply as ap, route as rt
    out_dir = _out(args.out)
    inv = _inventory(out_dir)
    src = Path(args.decisions).expanduser() if args.decisions else _newest_decisions()
    if not src or not src.exists():
        print("No decisions file. In review.html click 'Export accepted', then run this again "
              "(or pass the file path).", file=sys.stderr)
        return 2
    data = json.loads(src.read_text(encoding="utf-8"))
    decisions = data.get("decisions", [])
    cfg, yt, ctx = _context(required=True)
    log = lambda m: print(m, file=out, flush=True)  # noqa: E731
    print(f"applying {len(decisions)} decisions from {src}" + (" (dry run)" if args.dry_run else ""), file=out)
    counts = ap.apply(decisions, ctx, yt, out_dir, inbox=cfg["inbox_file"], dry=args.dry_run, log=log,
                      stack=rt.stack_links(cfg))
    if args.dry_run:
        return 0
    print(f"applied {sum(counts.values())} new decisions"
          + ("" if counts else " (everything in this file was already applied)"), file=out)
    for line in ap.close_plan(inv["items"], rt.read_json(out_dir / "applied.json")):
        print(line, file=out)
    html = report.rebuild(out_dir, ctx, inv)
    print(f"wrote {html}\nNext: close the groups marked CLOSE; the page's 'Close' step lists what is left.", file=out)
    return 0


def collect(firefox_dirs: list[Path], chromium_dirs: list[tuple[str, Path]]) -> tuple[list[Item], list[dict]]:
    items: list[Item] = []
    sources: list[dict] = []
    for d in firefox_dirs:
        tabs, bms = firefox.load_tabs(d), firefox.load_bookmarks(d)
        items += tabs + bms
        sources.append({"browser": "firefox", "profile": d.name, "tabs": len(tabs), "bookmarks": len(bms)})
    for browser, d in chromium_dirs:
        tabs, bms = chromium.load_tabs(d, browser), chromium.load_bookmarks(d, browser)
        items += tabs + bms
        sources.append({"browser": browser, "profile": d.name, "tabs": len(tabs), "bookmarks": len(bms)})
    return items, sources


def run_scan(args: argparse.Namespace, out=sys.stdout) -> int:
    ff = [Path(p).expanduser() for p in args.firefox]
    ch = [(classify_chromium(Path(p)), Path(p).expanduser()) for p in args.chromium]
    if args.auto:
        aff, ach = discover()
        ff += [p for p in aff if p not in ff]
        ch += [(b, p) for b, p in ach if p not in [q for _, q in ch]]
    if not ff and not ch:
        print("No profiles given or found. Use --auto, --firefox DIR or --chromium DIR.", file=sys.stderr)
        return 2

    items, sources = collect(ff, ch)
    an.analyze(items, stale_days=args.stale_days)

    out_dir = Path(args.out)
    method_note = ""
    vectors = None
    want_embed = args.embed
    if want_embed is None:
        want_embed = embed_mod.model_available(args.ollama)
    elif want_embed and not embed_mod.model_available(args.ollama):
        print("warning: --embed requested but Ollama/nomic-embed-text is not available; using TF-IDF", file=sys.stderr)
        want_embed = False
    t0 = time.time()
    if want_embed and items:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            vectors = embed_mod.embed_texts([item_text(it) for it in items], out_dir / "embed-cache.json", args.ollama)
        except Exception as exc:  # network/model trouble must never lose the scan
            print(f"warning: embedding failed ({type(exc).__name__}); using TF-IDF", file=sys.stderr)
            vectors = None
    t_embed = time.time() - t0
    t0 = time.time()
    clusters, method = cluster_items(items, vectors)
    t_cluster = time.time() - t0
    unsorted_id = next((c["id"] for c in clusters if c.get("unsorted")), None)
    an.mark_unsorted_for_review(items, unsorted_id)
    summary = an.summarize(items)
    summary["clusters"] = sum(1 for c in clusters if not c.get("unsorted"))
    summary["unsorted_items"] = next((c["size"] for c in clusters if c.get("unsorted")), 0)

    ctx = _context(required=False)[2]
    inv_path, html_path = write_outputs(out_dir, items, clusters, summary, method, sources, ctx)

    print(f"sources:", file=out)
    for s in sources:
        print(f"  {s['browser']:9s} {s['profile']:28s} tabs={s['tabs']:5d} bookmarks={s['bookmarks']:5d}", file=out)
    print(f"items: {len(items)} ({summary['tabs']} open tabs, {summary['bookmarks']} bookmarks)", file=out)
    print(f"duplicates: {summary['tab_duplicate_groups']} tab groups ({summary['tab_duplicate_extras']} extra tabs), "
          f"{summary['bookmark_duplicate_groups']} bookmark groups", file=out)
    print(f"temporary tabs: {summary['temporary']}   stale tabs: {summary['stale']}   "
          f"tabs also bookmarked: {summary['already_bookmarked']}", file=out)
    print(f"clusters: {summary['clusters']} (+{summary['unsorted_items']} unsorted items), method={method}, "
          f"embed={t_embed:.1f}s cluster={t_cluster:.1f}s", file=out)
    print(f"recommendations: close={summary['rec_close']} keep={summary['rec_keep']} review={summary['rec_review']}", file=out)
    print(f"wrote {inv_path}\nwrote {html_path}", file=out)
    if not (out_dir / "routes.json").exists():
        print(f"Next: python3 -m tab_rescue route --out {out_dir}", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "scan":
        return run_scan(args)
    if args.command == "route":
        return run_route(args)
    if args.command == "apply":
        return run_apply(args)
    if args.command == "page":
        out_dir = _out(args.out)
        print(f"wrote {report.rebuild(out_dir, _context(required=False)[2], _inventory(out_dir))}")
        return 0
    return 2
