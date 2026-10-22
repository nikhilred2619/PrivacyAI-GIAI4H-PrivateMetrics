"""Command-line entry points. Run as `privatemetrics ...` or `python -m privatemetrics ...`.

Host-side (clinical host tasks: the data host holds labels, scorer and ledger):

  host-register  --config host.json --epsilon-total 40 [--patient-map items.csv]
  host-preflight --config host.json --dispatch dispatch.json
  host-score     --config host.json --dispatch dispatch.json --worker-result worker.json

Operator tools (any ledger):

  ledger   --ledger L.db                      per-scope cap and spend (label- and patient-level)
  recover  --ledger L.db --older-than 3600    stale reservations -> FAILED_SPENT (stay charged)

Library-level scoring with an explicit participant ID (not the host path;
tests and experiments):

  register --ledger L.db --scope-id S --epsilon-total 10 --dataset-id D --version-id V
           --task-id T --labels labels.csv --group-factor 1
  score    --ledger L.db --task-id T --version-id V --labels labels.csv
           --predictions P.json --submission-id ID --participant-id U --epsilon 1

Exit codes: 0 = metrics released (or proceed / success), 2 = a failure body was
returned, 3 = preflight says do not run the model, 1 = usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import ROUTE_VERSION
from .adapter import to_sealed_run_result
from .config import PrivacyConfig, TaskConfig
from .host import HostConfig, HostScorer, read_labels_csv, read_patient_map, register_host_task
from .labels import LabelSet, check_group_factor
from .ledger import Ledger
from .scorer import PrivateScorer


def read_labels(path: Path, id_col: str, label_col: str) -> dict[str, int]:
    return read_labels_csv(path, id_col, label_col)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None  # refused downstream (malformed input), never a crash with content in it


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (ValueError, OSError, KeyError) as exc:
        # Configuration and registration errors (RegistryError and BudgetValueError
        # are ValueErrors). Messages are operator-facing and never contain labels.
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}), file=sys.stderr)
        return 1


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="privatemetrics", description=f"route {ROUTE_VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    hr = sub.add_parser("host-register", help="host operator: register this host task in the host ledger")
    hr.add_argument("--config", type=Path, required=True)
    hr.add_argument("--epsilon-total", required=True, help="cap for the scope (decimal string, multiple of 1e-6)")
    hr.add_argument("--patient-map", type=Path, help="CSV item_id,patient_id to check the declared group factor")

    hp = sub.add_parser("host-preflight", help="host: may this dispatch run? (before the sealed run)")
    hp.add_argument("--config", type=Path, required=True)
    hp.add_argument("--dispatch", type=Path, required=True)
    hp.add_argument("--proposed-codes", action="store_true")

    hs = sub.add_parser("host-score", help="host: score the worker's predictions, emit metrics or failure")
    hs.add_argument("--config", type=Path, required=True)
    hs.add_argument("--dispatch", type=Path, required=True)
    hs.add_argument("--worker-result", type=Path, required=True)
    hs.add_argument("--proposed-codes", action="store_true")

    r = sub.add_parser("register", help="operator: register scope, dataset, version and task")
    r.add_argument("--ledger", type=Path, required=True)
    r.add_argument("--scope-id", required=True)
    r.add_argument("--epsilon-total", required=True, help="cap for the scope (decimal string, multiple of 1e-6)")
    r.add_argument("--dataset-id", required=True)
    r.add_argument("--version-id", required=True)
    r.add_argument("--task-id", default="idrid-dr-grading")
    r.add_argument("--num-classes", type=int, default=5)
    r.add_argument("--referable-threshold", type=int, default=2)
    r.add_argument("--labels", type=Path, required=True)
    r.add_argument("--id-column", default="Image")
    r.add_argument("--label-column", default="dr_grade")
    r.add_argument("--group-factor", type=int, required=True,
                   help="declared maximum number of evaluation items per patient (1 = one item per patient)")
    r.add_argument("--patient-map", type=Path)

    s = sub.add_parser("score", help="score one submission with an explicit participant ID")
    s.add_argument("--ledger", type=Path, required=True)
    s.add_argument("--task-id", default="idrid-dr-grading")
    s.add_argument("--version-id", required=True)
    s.add_argument("--labels", type=Path, required=True)
    s.add_argument("--id-column", default="Image")
    s.add_argument("--label-column", default="dr_grade")
    s.add_argument("--num-classes", type=int, default=5)
    s.add_argument("--predictions", type=Path, required=True)
    s.add_argument("--submission-id", required=True)
    s.add_argument("--participant-id", required=True)
    s.add_argument("--epsilon", required=True, help="per-release epsilon (decimal string, multiple of 1e-6)")
    s.add_argument("--proposed-codes", action="store_true", help="emit PRIVACY_BUDGET_EXHAUSTED (not yet upstream)")

    st = sub.add_parser("ledger", help="operator: per-scope cap and spend")
    st.add_argument("--ledger", type=Path, required=True)
    rc = sub.add_parser("recover", help="operator: mark stale RESERVED rows FAILED_SPENT (stay charged)")
    rc.add_argument("--ledger", type=Path, required=True)
    rc.add_argument("--older-than", type=float, required=True, help="seconds")
    sub.add_parser("version", help="print the route version")
    a = ap.parse_args(argv)

    if a.cmd == "version":
        print(json.dumps({"routeVersion": ROUTE_VERSION}))
        return 0
    if a.cmd == "ledger":
        for row in Ledger(a.ledger).scope_summary():
            print(json.dumps(row))
        return 0
    if a.cmd == "recover":
        print(json.dumps({"recovered": Ledger(a.ledger).recover_stale(a.older_than)}))
        return 0
    if a.cmd == "host-register":
        print(json.dumps(register_host_task(HostConfig.from_file(a.config), epsilon_total=a.epsilon_total,
                                            patient_map=a.patient_map)))
        return 0
    if a.cmd == "host-preflight":
        verdict = HostScorer(HostConfig.from_file(a.config), proposed_codes=a.proposed_codes).preflight(
            _read_json(a.dispatch))
        print(json.dumps({"proceed": True} if verdict is None else verdict))
        return 0 if verdict is None else 3
    if a.cmd == "host-score":
        body = HostScorer(HostConfig.from_file(a.config), proposed_codes=a.proposed_codes).finish(
            _read_json(a.dispatch), _read_json(a.worker_result))
        print(json.dumps(body))
        return 0 if "metrics" in body else 2
    if a.cmd == "register":
        led = Ledger(a.ledger)
        task = TaskConfig(a.task_id, a.num_classes, a.referable_threshold)
        labels = LabelSet(a.num_classes, read_labels(a.labels, a.id_column, a.label_column))
        if a.patient_map is not None:
            check_group_factor(labels, read_patient_map(a.patient_map), a.group_factor)
        led.register_scope(a.scope_id, a.epsilon_total)
        led.register_dataset(a.dataset_id, a.scope_id)
        led.register_version(a.dataset_id, a.version_id, labels, group_factor=a.group_factor)
        led.register_task(task, a.dataset_id)
        print(json.dumps({"registered": True, "scope": a.scope_id, "dataset": a.dataset_id,
                          "version": a.version_id, "task": a.task_id, "group_factor": a.group_factor}))
        return 0

    started = time.monotonic()
    labels = LabelSet(a.num_classes, read_labels(a.labels, a.id_column, a.label_column))
    scorer = PrivateScorer(Ledger(a.ledger), task_id=a.task_id, version_id=a.version_id, labels=labels,
                           privacy=PrivacyConfig(a.epsilon))
    out = scorer.score(submission_id=a.submission_id, participant_id=a.participant_id, payload=_read_json(a.predictions))
    body = to_sealed_run_result(out, route_version=ROUTE_VERSION,
                                duration_ms=int((time.monotonic() - started) * 1000),
                                proposed_codes=a.proposed_codes)
    print(json.dumps(body))
    return 0 if out.metrics is not None else 2


if __name__ == "__main__":
    sys.exit(main())
