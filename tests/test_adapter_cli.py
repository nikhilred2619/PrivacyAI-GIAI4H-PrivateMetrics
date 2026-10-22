"""Upstream wire format and command-line entry point."""
import csv
import json

import pytest
from conftest import perfect

from privatemetrics import ROUTE_VERSION
from privatemetrics.adapter import to_sealed_run_result
from privatemetrics.cli import main
from privatemetrics.errors import FailureCode
from privatemetrics.scorer import Outcome

KEYS = {"qwk", "accuracy", "referableSensitivity", "referableSpecificity", "coverage"}


def test_metrics_body_matches_upstream_shape(make_scorer, labelset):
    o = make_scorer().score(submission_id="s", participant_id="p", payload=perfect(labelset))
    body = to_sealed_run_result(o, route_version=ROUTE_VERSION, duration_ms=12)
    assert set(body) == {"routeVersion", "durationMs", "metrics"}  # exactly one of metrics/failure
    assert set(body["metrics"]) == KEYS and all(isinstance(v, float) for v in body["metrics"].values())


def test_budget_exhaustion_maps_to_closed_upstream_taxonomy():
    o = Outcome(None, FailureCode.PRIVACY_BUDGET_EXHAUSTED, "privacy budget for this task is exhausted")
    assert to_sealed_run_result(o, route_version="v", duration_ms=0)["failure"]["code"] == "INTERNAL_ERROR"
    assert to_sealed_run_result(o, route_version="v", duration_ms=0, proposed_codes=True)["failure"]["code"] == \
        "PRIVACY_BUDGET_EXHAUSTED"


def test_body_validates_against_upstream_pydantic_contract_if_available(make_scorer, labelset):
    """Cross-check with the upstream worker's own Pydantic model when the upstream
    checkout is present next to this repository (UNVERIFIED otherwise)."""
    import sys
    from pathlib import Path
    up = Path(__file__).resolve().parents[2] / "upstream" / "apps" / "worker-eval" / "src"
    if not up.exists():
        pytest.skip("UNVERIFIED: upstream worker-eval checkout not found")
    pytest.importorskip("pydantic")
    sys.path.insert(0, str(up))
    from worker_eval.contract import SealedRunResult
    o = make_scorer().score(submission_id="s", participant_id="p", payload=perfect(labelset))
    SealedRunResult.model_validate(to_sealed_run_result(o, route_version=ROUTE_VERSION, duration_ms=5))
    f = Outcome(None, FailureCode.UNKNOWN_ITEM_IDS, "predictions contain unknown item ids")
    SealedRunResult.model_validate(to_sealed_run_result(f, route_version=ROUTE_VERSION, duration_ms=5))


def test_cli_register_score_and_report(tmp_path, labelset, capsys):
    lab = tmp_path / "labels.csv"
    with lab.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Image", "dr_grade"])
        w.writerows(labelset.labels.items())
    pred = tmp_path / "predictions.json"
    pred.write_text(json.dumps(perfect(labelset)))
    led = str(tmp_path / "l.db")
    assert main(["register", "--ledger", led, "--scope-id", "scope-x", "--epsilon-total", "1",
                 "--dataset-id", "ds-x", "--version-id", "v1", "--labels", str(lab), "--group-factor", "1"]) == 0
    capsys.readouterr()
    args = ["score", "--ledger", led, "--version-id", "v1", "--labels", str(lab), "--predictions", str(pred),
            "--participant-id", "p", "--epsilon", "1"]
    assert main([*args, "--submission-id", "a"]) == 0
    assert set(json.loads(capsys.readouterr().out)["metrics"]) == KEYS
    assert main([*args, "--submission-id", "b"]) == 2  # budget exhausted
    assert json.loads(capsys.readouterr().out)["failure"]["code"] == "INTERNAL_ERROR"
    assert main(["ledger", "--ledger", led]) == 0
    row = json.loads(capsys.readouterr().out.splitlines()[0])
    assert row == {"scope": "scope-x", "cap": "1", "spent": "1", "releases": 1,
                   "patient_level_spent": "1", "patient_level_cap_bound": "1"}
    assert main(["recover", "--ledger", led, "--older-than", "3600"]) == 0
    assert json.loads(capsys.readouterr().out) == {"recovered": 0}
    assert main(["register", "--ledger", led, "--scope-id", "scope-x", "--epsilon-total", "50",
                 "--dataset-id", "ds-x", "--version-id", "v1", "--labels", str(lab), "--group-factor", "1"]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "RegistryError"


def test_evaluation_scores_for_platform_side_scoring(make_scorer, labelset):
    from privatemetrics.adapter import to_evaluation_scores
    o = make_scorer().score(submission_id="s", participant_id="p", payload=perfect(labelset))
    assert set(to_evaluation_scores(o)) == KEYS
    with pytest.raises(ValueError):
        to_evaluation_scores(Outcome(None, FailureCode.INTERNAL_ERROR, "x"))
