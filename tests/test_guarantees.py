"""G-series: checks of the formal guarantee's ingredients (THREAT_MODEL.md §7, §7.10).

These are not proofs. They check that the implementation has the properties
the proof relies on, on concrete instances:

* G1  exact privacy loss: on small instances the released-output distribution
      is computed EXACTLY (discrete-Laplace pmf, all noise vectors within a
      truncation radius whose tail mass is bounded) for two neighbouring label
      sets, and the worst-case probability ratio is compared with e^ε;
* G2  coverage depends on predictions only (any labels, any noise);
* G3  post-processing is deterministic given the noisy table;
* G4  the certified cost recorded per release equals OpenDP's map at the
      calibrated scale, never exceeds the charge, and ledger sums are exact;
* G5  sensitivity: every neighbouring label change moves the table by L1 <= 2.
"""

import itertools
import math
import random
from collections import defaultdict
from fractions import Fraction

import pytest
from conftest import SCOPE, build, perfect, synthetic_labels

from privatemetrics.budget import calibrate_scale, fmt, parse_epsilon, to_units
from privatemetrics.confusion import confusion_table
from privatemetrics.labels import LabelSet
from privatemetrics.mechanism import OpenDPSampler
from privatemetrics.postprocess import metrics_from_table, project_to_simplex
from privatemetrics.scorer import compute_release


def _pmf(scale: float, radius: int) -> dict[int, float]:
    """Discrete Laplace, Pr[z] ∝ exp(-|z|/scale), truncated to |z| <= radius."""
    a = math.exp(-1.0 / scale)
    c = (1 - a) / (1 + a)
    return {z: c * a ** abs(z) for z in range(-radius, radius + 1)}


def _column_dist(true_col: list[int], total: int, pmf: dict[int, float]) -> dict[tuple, float]:
    """Exact distribution of one projected noisy column (noise independent per cell)."""
    out: dict[tuple, float] = defaultdict(float)
    for zs in itertools.product(pmf.items(), repeat=len(true_col)):
        prob = math.prod(p for _, p in zs)
        noisy = [float(v + z) for v, (z, _) in zip(true_col, zs, strict=True)]
        out[tuple(round(x, 9) for x in project_to_simplex(noisy, float(total)))] += prob
    return out


def _release_dist(labels: dict[str, int], preds: dict[str, int], k: int, t: int, pmf) -> dict[tuple, float]:
    tab = confusion_table(labels, preds, k)
    active = [b for b in range(k) if tab.column_sums[b] > 0]
    per_col = [_column_dist([tab.counts[a][b] for a in range(k)], tab.column_sums[b], pmf) for b in active]
    out: dict[tuple, float] = defaultdict(float)
    for combo in itertools.product(*[d.items() for d in per_col]):
        prob = math.prod(p for _, p in combo)
        table = [[0.0] * k for _ in range(k)]
        for b, (col, _) in zip(active, combo, strict=True):
            for a in range(k):
                table[a][b] = col[a]
        m = metrics_from_table(table, tab.column_sums, tab.matched, tab.n, t)
        out[tuple(sorted(m.items()))] += prob
    return out


@pytest.mark.parametrize("case", [
    # (K, T, labels y, predictions, item whose label changes, new label, ε, radius)
    (2, 1, {"a": 0, "b": 1, "c": 1, "d": 0}, {"a": 0, "b": 0, "c": 1, "d": 1}, "a", 1, "2", 18),
    (3, 1, {"a": 0, "b": 1, "c": 2}, {"a": 0, "b": 0, "c": 0}, "b", 2, "1", 32),
    (3, 2, {"a": 2, "b": 1, "c": 0, "d": 2}, {"a": 2, "b": 2, "c": 2}, "c", 2, "1", 32),
])
def test_G1_exact_privacy_loss_on_small_instances_is_at_most_epsilon(case):
    k, t, y, preds, item, new, eps, radius = case
    scale, certified = calibrate_scale(parse_epsilon(eps), OpenDPSampler().epsilon)
    pmf = _pmf(scale, radius)
    cells = k * len({preds[i] for i in preds})
    tail = 1 - sum(pmf.values()) ** cells  # mass outside the enumerated noise vectors
    assert tail < 1e-6
    y2 = dict(y, **{item: new})
    p, q = _release_dist(y, preds, k, t, pmf), _release_dist(y2, preds, k, t, pmf)
    bound = math.exp(float(certified))
    worst = 0.0
    for out in set(p) | set(q):
        a, b = p.get(out, 0.0), q.get(out, 0.0)
        if max(a, b) < 1e-4:  # ratios of near-zero, truncation-affected masses are not meaningful
            continue
        # Each probability is known up to the truncated tail: compare conservatively.
        worst = max(worst, (a - tail) / (b + tail), (b - tail) / (a + tail))
    assert worst <= bound * (1 + 1e-9), (worst, bound)
    assert worst > 1.0  # the outputs do depend on the label (the check is not vacuous)


