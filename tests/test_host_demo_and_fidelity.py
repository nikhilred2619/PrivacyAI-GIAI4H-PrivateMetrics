"""The documented host demonstration and the fidelity measurement are reproducible."""
import csv
import json
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

from privatemetrics.budget import calibrate_scale, parse_epsilon
from privatemetrics.labels import LabelSet
from privatemetrics.mechanism import OpenDPSampler, SimulationSampler
from privatemetrics.scorer import compute_release

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "reference"


def test_host_demo_runs_from_the_documented_command(tmp_path):
    r = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "host_demo.py"), "--workdir",
                        str(tmp_path / "demo"), "--no-transcript"], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and "ALL CHECKS PASSED" in r.stdout, r.stdout + r.stderr


def test_reference_fixture_is_the_upstream_file_and_predictions_regenerate():
    import hashlib
    assert hashlib.sha256((FIX / "labels.csv").read_bytes()).hexdigest() == \
        "83e8a543ae604e83a95d3f29bc77a1589f7bdf19e1565354d314cb3c9ff779b3"
    r = subprocess.run([sys.executable, "-B", str(FIX / "make_prediction_fixtures.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def _ref():
    with (FIX / "labels.csv").open(newline="") as fh:
        lab = {r["Image"]: int(r["dr_grade"]) for r in csv.DictReader(fh)}
    preds = json.loads((FIX / "predictions" / "strong.json").read_text())["predictions"]
    return LabelSet(5, lab), preds


def test_fidelity_simulation_mode_is_bit_reproducible(tmp_path):
    outs = []
    for d in ("a", "b"):
        r = subprocess.run([sys.executable, "-B", str(ROOT / "experiments" / "reference_fidelity.py"), "--simulation",
                            "3", "--trials", "40", "--e2e", "2", "--no-report", "--out-dir", str(tmp_path / d)],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr
        outs.append(json.loads((tmp_path / d / "reference_fidelity_sim3.json").read_text())["cells"])
    assert outs[0] == outs[1]


def test_committed_fidelity_numbers_are_reproduced_by_fresh_production_noise():
    """A fresh production run agrees with the committed report within 4 standard errors."""
    committed = json.loads((ROOT / "experiments" / "results" / "reference_fidelity.json").read_text())
    if committed["trials_per_cell"] < 1000:
        pytest.skip("UNVERIFIED: committed results are from a reduced run")
    lset, preds = _ref()
    sampler = OpenDPSampler()
    for eps in ("4", "8"):
        scale, _ = calibrate_scale(parse_epsilon(eps), sampler.epsilon)
        runs = [compute_release(lset, preds, 5, 2, sampler, scale) for _ in range(600)]
        for m in ("accuracy", "qwk"):
            cell = next(c for c in committed["cells"] if c["epsilon"] == eps and c["fixture"] == "strong"
                        and c["metric"] == m)
            mean = statistics.fmean(r[m] for r in runs)
            se = cell["sd_gap"] / 600 ** 0.5
            assert abs(mean - cell["mean_released"]) <= 4 * se + 1e-9, (eps, m, mean, cell["mean_released"])


def test_simulation_sampler_matches_production_distribution_on_reference_task():
    lset, preds = _ref()
    scale, _ = calibrate_scale(parse_epsilon("4"), OpenDPSampler().epsilon)
    a = [compute_release(lset, preds, 5, 2, OpenDPSampler(), scale)["accuracy"] for _ in range(1500)]
    sim = SimulationSampler(5)
    b = [compute_release(lset, preds, 5, 2, sim, scale)["accuracy"] for _ in range(1500)]
    se = (statistics.pvariance(a) / 1500 + statistics.pvariance(b) / 1500) ** 0.5
    assert abs(statistics.fmean(a) - statistics.fmean(b)) <= 4 * se
