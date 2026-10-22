"""Generate the labelled prediction fixtures for the reference task.

No official baseline predictions are published upstream for `idrid-dr-grading`
(checked at the commit in docs/UPSTREAM_COMMIT). These fixtures are therefore
SYNTHETIC predictions over the real item identifiers and the real public
labels: each is a deterministic, seeded perturbation of the reference labels
that stands in for a model of a given quality. They are not outputs of any
real model and must not be described as such.

Run: python fixtures/reference/make_prediction_fixtures.py [--check]
"""
from __future__ import annotations

import csv
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
K = 5
SEED = 20261022

# name -> (P(exact), P(off by 1), P(off by 2), fraction of items omitted)
PROFILES = {
    "perfect": (1.0, 0.0, 0.0, 0.0),
    "strong": (0.8, 0.2, 0.0, 0.0),
    "moderate": (0.55, 0.35, 0.10, 0.0),
    "strong-partial": (0.8, 0.2, 0.0, 0.2),
}


def labels() -> dict[str, int]:
    with (HERE / "labels.csv").open(newline="") as fh:
        return {r["Image"]: int(r["dr_grade"]) for r in csv.DictReader(fh)}


def perturb(y: int, rng: random.Random, p_exact: float, p1: float) -> int:
    u = rng.random()
    if u < p_exact:
        return y
    step = 1 if u < p_exact + p1 else 2
    cands = [v for v in (y - step, y + step) if 0 <= v < K]
    return rng.choice(cands)


def build() -> dict[str, dict]:
    lab = labels()
    out = {}
    for i, (name, (pe, p1, _p2, omit)) in enumerate(PROFILES.items()):
        rng = random.Random(SEED + i)
        ids = sorted(lab)
        dropped = set(rng.sample(ids, round(omit * len(ids)))) if omit else set()
        out[name] = {"predictions": {k: perturb(lab[k], rng, pe, p1) for k in ids if k not in dropped}}
    # Majority-class constant model: every item gets the most frequent grade.
    counts = [sum(1 for v in lab.values() if v == g) for g in range(K)]
    out["majority-constant"] = {"predictions": dict.fromkeys(sorted(lab), counts.index(max(counts)))}
    return out


def main() -> int:
    check = "--check" in sys.argv
    bad = 0
    for name, body in build().items():
        path = HERE / "predictions" / f"{name}.json"
        text = json.dumps(body, indent=1, sort_keys=True) + "\n"
        if check:
            if not path.exists() or path.read_text() != text:
                print(f"DIFFERS: {path.name}")
                bad += 1
        else:
            path.write_text(text)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