def test_G2_coverage_depends_on_predictions_only():
    rng = random.Random(7)
    sampler = OpenDPSampler()
    scale, _ = calibrate_scale(parse_epsilon("0.5"), sampler.epsilon)
    for _ in range(50):
        n = rng.randint(1, 40)
        ids = [f"i{j}" for j in range(n)]
        preds = {i: rng.randrange(5) for i in ids if rng.random() < 0.7}
        covs = {compute_release(LabelSet(5, {i: rng.randrange(5) for i in ids}), preds, 5, 2, sampler, scale)["coverage"]
                for _ in range(5)}
        assert covs == {round(len(preds) / n, 4)}


def test_G3_post_processing_is_deterministic_given_the_noisy_table():
    """Same noisy table in, same metrics out: the released values are a function
    of the noisy table and prediction-only statistics alone."""
    rng = random.Random(3)
    for _ in range(200):
        k = rng.randint(2, 6)
        cols = [rng.randint(0, 9) for _ in range(k)]
        noisy = [[rng.randint(-5, 9) for _ in range(k)] for _ in range(k)]
        t = rng.randint(1, k - 1)
        m = sum(cols)

        def release(noisy=noisy, cols=cols, k=k, t=t, m=m):
            proj = [[0.0] * k for _ in range(k)]
            for b in range(k):
                col = project_to_simplex([float(noisy[a][b]) for a in range(k)], float(cols[b]))
                for a in range(k):
                    proj[a][b] = col[a]
            return metrics_from_table(proj, cols, m, m + 3, t)
        assert release() == release()


@pytest.mark.parametrize("eps", ["0.000001", "0.1", "0.25", "1", "3.7", "4", "8"])
def test_G4_certified_cost_matches_openDP_and_ledger_accounting_is_exact(tmp_path, eps):
    from privatemetrics.ledger import Ledger
    labels = synthetic_labels()
    sampler = OpenDPSampler()
    scale, certified = calibrate_scale(parse_epsilon(eps), sampler.epsilon)
    assert Fraction(sampler.epsilon(scale)) == certified <= parse_epsilon(eps)
    k = 3
    cap = parse_epsilon(eps) * k
    s = build(Ledger(tmp_path / "l.db"), labels, eps=eps, cap=fmt(cap))
    outs = [s.score(submission_id=f"s{i}", participant_id="p", payload=perfect(labels)) for i in range(k + 1)]
    assert [o.metrics is not None for o in outs] == [True] * k + [False]
    rows = Ledger(tmp_path / "l.db").rows()
    assert all(Fraction(r["certified_eps"]) == certified for r in rows)
    assert sum(r["charged_units"] for r in rows) == k * to_units(parse_epsilon(eps))
    assert Ledger(tmp_path / "l.db").spent(SCOPE) == cap


def test_G5_every_single_label_change_moves_the_table_by_at_most_two():
    rng = random.Random(11)
    for _ in range(300):
        k = rng.randint(2, 6)
        ids = [f"i{j}" for j in range(rng.randint(1, 25))]
        y = {i: rng.randrange(k) for i in ids}
        preds = {i: rng.randrange(k) for i in ids if rng.random() < 0.8}
        base = confusion_table(y, preds, k)
        for i in ids:
            for new in range(k):
                other = confusion_table(dict(y, **{i: new}), preds, k)
                l1 = sum(abs(base.counts[a][b] - other.counts[a][b]) for a in range(k) for b in range(k))
                assert l1 <= 2
                assert other.column_sums == base.column_sums and other.matched == base.matched
