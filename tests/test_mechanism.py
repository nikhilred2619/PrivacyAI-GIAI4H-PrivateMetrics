"""Mechanism, privacy map, sensitivity and post-processing properties."""
import math
import random

import pytest

from privatemetrics.confusion import confusion_table
from privatemetrics.mechanism import OpenDPSampler, SimulationSampler, discrete_laplace_variance
from privatemetrics.postprocess import metrics_from_table, privatize_table, project_to_simplex


@pytest.mark.parametrize("eps", [0.25, 0.5, 1.0, 2.0, 4.0, 8.0])
def test_opendp_privacy_map_equals_declared_epsilon(eps):
    assert OpenDPSampler().epsilon(2.0 / eps) == pytest.approx(eps, rel=1e-12)


def test_sensitivity_of_table_is_two_for_every_single_label_change():
    rng = random.Random(1)
    k = 5
    for _ in range(300):
        n = rng.randint(1, 25)
        y = {f"i{j}": rng.randrange(k) for j in range(n)}
        p = {i: rng.randrange(k) for i in y if rng.random() < 0.8}
        i = rng.choice(list(y))
        y2 = dict(y, **{i: rng.randrange(k)})
        a, b = confusion_table(y, p, k), confusion_table(y2, p, k)
        l1 = sum(abs(a.counts[r][c] - b.counts[r][c]) for r in range(k) for c in range(k))
        assert l1 <= 2
        assert a.column_sums == b.column_sums and a.matched == b.matched  # prediction-only


def test_projection_is_onto_simplex_and_idempotent():
    rng = random.Random(2)
    for _ in range(500):
        v = [rng.uniform(-10, 15) for _ in range(5)]
        c = rng.randint(1, 20)
        x = project_to_simplex(v, c)
        assert min(x) >= 0 and sum(x) == pytest.approx(c)
        assert project_to_simplex(x, c) == pytest.approx(x)
    assert project_to_simplex([1.0, 2.0, 0.0], 3) == [1.0, 2.0, 0.0]


def test_released_metrics_always_in_valid_range():
    rng = random.Random(3)
    s = SimulationSampler(3)
    for _ in range(400):
        n = rng.randint(1, 30)
        y = {f"i{j}": rng.randrange(5) for j in range(n)}
        p = {i: rng.randrange(5) for i in y if rng.random() < 0.9}
        t = confusion_table(y, p, 5)
        noisy = privatize_table(t.counts, t.column_sums, lambda v: s.noisy(v, 8.0))
        m = metrics_from_table(noisy, t.column_sums, t.matched, t.n, 2)
        assert -1 <= m["qwk"] <= 1
        for key in ("accuracy", "referableSensitivity", "referableSpecificity", "coverage"):
            assert 0 <= m[key] <= 1
            assert not math.isnan(m[key])


@pytest.mark.slow
def test_simulation_sampler_matches_opendp_distribution():
    """The seeded simulation sampler must draw the same distribution as production."""
    scale, n = 2.0, 40_000
    a = SimulationSampler(7).noisy([0] * n, scale)
    b = OpenDPSampler().noisy([0] * n, scale)
    var = discrete_laplace_variance(scale)
    for xs in (a, b):
        mean = sum(xs) / n
        v = sum((x - mean) ** 2 for x in xs) / n
        assert abs(mean) < 4 * math.sqrt(var / n)
        assert v == pytest.approx(var, rel=0.05)
    pa = sum(1 for x in a if x == 0) / n
    pb = sum(1 for x in b if x == 0) / n
    p0 = math.tanh(1 / (2 * scale))  # Pr[Z=0] = (1-a)/(1+a), a = exp(-1/scale)
    assert pa == pytest.approx(p0, abs=0.01) and pb == pytest.approx(p0, abs=0.01)


@pytest.mark.slow
def test_empirical_privacy_ratio_on_neighbouring_label_sets():
    """Empirical audit (not a proof): released accuracy distributions on two
    neighbouring label sets stay within e^ε up to sampling error."""
    eps = 1.0
    y = {"a": 0, "b": 1, "c": 2, "d": 3}
    y2 = dict(y, a=4)  # one label changed
    p = {"a": 0, "b": 1, "c": 2, "d": 3}
    s = SimulationSampler(11)

    def release(lbl):
        t = confusion_table(lbl, p, 5)
        nt = privatize_table(t.counts, t.column_sums, lambda v: s.noisy(v, 2 / eps))
        return metrics_from_table(nt, t.column_sums, t.matched, t.n, 2)["accuracy"]

    N = 60_000
    from collections import Counter
    h1 = Counter(round(release(y), 1) for _ in range(N))
    h2 = Counter(round(release(y2), 1) for _ in range(N))
    worst = 0.0
    for k in set(h1) | set(h2):
        if h1[k] >= 2000 and h2[k] >= 2000:  # bins with enough mass for a stable ratio
            worst = max(worst, abs(math.log(h1[k] / h2[k])))
    assert worst <= eps + 0.1, worst


def test_empty_matched_set_draws_no_noise_and_reveals_no_label_information():
    calls = []
    t = confusion_table({"a": 1, "b": 3}, {}, 5)
    tab = privatize_table(t.counts, t.column_sums, lambda v: calls.append(v) or v)
    assert calls == [[]] or calls == []
    m = metrics_from_table(tab, t.column_sums, t.matched, t.n, 2)
    assert m == {"qwk": 0.0, "accuracy": 0.0, "referableSensitivity": 0.0, "referableSpecificity": 0.0,
                 "coverage": 0.0}


def test_single_predicted_class_makes_qwk_and_sensitivity_label_independent():
    """With every prediction in one column the QWK formula and sensitivity do
    not depend on the noisy cells (documented behaviour, not a leak)."""
    s = SimulationSampler(5)
    y = {f"i{j}": j % 5 for j in range(30)}
    p = {i: 0 for i in y}
    t = confusion_table(y, p, 5)
    vals = {tuple(sorted(metrics_from_table(privatize_table(t.counts, t.column_sums, lambda v: s.noisy(v, 2.0)),
                                            t.column_sums, t.matched, t.n, 2).items())) for _ in range(50)}
    assert {dict(v)["qwk"] for v in vals} == {0.0} and {dict(v)["referableSensitivity"] for v in vals} == {0.0}


def test_undefined_denominator_follows_upstream_convention():
    t = confusion_table({"a": 0, "b": 1}, {"a": 0, "b": 1}, 5)  # no referable items
    m = metrics_from_table(privatize_table(t.counts, t.column_sums, lambda v: v), t.column_sums, t.matched, t.n, 2)
    assert m["referableSensitivity"] == 0.0 and m["referableSpecificity"] == 1.0


def test_opendp_domain_is_i32_and_counts_fit():
    import opendp.prelude as dp
    dp.enable_features("contrib")
    assert "i32" in str(dp.atom_domain(T=int))
    assert 100_000 < 2**31 - 1  # largest possible count is the 100,000-item payload cap
