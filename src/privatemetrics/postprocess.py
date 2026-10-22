"""Post-processing of the noisy table into the upstream GRADING metrics.

Only functions of (noisy table, prediction-only statistics), hence free under
the post-processing property. With zero noise, the metrics equal upstream
`scoring.ts` exactly (tests/test_parity.py).
"""

from __future__ import annotations

import math


def project_to_simplex(v: list[float], total: float) -> list[float]:
    """Euclidean projection of v onto {x >= 0, sum(x) = total}, total > 0
    (Duchi et al., 2008). Deterministic."""
    if total <= 0:
        return [0.0] * len(v)
    u = sorted(v, reverse=True)
    css = 0.0
    theta = 0.0
    for j, uj in enumerate(u, start=1):
        css += uj
        t = (css - total) / j
        if uj - t > 0:
            theta = t
    return [max(x - theta, 0.0) for x in v]


def round4(x: float) -> float:
    """Upstream `round4`: Math.round((x + EPSILON) * 1e4) / 1e4, -0 -> 0.
    JavaScript's Math.round rounds half up (towards +inf)."""
    r = math.floor((x + 2.220446049250313e-16) * 1e4 + 0.5) / 1e4
    return 0.0 if r == 0 else r


def clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def metrics_from_table(table: list[list[float]], column_sums: list[int], matched: int, n: int,
                       referable_threshold: int) -> dict[str, float]:
    k = len(table)
    m = matched
    coverage = m / n if n else 0.0
    if m == 0:
        return {"qwk": 0.0, "accuracy": 0.0, "referableSensitivity": 0.0,
                "referableSpecificity": 0.0, "coverage": round4(coverage)}
    acc = sum(table[i][i] for i in range(k)) / m
    pos = sum(table[a][b] for a in range(referable_threshold, k) for b in range(k))
    tp = sum(table[a][b] for a in range(referable_threshold, k) for b in range(referable_threshold, k))
    neg = sum(table[a][b] for a in range(referable_threshold) for b in range(k))
    tn = sum(table[a][b] for a in range(referable_threshold) for b in range(referable_threshold))
    # Upstream convention: an empty class yields 0 (GAP_ANALYSIS G12). After
    # projection all cells are >= 0, so 0 <= tp <= pos and 0 <= tn <= neg.
    sens = tp / pos if pos > 0 else 0.0
    spec = tn / neg if neg > 0 else 0.0

    hist_true = [sum(table[a]) for a in range(k)]
    hist_pred = column_sums
    w = (k - 1) ** 2
    num = sum(((i - j) ** 2 / w) * table[i][j] for i in range(k) for j in range(k))
    den = sum(((i - j) ** 2 / w) * hist_true[i] * hist_pred[j] / m for i in range(k) for j in range(k))
    qwk = 1.0 if den == 0 else 1.0 - num / den

    return {
        "qwk": round4(clamp(qwk, -1.0, 1.0)),
        "accuracy": round4(clamp(acc, 0.0, 1.0)),
        "referableSensitivity": round4(clamp(sens, 0.0, 1.0)),
        "referableSpecificity": round4(clamp(spec, 0.0, 1.0)),
        "coverage": round4(coverage),
    }


def privatize_table(counts: list[list[int]], column_sums: list[int], noise) -> list[list[float]]:
    """Noise every cell of every active column, then project each column back
    onto its known (prediction-only) sum. `noise(list[int]) -> list[int]`."""
    k = len(counts)
    active = [b for b in range(k) if column_sums[b] > 0]
    flat = [counts[a][b] for b in active for a in range(k)]
    noisy = noise(flat)
    out = [[0.0] * k for _ in range(k)]
    for idx, b in enumerate(active):
        col = [float(x) for x in noisy[idx * k:(idx + 1) * k]]
        proj = project_to_simplex(col, float(column_sums[b]))
        for a in range(k):
            out[a][b] = proj[a]
    return out
