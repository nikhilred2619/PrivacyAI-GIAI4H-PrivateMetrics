"""Reproducible SIMULATION experiments for the private metrics release.

Every number here comes from the seeded simulation sampler (same distribution
as production; checked in tests/test_mechanism.py) on synthetic fixtures with
the reference task's size and class balance. Runtime and memory are measured
on the production (OpenDP) path. Outputs: experiments/results/*.json and
docs/EXPERIMENT_REPORT.md. Every record carries "mode": "SIMULATION".

Run: python experiments/run_experiments.py [--trials 2000]
"""
from __future__ import annotations

import argparse
import functools
import json
import math
import platform
import random
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from privatemetrics.budget import calibrate_scale, parse_epsilon
from privatemetrics.config import PrivacyConfig, TaskConfig
from privatemetrics.confusion import confusion_table
from privatemetrics.labels import LabelSet
from privatemetrics.ledger import Ledger
from privatemetrics.mechanism import (
    OpenDPSampler,
    SimulationSampler,
)
from privatemetrics.postprocess import clamp, metrics_from_table, privatize_table, round4
from privatemetrics.scorer import PrivateScorer

SEED = 20261013
K, T = 5, 2
REF_COUNTS = [7, 5, 6, 6, 6]
METRICS = ["qwk", "accuracy", "referableSensitivity", "referableSpecificity", "coverage"]
EPSILONS = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0]


def labels_from_counts(counts):
    out, i = {}, 0
    for g, c in enumerate(counts):
        for _ in range(c):
            out[f"i{i:05d}"] = g
            i += 1
    return out


def scaled_counts(n, props):
    raw = [n * p for p in props]
    c = [int(x) for x in raw]
    for j in sorted(range(len(c)), key=lambda j: raw[j] - c[j], reverse=True)[: n - sum(c)]:
        c[j] += 1
    return c


def synthetic_model(labels, quality, coverage, rng):
    """Fixed predictions: correct with prob `quality`, else off by one grade;
    each item predicted with prob `coverage`."""
    preds = {}
    for i, y in labels.items():
        if rng.random() >= coverage:
            continue
        if quality == "constant0":
            preds[i] = 0
        elif rng.random() < quality:
            preds[i] = y
        else:
            preds[i] = min(K - 1, max(0, y + rng.choice([-1, 1])))
    return preds


MODELS = {"strong (90% exact)": (0.9, 1.0), "moderate (65% exact, 90% coverage)": (0.65, 0.9),
          "weak (40% exact)": (0.4, 1.0), "constant grade 0": ("constant0", 1.0)}


def plaintext(labels, preds):
    t = confusion_table(labels, preds, K)
    return metrics_from_table(privatize_table(t.counts, t.column_sums, lambda v: v), t.column_sums, t.matched, t.n, T)


def dp_release(labels, preds, eps, sampler, project=True):
    t = confusion_table(labels, preds, K)
    if project:
        tab = privatize_table(t.counts, t.column_sums, lambda v: sampler.noisy(v, calibrated_scale(eps)))
    else:  # ablation: raw noisy cells, negatives clipped to 0, no column constraint
        flat = sampler.noisy([t.counts[a][b] for b in range(K) for a in range(K)], calibrated_scale(eps))
        tab = [[max(0.0, float(flat[b * K + a])) for b in range(K)] for a in range(K)]
        colsum = [sum(tab[a][b] for a in range(K)) for b in range(K)]
        m = sum(colsum)
        return metrics_from_table(tab, [round(x) for x in colsum], max(1, round(m)), t.n, T) | \
            {"coverage": round4(t.matched / t.n)}
    return metrics_from_table(tab, t.column_sums, t.matched, t.n, T)


