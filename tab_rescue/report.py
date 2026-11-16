"""Write inventory.json and the self-contained review.html."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from pathlib import Path

from .model import Item, KIND_TAB
from .route import REPO, key, load_config, read_json, suggestions

TEMPLATE = Path(__file__).with_name("review_template.html")
MARKER = "/*__TAB_RESCUE_DATA__*/null"


def scan_id(items: list[Item]) -> str:
    h = hashlib.sha1()
    for i in sorted(it.id for it in items):
        h.update(i.encode())
    return h.hexdigest()[:10]


def _as_dict(it) -> dict:
    return it.to_dict() if isinstance(it, Item) else it


def compact_suggestion(sg: dict | None) -> dict | None:
    if not sg:
        return None
    d = {"r": sg["route"]}
    for k, short in (("target", "t"), ("field", "f"), ("mode", "m"), ("bridge_to", "b"), ("next_step", "x"),
                     ("why", "y"), ("one_liner", "o"), ("category", "g")):
        if sg.get(k):
            d[short] = sg[k]
    if sg.get("noise"):
        d["z"] = 1
    return d


def compact_item(it, sg: dict | None = None, applied: dict | None = None, ex: dict | None = None) -> dict:
    it = _as_dict(it)
    d = {
        "i": it["id"], "u": it["url"], "t": it["title"], "h": it["host"], "n": it["norm_url"],
        "k": "t" if it["kind"] == KIND_TAB else "b", "br": it["browser"], "pr": it["profile"], "w": it["window"],
        "c": it["cluster"], "r": it["rec"], "y": it["reason"],
    }
    for src, dst in (("folder", "f"), ("group", "g")):
        if it.get(src):
            d[dst] = it[src]
    if it.get("dup_group"):
        d["dg"] = it["dup_group"]
        d["dp"] = 1 if it["dup_primary"] else 0
        d["ds"] = it["dup_size"]
    if it.get("temp_reasons"):
        d["tm"] = it["temp_reasons"]
    if it.get("stale_days") is not None:
        d["sd"] = it["stale_days"]
    if it.get("bookmarked"):
        d["bm"] = 1
    if it.get("pinned"):
        d["pin"] = 1
    sg = compact_suggestion(sg)
    if sg:
        d["s"] = sg
    if ex and it["id"] in ex:
        d["ex"] = ex[it["id"]]
    ap = (applied or {}).get(key(it))
    if ap:
        d["ap"] = {"r": ap["route"], "t": ap.get("target", ""), "f": ap.get("field", "")}
    return d


def build_payload(items, clusters: list[dict], summary: dict, method: str, sid: str,
                  routes: dict | None = None, applied: dict | None = None, ctx=None, cfg: dict | None = None,
                  ex: dict | None = None) -> dict:
    items = [_as_dict(it) for it in items]
    sgs = suggestions(items, routes or {}, cfg, ctx)
    return {
        "scanId": sid,
        "generated": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "method": method,
        "summary": summary,
        "clusters": clusters,
        "ctx": ctx_payload(ctx),
        "cmd": apply_command(),
        "items": [compact_item(it, sg, applied, ex) for it, sg in zip(items, sgs)],
    }


def safe_json_for_html(obj) -> str:
    """JSON that can sit inside <script> without any chance of closing it."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (s.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
             .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_html(payload: dict) -> str:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    if MARKER not in tpl:
        raise RuntimeError("template marker missing")
    return tpl.replace(MARKER, safe_json_for_html(payload), 1)


def ctx_payload(ctx) -> dict:
    """What the page needs to offer every destination in its "Move to" menu."""
    if ctx is None:
        return {}
    if isinstance(ctx, dict):
        return ctx
    return {"projects": [{"id": p["id"], "label": p["label"]} for p in ctx.projects],
            "modules": ctx.modules, "fields": ctx.fields, "backlog": ctx.backlog,
            "areas": {n: bool(u) for n, u in getattr(ctx, "areas", {}).items()}}


def apply_command() -> str:
    """The apply command as the person types it, from any folder."""
    launcher = REPO / "bin" / "tab-rescue"
    home = str(Path.home())
    path = str(launcher)
    return ("~" + path[len(home):] if path.startswith(home + "/") else path) + " apply"


def last_export(sid: str) -> dict:
    """id -> "route|target|field" from the newest exported decisions of this scan.

    The page compares it with the current proposal: an item accepted before the proposal changed
    (a re-route, a merged field) is asked again instead of being exported silently."""
    files = sorted((Path.home() / "Downloads").glob("tab-rescue-decisions*.json"), key=lambda p: p.stat().st_mtime)
    for f in reversed(files):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("scanId") != sid:
            continue
        return {d["id"]: f"{d.get('route', '')}|{d.get('target', '')}|{d.get('field', '')}"
                for d in data.get("decisions", []) if d.get("id") and d.get("why") != "moved by hand"}
    return {}


def write_outputs(out_dir: Path, items: list[Item], clusters: list[dict], summary: dict,
                  method: str, sources: list[dict], ctx=None) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    sid = scan_id(items)
    inv = {
        "scan_id": sid,
        "generated": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "cluster_method": method,
        "sources": sources,
        "summary": summary,
        "clusters": clusters,
        "items": [it.to_dict() for it in items],
    }
    inv_path = out_dir / "inventory.json"
    inv_path.write_text(json.dumps(inv, ensure_ascii=False, indent=1), encoding="utf-8")
    return inv_path, rebuild(out_dir, ctx, inv)


def rebuild(out_dir: Path, ctx=None, inv: dict | None = None) -> Path:
    """(Re)write review.html from inventory.json + routes.json + applied.json in ``out_dir``."""
    inv = inv or json.loads((out_dir / "inventory.json").read_text(encoding="utf-8"))
    payload = build_payload(inv["items"], inv["clusters"], inv["summary"], inv["cluster_method"], inv["scan_id"],
                            read_json(out_dir / "routes.json"), read_json(out_dir / "applied.json"), ctx,
                            load_config(), last_export(inv["scan_id"]))
    html_path = out_dir / "review.html"
    html_path.write_text(render_html(payload), encoding="utf-8")
    return html_path
