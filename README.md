# tab-rescue

Turns open tabs and bookmarks (Firefox, Chromium/Chrome/Brave) into next actions. Every item gets
one destination: a project inbox, Venture OS, a knowledge field in the vault, a clean reference
bookmark folder, or drop. Once it's written there, the tab can be closed.

```
bin/tab-rescue scan --auto     # 1. read browsers (read-only) -> out/inventory.json + review.html
bin/tab-rescue route           # 2. propose a destination per item (one model call per 50 items)
#   open out/review.html: one question at a time ("Send these 20 to the <project> inbox?").
#   Untick a row and it comes back at the end on its own; "Change / note" moves one row or adds
#   your own note (it goes with the item to its destination); Back (Backspace) undoes an answer.
#   "Show overview" switches to the full dashboard (sidebar, noise filters, every destination).
bin/tab-rescue apply           # 3. write the newest ~/Downloads/tab-rescue-decisions*.json
#   reload review.html, step 4 "Close tabs" shows which tab groups / folders are fully saved
bin/tab-rescue page            # rebuild review.html (after editing tab-rescue.toml); no model call
```

`bin/tab-rescue` works from any folder (link it into `~/.local/bin` if you like); inside this folder
`python3 -m tab_rescue` does the same.

| Route | Lands in |
|---|---|
| `integrate` → project | `<repo>/TAB-INBOX.md` (client repos: git-excluded); vault `Next/Dev - <id>.md` if no repo |
| `venture-os` m1–m4 | the module's `TAB-INBOX.md` (m2 = forge) |
| `intake` | yt-extract's idea queue → `yt-extract ideas --send` → Coda |
| `learn` (depth / bridge / breadth) | vault `Knowledge/Reading/<field>.md`, linked from `Knowledge/<field>.md` |
| `practice` | vault `Next/Dev - Practice.md` + the field's reading list |
| `backlog` | vault `Next/Dev - Backlog.md` (shared with yt-extract) |
| `reference` → folder | `out/reference-bookmarks.html`, import it to replace old bookmark folders |
| `video` → area | YouTube videos: `out/youtube-playlists.md`, to add to the yt-extract area playlist (dev / business / knowledge / dump) |
| `drop` | nothing |
| `keep` | pinned tabs (and this review page): stay open, never sent to the model |

**Projects** are star-sort's `config/projects.json` (id, label, path, needs, `sensitive`) plus every git
repo directly inside `~/Dev/projects` and `~/Dev/clients` (client repos count as sensitive). A repo nested
deeper, like `projects/back-up/task-brain`, needs an entry in `projects.json`; `needs` keywords sharpen
matching for both tab-rescue and yt-extract. After adding one: `python3 -m tab_rescue route --redo '<host regex>'`.

**Stack** services (`[stack]` in `tab-rescue.toml`: Cloudflare, Resend, Vercel, Supabase, GitHub, Google
Cloud): the reference folder `Stack` gets one link per service (its `console`), and every dashboard,
settings or plan page of that service is proposed as drop ("Stack console pages"). Docs and guides are
routed like any other page; on GitHub only your own dashboard and settings count, other repos are routed
by content. Other consoles (`console.*`, `dash.*`, `app.*`) keep one bookmark per host.

**Knowledge fields** are disciplines, about a dozen in total. `[merge_fields]` in `tab-rescue.toml` folds
narrow ones into broader ones (`"Distributed Systems" = "Software Architecture"`); edit it and run
`bin/tab-rescue page`. If a proposal changes after you accepted it (merged field, re-route), the page asks
about it again and shows what it was.

Destinations, the project registry (star-sort `projects.json` + discovered repos) and model backends
are imported from yt-extract (`tab-rescue.toml` → `yt_extract`). Only Claude or local Ollama models are
allowed; the model sees titles, hosts, URL paths, tab group and folder names, never page content or
query strings, and titles on private hosts (mail, banking, payments) are withheld. Duplicates, search,
login and browser pages, and tabs that are already bookmarked are proposed as drop without a model call.
Suggestions are cached in `out/routes.json`, applied items in `out/applied.json` (re-applying is a no-op,
moving an item re-applies it). `apply --dry-run` shows what would be written. `route --redo REGEX` routes matching hosts/URLs again.

Python 3.14, stdlib + numpy. Browser files are only ever copied to a temp dir and read from the copy;
nothing writes to a browser profile. `scan` clusters with local Ollama `nomic-embed-text` or TF-IDF. The
HTML page makes no network requests (CSP `default-src 'none'`). `out/` and `scratch/` are git-ignored:
they contain your real URLs. Tests: `python3 -m unittest`.
