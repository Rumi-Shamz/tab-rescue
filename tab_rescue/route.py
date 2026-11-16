"""Give every tab and bookmark one destination: a project, Venture OS, the knowledge vault, a
reference folder, or drop.

The destinations, the project registry and the model backends are yt-extract's (imported, not
copied), so tabs and YouTube videos land in the same inboxes and knowledge fields:

  integrate  -> <repo>/TAB-INBOX.md of a current project (star-sort projects.json + discovered repos)
  backlog    -> vault Next/Dev - Backlog.md          practice -> vault Next/Dev - Practice.md
  venture-os -> <module>/TAB-INBOX.md                intake   -> yt-extract's idea queue -> Coda
  learn      -> vault Knowledge/Reading/<field>.md (depth / bridge / breadth)
  reference  -> out/reference-bookmarks.html, one folder per target (import into the browser)
  drop       -> nothing; the tab can be closed

Noise found by the analysis (duplicates, search/login/internal pages, tabs already bookmarked) is
proposed as `drop` without a model call. Everything else goes to the model in batches of titles,
hosts and URL paths (never page content, never query strings). Titles on private hosts (mail,
banking, payments) are withheld. Only Claude or a local Ollama model may be used, as in yt-extract.
Suggestions are cached in <out>/routes.json by kind + normalised URL, so a re-scan only routes new
items.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "tab-rescue.toml"
SYSTEM = (Path(__file__).with_name("prompts") / "route.md").read_text(encoding="utf-8")

ROUTES = ("integrate", "backlog", "practice", "venture-os", "intake", "learn", "reference", "drop")
MODES = ("depth", "bridge", "breadth")
ALLOWED_BACKENDS = ("claude", "ollama")
DEFAULTS = {
    "yt_extract": "~/Dev/projects/yt-extract",
    "inbox_file": "TAB-INBOX.md",
    "model": "",                 # empty: yt-extract's [routing] model, else its models.toml default
    "batch": 50,
    # Services you build with. Their consoles, settings and plan pages are not kept one by one: the
    # reference folder "Stack" gets one link per service (its console), and the pages are dropped.
    # Docs and guides of the service are routed like any other page. Value: a host regex, or a table
    # with host, console (the one link to keep) and path (only these paths count as console pages).
    "stack": {
        "Cloudflare": {"host": r"(^|\.)cloudflare\.com$", "console": "https://dash.cloudflare.com/"},
        "Resend": {"host": r"(^|\.)resend\.com$", "console": "https://resend.com/emails"},
        "Vercel": {"host": r"(^|\.)vercel\.com$", "console": "https://vercel.com/dashboard"},
        "Supabase": {"host": r"(^|\.)supabase\.com$", "console": "https://supabase.com/dashboard/projects"},
        "GitHub": {"host": r"^github\.com$", "console": "https://github.com/",
                   "path": r"^/?$|^/(settings|notifications|dashboard|codespaces|organizations|account|billing|sponsors)(/|$)"},
        "Google Cloud": {"host": r"^console\.cloud\.google\.com$", "console": "https://console.cloud.google.com/"},
    },
    # Knowledge fields folded into a broader one (old name = new name). Applied to the proposals.
    "merge_fields": {},
    "private_hosts": [r"^mail\.", r"^outlook\.", r"^webmail\.", r"bank", r"^(.+\.)?paypal\.",
                      r"^(.+\.)?revolut\.", r"^(.+\.)?wise\.com$", r"^(.+\.)?stripe\.com$"],
}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["routes"],
    "properties": {"routes": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["i", "route", "target", "field", "mode", "bridge_to", "next_step", "why", "one_liner"],
        "properties": {
            "i": {"type": "integer"},
            "route": {"type": "string", "enum": list(ROUTES)},
            "target": {"type": "string"},
            "field": {"type": "string"},
            "mode": {"type": "string", "enum": ["", *MODES]},
            "bridge_to": {"type": "string"},
            "next_step": {"type": "string"},
            "why": {"type": "string"},
            "one_liner": {"type": "string"},
        }}}},
}


# --- config and the yt-extract link -------------------------------------------------------------

def load_config(path: Path | None = None) -> dict:
    p = path or CONFIG
    cfg = dict(DEFAULTS)
    if p.exists():
        cfg.update(tomllib.loads(p.read_text(encoding="utf-8")))
    return cfg


def yt_extract(cfg: dict) -> SimpleNamespace:
    """Import yt-extract from its checkout (it is not an installed package)."""
    root = Path(cfg["yt_extract"]).expanduser().resolve()
    if not (root / "yt_extract" / "route.py").exists():
        raise SystemExit(f"yt-extract not found at {root} (set yt_extract in tab-rescue.toml)")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from yt_extract import backends, ideas, render, route  # noqa: PLC0415
    ycfg_path = root / "config.toml"
    ycfg = tomllib.loads(ycfg_path.read_text()) if ycfg_path.exists() else {}
    return SimpleNamespace(root=root, backends=backends, ideas=ideas, render=render, route=route, cfg=ycfg)


@dataclass
class Context:
    """Everything the router and the writers need to know about the reader's destinations."""
    projects: list[dict]
    modules: dict[str, str]                     # m1 -> description
    vault: Path
    fields: list[str] = field(default_factory=list)
    backlog: list[str] = field(default_factory=list)
    rcfg: dict = field(default_factory=dict)    # yt-extract [routing]
    areas: dict[str, str] = field(default_factory=dict)   # yt-extract area -> playlist URL

    @property
    def project_ids(self) -> set[str]:
        return {p["id"] for p in self.projects}


