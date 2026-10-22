"""With zero noise the metrics must equal upstream scoring.ts exactly.

The upstream file is copied verbatim into tests/upstream/ (commit in
tests/upstream/UPSTREAM_COMMIT) and executed with Node. Without Node the
cross-language check is skipped and reported as UNVERIFIED.
"""
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from privatemetrics.confusion import confusion_table
from privatemetrics.postprocess import metrics_from_table, privatize_table

UP = Path(__file__).parent / "upstream"


def ours(gt, preds, k, t):
    tab = confusion_table(gt, preds, k)
    exact = privatize_table(tab.counts, tab.column_sums, lambda v: v)  # zero noise, projection only
    return metrics_from_table(exact, tab.column_sums, tab.matched, tab.n, t)


def cases():
    rng = random.Random(20261013)
    out = [
        ({"a": 0, "b": 1, "c": 2, "d": 3, "e": 4}, {"a": 0, "b": 1, "c": 2, "d": 3, "e": 4}, 5, 2),
        ({"x0": 0, "x1": 1, "x2": 2}, {"x0": 1, "x1": 2, "x2": 0}, 3, 2),
        ({"a": 0, "b": 1, "c": 0, "d": 1}, {"a": 0, "b": 1, "c": 2, "d": 0}, 5, 2),
        ({"a": 0, "b": 1}, {}, 5, 2),
        ({"a": 2, "b": 2, "c": 2}, {"a": 2, "b": 2, "c": 2}, 5, 2),  # Σw·E = 0 branch
    ]
    for _ in range(200):
        n = rng.randint(1, 40)
        gt = {f"i{j}": rng.randrange(5) for j in range(n)}
        preds = {i: rng.randrange(5) for i in gt if rng.random() < 0.85}
        out.append((gt, preds, 5, 2))
    return out


def test_hand_computed_upstream_cases():
    assert ours({"x0": 0, "x1": 1, "x2": 2}, {"x0": 1, "x1": 2, "x2": 0}, 3, 2) == {
        "qwk": -0.5, "accuracy": 0.0, "referableSensitivity": 0.0, "referableSpecificity": 0.5, "coverage": 1.0}
    s = ours({"a": 0, "b": 1, "c": 0, "d": 1}, {"a": 0, "b": 1, "c": 2, "d": 0}, 5, 2)
    assert s["qwk"] == -0.25 and s["referableSpecificity"] == 0.75 and s["referableSensitivity"] == 0.0


@pytest.mark.skipif(shutil.which("node") is None, reason="UNVERIFIED: Node not available to run upstream scoring.ts")
def test_matches_upstream_typescript_on_205_cases():
    cs = cases()
    payload = json.dumps([{"groundTruth": g, "predictions": p, "numClasses": k, "referableThreshold": t}
                          for g, p, k, t in cs])
    r = subprocess.run(["node", "--experimental-strip-types", "--no-warnings", "run_scoring.mjs"],
                       cwd=UP, input=payload, capture_output=True, text=True, timeout=60, check=True)
    upstream = json.loads(r.stdout)
    for (g, p, k, t), want in zip(cs, upstream, strict=True):
        assert ours(g, p, k, t) == want, (g, p)
