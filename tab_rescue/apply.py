"""Carry out the accepted routes from the review page: write each item to its destination.

This is the only part of tab-rescue that writes outside its output folder, and it never touches a
browser profile. Every write is append/prepend-only into an inbox or list file:

  integrate  <repo>/TAB-INBOX.md (client repos: kept out of git via .git/info/exclude)
             vault Next/Dev - <id>.md when the project has no repo
  backlog    vault Next/Dev - Backlog.md (same line format as yt-extract, so names are shared)
  practice   vault Next/Dev - Practice.md + the field's reading list
  venture-os <venture-os module>/TAB-INBOX.md (m2 = forge)
  intake     yt-extract state/ideas-queue.jsonl  ->  `yt-extract ideas --send`
  learn      vault Knowledge/Reading/<field>.md, linked from Knowledge/<field>.md
  reference  <out>/reference-bookmarks.html (rebuilt from every applied reference item, plus one
             console link per stack service in "Stack")
  video      yt-extract state/queue/<area>.txt: processed on the area's next `yt-extract poll`
             (its timer), like a video in the area playlist; listed in <out>/youtube-playlists.md
  keep       nothing (pinned tabs stay open)

A note typed on the review page goes with the item into every destination.

Applied items are recorded in <out>/applied.json by kind + normalised URL, so applying the same
decisions twice writes nothing new. That record is also what makes a tab "saved -> safe to close".
"""

from __future__ import annotations

import datetime as _dt
import html
import json
from collections import defaultdict
from pathlib import Path

from .route import Context, read_json, write_json

TAG = "Written by tab-rescue."


def _md_link(title: str, url: str) -> str:
    t = (title or url).replace("[", "(").replace("]", ")").strip() or url
    return f"[{t}]({url.replace(' ', '%20').replace(')', '%29')})"


def _prepend(path: Path, header: str, lines: list[str], intro: str = "Newest first.") -> None:
    """Insert lines under the file's intro paragraph (newest first), creating the file if needed."""
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else f"# {header}\n\n{intro} {TAG}\n"
    head, _, rest = old.partition("\n\n")
    first, _, tail = rest.partition("\n")
    path.write_text(f"{head}\n\n{first}\n\n" + "\n".join(lines) + "\n" + tail, encoding="utf-8")


def _exclude(repo: Path, name: str) -> None:
    ex = repo / ".git/info/exclude"
    if ex.parent.is_dir():
        old = ex.read_text() if ex.exists() else ""
        if name not in old.split("\n"):
            ex.write_text(old.rstrip("\n") + ("\n" if old else "") + f"{name}\n")


def _is_repo(p: Path) -> bool:
    return (p / ".git").exists() and p.resolve() != Path.home().resolve()


def _where(d: dict) -> str:
    bits = [d.get("kind", "tab")]
    if d.get("group"):
        bits.append(f"group “{d['group']}”")
    if d.get("folder"):
        bits.append(f"folder “{d['folder']}”")
    return " · ".join(bits)


def _field_file(vault: Path, name: str, safe) -> Path:
    """Knowledge/<field>.md in yt-extract's format, created for a breadth field."""
    p = vault / "Knowledge" / f"{safe(name)}.md"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntype: field\n---\n\n# {name}\n\nConcepts in this field, oldest first. "
                     "A depth session works down this list; a breadth session follows a concept's *Connects to* "
                     "links into another field. Written by yt-extract.\n\n", encoding="utf-8")
    return p


def _link_reading(field_note: Path, name: str, safe) -> None:
    link = f"[[Reading/{safe(name)}|reading queue]]"
    txt = field_note.read_text(encoding="utf-8")
    if link in txt:
        return
    line = f"Tabs to read: {link}\n"
    at = txt.find("\n- ")
    txt = txt[:at + 1] + line + "\n" + txt[at + 1:] if at >= 0 else txt.rstrip("\n") + "\n\n" + line
    field_note.write_text(txt, encoding="utf-8")


def reference_html(entries: list[dict]) -> str:
    """Netscape bookmark file: one folder per reference target ("Stack / Vercel" nests), importable anywhere."""
    tree: dict = {}
    for e in entries:
        node = tree
        for part in [x.strip() for x in (e.get("target") or "Reference").split("/") if x.strip()] or ["Reference"]:
            node = node.setdefault(part, {})
        node.setdefault("", []).append(e)
    esc = lambda s: html.escape(s or "", quote=True)  # noqa: E731
    out = ["<!DOCTYPE NETSCAPE-Bookmark-file-1>",
           '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
           "<TITLE>Bookmarks</TITLE>", "<H1>Bookmarks</H1>", "<DL><p>"]

    def walk(name: str, node: dict, ind: str):
        out.append(f"{ind}<DT><H3>{esc(name)}</H3>")
        out.append(f"{ind}<DL><p>")
        for sub in sorted(k for k in node if k):
            walk(sub, node[sub], ind + "    ")
        for e in sorted(node.get("", []), key=lambda e: (e.get("title") or "").lower()):
            out.append(f'{ind}    <DT><A HREF="{esc(e["url"])}">{esc(e.get("title") or e["url"])}</A>')
            if e.get("note"):
                out.append(f"{ind}    <DD>{esc(e['note'])}")
        out.append(f"{ind}</DL><p>")

    walk("tab-rescue reference", tree, "    ")
    out.append("</DL><p>")
    return "\n".join(out) + "\n"


def videos_md(entries: list[dict], areas: dict[str, str]) -> str:
    """The videos to add to each yt-extract area playlist (adding needs a YouTube login, so it is by hand)."""
    by: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by[e.get("target") or "dump"].append(e)
    out = ["# Videos for yt-extract", "",
           "Queued in yt-extract's state/queue/<area>.txt: the area's next scheduled `yt-extract poll` processes them "
           "like videos in its playlist. Run it now with `yt-extract poll <area>`. Written by tab-rescue.", ""]
    for area in sorted(by):
        pl = areas.get(area, "")
        out.append(f"## {area}" + (f" · [playlist]({pl})" if pl else ""))
        out.append("")
        for e in by[area]:
            out.append(f"- [ ] {_md_link(e.get('title', ''), e['url'])}" + (f" · {e['note']}" if e.get("note") else ""))
        out.append("")
    return "\n".join(out)


