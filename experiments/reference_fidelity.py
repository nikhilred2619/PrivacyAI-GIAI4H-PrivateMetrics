"""Reference-task fidelity gap: DP scorer vs. the upstream plaintext scorer.

Task: `idrid-dr-grading` (30 items, K = 5, referable threshold T = 2).
Labels: the public upstream fixture (fixtures/reference/labels.csv; provenance
in fixtures/reference/README.md). Predictions: the labelled SYNTHETIC fixtures
in fixtures/reference/predictions/ (no official baseline predictions exist
upstream).

Baseline: upstream `scoring.ts` (verbatim copy in tests/upstream/), executed
with Node on each fixture. The script also checks that this component's
zero-noise computation equals that baseline exactly.

DP releases: the production sampler (OpenDP discrete Laplace, fresh CSPRNG
noise, scale calibrated so that OpenDP's certified ε <= declared ε) through
`scorer.compute_release`, the function every scorer release uses. Results are
therefore statistically, not bit-for-bit, reproducible; `--simulation SEED`
switches to the seeded sampler for exact reproduction.

The end-to-end section additionally runs releases through the full host path
(HostScorer: dispatch check, production ledger, schema check) to confirm the
same distribution and to measure runtime and memory.

Usage:
  python experiments/reference_fidelity.py                 # full run (5000 trials per cell)
  python experiments/reference_fidelity.py --trials 300    # quick run
  python experiments/reference_fidelity.py --simulation 7  # bit-reproducible run, seeded sampler
Outputs: experiments/results/reference_fidelity.json,
         experiments/results/reference_fidelity_summary.csv,
         docs/REFERENCE_TASK_FIDELITY_REPORT.md
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import uuid
from importlib.metadata import version as _pkg_version
from pathlib import Path

from privatemetrics import ROUTE_VERSION
from privatemetrics.budget import calibrate_scale, parse_epsilon
from privatemetrics.confusion import confusion_table
from privatemetrics.host import HostConfig, HostScorer, register_host_task
from privatemetrics.labels import LabelSet
from privatemetrics.mechanism import OpenDPSampler, SimulationSampler
from privatemetrics.postprocess import metrics_from_table, privatize_table
from privatemetrics.scorer import compute_release

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "reference"
UP = ROOT / "tests" / "upstream"
RES = ROOT / "experiments" / "results"
K, T = 5, 2
EPSILONS = ["0.5", "1", "2", "4", "8", "16"]
METRICS = ["qwk", "accuracy", "referableSensitivity", "referableSpecificity", "coverage"]
FIXTURES = ["perfect", "strong", "moderate", "strong-partial", "majority-constant"]
DECLARED_EPS = "8"  # = the selection rule's result in the committed run; see the report, section 7
TOLERANCE = {"accuracy": 0.10, "qwk": 0.10}  # selection criterion, see report


def load_labels() -> dict[str, int]:
    with (FIX / "labels.csv").open(newline="") as fh:
        return {r["Image"]: int(r["dr_grade"]) for r in csv.DictReader(fh)}


def load_fixture(name: str) -> dict[str, int]:
    return json.loads((FIX / "predictions" / f"{name}.json").read_text())["predictions"]


def upstream_baseline(labels: dict[str, int], fixtures: dict[str, dict[str, int]]) -> dict[str, dict] | None:
    node = shutil.which("node")
    if node is None:
        return None
    cases = [{"groundTruth": labels, "predictions": fixtures[n], "numClasses": K, "referableThreshold": T}
             for n in FIXTURES]
    r = subprocess.run([node, "--experimental-strip-types", "--no-warnings", str(UP / "run_scoring.mjs")],
                       input=json.dumps(cases), capture_output=True, text=True, cwd=UP, check=True)
    return dict(zip(FIXTURES, json.loads(r.stdout), strict=True))


def zero_noise(labels: dict[str, int], preds: dict[str, int]) -> dict[str, float]:
    t = confusion_table(labels, preds, K)
    exact = privatize_table(t.counts, t.column_sums, lambda v: v)
    return metrics_from_table(exact, t.column_sums, t.matched, t.n, T)


def clip_only(labels: dict[str, int], preds: dict[str, int], sampler, scale: float) -> dict[str, float]:
    """Ablation (never released): noisy cells clipped at 0, no projection onto
    the known column totals. Used only to attribute post-processing bias."""
    t = confusion_table(labels, preds, K)
    active = [b for b in range(K) if t.column_sums[b] > 0]
    flat = [t.counts[a][b] for b in active for a in range(K)]
    noisy = sampler.noisy(flat, scale)
    tab = [[0.0] * K for _ in range(K)]
    for i, b in enumerate(active):
        for a in range(K):
            tab[a][b] = float(max(noisy[i * K + a], 0))
    return metrics_from_table(tab, t.column_sums, t.matched, t.n, T)


def q(xs: list[float], p: float) -> float:
    s = sorted(xs)
    i = (len(s) - 1) * p
    lo, hi = math.floor(i), math.ceil(i)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def stats(base: float, vals: list[float]) -> dict[str, float]:
    gaps = [v - base for v in vals]
    n = len(gaps)
    mean = statistics.fmean(gaps)
    sd = statistics.stdev(gaps) if n > 1 else 0.0
    half = 1.96 * sd / math.sqrt(n) if n > 1 else 0.0
    return {
        "baseline": base, "mean_released": statistics.fmean(vals), "mean_gap": mean,
        "mean_gap_ci95_low": mean - half, "mean_gap_ci95_high": mean + half, "sd_gap": sd,
        "mae": statistics.fmean(abs(g) for g in gaps), "rmse": math.sqrt(statistics.fmean(g * g for g in gaps)),
        "q025": q(gaps, 0.025), "q25": q(gaps, 0.25), "median": q(gaps, 0.5), "q75": q(gaps, 0.75),
        "q975": q(gaps, 0.975), "within_005": sum(abs(g) <= 0.05 for g in gaps) / n,
        "within_010": sum(abs(g) <= 0.10 for g in gaps) / n,
        "exact": sum(g == 0 for g in gaps) / n,
    }


def end_to_end(n: int) -> dict:
    """Full host path at the declared ε on the `strong` fixture: n releases, each
    a separate dispatch through HostScorer with a production ledger."""
    with tempfile.TemporaryDirectory() as d:
        cfg_raw = json.loads((FIX / "host.json").read_text())
        cfg_raw.update(labels_csv=str(FIX / "labels.csv"), ledger=str(Path(d) / "ledger.db"))
        cfg_path = Path(d) / "host.json"
        cfg_path.write_text(json.dumps(cfg_raw))
        cfg = HostConfig.from_file(cfg_path)
        register_host_task(cfg, epsilon_total=str(int(DECLARED_EPS) * (n + 1)))
        hs = HostScorer(cfg)
        preds = load_fixture("strong")
        worker = {"routeVersion": None, "durationMs": 1000, "predictions": preds}
        out, times = [], []
        tracemalloc.start()
        for _ in range(n):
            disp = {"submissionId": str(uuid.uuid4()), "taskSlug": cfg.task_id, "routeVersion": ROUTE_VERSION}
            t0 = time.perf_counter()
            body = hs.finish(disp, worker)
            times.append((time.perf_counter() - t0) * 1000)
            out.append(body["metrics"])
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        spent = hs.ledger.spent(cfg.scope_id)
    return {"releases": n, "metrics": out, "ms_median": statistics.median(times), "ms_p95": q(times, 0.95),
            "ms_max": max(times), "tracemalloc_peak_mib": peak / 2**20, "spent_epsilon": float(spent)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=5000)
    ap.add_argument("--e2e", type=int, default=120)  # cap = ε·(n+1) must stay <= 1000
    ap.add_argument("--simulation", type=int, default=None, help="seed: use the seeded simulation sampler")
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--out-dir", type=Path, default=RES)
    ap.add_argument("--previous", type=Path, default=RES / "reference_fidelity_previous_v0.2.1-draft.json",
                    help="an earlier committed run to compare against (reproduction check)")
    a = ap.parse_args()

    labels = load_labels()
    lset = LabelSet(K, labels)
    fixtures = {n: load_fixture(n) for n in FIXTURES}
    up = upstream_baseline(labels, fixtures)
    ours0 = {n: zero_noise(labels, fixtures[n]) for n in FIXTURES}
    parity = None if up is None else all(ours0[n] == {m: up[n][m] for m in METRICS} for n in FIXTURES)
    if parity is False:
        print("ERROR: zero-noise output differs from upstream scoring.ts", file=sys.stderr)
        return 1
    baseline = {n: (up[n] if up is not None else ours0[n]) for n in FIXTURES}

    prod = OpenDPSampler()
    sampler = SimulationSampler(a.simulation) if a.simulation is not None else prod
    cells, ablation, rows, all_exact = {}, {}, [], {}
    started = time.perf_counter()
    for eps in EPSILONS:
        scale, certified = calibrate_scale(parse_epsilon(eps), prod.epsilon)
        for name in FIXTURES:
            preds = fixtures[name]
            vals = {m: [] for m in METRICS}
            exact = 0
            for _ in range(a.trials):
                r = compute_release(lset, preds, K, T, sampler, scale)
                exact += all(r[m] == baseline[name][m] for m in METRICS)
                for m in METRICS:
                    vals[m].append(r[m])
            if name == "strong":
                all_exact[eps] = exact / a.trials
            for m in METRICS:
                st = stats(baseline[name][m], vals[m])
                cells[(eps, name, m)] = st
                rows.append({"epsilon": eps, "fixture": name, "metric": m, **st})
            if name == "strong":
                abl = {m: [] for m in METRICS}
                for _ in range(min(a.trials, 2000)):
                    r = clip_only(labels, preds, sampler, scale)
                    for m in METRICS:
                        abl[m].append(r[m])
                ablation[eps] = {m: {"mean_gap": statistics.fmean(v - baseline[name][m] for v in abl[m]),
                                     "rmse": math.sqrt(statistics.fmean((v - baseline[name][m]) ** 2
                                                                        for v in abl[m]))} for m in METRICS}
        cells[(eps, "_scale", "")] = {"scale": scale, "certified": float(certified)}
    mech_seconds = time.perf_counter() - started

    e2e = end_to_end(a.e2e)
    e2e_stats = {m: stats(baseline["strong"][m], [r[m] for r in e2e["metrics"]]) for m in METRICS}
    rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    # ε selection: smallest ε at which, for the strong fixture, at least 80% of
    # releases have |gap| <= 0.10 for both accuracy and QWK.
    def ok(eps: str) -> bool:
        return all(cells[(eps, "strong", m)]["within_010"] >= 0.80 for m in TOLERANCE)
    smallest_ok = next((e for e in EPSILONS if ok(e)), None)

    result = {
        "route_version": ROUTE_VERSION, "task": "idrid-dr-grading", "items": len(labels), "num_classes": K,
        "referable_threshold": T, "trials_per_cell": a.trials,
        "sampler": f"SimulationSampler(seed={a.simulation})" if a.simulation is not None
        else "OpenDP make_laplace (production, CSPRNG)",
        "opendp_version": _pkg_version("opendp"), "python": platform.python_version(),
        "upstream_baseline": "scoring.ts via Node" if up is not None else "UNVERIFIED: Node unavailable; "
                             "this component's zero-noise computation used",
        "zero_noise_parity_with_upstream": parity, "baseline": baseline,
        "epsilons": EPSILONS, "fixtures": FIXTURES, "declared_epsilon": DECLARED_EPS,
        "selection_rule": "smallest ε with >=80% of strong-fixture releases within ±0.10 for accuracy and QWK",
        "smallest_epsilon_meeting_rule": smallest_ok,
        "scales": {e: cells[(e, "_scale", "")] for e in EPSILONS},
        "cells": [r for r in rows], "ablation_clip_only": ablation, "all_exact": all_exact,
        "end_to_end": {**{k: v for k, v in e2e.items() if k != "metrics"}, "stats": e2e_stats},
        "mechanism_seconds_total": mech_seconds, "max_rss_mib": rss_mib,
    }
    out = a.out_dir
    out.mkdir(parents=True, exist_ok=True)
    suffix = "" if a.simulation is None else f"_sim{a.simulation}"
    (out / f"reference_fidelity{suffix}.json").write_text(json.dumps(result, indent=1) + "\n")
    with (out / f"reference_fidelity_summary{suffix}.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    if a.previous.exists() and a.simulation is None:
        result["reproduction"] = compare(json.loads(a.previous.read_text()), result)
        result["reproduction"]["previous_file"] = a.previous.name
        (out / f"reference_fidelity{suffix}.json").write_text(json.dumps(result, indent=1) + "\n")
    if not a.no_report and a.simulation is None:
        write_report(result, cells)
    print(json.dumps({"parity": parity, "smallest_epsilon_meeting_rule": smallest_ok,
                      "seconds": round(mech_seconds, 1)}))
    return 0


def compare(prev: dict, cur: dict) -> dict:
    """Independent reproduction check: does this run's mean released value agree
    with an earlier run's, cell by cell, within 4 combined standard errors?"""
    old = {(c["epsilon"], c["fixture"], c["metric"]): c for c in prev["cells"]}
    rows, worst = [], 0.0
    for c in cur["cells"]:
        o = old.get((c["epsilon"], c["fixture"], c["metric"]))
        if o is None:
            continue
        se = math.sqrt(o["sd_gap"] ** 2 / prev["trials_per_cell"] + c["sd_gap"] ** 2 / cur["trials_per_cell"])
        diff = c["mean_released"] - o["mean_released"]
        z = abs(diff) / se if se > 0 else (0.0 if diff == 0 else math.inf)
        worst = max(worst, z)
        rows.append({"epsilon": c["epsilon"], "fixture": c["fixture"], "metric": c["metric"], "diff": diff, "z": z})
    return {"previous_route_version": prev["route_version"], "previous_trials": prev["trials_per_cell"],
            "cells_compared": len(rows), "cells_within_4se": sum(r["z"] <= 4 for r in rows),
            "max_abs_z": worst, "max_abs_diff": max((abs(r["diff"]) for r in rows), default=0.0)}