def context(yt: SimpleNamespace) -> Context:
    rcfg = yt.cfg.get("routing", {})
    vault = Path(yt.cfg.get("vault", "~/Documents/Obsidian Vault")).expanduser()
    return Context(projects=yt.route.projects(rcfg),
                   modules={m: desc for m, (desc, _) in yt.route.MODULES.items()},
                   vault=vault, fields=yt.route.fields(vault), backlog=yt.route.backlog_names(vault), rcfg=rcfg,
                   areas={n: a.get("playlist", "") for n, a in yt.cfg.get("areas", {}).items()})


def backend(yt: SimpleNamespace, cfg: dict, alias: str | None = None):
    default, reg = yt.backends.registry()
    name = alias or cfg.get("model") or yt.cfg.get("routing", {}).get("model") or default
    if name not in reg:
        raise SystemExit(f"unknown model {name!r}; yt-extract models.toml has: {', '.join(reg)}")
    b = reg[name]
    if b.kind not in ALLOWED_BACKENDS:
        raise SystemExit(f"{name} is a {b.kind} model; browsing history may only go to Claude or a local model")
    return b


# --- items ----------------------------------------------------------------------------------------

def key(it: dict) -> str:
    return f"{it['kind']}|{it['norm_url']}"


NOISE_CATEGORIES = (            # first match wins; shown as filters and as one question each
    ("duplicate tab", "Duplicate tabs"),
    ("already bookmarked", "Already bookmarked"),
    ("duplicate bookmark", "Duplicate bookmarks"),
    ("search results", "Search results"),
    ("login", "Login and checkout pages"),
    ("browser internal", "Browser pages"),
    ("local development", "Local and file pages"),
    ("empty title", "Pages without a title"),
)


def noise_reason(it: dict) -> str:
    """Why an item is proposed as `drop` without asking the model ('' = route it)."""
    if it.get("pinned"):
        return ""
    if it["kind"] == "tab" and it["rec"] == "close":
        return it["reason"]
    if it["kind"] == "bookmark" and it.get("dup_group") and not it.get("dup_primary"):
        return it["reason"]
    if not it["norm_url"].startswith(("http:", "https:")):
        return "not a web page"
    return ""


def noise_category(reason: str) -> str:
    low = reason.lower()
    return next((label for needle, label in NOISE_CATEGORIES if needle in low), "Not a web page")


DOC_PATH = r"^/(docs|blog|guides?|changelog|learn|customers|templates|examples)(/|$)"


