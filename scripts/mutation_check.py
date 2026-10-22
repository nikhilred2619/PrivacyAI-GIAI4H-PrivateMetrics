"""Negative control: disable one control at a time and confirm the test suite
catches it. Writes experiments/results/mutation-check.json."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUTANTS = [
    ("no noise", "src/privatemetrics/postprocess.py", "    noisy = noise(flat)\n", "    noisy = flat\n",
     "tests/test_security.py::test_S5_single_item_subgroup_is_noised"),
    ("ledger ignores cap", "src/privatemetrics/ledger.py", "            if spent + need > cap[0]:\n",
     "            if False:\n", "tests/test_security.py::test_S1_repeated_requests_spend_budget_until_blocked"),
    ("replay re-samples", "src/privatemetrics/ledger.py",
     '                if status == "RELEASED":\n                    return Reservation("replay", rid, json.loads(mj), mode=rmode)\n',
     '                if status == "RELEASED":\n                    return Reservation("exhausted")\n',
     "tests/test_security.py::test_S2_replay_returns_identical_release_without_spending"),
    ("per-participant budget", "src/privatemetrics/ledger.py",
     '"SELECT COALESCE(SUM(charged_units),0) FROM releases WHERE scope_id=?",\n                              (binding.scope_id,)',
     '"SELECT COALESCE(SUM(charged_units),0) FROM releases WHERE scope_id=? AND participant_id=?",\n                              (binding.scope_id, participant_id)',
     "tests/test_security.py::test_S3_budget_is_shared_across_participants"),
    ("check-then-act race (no transaction)", "src/privatemetrics/ledger.py",
     [('            conn.execute("PRAGMA foreign_keys=ON")\n            conn.execute("BEGIN IMMEDIATE")\n',
       '            conn.execute("PRAGMA foreign_keys=ON")\n'),
      ('                yield conn\n                conn.execute("COMMIT")\n', '                yield conn\n'),
      ('            except BaseException:\n                conn.execute("ROLLBACK")\n                raise\n\n    # --- registry',
       '            except BaseException:\n                raise\n\n    # --- registry'),
      ('            if spent + need > cap[0]:\n', '            time.sleep(0.02)  # widen the race window\n            if spent + need > cap[0]:\n')],
     None, "tests/test_security.py::test_S10_concurrent_threads_never_overspend"),
    ("uncalibrated scale (textbook 2/eps)", "src/privatemetrics/budget.py",
     "        if certified <= declared:\n", "        if True:\n",
     "tests/test_budget.py::test_D1b_calibrated_scale_certified_cost_never_exceeds_declared"),
    ("crash refunds budget", "src/privatemetrics/ledger.py",
     "UPDATE releases SET status='FAILED_SPENT', updated_at=? WHERE release_id=? AND status='RESERVED'\",\n                      (time.time(), release_id)",
     "DELETE FROM releases WHERE release_id=? AND ?>0\",\n                      (release_id, time.time())",
     "tests/test_security.py::test_S11_crash_after_reserve_keeps_budget_spent"),
    ("D1 round-to-nearest grid", "src/privatemetrics/budget.py",
     "    if (f / QUANTUM).denominator != 1:\n        raise BudgetValueError(f\"{name} must be a multiple of the accounting precision 1e-6\")\n",
     "    f = round(f / QUANTUM) * QUANTUM\n",
     "tests/test_budget.py::test_D1_values_off_the_accounting_grid_are_rejected"),
    ("D2 scope from label content", "src/privatemetrics/ledger.py",
     "        return Binding(task_id, dataset_id, version_id, scope_id, k, thr, gf)\n",
     "        return Binding(task_id, dataset_id, version_id, \"ls_\" + labels.content_fingerprint[:16], k, thr, gf)\n",
     "tests/test_identity.py::test_D2_one_label_changed_new_version_shares_budget"),
    ("D2 no item-overlap check", "src/privatemetrics/ledger.py",
     "            if overlap:\n", "            if False:\n",
     "tests/test_identity.py::test_minimally_modified_copy_in_new_scope_refused_by_item_overlap"),
    ("D3 replay ignores owner", "src/privatemetrics/ledger.py",
     "                if (pid, tid, vid, sid, ph, units) != (participant_id,", "                if (tid, vid, sid, ph, units) != (",
     "tests/test_replay.py::test_D3_other_participant_cannot_fetch_stored_release"),
    ("D8 ledger serves rows across modes", "src/privatemetrics/ledger.py",
     "                if rmode != mode:  # defence in depth: never serve a row across modes\n",
     "                if False:\n",
     "tests/test_modes.py::test_cross_mode_row_is_a_conflict_at_the_ledger"),
    ("D8 cross-mode replay (ledger and scorer checks removed)", None,
     [("src/privatemetrics/ledger.py",
       "                if rmode != mode:  # defence in depth: never serve a row across modes\n",
       "                if False:\n"),
      ("src/privatemetrics/scorer.py",
       "            if r.mode != self.mode:  # unreachable while the ledger enforces it; checked again here\n",
       "            if False:\n")],
     None, "tests/test_modes.py::test_cross_mode_replay_after_restart_releases_nothing"),
    ("D8 ledger file mode not enforced", "src/privatemetrics/ledger.py",
     [("            if stored is None or stored[0] != mode:\n", "            if False:\n")],
     None, "tests/test_modes.py::test_simulation_file_cannot_be_reopened_as_production_after_restart"),
    ("D8 scorer accepts other-mode ledger", "src/privatemetrics/scorer.py",
     "        if ledger.mode != self.mode:\n", "        if False:\n",
     "tests/test_modes.py::test_simulation_scorer_refused_on_production_ledger"),
    ("D8 adapter trusts the simulation flag only", "src/privatemetrics/adapter.py",
     "    if outcome.simulation or outcome.mode != PRODUCTION:\n", "    if outcome.simulation:\n",
     "tests/test_modes.py::test_adapter_refuses_outcome_whose_mode_was_tampered"),
    ("H1 worker metrics forwarded", "src/privatemetrics/host.py",
     '            return _failure(_INTERNAL, "worker result carried metrics; refused by host scorer", duration)\n',
     '            return {"routeVersion": ROUTE_VERSION, "durationMs": duration, "metrics": dict(wb["metrics"])}\n',
     "tests/test_host.py::test_H4c_worker_metrics_are_never_forwarded"),
    ("H2 result schema gate removed", "src/privatemetrics/host.py",
     "            check_result_body(body, proposed_codes=self.proposed_codes)\n", "            pass\n",
     "tests/test_host.py::test_H4g_schema_check_is_the_last_gate"),
    ("H3 route version not checked", "src/privatemetrics/host.py",
     "        if rv != ROUTE_VERSION:\n", "        if False:\n",
     "tests/test_host.py::test_H5b_route_version_is_required_and_exact"),
    ("D9 non-canonical submission UUID accepted", "src/privatemetrics/host.py",
     "            if not isinstance(sid, str) or str(uuid.UUID(sid)) != sid:\n",
     "            if not isinstance(sid, str) or str(uuid.UUID(sid)) != sid.lower():\n",
     "tests/test_host.py::test_H5_dispatch_for_another_task_route_or_bad_id_is_refused"),
    ("H4 preflight ignores the budget", "src/privatemetrics/host.py",
     "        if self.ledger.remaining(self.scorer.binding.scope_id) < self.scorer.charge:\n", "        if False:\n",
     "tests/test_host.py::test_H6b_preflight_refuses_to_run_a_model_when_budget_is_exhausted"),
    ("H5 understated group factor accepted", "src/privatemetrics/labels.py",
     "    if observed > declared:\n", "    if False:\n",
     "tests/test_host.py::test_H7_group_factor_validated_against_patient_map"),
    ("D10 absent route version accepted", "src/privatemetrics/host.py",
     '        if not isinstance(rv, str) or not 1 <= len(rv) <= 64:\n            raise DispatchError("dispatch must name a route version")\n',
     "        if rv is None:\n            return sid\n",
     "tests/test_host.py::test_H5b_route_version_is_required_and_exact"),
    ("D11 duplicate patient-map rows accepted", "src/privatemetrics/host.py",
     "            if item in out:\n                # A later row", "            if False:\n                # A later row",
     "tests/test_host.py::test_H7d_patient_map_with_duplicate_items_is_refused"),
    ("D12 preflight runs models for unreleasable submissions", "src/privatemetrics/host.py",
     '        if status is not None:\n            return _failure(_INTERNAL, "submission cannot be scored again")\n', "",
     "tests/test_host.py::test_H6d_preflight_refuses_submissions_that_can_never_be_released"),
    ("D13 world-readable ledger file", "src/privatemetrics/ledger.py",
     "os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600", "os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644",
     "tests/test_host.py::test_H8f_new_ledger_files_are_owner_only"),
    ("D14 worker failure detail forwarded", "src/privatemetrics/host.py",
     '"failure": {"code": code, "detail": f"{code} reported by the host\'s sealed-execution worker"}}',
     '"failure": {"code": code, "detail": f.get("detail")}}',
     "tests/test_host.py::test_H4h_worker_failure_detail_never_leaves_the_host"),
]



def run(test):
    # No bytecode cache: a same-size mutant restored within one second would
    # otherwise leave stale .pyc files behind.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", test],
                       cwd=ROOT, capture_output=True, text=True, env=env)
    return r.returncode == 0


rows = []
for name, path, old, new, test in MUTANTS:
    # path None: the edits span several files and are given as (path, anchor, replacement)
    edits = old if path is None else [(path, a, b) for a, b in (old if isinstance(old, list) else [(old, new)])]
    originals: dict[Path, str] = {}
    for p, a, b in edits:
        f = ROOT / p
        originals.setdefault(f, f.read_text())
        cur = f.read_text()
        assert cur.count(a) == 1, f"mutation anchor not unique or not found: {name}"
        f.write_text(cur.replace(a, b, 1))
    try:
        passed = run(test)
    finally:
        for f, src in originals.items():
            f.write_text(src)
    rows.append({"mutation": name, "test": test, "test_passed_under_mutation": passed, "caught": not passed})
    print(("CAUGHT " if not passed else "MISSED ") + name)
out = ROOT / "experiments" / "results"
out.mkdir(parents=True, exist_ok=True)
(out / "mutation-check.json").write_text(json.dumps(rows, indent=2) + "\n")
sys.exit(0 if all(r["caught"] for r in rows) else 1)