def apply(decisions: list[dict], ctx: Context, yt, out_dir: Path, *, inbox: str = "TAB-INBOX.md",
          dry: bool = False, log=print, stack: list[dict] | None = None) -> dict[str, int]:
    """Write every decision that isn't applied yet. Returns counts per destination label."""
    safe = yt.render.safe
    state_path = out_dir / "applied.json"
    state = read_json(state_path)
    projs = {p["id"]: p for p in ctx.projects}
    vault = ctx.vault
    today = _dt.date.today().isoformat()
    repos: dict[Path, tuple[bool, list[str]]] = {}
    boards: dict[str, list[str]] = defaultdict(list)
    reading: dict[str, list[str]] = defaultdict(list)
    ideas: list[dict] = []
    counts: dict[str, int] = defaultdict(int)
    new_state = dict(state)

    for d in decisions:
        k = f"{d['kind']}|{d['norm']}"
        route, target = d["route"], d.get("target", "")
        prev = state.get(k)
        if prev and prev.get("route") == route and prev.get("target", "") == target:
            continue
        link = _md_link(d.get("title", ""), d["url"])
        note = " ".join(str(d.get("note") or "").split())
        step = d.get("next_step") or ""
        if note:     # the reader's own words come first; the model's next step stays as a hint
            step = f"{note}" + (f" (suggested: {step})" if step else "")
        why = f"\n  - Why: {d['why']}" if d.get("why") and d.get("why") != "moved by hand" else ""
        src = f"\n  - From: {_where(d)}, {today}"
        if route == "integrate" and target in projs:
            p = projs[target]
            if _is_repo(p["path"]):
                repos.setdefault(p["path"], (p["sensitive"], []))[1].append(
                    f"- [ ] **{link}**" + (f": {step}" if step else "") + why + src)
                label = f"{target} repo"
            else:
                boards[f"Dev - {target}"].append(f"- [ ] {link}" + (f": {step}" if step else ""))
                label = f"vault Next/Dev - {target}"
        elif route == "backlog":
            boards["Dev - Backlog"].append(f"- [ ] **{target or 'Unsorted'}**: {link}" + (f": {step}" if step else ""))
            label = f"backlog: {target}"
        elif route == "practice":
            fld = d.get("field") or "Unfiled"
            boards["Dev - Practice"].append(f"- [ ] {link} ({fld})" + (f": {step}" if step else ""))
            reading[fld].append(f"- [ ] {link} · practice" + (f": {step}" if step else ""))
            label = f"practice · {fld}"
        elif route == "venture-os" and target in ctx.modules:
            sub = yt.route.MODULES[target][1]
            vos = Path(ctx.rcfg.get("venture_os", "~/Dev/projects/venture-os")).expanduser()
            folder = Path(ctx.rcfg.get("forge", "~/Dev/projects/forge")).expanduser() if target == "m2" else vos / (sub or "")
            line = f"- [ ] **{link}**" + (f": {step}" if step else "") + why + src
            if folder.is_dir():
                repos.setdefault(folder, (False, []))[1].append(line)
            else:
                boards[f"Business - {target}"].append(line)
            label = f"venture-os {target.upper()}"
        elif route == "intake":
            ideas.append(d)
            label = "idea intake"
        elif route == "learn":
            fld = d.get("field") or "Unfiled"
            mode = d.get("mode") or "depth"
            tag = f"bridge → {d['bridge_to']}" if mode == "bridge" and d.get("bridge_to") else mode
            reading[fld].append(f"- [ ] {link} · {tag}" + (f": {step}" if step else ""))
            label = f"knowledge · {fld}"
        elif route == "reference":
            label = f"reference · {target or 'Reference'}"
        elif route == "video":
            target = target or "dump"
            label = f"youtube · {target}"
        elif route == "keep":
            target = ""
            label = "kept open (pinned)"
        else:
            route, target = "drop", ""
            label = "dropped"
        counts[label] += 1
        new_state[k] = {"route": route, "target": target, "field": d.get("field", ""), "date": today,
                        "url": d["url"], "title": d.get("title", "")}
        if note:
            new_state[k]["note"] = note

    if dry:
        for label, n in sorted(counts.items(), key=lambda x: -x[1]):
            log(f"  would apply {n:4d}  {label}")
        return dict(counts)

    out_dir.mkdir(parents=True, exist_ok=True)
    for path, (sensitive, lines) in repos.items():
        if sensitive:
            _exclude(path, inbox)
        _prepend(path / inbox, "Tab inbox", lines,
                 "Newest first. Tabs and bookmarks routed here from the browser; tick or delete when done.")
        log(f"  wrote {len(lines):3d} → {path / inbox}")
    for name, lines in boards.items():
        p = vault / "Next" / f"{safe(name)}.md"
        _prepend(p, name, lines)
        log(f"  wrote {len(lines):3d} → {p}")
    for fld, lines in reading.items():
        note = _field_file(vault, fld, safe)
        p = vault / "Knowledge" / "Reading" / f"{safe(fld)}.md"
        _prepend(p, f"{fld}: reading queue", lines,
                 f"Newest first. Tabs worth reading for [[{safe(fld)}]]: depth deepens it, bridge links it to "
                 "another field, breadth opened it.")
        _link_reading(note, fld, safe)
        log(f"  wrote {len(lines):3d} → {p}")
    if ideas:
        log(f"  queued {_queue_ideas(ideas, yt)} new idea(s) → {yt.ideas.QUEUE}  (send with: yt-extract ideas --send)")
    refs = [v for v in new_state.values() if v.get("route") == "reference"]
    if refs:
        p = out_dir / "reference-bookmarks.html"
        p.write_text(reference_html(refs + list(stack or [])), encoding="utf-8")
        log(f"  wrote {len(refs):3d} → {p}  (import it in the browser's bookmark manager)")
    vids = [v for v in new_state.values() if v.get("route") == "video"]
    if vids:
        n = queue_videos(vids, yt)
        if n:
            log(f"  queued {n:3d} → {Path(yt.root) / 'state/queue'}  (yt-extract poll <area> processes them)")
        p = out_dir / "youtube-playlists.md"
        p.write_text(videos_md(vids, getattr(ctx, "areas", {})), encoding="utf-8")
        log(f"  wrote {len(vids):3d} → {p}  (what was queued for yt-extract, per area)")
    write_json(state_path, new_state)
    return dict(counts)


