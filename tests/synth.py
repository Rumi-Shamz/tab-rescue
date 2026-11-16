"""Synthetic large data set (made-up hosts and words) for performance and UI checks."""

from __future__ import annotations

import random

from tab_rescue.model import Item, KIND_BOOKMARK, KIND_TAB

TOPICS = {
    "rust": "borrow lifetime trait cargo crate macro slice iterator ownership async".split(),
    "baking": "sourdough flour yeast oven loaf crumb proof hydration starter dough".split(),
    "travel": "flight hotel itinerary visa airport passport museum beach train ticket".split(),
    "garden": "tomato compost seedling mulch pruning soil greenhouse herb pollinator raised".split(),
    "finance": "invoice budget ledger mortgage dividend index portfolio tax deduction savings".split(),
    "music": "guitar chord scale tempo synth reverb mixing vinyl melody harmony".split(),
    "photo": "lens aperture shutter raw exposure tripod bokeh sensor lightroom portrait".split(),
    "fitness": "squat deadlift cardio protein stretch mobility marathon interval kettlebell sleep".split(),
}


def make_items(n: int, seed: int = 1, bookmark_ratio: float = 0.7, dup_ratio: float = 0.05) -> list[Item]:
    rng = random.Random(seed)
    topics = list(TOPICS)
    items: list[Item] = []
    now = 1_800_000_000_000
    for i in range(n):
        topic = topics[rng.randrange(len(topics))]
        words = rng.sample(TOPICS[topic], 3)
        host = f"{topic}{rng.randrange(6)}.example.com"
        path = "/".join(words[:2]) + f"/{rng.randrange(10_000)}"
        url = f"https://{host}/{path}"
        if items and rng.random() < dup_ratio:
            url = rng.choice(items).url
        is_bm = rng.random() < bookmark_ratio
        items.append(Item(
            kind=KIND_BOOKMARK if is_bm else KIND_TAB,
            browser=rng.choice(["firefox", "firefox", "brave", "chromium"]),
            profile="synthetic",
            url=url,
            title=" ".join(words).title() + f" {topic}",
            folder=f"Bookmarks Bar / {topic.title()}" if is_bm else "",
            last_accessed_ms=None if is_bm else now - rng.randrange(0, 200) * 86_400_000,
        ))
    # sprinkle in temporary pages
    for i in range(0, n, 97):
        items[i] = Item(kind=KIND_TAB, browser="firefox", profile="synthetic",
                        url=f"https://www.google.com/search?q=thing{i}", title="thing - Google Search",
                        last_accessed_ms=now)
    return items