def stack_rules(stack: dict | None) -> dict[str, dict]:
    """[stack] entries as {name: {host, console, path}}; a plain string is the host regex."""
    out = {}
    for name, v in (stack or {}).items():
        v = {"host": v} if isinstance(v, str) else dict(v)
        out[name] = {"host": v.get("host", ""), "console": v.get("console", ""), "path": v.get("path", "")}
    return out


def stack_service(host: str, stack: dict | None, path: str = "/") -> str:
    """The stack service whose console this page belongs to ('' for docs, blogs and other hosts)."""
    if re.search(DOC_PATH, path or "/"):
        return ""
    for name, r in stack_rules(stack).items():
        if r["host"] and re.search(r["host"], host or "") and (not r["path"] or re.search(r["path"], path or "/")):
            return name
    return ""


def is_private(host: str, patterns: list[str]) -> bool:
    return any(re.search(p, host or "") for p in patterns)


def describe(i: int, it: dict, private: list[str], stack: dict | None = None) -> str:
    parts = urlsplit(it["url"])
    path = parts.path if len(parts.path) <= 90 else parts.path[:87] + "..."
    d = {"i": i, "kind": it["kind"], "title": "(private)" if is_private(it["host"], private) else it["title"][:160],
         "host": it["host"], "path": path}
    for k in ("group", "folder"):
        if it.get(k):
            d[k] = it[k]
    svc = stack_service(it["host"], stack or {}, parts.path)
    if svc:
        d["stack"] = svc
    return json.dumps(d, ensure_ascii=False)


def used(cache: dict) -> dict[str, list[str]]:
    """Names earlier batches already proposed, so later batches reuse them instead of inventing synonyms."""
    out: dict[str, set] = {"fields": set(), "backlog": set(), "reference": set()}
    for r in cache.values():
        if r.get("field"):
            out["fields"].add(r["field"])
        if r.get("route") in ("backlog", "reference") and r.get("target"):
            out[r["route"]].add(r["target"])
    return {k: sorted(v) for k, v in out.items()}


def user_prompt(batch: list[dict], ctx: Context, private: list[str], seen: dict | None = None,
                stack: dict | None = None) -> str:
    seen = seen or {"fields": [], "backlog": [], "reference": []}
    new_fields = [f for f in seen["fields"] if f not in ctx.fields]
    out = ["## Projects (id: description; needs)"]
    out += [f"- {p['id']}: {p['label']}" + (f"; needs: {', '.join(p['needs'][:20])}" if p["needs"] else "")
            for p in ctx.projects]
    out += ["", "## Venture OS modules"] + [f"- {m}: {d}" for m, d in ctx.modules.items()]
    out += ["", "## Existing fields", ", ".join(ctx.fields) or "(none)",
            "", "## Fields already opened for other tabs (reuse before inventing; mode stays breadth)",
            ", ".join(new_fields) or "(none)",
            "", "## Existing backlog projects", ", ".join(sorted(set(ctx.backlog) | set(seen["backlog"]))) or "(none)",
            "", "## Reference folders already used", ", ".join(seen["reference"]) or "(none)",
            "", "## Stack (services the reader uses across projects)", ", ".join(stack or {}) or "(none)",
            "", "## Items"]
    out += [describe(i, it, private, stack) for i, it in enumerate(batch)]
    return "\n".join(out)


def _title(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()[:n]
    return " ".join(w[:1].upper() + w[1:] for w in s.split(" ")) if s else ""


def clean(r: dict, ctx: Context) -> dict:
    """Coerce one model answer into a valid route (unknown project -> backlog, etc.)."""
    route = r.get("route") if r.get("route") in ROUTES else "drop"
    target = str(r.get("target", "")).strip()
    fld, mode, bridge = _title(r.get("field"), 40), str(r.get("mode", "")), _title(r.get("bridge_to"), 40)
    if route == "integrate" and target not in ctx.project_ids:
        low = {p.lower(): p for p in ctx.project_ids}
        if target.lower() in low:
            target = low[target.lower()]
        else:
            route, target = "backlog", target or "Unsorted"
    if route == "venture-os":
        target = target.lower() if target.lower() in ctx.modules else "m1"
    if route == "reference":
        parts = [x.strip() for x in target.split("/") if x.strip()][:2]
        target = " / ".join(x if x in ctx.project_ids else _title(x, 30) for x in parts) or "Reference"
    if route == "backlog":
        target = target[:40] or "Unsorted"
    if route not in ("integrate", "backlog", "venture-os", "reference"):
        target = ""
    if route in ("learn", "practice"):
        fld = fld or "Unfiled"
        known = {f.lower(): f for f in ctx.fields}
        fld = known.get(fld.lower(), fld)
        bridge = known.get(bridge.lower(), "") if bridge else ""
        if fld.lower() not in known:
            mode, bridge = "breadth", ""
        elif mode == "bridge" and bridge and bridge != fld:
            pass
        else:
            mode, bridge = "depth", ""
    else:
        fld, mode, bridge = "", "", ""
    return {"route": route, "target": target, "field": fld, "mode": mode, "bridge_to": bridge,
            "next_step": "" if route in ("drop", "reference") else str(r.get("next_step", "")).strip(),
            "why": str(r.get("why", "")).strip(),
            "one_liner": str(r.get("one_liner", "")).strip() if route == "intake" else ""}


YOUTUBE = r"^(www\.|m\.)?(youtube\.com|youtu\.be)$"
AREA_OF = {"integrate": "dev", "backlog": "dev", "practice": "dev", "venture-os": "business", "intake": "business",
           "learn": "knowledge", "reference": "dump"}
CONSOLE_HOST = r"^(console|dash|dashboard|app|admin|account|my)\."


def is_video(it: dict) -> bool:
    parts = urlsplit(it["url"])
    return bool(re.search(YOUTUBE, parts.hostname or "")) and (
        parts.path.startswith(("/watch", "/shorts/", "/live/")) or (parts.hostname or "").endswith("youtu.be"))


def is_self(it: dict) -> bool:
    """The review page itself, open while you use it."""
    return it["url"].startswith("file://") and it["url"].split("#")[0].endswith("/review.html") and "tab-rescue" in it["url"]


def suggestion(it: dict, cache: dict, cfg: dict | None = None, ctx: Context | None = None) -> dict | None:
    """The proposed route for one inventory item: pinned -> keep, noise -> drop, else the model's answer
    with the reader's rules on top (YouTube videos -> a yt-extract playlist, merged knowledge fields)."""
    if it["kind"] == "tab" and (it.get("pinned") or is_self(it)):
        why = "pinned tab: stays open" if it.get("pinned") else "this review page: stays open while you use it"
        return {"route": "keep", "target": "", "why": why, "fixed": True}
    reason = noise_reason(it)
    if reason:
        return {"route": "drop", "target": "", "why": reason, "noise": True, "category": noise_category(reason)}
    sg = cache.get(key(it))
    if not sg:
        return None
    sg = dict(sg)
    merge = {k.lower(): v for k, v in ((cfg or {}).get("merge_fields") or {}).items()}
    if merge and sg.get("field"):
        known = set(getattr(ctx, "fields", None) or [])
        sg["field"] = merge.get(sg["field"].lower(), sg["field"])
        if sg.get("bridge_to"):
            sg["bridge_to"] = merge.get(sg["bridge_to"].lower(), sg["bridge_to"])
            if sg["bridge_to"] == sg["field"]:
                sg["mode"], sg["bridge_to"] = "depth", ""
        if sg.get("mode") == "breadth" and sg["field"] in known:
            sg["mode"] = "depth"
    areas = getattr(ctx, "areas", None) or {}
    if areas and sg["route"] not in ("drop", "keep") and is_video(it):
        area = AREA_OF.get(sg["route"], "dump")
        area = area if area in areas else ("dump" if "dump" in areas else next(iter(areas)))
        sg = {"route": "video", "target": area, "next_step": sg.get("next_step", ""), "why": sg.get("why", "")}
    return sg


def suggestions(items: list[dict], cache: dict, cfg: dict | None = None, ctx: Context | None = None) -> list[dict | None]:
    """suggestion() for every item, plus the rules that need to see all items at once:

    * stack consoles: the reference folder "Stack" keeps one link per service (its console URL), so
      every console, settings or plan page of that service is proposed as drop;
    * other consoles (console.*, dash.*, app.* ...): one bookmark per host is enough.
    """
    stack = stack_rules((cfg or {}).get("stack", DEFAULTS["stack"]))
    out, seen_console = [], set()
    for it in items:
        sg = suggestion(it, cache, cfg, ctx)
        if sg and sg["route"] == "reference":
            svc = stack_service(it["host"], stack, urlsplit(it["url"]).path)
            if svc in stack and stack[svc]["console"]:
                sg = {"route": "drop", "target": "", "noise": True, "category": "Stack console pages",
                      "why": f"the Stack folder keeps one {svc} link ({stack[svc]['console']}); this page is inside it"}
            elif re.search(CONSOLE_HOST, it["host"]):
                if it["host"] in seen_console:
                    sg = {"route": "drop", "target": "", "noise": True, "category": "Same console twice",
                          "why": f"another page of {it['host']}; one bookmark of it is kept"}
                seen_console.add(it["host"])
        out.append(sg)
    return out


def stack_links(cfg: dict | None) -> list[dict]:
    """The one console link per stack service that the reference bookmarks always contain."""
    return [{"title": name, "url": r["console"], "target": "Stack"}
            for name, r in stack_rules((cfg or {}).get("stack", DEFAULTS["stack"])).items() if r["console"]]


# --- cache ----------------------------------------------------------------------------------------

def read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def todo(items: list[dict], cache: dict) -> list[dict]:
    """Items still waiting for the model, one per key, ordered so related items share a batch."""
    seen, out = set(), []
    for it in items:
        k = key(it)
        if k in seen or k in cache or noise_reason(it) or (it.get("pinned") and it["kind"] == "tab"):
            continue
        seen.add(k)
        out.append(it)
    out.sort(key=lambda it: (it.get("group") or "~", it.get("folder") or "~", it["host"]))
    return out


def route_items(items: list[dict], ctx: Context, be, cache_path: Path, *, batch: int = 50,
                private: list[str] | None = None, limit: int | None = None, stack: dict | None = None,
                redo: str | None = None, log=print) -> tuple[int, int]:
    """Ask the model for every unrouted item; returns (routed, failed). The cache is saved per batch.

    Batches run one after another: each one sees the fields, backlog names and reference folders the
    earlier ones proposed, which keeps the vault from filling up with synonyms.
    """
    private = private if private is not None else DEFAULTS["private_hosts"]
    stack = stack if stack is not None else DEFAULTS["stack"]
    cache = read_json(cache_path)
    if redo:
        drop = {key(it) for it in items if re.search(redo, it["host"]) or re.search(redo, it["url"])}
        cache = {k: v for k, v in cache.items() if k not in drop}
    pending = todo(items, cache)
    if limit is not None:
        pending = pending[:limit]
    batches = [pending[i:i + batch] for i in range(0, len(pending), batch)]
    log(f"[route] {len(pending)} items to route in {len(batches)} batches · {len(ctx.projects)} projects · "
        f"{len(ctx.fields)} fields · model {be.alias}")
    today = _dt.date.today().isoformat()
    routed = failed = 0
    for n, b in enumerate(batches, 1):
        res = be.run(SYSTEM, user_prompt(b, ctx, private, used(cache), stack), SCHEMA)
        if not res.data:
            failed += len(b)
            log(f"  batch {n}/{len(batches)}: FAILED {res.error[:200]}")
            continue
        by_i = {x.get("i"): x for x in res.data.get("routes", []) if isinstance(x, dict)}
        missing = 0
        for i, it in enumerate(b):
            if i not in by_i:
                missing += 1
                continue
            r = clean(by_i[i], ctx)
            svc = stack_service(it["host"], stack, urlsplit(it["url"]).path)
            if svc and r["route"] == "reference":      # every console of a stack service in one place
                r["target"] = f"Stack / {svc}"
            cache[key(it)] = {**r, "model": be.alias, "date": today}
            routed += 1
        failed += missing
        write_json(cache_path, cache)
        log(f"  batch {n}/{len(batches)}: {len(b) - missing} routed" + (f", {missing} missing" if missing else "")
            + f" ({res.seconds}s)")
    return routed, failed