def queue_videos(entries: list[dict], yt) -> int:
    """Append videos to yt-extract's per-area queue (state/queue/<area>.txt). Idempotent per URL."""
    root = getattr(yt, "root", None)
    if not root:
        return 0
    qdir = Path(root) / "state" / "queue"
    n = 0
    by: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by[e.get("target") or "dump"].append(e)
    for area, es in by.items():
        p = qdir / f"{area}.txt"
        old = p.read_text(encoding="utf-8") if p.exists() else ""
        have = {ln.split("\t", 1)[0] for ln in old.splitlines()}
        new = [f"{e['url']}\t{' '.join((e.get('title') or '').split())}" for e in es if e["url"] not in have]
        if new:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(old + "".join(x + "\n" for x in new), encoding="utf-8")
            n += len(new)
    return n


def _queue_ideas(decisions: list[dict], yt) -> int:
    q = yt.ideas
    known = {x["key"] for x in q._read(q.QUEUE) + q._read(q.SENT)}
    q.QUEUE.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with q.QUEUE.open("a", encoding="utf-8") as f:
        for d in decisions:
            k = q._key(d["url"], d.get("title", ""))
            if k in known:
                continue
            f.write(json.dumps({"key": k, "title": d.get("title", ""), "one_liner": d.get("one_liner", ""),
                                "detail": " ".join(x for x in (d.get("note", ""), d.get("why", "")) if x and x != "moved by hand"), "quote": d.get("title", ""), "video": d.get("title", ""),
                                "url": d["url"], "origin": "browser tab"}, ensure_ascii=False) + "\n")
            n += 1
    return n


ROOT_FOLDERS = {"Bookmarks Toolbar", "Bookmarks Menu", "Other Bookmarks", "Mobile Bookmarks", "Bookmarks Bar",
                "Other bookmarks", "Mobile bookmarks", "(top level)"}


def close_plan(items: list[dict], applied: dict) -> list[str]:
    """Which tab groups / bookmark folders are fully saved (safe to close or delete) and what is left."""
    tabs: dict[str, list[dict]] = defaultdict(list)
    bms: dict[str, list[dict]] = defaultdict(list)
    bm_norm = {it["norm_url"] for it in items if it["kind"] == "bookmark"}
    for it in items:
        (tabs[it.get("group") or "(no group)"] if it["kind"] == "tab" else bms[it.get("folder") or "(top level)"]).append(it)

    def saved(it):
        return f"{it['kind']}|{it['norm_url']}" in applied or (it["kind"] == "tab" and it["norm_url"] in bm_norm)

    out = ["Open tabs by group (saved = written to its destination, or already bookmarked):"]
    for g in sorted(tabs, key=lambda g: -len(tabs[g])):
        n, s = len(tabs[g]), sum(saved(it) for it in tabs[g])
        out.append(f"  {'CLOSE ' if s == n else '      '}{g}: {s}/{n} saved")
    full = [f for f in bms if all(saved(it) for it in bms[f])]
    out.append(f"Bookmark folders fully routed (after importing reference-bookmarks.html): {len(full)} of {len(bms)}")
    for f in sorted(full):
        if f in ROOT_FOLDERS:
            out.append(f"  EMPTY  {f}  (built-in folder: delete the loose bookmarks in it, keep the folder)")
        elif any(g.startswith(f + " / ") and g not in full for g in bms):
            out.append(f"  EMPTY  {f}  (a subfolder still has undecided bookmarks)")
        elif not any(f.startswith(g + " / ") for g in full if g not in ROOT_FOLDERS):
            out.append(f"  DELETE {f}" + ("  (with its subfolders)" if any(g.startswith(f + " / ") for g in full) else ""))
    return out
