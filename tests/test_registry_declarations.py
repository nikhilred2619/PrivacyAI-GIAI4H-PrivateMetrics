"""The route-registry declarations (registry/*.json) are valid for the
platform's own Zod schemas and agree with this repository.

The Zod validation needs the upstream checkout, Node and npm; without them it
is SKIPPED and reported as UNVERIFIED."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from privatemetrics import ROUTE_VERSION

ROOT = Path(__file__).resolve().parents[1]
REG = ROOT / "registry"


def _validate(*extra):
    r = subprocess.run([sys.executable, "-B", str(REG / "validate_declarations.py"), *extra],
                       capture_output=True, text=True, timeout=300)
    if r.returncode == 77:
        if os.environ.get("PM_REQUIRE_REGISTRY_CHECK") == "1":  # set in CI
            pytest.fail("registry schema check required but not executed: " + r.stdout.strip())
        pytest.skip("UNVERIFIED: " + r.stdout.strip())
    return r


def test_R1_declarations_parse_with_upstream_route_registry_schemas():
    r = _validate()
    assert r.returncode == 0, r.stdout + r.stderr


def test_R2_validator_rejects_an_unregistrable_version(tmp_path):
    bad = json.loads((REG / "route-version.json").read_text())
    bad["version"] = ROUTE_VERSION + "-draft"
    bad["threatModel"]["outOfScope"] = []
    (tmp_path / "v.json").write_text(json.dumps(bad))
    r = _validate("--version", str(tmp_path / "v.json"))
    assert r.returncode == 1 and "MAJOR.MINOR.PATCH" in r.stdout


def test_R3_declarations_agree_with_code_and_committed_measurements():
    v = json.loads((REG / "route-version.json").read_text())
    assert v["version"] == ROUTE_VERSION
    fid = json.loads((ROOT / "experiments" / "results" / "reference_fidelity.json").read_text())
    cell = next(c for c in fid["cells"] if c["epsilon"] == fid["declared_epsilon"] and c["fixture"] == "strong"
                and c["metric"] == "accuracy")
    gap = v["operationalEnvelope"]["fidelityGap"]
    assert gap["metric"] == "accuracy" and gap["delta"] == round(cell["mean_gap"], 3)
    assert f"eps={fid['declared_epsilon']}" in gap["measuredOn"] and f"{fid['trials_per_cell']:,}" in gap["measuredOn"]