def f(x: float, d: int = 3) -> str:
    return f"{x:+.{d}f}"


def write_report(r: dict, cells: dict) -> None:
    eps = r["declared_epsilon"]
    b = r["baseline"]
    L = ["# Reference-task fidelity report", "",
         f"**Status: DRAFT — NOT YET APPROVED OR INTEGRATED.** Route version `{r['route_version']}`.", "",
         "Generated by `experiments/reference_fidelity.py`. Do not edit by hand; rerun to regenerate. Every number "
         "below comes from that run. This is a **local measurement of the noise mechanism on the public reference "
         "task**, run by the contributor. It is not a run on the GI-AI4H platform and not an official challenge "
         "evaluation.", "",
         "## 1. Configuration", "",
         "| Item | Value |", "|---|---|",
         "| Task | `idrid-dr-grading` (GRADING), 30 items, K = 5, referable threshold T = 2 |",
         "| Labels | Public upstream fixture, `fixtures/reference/labels.csv` (SHA-256 `83e8a543…f779b3`); "
         "provenance in `fixtures/reference/README.md` |",
         "| Predictions | **Synthetic** fixtures in `fixtures/reference/predictions/` (seeded perturbations of the "
         "labels; no official baseline predictions are published upstream) |",
         f"| Plaintext baseline | {r['upstream_baseline']} (`tests/upstream/scoring.ts`, verbatim upstream copy) |",
         f"| Zero-noise parity with upstream | {r['zero_noise_parity_with_upstream']} (exact equality on every fixture) |",
         f"| DP sampler | {r['sampler']}; OpenDP {r['opendp_version']}; Python {r['python']} |",
         f"| Trials | {r['trials_per_cell']} independent releases per (ε, fixture) |",
         "| Privacy unit | One item's label, replace-one; pure ε-DP (δ = 0) |",
         "| Randomisation protocol | Each trial is one complete release: exact confusion table → fresh discrete-"
         "Laplace noise on every cell of each active column → projection onto the known column total → metrics → "
         "clamp → round to 4 decimals (`scorer.compute_release`, the function every release uses) |", "",
         "## 2. Plaintext baseline (upstream `scoring.ts`)", "",
         "| Fixture | " + " | ".join(METRICS) + " |", "|---|" + "---|" * len(METRICS)]
    for n in FIXTURES:
        L.append(f"| {n} | " + " | ".join(f"{b[n][m]:.4f}" for m in METRICS) + " |")
    L += ["", "Fixtures: `perfect` = the labels; `strong` = 80% exact, 20% off by one grade; `moderate` = 55% exact, "
          "35% off by one, 10% off by two; `strong-partial` = `strong` with 6 of 30 items omitted (coverage 0.8); "
          "`majority-constant` = every item predicted grade 0.", "",
          f"## 3. Fidelity gap at the declared ε = {eps}", "",
          "Gap = released − plaintext. CI = 95% normal-approximation interval of the mean gap. Quantiles are of the "
          "per-release gap.", ""]
    for n in FIXTURES:
        L += [f"### {n}", "", "| Metric | Baseline | Mean released | Mean gap [95% CI] | SD | MAE | RMSE | "
              "2.5% / 50% / 97.5% | within ±0.05 | within ±0.10 |", "|---|---|---|---|---|---|---|---|---|---|"]
        for m in METRICS:
            c = cells[(eps, n, m)]
            L.append(f"| {m} | {c['baseline']:.4f} | {c['mean_released']:.4f} | {f(c['mean_gap'])} "
                     f"[{f(c['mean_gap_ci95_low'])}, {f(c['mean_gap_ci95_high'])}] | {c['sd_gap']:.3f} | "
                     f"{c['mae']:.3f} | {c['rmse']:.3f} | {f(c['q025'], 2)} / {f(c['median'], 2)} / "
                     f"{f(c['q975'], 2)} | {c['within_005']:.0%} | {c['within_010']:.0%} |")
        L.append("")
    L += ["## 4. Gap across ε (strong fixture)", "",
          "| ε | scale | certified ε | metric | mean gap | SD | RMSE | within ±0.05 | within ±0.10 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for e in r["epsilons"]:
        sc = r["scales"][e]
        for m in ["accuracy", "qwk", "referableSensitivity", "referableSpecificity"]:
            c = cells[(e, "strong", m)]
            L.append(f"| {e} | {sc['scale']:.6g} | {sc['certified']:.6g} | {m} | {f(c['mean_gap'])} | "
                     f"{c['sd_gap']:.3f} | {c['rmse']:.3f} | {c['within_005']:.0%} | {c['within_010']:.0%} |")
    L += ["", "Share of releases whose **five metrics all equal the plaintext values** (strong fixture). At large "
          "ε the noise is so small that many releases are the exact plaintext result; this is what a large ε "
          "means, and it is why the reference ε is a utility choice, not a protection claim:", "",
          "| ε | " + " | ".join(r["epsilons"]) + " |", "|---|" + "---|" * len(r["epsilons"]),
          "| all five metrics exact | " + " | ".join(f"{r['all_exact'][e]:.0%}" for e in r["epsilons"]) + " |"]
    L += ["", "Accuracy RMSE by ε and fixture:", "", "| ε | " + " | ".join(FIXTURES) + " |",
          "|---|" + "---|" * len(FIXTURES)]
    for e in r["epsilons"]:
        L.append(f"| {e} | " + " | ".join(f"{cells[(e, n, 'accuracy')]['rmse']:.3f}" for n in FIXTURES) + " |")
    L += ["", "QWK RMSE by ε and fixture:", "", "| ε | " + " | ".join(FIXTURES) + " |",
          "|---|" + "---|" * len(FIXTURES)]
    for e in r["epsilons"]:
        L.append(f"| {e} | " + " | ".join(f"{cells[(e, n, 'qwk')]['rmse']:.3f}" for n in FIXTURES) + " |")
    L += ["", "## 5. Bias from post-processing", "",
          "Mean gaps are negative: released values understate the plaintext metrics, more so at small ε. The "
          "noise itself is symmetric and unbiased, so the bias comes from what is done with it: each noisy column "
          "is projected onto its known total (cells cannot be negative and must sum to the column count), released "
          "values are clamped to their valid range, and QWK, sensitivity and specificity are nonlinear in the "
          "cells. For a column concentrated on its diagonal, any noise mass the projection keeps lands off the "
          "diagonal, so good models are reported as somewhat worse. The ablation below replaces the projection "
          "with clipping at zero (analysis only, never released) on the `strong` fixture, to show which part of "
          "the bias the projection causes:", "",
          "| ε | metric | mean gap, released (projection) | mean gap, clip-only | RMSE, released | RMSE, clip-only |",
          "|---|---|---|---|---|---|"]
    for e in r["epsilons"]:
        for m in ["accuracy", "qwk"]:
            ab = r["ablation_clip_only"][e][m]
            c = cells[(e, "strong", m)]
            L.append(f"| {e} | {m} | {f(c['mean_gap'])} | {f(ab['mean_gap'])} | {c['rmse']:.3f} | {ab['rmse']:.3f} |")
    L += ["", "Reading: where the clip-only accuracy gap is near zero but the released one is not, the accuracy bias "
          "is caused by the projection. QWK is biased under both variants, so most of its bias comes from noise "
          "entering a nonlinear statistic, not from the projection. The projection is kept because it guarantees that all five released metrics come from one "
          "valid table (non-negative cells that sum to the known column totals). It is **not** uniformly more "
          "accurate: compare the RMSE columns. Changing the post-processing step would not change the privacy "
          "guarantee (post-processing is free) and is a possible improvement; it is not made in this version. The "
          "bias is reported rather than corrected, since a correction would itself depend on the unknown true "
          "table.", "",
          "Coverage has no gap and no noise: it depends only on predictions (`THREAT_MODEL.md` §7.5).", "",
          "## 6. End-to-end host path", "",
          f"{r['end_to_end']['releases']} releases of the `strong` fixture through `HostScorer.finish` (dispatch check, "
          f"production ledger with `BEGIN IMMEDIATE` reservation and persistence, schema check) at ε = {eps}, each "
          "under a new submission ID.", "",
          "| Metric | Mean gap | SD | Mechanism-only mean gap (same ε, §3) | Mechanism-only SD |", "|---|---|---|---|---|"]
    for m in METRICS:
        e2 = r["end_to_end"]["stats"][m]
        c = cells[(eps, "strong", m)]
        L.append(f"| {m} | {f(e2['mean_gap'])} | {e2['sd_gap']:.3f} | {f(c['mean_gap'])} | {c['sd_gap']:.3f} |")
    e = r["end_to_end"]
    L += ["", "| Measurement | Value |", "|---|---|",
          f"| Time per host release (median / p95 / max) | {e['ms_median']:.2f} / {e['ms_p95']:.2f} / "
          f"{e['ms_max']:.2f} ms |",
          f"| Peak traced Python allocation during the {e['releases']} releases | {e['tracemalloc_peak_mib']:.2f} MiB |",
          f"| Maximum resident set size of the whole experiment process | {r['max_rss_mib']:.0f} MiB |",
          f"| ε charged to the scope by the end-to-end run | {e['spent_epsilon']:g} ({e['releases']} × {eps}) |",
          f"| Mechanism-only releases, total wall time | {r['mechanism_seconds_total']:.1f} s for "
          f"{len(r['epsilons']) * len(FIXTURES) * r['trials_per_cell']} releases |",
          "", "Timing varies by machine; these are observations on the contributor's environment, not caps.", "",
          "## 7. ε selection", "",
          f"Rule: {r['selection_rule']}. Smallest ε meeting it in this run: **{r['smallest_epsilon_meeting_rule']}**. "
          f"Declared reference-demonstration ε: **{eps}**.", "",
          "This is a utility rule for a 30-item demonstration, not a privacy recommendation. At this ε one "
          "release is a weak guarantee for any individual label. The reference labels are public, so nothing "
          "secret is at stake on the reference task; the ε is chosen so that the demonstration shows usable "
          "numbers.", "",
          "Three different numbers must be kept apart:", "",
          "| Quantity | Reference demonstration | Host task |", "|---|---|---|",
          f"| ε per release | {eps} (this report) | **[DECISION G4]**, set by the host. At ε = 1, accuracy SD is "
          "about 0.02 at 300 items and 0.006 at 1,000 (`EXPERIMENT_REPORT.md` §2) |",
          f"| Global cap per privacy scope | {int(eps) * 10} in the demonstration (`scripts/host_demo.py`): "
          "10 releases | **[DECISION G4]**, set by the host at registration |",
          "| Patient-level ε | g × label-level ε; g = 2 declared for the reference slice (unverified; patients are "
          "not identified upstream) | g declared and, where a patient map exists, checked by the host |", "",
          "Cost of repeated releases (basic composition, pure DP): k releases at ε each cost k·ε against one scope, "
          "whoever submits them. Upstream quotas (3 per task per week, 10 per task, per participant) do not bound "
          "the total across participants; the scope cap does.", "",
          "| Releases k | Label-level ε at ε = " + eps + " | Patient-level ε with g = 2 |", "|---|---|---|"]
    for k in (1, 3, 10, 30):
        L.append(f"| {k} | {k * int(eps)} | {2 * k * int(eps)} |")
    L += ["", "## 8. Figures", "",
          "Generated from `experiments/results/reference_fidelity.json` by `python experiments/plot_fidelity.py` "
          "(needs matplotlib, `pip install -e \".[plots]\"`).", "",
          "![Privacy-utility trade-off](figures/fidelity_privacy_utility.png)", "",
          "*RMSE of released accuracy and QWK against ε, per fixture (log-scaled ε).*", "",
          "![Gap distribution, strong fixture](figures/fidelity_gap_bands.png)", "",
          "*Strong fixture: mean gap (line) with the 2.5–97.5% and 25–75% ranges of the per-release gap (bands), "
          "per metric and ε.*", "",
          "![Post-processing bias](figures/fidelity_bias_ablation.png)", "",
          "*Strong fixture: mean gap of released values (projection) and of the clip-only ablation.*"]
    L += ["", "## 9. Reproduction", "", "```sh",
          "python experiments/reference_fidelity.py                  # this report (fresh production noise)",
          "python experiments/reference_fidelity.py --simulation 7   # bit-reproducible run with the seeded sampler",
          "python fixtures/reference/make_prediction_fixtures.py --check",
          "```", "",
          "Production-noise reruns differ release by release; summary statistics agree within their CIs "
          "(`tests/test_host_demo_and_fidelity.py` checks this on a reduced run). Raw per-cell results: "
          "`experiments/results/reference_fidelity.json` and `reference_fidelity_summary.csv`."]
    rep = r.get("reproduction")
    if rep:
        L += ["", "### Agreement with the previous committed run", "",
              f"This run was compared cell by cell with the earlier committed run (route `{rep['previous_route_version']}`, "
              f"{rep['previous_trials']} trials per cell, kept as `experiments/results/{rep.get('previous_file', '')}`; "
              "an earlier run is kept as `reference_fidelity_previous_v0.2.0.json`). The mechanism code is the same in all "
              "three. A cell agrees if the two mean released values differ by at most 4 combined standard errors.", "",
              "| Cells compared | Within 4 SE | Largest z (absolute) | Largest absolute difference in mean released value |",
              "|---|---|---|---|",
              f"| {rep['cells_compared']} | {rep['cells_within_4se']} | {rep['max_abs_z']:.2f} | {rep['max_abs_diff']:.4f} |"]
    L += ["", "## 10. Limitations", "",
          "* Predictions are synthetic fixtures, not real models. The gap depends on the model's confusion "
          "structure; real submissions will differ.",
          "* 30 items is very small: the noise needed for one label's protection is large relative to each cell. "
          "This is the main utility limitation of label-DP on this task (`THREAT_MODEL.md` §8.1).",
          "* Measured locally, not on the platform. A platform run needs integration (`GAP_ANALYSIS.md` G1).",
          "* The reference labels are public; the measurement shows behaviour and utility, not protection.",
          "* Released values are biased (§5); the bias is reported, not corrected.", ""]
    (ROOT / "docs" / "REFERENCE_TASK_FIDELITY_REPORT.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    sys.exit(main())