def sufficient_stats_release(labels, preds, eps, sampler):
    """Alternative mechanism (ARCHITECTURE: rejected): noise six label-dependent
    sufficient statistics, joint L1 sensitivity 39 for K=5."""
    m = len(preds)
    ys = [(labels[i], p) for i, p in preds.items()]
    stats = [sum(y == p for y, p in ys), sum((y - p) ** 2 for y, p in ys), sum(y for y, _ in ys),
             sum(y * y for y, _ in ys), sum(y >= T for y, _ in ys), sum(y >= T and p >= T for y, p in ys)]
    A, D2, S1y, S2y, POS, TP = sampler.noisy(stats, 39 / eps)
    S1p, S2p = sum(p for _, p in ys), sum(p * p for _, p in ys)
    n_pred_neg = sum(p < T for _, p in ys)
    if m == 0:
        return dict.fromkeys(METRICS, 0.0)
    POS = clamp(POS, 0, m)
    TP = clamp(TP, 0, POS)
    TN = clamp(n_pred_neg - (POS - TP), 0, m - POS)
    den = S2y + S2p - 2 * S1y * S1p / m
    qwk = 1.0 if den <= 0 else 1 - clamp(D2, 0, 16 * m) / den
    return {"qwk": round4(clamp(qwk, -1, 1)), "accuracy": round4(clamp(A / m, 0, 1)),
            "referableSensitivity": round4(TP / POS if POS > 0 else 0.0),
            "referableSpecificity": round4(TN / (m - POS) if m - POS > 0 else 0.0),
            "coverage": round4(m / len(labels))}


def registered_scorer(db, labels, *, cap, task):
    led = Ledger(db)
    led.register_scope("scope-exp", cap)
    led.register_dataset("ds-exp", "scope-exp")
    led.register_version("ds-exp", "v1", labels, group_factor=1)
    led.register_task(TaskConfig(task, K, T), "ds-exp")
    return PrivateScorer(led, task_id=task, version_id="v1", labels=labels, privacy=PrivacyConfig("1"))


@functools.cache
def calibrated_scale(eps):
    """The scale production uses for this ε (certified cost <= ε). Simulations
    use the same scale so that simulated noise matches production."""
    return calibrate_scale(parse_epsilon(str(eps)), OpenDPSampler().epsilon)[0]


def summarize(diffs):
    s = sorted(diffs)
    n = len(s)
    return {"bias": statistics.fmean(s), "sd": statistics.pstdev(s),
            "rmse": math.sqrt(statistics.fmean(d * d for d in s)),
            "p2_5": s[int(0.025 * (n - 1))], "p97_5": s[int(0.975 * (n - 1))],
            "mean_abs": statistics.fmean(abs(d) for d in s),
            "share_within_0_05": sum(abs(d) <= 0.05 for d in s) / n}


