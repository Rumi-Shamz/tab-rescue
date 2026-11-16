import time
import unittest

import numpy as np

from tab_rescue import analyze as an
from tab_rescue.cluster import (Tfidf, cluster_items, item_text, lsa, spherical_kmeans, tokenize, UNSORTED_LABEL)
from .fixtures import TOPIC_A, TOPIC_B, make_item
from .synth import make_items


class TokenizeTests(unittest.TestCase):
    def test_tokenize(self):
        self.assertEqual(tokenize("The Python 3 asyncio-tutorial, www.example.com/12345"), ["python", "asyncio", "tutorial", "example"])


class ClusterTests(unittest.TestCase):
    def make(self):
        items = [make_item(u, t) for t, u in TOPIC_A] + [make_item(u, t) for t, u in TOPIC_B]
        an.analyze(items, now_ms=1_800_000_000_000)
        return items

    def test_two_topics_separate_with_tfidf(self):
        items = self.make()
        clusters, method = cluster_items(items)
        self.assertEqual(method, "tfidf")
        a = {it.cluster for it in items[:6]}
        b = {it.cluster for it in items[6:]}
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertNotEqual(a, b)
        self.assertEqual(sorted(c["size"] for c in clusters), [6, 6])
        labels = " ".join(c["label"] for c in clusters)
        self.assertIn("asyncio", labels)
        self.assertTrue("sourdough" in labels or "bread" in labels)

    def test_labels_have_top_terms_and_dominant_domain(self):
        items = [make_item(f"https://docs.python.example/asyncio/{i}", f"Python asyncio topic {w}")
                 for i, w in enumerate("alpha beta gamma delta epsilon zeta".split())]
        items += [make_item(f"https://bakery.example/r/{i}", f"Sourdough bread {w}") for i, w in enumerate("one two three four five six".split())]
        an.analyze(items, now_ms=1)
        clusters, _ = cluster_items(items)
        py = next(c for c in clusters if "python" in c["label"] or "asyncio" in c["label"])
        self.assertEqual(py["dominant"], "docs.python.example")
        self.assertIn("(docs.python.example)", py["label"])
        self.assertLessEqual(len(py["label"].split(" · ")), 3)

    def test_embedding_vectors_path(self):
        items = self.make()
        rng = np.random.default_rng(0)
        v = np.zeros((12, 8), dtype=np.float32)
        v[:6, 0] = 1; v[6:, 1] = 1
        v += rng.normal(0, 0.05, v.shape).astype(np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        clusters, method = cluster_items(items, vectors=v)
        self.assertEqual(method, "embeddings")
        self.assertEqual(len({it.cluster for it in items[:6]}), 1)
        self.assertNotEqual(items[0].cluster, items[6].cluster)

    def test_small_leftovers_go_to_unsorted(self):
        items = self.make()
        items.append(make_item("https://zzz.example/odd", "Quokka xylophone"))
        an.analyze(items, now_ms=1)
        clusters, _ = cluster_items(items)
        self.assertEqual(clusters[-1]["label"], UNSORTED_LABEL)
        self.assertTrue(clusters[-1]["unsorted"])
        self.assertEqual(items[-1].cluster, clusters[-1]["id"])

    def test_empty_and_degenerate(self):
        self.assertEqual(cluster_items([]), ([], "none"))
        items = [make_item("about:blank", "") for _ in range(3)]
        an.analyze(items)
        clusters, _ = cluster_items(items)
        self.assertEqual([c["label"] for c in clusters], [UNSORTED_LABEL])

    def test_kmeans_and_lsa_shapes(self):
        rng = np.random.default_rng(1)
        x = rng.random((60, 30)).astype(np.float32)
        x /= np.linalg.norm(x, axis=1, keepdims=True)
        lab = spherical_kmeans(x, 4)
        self.assertEqual(lab.shape, (60,))
        self.assertLessEqual(lab.max(), 3)
        z = lsa(x, dims=10)
        self.assertEqual(z.shape, (60, 10))
        self.assertTrue(np.allclose(np.linalg.norm(z, axis=1), 1, atol=1e-4))

    def test_five_thousand_items_under_a_minute(self):
        items = make_items(5000)
        an.analyze(items, now_ms=1_800_000_000_000)
        t0 = time.time()
        clusters, method = cluster_items(items)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 60)
        self.assertTrue(all(it.cluster >= 0 for it in items))
        self.assertGreater(len([c for c in clusters if not c["unsorted"]]), 5)
        # synthetic topics are recoverable: most clusters are dominated by one topic
        by_cluster = {}
        for it in items:
            by_cluster.setdefault(it.cluster, []).append(it.host.split(".")[0].rstrip("0123456789"))
        purity = sum(max(v.count(t) for t in set(v)) for v in by_cluster.values()) / len(items)
        self.assertGreater(purity, 0.8)


if __name__ == "__main__":
    unittest.main()
