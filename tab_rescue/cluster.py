"""Text features, spherical k-means clustering and cluster labels (numpy only)."""

from __future__ import annotations

import math
import re
from collections import Counter
from urllib.parse import urlsplit, unquote

import numpy as np

from .model import Item, KIND_TAB, KIND_BOOKMARK

STOPWORDS = set("""
a about after all also am an and any are as at be been but by can com did do does for from get had has have how
i if in into is it its just more my new no not of on one or org our out over so than that the their them then there
these they this to up us use was we were what when which who why will with you your net io www http https html htm
php aspx index home page pages
""".split())
MAX_FEATURES = 4000
UNSORTED_LABEL = "Unsorted"
# Browser-provided root folders carry no topic signal.
ROOT_FOLDERS = {"Bookmarks Menu", "Bookmarks Toolbar", "Bookmarks Bar", "Other Bookmarks", "Mobile Bookmarks"}


def tokenize(text: str) -> list[str]:
    out = []
    for t in re.findall(r"[^\W_]+", text.lower()):
        if len(t) < 2 or len(t) > 24 or t.isdigit() or t in STOPWORDS:
            continue
        out.append(t)
    return out


def path_words(url: str, limit: int = 8) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    words = [w for w in re.split(r"[^\w]+", unquote(parts.path)) if w and not w.isdigit() and len(w) <= 24]
    return " ".join(words[:limit])


def item_text(it: Item, with_folder: bool = True) -> str:
    """Text used for clustering: title + host + URL path words (+ user bookmark folders)."""
    bits = [it.title.strip(), it.host, path_words(it.url)]
    if with_folder and it.kind == KIND_BOOKMARK and it.folder:
        bits.append(" / ".join(f for f in it.folder.split(" / ") if f not in ROOT_FOLDERS))
    return " | ".join(b for b in bits if b)


# ---------------------------------------------------------------------- TF-IDF

class Tfidf:
    def __init__(self, docs: list[list[str]], max_features: int = MAX_FEATURES, min_df: int | None = None):
        n = len(docs)
        if min_df is None:
            min_df = 2 if n >= 30 else 1
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        vocab = [t for t, c in sorted(df.items(), key=lambda kv: (-kv[1], kv[0])) if c >= min_df][:max_features]
        self.vocab = vocab
        index = {t: i for i, t in enumerate(vocab)}
        mat = np.zeros((n, len(vocab)), dtype=np.float32)
        for r, d in enumerate(docs):
            for t, c in Counter(d).items():
                j = index.get(t)
                if j is not None:
                    mat[r, j] = 1.0 + math.log(c)
        dfv = np.array([df[t] for t in vocab], dtype=np.float32)
        idf = np.log((1.0 + n) / (1.0 + dfv)) + 1.0 if len(vocab) else dfv
        mat *= idf
        self.matrix = _normalize_rows(mat)


def _normalize_rows(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


def lsa(x: np.ndarray, dims: int = 100, seed: int = 0) -> np.ndarray:
    """Randomised truncated SVD; returns L2-normalised row embeddings (n, <=dims)."""
    n, v = x.shape
    dims = min(dims, n - 1, v)
    if dims < 2:
        return x
    rng = np.random.default_rng(seed)
    omega = rng.standard_normal((v, dims + 10)).astype(np.float32)
    q, _ = np.linalg.qr(x @ omega)
    for _ in range(2):
        q, _ = np.linalg.qr(x.T @ q)
        q, _ = np.linalg.qr(x @ q)
    b = q.T @ x
    u_b, s, _ = np.linalg.svd(b, full_matrices=False)
    u = (q @ u_b)[:, :dims] * s[:dims]
    return _normalize_rows(u.astype(np.float32))


# ---------------------------------------------------------------------- k-means

def spherical_kmeans(x: np.ndarray, k: int, iters: int = 40, seed: int = 0) -> np.ndarray:
    """Cosine k-means with k-means++ seeding. ``x`` rows must be L2-normalised."""
    n = x.shape[0]
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)
    centers = np.empty((k, x.shape[1]), dtype=np.float32)
    centers[0] = x[rng.integers(n)]
    dist = 1.0 - x @ centers[0]
    for j in range(1, k):
        w = np.clip(dist, 0, None) ** 2
        total = float(w.sum())
        idx = int(rng.choice(n, p=w / total)) if total > 1e-12 else int(rng.integers(n))
        centers[j] = x[idx]
        dist = np.minimum(dist, 1.0 - x @ centers[j])
    labels = np.full(n, -1)
    cols = np.arange(n)
    for _ in range(iters):
        sims = x @ centers.T
        new = sims.argmax(axis=1)
        if np.array_equal(new, labels):
            break
        labels = new
        onehot = np.zeros((k, n), dtype=np.float32)
        onehot[labels, cols] = 1.0
        sums = onehot @ x
        counts = onehot.sum(axis=1)
        empty = np.where(counts == 0)[0]
        if len(empty):  # re-seed empty clusters with the worst-fitting points
            worst = np.argsort(sims[cols, labels])[: len(empty)]
            sums[empty] = x[worst]
            counts[empty] = 1
        centers = _normalize_rows(sums)
    return labels


def _best_kmeans(x: np.ndarray, k: int, restarts: int = 3) -> np.ndarray:
    best, best_score = None, -1.0
    for seed in range(restarts):
        lab = spherical_kmeans(x, k, seed=seed)
        cent = np.zeros((k, x.shape[1]), dtype=np.float32)
        np.add.at(cent, lab, x)
        score = float((x * _normalize_rows(cent)[lab]).sum())
        if score > best_score:
            best, best_score = lab, score
    assert best is not None
    return best


def _split_oversized(x: np.ndarray, labels: np.ndarray, limit: int, rounds: int = 3) -> np.ndarray:
    labels = labels.copy()
    for _ in range(rounds):
        counts = Counter(labels.tolist())
        big = [c for c, m in counts.items() if m > limit]
        if not big:
            break
        nxt = labels.max() + 1
        for c in big:
            idx = np.where(labels == c)[0]
            sub = spherical_kmeans(x[idx], 2, seed=1)
            if min((sub == 0).sum(), (sub == 1).sum()) < 5:
                continue
            labels[idx[sub == 1]] = nxt
            nxt += 1
    return labels


# ---------------------------------------------------------------------- orchestration

def cluster_items(items: list[Item], vectors: np.ndarray | None = None, min_size: int = 3):
    """Cluster ``items`` and set ``item.cluster``.

    ``vectors``: optional L2-normalised embeddings (n, d); if ``None`` TF-IDF (+LSA) is used.
    Returns ``(clusters, method)`` where ``clusters`` is a list of dicts
    ``{id, label, size, tabs, bookmarks, domains, dominant}``; the "Unsorted"
    cluster, if any, is last.
    """
    n = len(items)
    texts = [item_text(it) for it in items]
    tf = Tfidf([tokenize(t) for t in texts])
    # Labels describe content, so folder names (the user's old filing) stay out of them.
    tf_label = Tfidf([tokenize(item_text(it, with_folder=False)) for it in items])
    if n == 0:
        return [], "none"

    if vectors is not None:
        feats, method = vectors, "embeddings"
    elif tf.matrix.shape[1] and n >= 40 and tf.matrix.shape[1] > 120:
        feats, method = lsa(tf.matrix), "tfidf+lsa"
    else:
        feats, method = tf.matrix, "tfidf"

    zero = np.linalg.norm(feats, axis=1) == 0
    labels = np.full(n, -1)
    live = np.where(~zero)[0]
    if len(live) >= 2 * min_size:
        k = max(2, int(round(math.sqrt(len(live) / 2))))
        sub = feats[live]
        lab = _best_kmeans(sub, k)
        limit = max(60, 3 * math.ceil(len(live) / k))
        lab = _split_oversized(sub, lab, limit)
        labels[live] = lab

    counts = Counter(l for l in labels.tolist() if l >= 0)
    groups: list[list[int]] = []
    unsorted: list[int] = []
    by_label: dict[int, list[int]] = {}
    for i, l in enumerate(labels.tolist()):
        if l >= 0 and counts[l] >= min_size:
            by_label.setdefault(l, []).append(i)
        else:
            unsorted.append(i)
    groups = sorted(by_label.values(), key=lambda g: (-len(g), g[0]))

    clusters: list[dict] = []
    gmean = tf_label.matrix.mean(axis=0) if tf_label.matrix.shape[1] else None
    for cid, members in enumerate(groups):
        clusters.append(_describe(cid, members, items, tf_label, gmean))
        for i in members:
            items[i].cluster = cid
    if unsorted:
        cid = len(clusters)
        d = _describe(cid, unsorted, items, tf_label, gmean)
        d["label"] = UNSORTED_LABEL
        d["unsorted"] = True
        clusters.append(d)
        for i in unsorted:
            items[i].cluster = cid
    return clusters, method


def _describe(cid: int, members: list[int], items: list[Item], tf: Tfidf, gmean) -> dict:
    hosts = Counter(items[i].host for i in members if items[i].host)
    top_hosts = hosts.most_common(5)
    dominant = ""
    if top_hosts and top_hosts[0][1] * 2 > len(members):
        dominant = top_hosts[0][0]
    terms = _top_terms(members, tf, gmean)
    if terms:
        label = " · ".join(terms)
    elif dominant:
        label = dominant
    else:
        label = "Mixed: " + ", ".join(h for h, _ in top_hosts[:2]) if top_hosts else f"Group {cid + 1}"
    if dominant and dominant not in label:
        label += f" ({dominant})"
    return {
        "id": cid,
        "label": label,
        "size": len(members),
        "tabs": sum(1 for i in members if items[i].kind == KIND_TAB),
        "bookmarks": sum(1 for i in members if items[i].kind == KIND_BOOKMARK),
        "domains": [[h, c] for h, c in top_hosts],
        "dominant": dominant,
        "unsorted": False,
    }


def _top_terms(members: list[int], tf: Tfidf, gmean, count: int = 3) -> list[str]:
    if not tf.vocab or gmean is None:
        return []
    sub = tf.matrix[members]
    score = sub.mean(axis=0) - gmean
    presence = (sub > 0).mean(axis=0)
    need = max(2.0 / len(members), 0.2) if len(members) >= 4 else 0.0
    score = np.where(presence >= need, score, -np.inf)
    out: list[str] = []
    for j in np.argsort(-score)[: count * 3]:
        if not np.isfinite(score[j]) or score[j] <= 0:
            break
        out.append(tf.vocab[j])
        if len(out) == count:
            break
    if not out:
        # Nothing distinctive versus the whole collection: fall back to the words
        # most members share, so the group still reads as something.
        for j in np.argsort(-presence)[:count]:
            if presence[j] >= 0.15:
                out.append(tf.vocab[j])
    return out