def run_grid(labels, preds, eps_list, trials, sampler, fn=dp_release):
    base = plaintext(labels, preds)
    out = {}
    for eps in eps_list:
        rel = [fn(labels, preds, eps, sampler) for _ in range(trials)]
        out[str(eps)] = {m: summarize([r[m] - base[m] for r in rel]) for m in METRICS}
        out[str(eps)]["undefined_sensitivity_rate"] = sum(r["referableSensitivity"] == 0 for r in rel) / trials
    return base, out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=2000)
    args = ap.parse_args()
    rng = random.Random(SEED)
    sampler = SimulationSampler(SEED)
    res_dir = ROOT / "experiments" / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    meta = {"mode": "SIMULATION", "seed": SEED, "trials": args.trials, "python": platform.python_version(),
            "platform": platform.platform(), "fixture": "synthetic labels with reference-task class counts [7,5,6,6,6]"}
    results: dict = {"meta": meta}

    # E1-E7: fidelity on the reference-sized task, per model and epsilon
    ref = labels_from_counts(REF_COUNTS)
    results["reference"] = {}
    for name, (q, cov) in MODELS.items():
        preds = synthetic_model(ref, q, cov, rng)
        base, grid = run_grid(ref, preds, EPSILONS, args.trials, sampler)
        results["reference"][name] = {"plaintext": base, "dp": grid}

    # E8a: sample size at fixed epsilon (same class proportions)
    props = [c / 30 for c in REF_COUNTS]
    results["sample_size"] = {}
    for n in [30, 100, 300, 1000, 3000]:
        lab = labels_from_counts(scaled_counts(n, props))
        preds = synthetic_model(lab, 0.65, 0.9, random.Random(SEED + n))
        base, grid = run_grid(lab, preds, [1.0], max(400, args.trials // 4), sampler)
        results["sample_size"][str(n)] = {"plaintext": base, "dp": grid}

    # E8b: class imbalance (referable prevalence)
    results["imbalance"] = {}
    for prev, counts in {"5%": [57, 38, 3, 1, 1], "20%": [48, 32, 12, 4, 4], "60%": [24, 16, 36, 12, 12]}.items():
        lab = labels_from_counts(counts)
        preds = synthetic_model(lab, 0.65, 1.0, random.Random(SEED + len(prev)))
        base, grid = run_grid(lab, preds, [1.0, 4.0], max(400, args.trials // 4), sampler)
        results["imbalance"][prev] = {"n": 100, "plaintext": base, "dp": grid}

    # Mechanism comparison and ablation on the reference-sized task
    preds = synthetic_model(ref, 0.65, 0.9, random.Random(SEED + 7))
    cmp_ = {}
    for label, fn in {"chosen: projected table": dp_release,
                      "ablation: no column projection": lambda lab, p, e, s: dp_release(lab, p, e, s, project=False),
                      "alternative: sufficient statistics (L1=39)": sufficient_stats_release}.items():
        cmp_[label] = run_grid(ref, preds, [1.0, 4.0], max(400, args.trials // 2), sampler, fn)[1]
    results["mechanism_comparison"] = cmp_

    # E9: runtime and memory on the production (OpenDP) path
    perf = {}
    for n in [30, 1000, 100_000]:
        lab = LabelSet(K, labels_from_counts(scaled_counts(n, props)))
        preds = {"predictions": synthetic_model(lab.labels, 0.65, 0.95, random.Random(n))}
        with tempfile.TemporaryDirectory() as d:
            s = registered_scorer(Path(d) / "l.db", lab, cap="1000", task="perf")
            times = []
            tracemalloc.start()
            for i in range(20):
                t0 = time.perf_counter()
                s.score(submission_id=f"perf{i}", participant_id="p", payload=preds)
                times.append((time.perf_counter() - t0) * 1000)
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
        perf[str(n)] = {"median_ms": statistics.median(times), "max_ms": max(times), "peak_mem_mib": peak / 2**20}
    results["performance"] = {"mode": "PRODUCTION_PATH_TIMING", **perf}

    # E10: budget exhaustion behaviour
    with tempfile.TemporaryDirectory() as d:
        lab = LabelSet(K, ref)
        s = registered_scorer(Path(d) / "l.db", lab, cap="10", task="idrid-dr-grading")
        seq = [s.score(submission_id=f"b{i}", participant_id=f"p{i % 4}", payload={"predictions": ref}) for i in range(13)]
        replay = s.score(submission_id="b0", participant_id="p0", payload={"predictions": ref})
        results["budget"] = {"epsilon_per_release": 1.0, "epsilon_total": 10.0,
                             "outcomes": [o.failure.value if o.failure else "RELEASED" for o in seq],
                             "replay_of_first_is_identical": replay.replayed and replay.metrics == seq[0].metrics,
                             "spent_after": float(Ledger(Path(d) / "l.db").spent("scope-exp"))}

    (res_dir / "experiments.json").write_text(json.dumps(results, indent=2) + "\n")
    write_report(results)
    print("wrote experiments/results/experiments.json and docs/EXPERIMENT_REPORT.md")


def f(x, d=3):
    return f"{x:+.{d}f}" if isinstance(x, float) else str(x)


def write_report(r):
    meta = r["meta"]
    from privatemetrics import ROUTE_VERSION
    L = ["# Experiment report", "",
         f"**Status: DRAFT — NOT YET APPROVED OR INTEGRATED.** Route version `{ROUTE_VERSION}`. "
         "For the reference task with its real public labels, see `REFERENCE_TASK_FIDELITY_REPORT.md`.", "",
         f"**Mode: SIMULATION.** Generated by `experiments/run_experiments.py` (seed {meta['seed']}, "
         f"{meta['trials']} trials per cell, Python {meta['python']}). Do not edit by hand; rerun to regenerate. "
         "Fixtures are synthetic labels with the reference task's size and class counts (7/5/6/6/6), not the "
         "published reference labels, and synthetic models; no real model was evaluated. "
         "Fidelity gap = released − plaintext metric. Simulated noise uses the same calibrated scale as production "
         "(for every ε below it equals the textbook 2/ε, checked by `calibrated_scale`).", ""]
    L += ["## 1. Reference-sized task (n = 30): fidelity gap by ε", "",
          "Plaintext baseline values, then, per ε, the gap's standard deviation, 95% interval and share of releases "
          "within ±0.05 of plaintext. Coverage has no label dependence and is exact (gap 0) in every row.", ""]
    for name, d in r["reference"].items():
        b = d["plaintext"]
        L.append(f"### Model: {name}")
        L.append("")
        L.append("Plaintext: " + ", ".join(f"{m} {b[m]:.4f}" for m in METRICS))
        L.append("")
        L.append("| ε | metric | bias | SD | 95% interval | within ±0.05 |")
        L.append("|---|---|---|---|---|---|")
        for eps, g in d["dp"].items():
            for m in ["qwk", "accuracy", "referableSensitivity", "referableSpecificity"]:
                s = g[m]
                L.append(f"| {eps} | {m} | {f(s['bias'])} | {s['sd']:.3f} | [{f(s['p2_5'])}, {f(s['p97_5'])}] | "
                         f"{s['share_within_0_05']:.0%} |")
        L.append("")
    L += ["## 2. Sample size (ε = 1, moderate model, same class proportions)", "",
          "| n | accuracy SD | QWK SD | sensitivity SD | specificity SD | accuracy within ±0.05 |", "|---|---|---|---|---|---|"]
    for n, d in r["sample_size"].items():
        g = d["dp"]["1.0"]
        L.append(f"| {n} | {g['accuracy']['sd']:.4f} | {g['qwk']['sd']:.4f} | {g['referableSensitivity']['sd']:.4f} | "
                 f"{g['referableSpecificity']['sd']:.4f} | {g['accuracy']['share_within_0_05']:.0%} |")
    L += ["", "## 3. Class imbalance (n = 100, referable prevalence varied)", "",
          "| referable prevalence | ε | sensitivity bias | sensitivity SD | specificity SD | releases with sensitivity reported as 0 |",
          "|---|---|---|---|---|---|"]
    for prev, d in r["imbalance"].items():
        for eps, g in d["dp"].items():
            L.append(f"| {prev} | {eps} | {f(g['referableSensitivity']['bias'])} | {g['referableSensitivity']['sd']:.3f} | "
                     f"{g['referableSpecificity']['sd']:.3f} | {g['undefined_sensitivity_rate']:.1%} |")
    L += ["", "## 4. Mechanism comparison (n = 30, moderate model)", "",
          "| mechanism | ε | accuracy RMSE | QWK RMSE | sensitivity RMSE | specificity RMSE |", "|---|---|---|---|---|---|"]
    for name, g in r["mechanism_comparison"].items():
        for eps, x in g.items():
            L.append(f"| {name} | {eps} | {x['accuracy']['rmse']:.3f} | {x['qwk']['rmse']:.3f} | "
                     f"{x['referableSensitivity']['rmse']:.3f} | {x['referableSpecificity']['rmse']:.3f} |")
    p = r["performance"]
    L += ["", "## 5. Runtime and memory (production OpenDP path, 20 releases each)", "",
          "| items | median ms per release | max ms | peak traced memory (MiB) |", "|---|---|---|---|"]
    for n in ["30", "1000", "100000"]:
        L.append(f"| {n} | {p[n]['median_ms']:.2f} | {p[n]['max_ms']:.2f} | {p[n]['peak_mem_mib']:.2f} |")
    b = r["budget"]
    L += ["", "## 6. Budget exhaustion (ε = 1 per release, cap 10, four participants)", "",
          "Outcomes in order: " + ", ".join(b["outcomes"]) + ".",
          f"Replay of the first submission returned the identical stored release: {b['replay_of_first_is_identical']}. "
          f"ε spent after 13 requests: {b['spent_after']}.", ""]
    (ROOT / "docs" / "EXPERIMENT_REPORT.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
