"""Optional local embeddings via Ollama (http://localhost:11434 only)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

DEFAULT_BASE = "http://localhost:11434"
MODEL = "nomic-embed-text"
PREFIX = "clustering: "
MAX_CHARS = 600

# never go through an environment-configured proxy: this is strictly local traffic
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _check_local(base: str) -> str:
    host = urlsplit(base).hostname
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError(f"refusing non-local embedding endpoint: {host}")
    return base.rstrip("/")


def model_available(base: str = DEFAULT_BASE, timeout: float = 2.0) -> bool:
    base = _check_local(base)
    try:
        with _OPENER.open(base + "/api/tags", timeout=timeout) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return any(str(m.get("name", "")).split(":")[0] == MODEL for m in data.get("models", []))


def _key(text: str) -> str:
    return hashlib.sha1((PREFIX + text).encode("utf-8", "replace")).hexdigest()


def _load_cache(path: Path | None) -> dict[str, np.ndarray]:
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("model") != MODEL:
            return {}
        return {k: np.frombuffer(base64.b64decode(v), dtype=np.float16) for k, v in raw["vectors"].items()}
    except (OSError, ValueError, KeyError):
        return {}


def _save_cache(path: Path | None, cache: dict[str, np.ndarray]) -> None:
    if path is None:
        return
    payload = {
        "model": MODEL,
        "vectors": {k: base64.b64encode(v.astype(np.float16).tobytes()).decode("ascii") for k, v in cache.items()},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _post_embed(base: str, texts: list[str], timeout: float) -> list[list[float]]:
    body = json.dumps({"model": MODEL, "input": [PREFIX + t for t in texts]}).encode()
    req = urllib.request.Request(base + "/api/embed", data=body, headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=timeout) as resp:
        data = json.load(resp)
    vecs = data.get("embeddings")
    if not isinstance(vecs, list) or len(vecs) != len(texts):
        raise ValueError("unexpected /api/embed response")
    return vecs


def embed_texts(texts: list[str], cache_path: Path | None = None, base: str = DEFAULT_BASE,
                batch: int = 64, timeout: float = 180.0) -> np.ndarray:
    """Return an (n, d) float32 matrix of L2-normalised embeddings, using the on-disk cache."""
    base = _check_local(base)
    texts = [t[:MAX_CHARS] for t in texts]
    cache = _load_cache(cache_path)
    todo: list[str] = []
    seen: set[str] = set()
    for t in texts:
        k = _key(t)
        if k not in cache and k not in seen:
            seen.add(k)
            todo.append(t)
    try:
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            for t, v in zip(chunk, _post_embed(base, chunk, timeout)):
                cache[_key(t)] = np.asarray(v, dtype=np.float32).astype(np.float16)
    finally:
        if todo:
            _save_cache(cache_path, cache)
    mat = np.stack([cache[_key(t)] for t in texts]).astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms
