"""Run every available check and write a short summary of what actually ran.

Writes experiments/results/verification.json (machine-readable) and
docs/VERIFICATION_SUMMARY.md (human-readable). A step that cannot run is
recorded as NOT EXECUTED with the reason; nothing is reported as passed unless
its command ran and succeeded.

Usage: python scripts/verify_all.py [--full-fidelity]
  By default the fidelity experiment is re-run with 300 trials into a temporary
  directory, so the committed measurements are not changed. --full-fidelity
  re-runs the 5,000-trial experiment and REGENERATES the committed report and
  figures (the declarations quote that report; update them if numbers move).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "experiments" / "results"
PY = sys.executable
ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def run(name: str, cmd: list[str], timeout: int = 1800) -> dict:
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=ENV)
    except FileNotFoundError as exc:
        return {"step": name, "status": "NOT EXECUTED", "reason": f"command not found: {exc.filename}"}
    except subprocess.TimeoutExpired:
        return {"step": name, "status": "FAIL", "reason": f"timed out after {timeout} s"}
    out = (r.stdout + r.stderr).strip().splitlines()
    status = "PASS" if r.returncode == 0 else ("NOT EXECUTED" if r.returncode == 77 else "FAIL")
    return {"step": name, "status": status, "exit": r.returncode, "seconds": round(time.monotonic() - t0, 1),
            "tail": out[-6:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-fidelity", action="store_true")
    a = ap.parse_args()
    # Recorded before any step runs: the steps regenerate tracked reports.
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip() or "n/a"
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip())
    steps = [
        run("ruff", [PY, "-m", "ruff", "check", "."]),
        run("mypy", [PY, "-m", "mypy", "src"]),
        run("pytest + security report (JUnit)", [PY, "-B", "scripts/security_report.py"]),
        run("mutation check", [PY, "-B", "scripts/mutation_check.py"]),
        run("prediction fixtures regenerate exactly", [PY, "-B", "fixtures/reference/make_prediction_fixtures.py", "--check"]),
        run("host demonstration (CLI)", [PY, "-B", "scripts/host_demo.py"]),
        run("real sealed run (upstream worker + Docker)", [PY, "-B", "integration/sealed_run/run_sealed_host.py"]),
        run("route-registry declarations vs upstream Zod schemas", [PY, "-B", "registry/validate_declarations.py"]),
    ]
    if not a.full_fidelity:
        tmp = tempfile.mkdtemp(prefix="fid-")
        steps.append(run("reference fidelity (reduced, 300 trials, not committed)",
                         [PY, "-B", "experiments/reference_fidelity.py", "--trials", "300", "--e2e", "20", "--no-report",
                          "--out-dir", tmp]))
    else:
        steps.append(run("reference fidelity (5,000 trials)", [PY, "-B", "experiments/reference_fidelity.py"]))
        steps.append(run("fidelity figures", [PY, "-B", "experiments/plot_fidelity.py"]))

    tests = json.loads((RES / "test-results.json").read_text()) if (RES / "test-results.json").exists() else None
    mutants = json.loads((RES / "mutation-check.json").read_text()) if (RES / "mutation-check.json").exists() else []
    sealed = json.loads((RES / "sealed_run.json").read_text()) if (RES / "sealed_run.json").exists() else None
    fid = json.loads((RES / "reference_fidelity.json").read_text()) if (RES / "reference_fidelity.json").exists() else None
    sys.path.insert(0, str(ROOT / "src"))
    from privatemetrics import ROUTE_VERSION
    summary = {
        "route_version": ROUTE_VERSION, "commit": commit, "uncommitted_changes": dirty,
        "when_utc": datetime.now(UTC).isoformat(timespec="seconds"), "python": platform.python_version(),
        "platform": platform.platform(), "steps": steps,
        "tests": tests and tests["counts"] | {"total": tests["total"]},
        "mutants": {"caught": sum(m["caught"] for m in mutants), "total": len(mutants)},
        "sealed_run_checks": sealed and {"passed": sum(sealed["checks"].values()), "total": len(sealed["checks"])},
        "fidelity_reproduction": fid and fid.get("reproduction"),
    }
    RES.mkdir(parents=True, exist_ok=True)
    (RES / "verification.json").write_text(json.dumps(summary, indent=1) + "\n")

    L = ["# Verification summary", "",
         f"**Status: DRAFT — NOT YET APPROVED OR INTEGRATED.** Route version `{ROUTE_VERSION}`.", "",
         "Generated by `python scripts/verify_all.py`. These are **contributor-run** checks on the contributor's "
         "machine, not an independent review and not a run on the GI-AI4H platform.", "",
         f"Run at {summary['when_utc']} on Python {summary['python']} ({summary['platform']}); commit `{commit[:12]}`"
         + (" with uncommitted changes" if dirty else " (clean working tree)") + ".", "",
         "| Step | Result | Time (s) |", "|---|---|---|"]
    for s in steps:
        L.append(f"| {s['step']} | **{s['status']}**{' — ' + s['reason'] if s.get('reason') else ''} | {s.get('seconds', '')} |")
    L += ["", "| Evidence | Value |", "|---|---|"]
    if tests:
        c = tests["counts"]
        L.append(f"| Tests (`experiments/results/test-results.json`) | {c['PASS']} passed, {c['FAIL']} failed, "
                 f"{c['UNVERIFIED']} not executed (skipped) of {tests['total']} |")
    L.append(f"| Security mutants caught (`experiments/results/mutation-check.json`) | "
             f"{summary['mutants']['caught']} of {summary['mutants']['total']} |")
    if tests:
        docker = [t for t in tests["tests"] if t["module"] == "test_sealed_integration"]
        L.append("| Real-Docker test in the suite | " + ", ".join(f"`{t['test']}`: {t['result']}" for t in docker) + " |")
    if sealed:
        L.append(f"| Real sealed-run checks (`SEALED_RUN_TRANSCRIPT.md`) | {summary['sealed_run_checks']['passed']} of "
                 f"{summary['sealed_run_checks']['total']} passed |")
    if fid and fid.get("reproduction"):
        rep = fid["reproduction"]
        L.append(f"| Fidelity reproduction against the previous run | {rep['cells_within_4se']} of {rep['cells_compared']} "
                 f"cells within 4 SE (largest z = {rep['max_abs_z']:.2f}) |")
    L += ["", "Any step marked NOT EXECUTED or FAIL above is exactly that; see the step's output in "
          "`experiments/results/verification.json`.", ""]
    (ROOT / "docs" / "VERIFICATION_SUMMARY.md").write_text("\n".join(L) + "\n")
    print("\n".join(f"{s['status']:13} {s['step']}" for s in steps))
    return 0 if all(s["status"] == "PASS" for s in steps) else 1


if __name__ == "__main__":
    sys.exit(main())
