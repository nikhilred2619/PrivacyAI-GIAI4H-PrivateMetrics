"""Real sealed execution (upstream worker + Docker) feeding the host scorer.

Runs integration/sealed_run/run_sealed_host.py. Needs a Docker daemon and the
upstream checkout next to this repository; otherwise SKIPPED and reported as
UNVERIFIED, never as passed."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_S16_real_sealed_run_through_upstream_worker_and_host_scorer():
    r = subprocess.run([sys.executable, "-B", str(ROOT / "integration" / "sealed_run" / "run_sealed_host.py")],
                       capture_output=True, text=True, timeout=900, cwd=ROOT)
    if r.returncode == 77:
        if os.environ.get("PM_REQUIRE_SEALED_RUN") == "1":  # set in CI: a skip there is a failure
            pytest.fail("sealed run required but not executed: " + r.stdout.strip())
        pytest.skip("UNVERIFIED: " + r.stdout.strip())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout
