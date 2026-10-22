"""Exact confusion table restricted to matched items.

C[a][b] = number of matched items with reference label a and prediction b.
Column sums c_b, the matched count m and the item count n depend only on the
predictions and the public item set (zero label sensitivity, THREAT_MODEL §7.3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Table:
    counts: list[list[int]]  # counts[a][b]
    column_sums: list[int]   # c_b, prediction-only
    matched: int             # m, prediction-only
    n: int                   # public


def confusion_table(labels: Mapping[str, int], predictions: Mapping[str, int], k: int) -> Table:
    counts = [[0] * k for _ in range(k)]
    col = [0] * k
    for item_id, p in predictions.items():
        y = labels[item_id]  # validation guarantees item_id is known
        counts[y][p] += 1
        col[p] += 1
    return Table(counts, col, sum(col), len(labels))
