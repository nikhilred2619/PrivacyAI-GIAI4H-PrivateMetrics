"""The release pipeline (docs/ARCHITECTURE.md, steps 1-8).

Order of operations for one scored submission:
  1. validate predictions (label-free; no budget)
  2. reserve: idempotency + ownership check, cap check, insert RESERVED (one transaction)
  3. compute exact table, draw noise, post-process            (in memory only)
  4. persist the released metrics (RELEASED)                   (one transaction)
  5. return the metrics
A failure in 3 or 4 leaves the row charged (FAILED_SPENT or RESERVED) and
returns no metrics. A ledger failure at 2 releases nothing (fail closed).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .budget import calibrate_scale, to_units, units_ceil
from .config import PrivacyConfig
from .confusion import confusion_table
from .errors import FailureCode, ReleaseRefused
from .labels import LabelSet
from .ledger import PRODUCTION, SIMULATION, Ledger
from .mechanism import L1_SENSITIVITY, OpenDPSampler, Sampler
from .postprocess import metrics_from_table, privatize_table
from .validation import validate_predictions

log = logging.getLogger("privatemetrics")


@dataclass(frozen=True)
class Outcome:
    metrics: dict[str, float] | None
    failure: FailureCode | None
    detail: str | None
    replayed: bool = False
    simulation: bool = False
    # Explicit provenance. Only "production" outcomes may leave this component
    # (adapter.py). When not given, the mode is derived from `simulation`;
    # when given, it must agree with it.
    mode: str = ""

    def __post_init__(self) -> None:
        if self.mode == "":
            object.__setattr__(self, "mode", SIMULATION if self.simulation else PRODUCTION)
        if self.mode not in (PRODUCTION, SIMULATION):
            raise ValueError("invalid outcome mode")
        if self.simulation != (self.mode == SIMULATION):
            raise ValueError("outcome simulation flag and mode disagree")


def compute_release(labels: LabelSet, preds: dict[str, int], num_classes: int, referable_threshold: int,
                    sampler: Sampler, scale: float) -> dict[str, float]:
    """Steps 3 of the pipeline: exact table -> noise -> post-processing, in
    memory only. Used by the scorer for every release and, unchanged, by the
    fidelity experiments, so the measured distribution is the released one.
    Does no accounting: callers outside the scorer must not publish its output."""
    t = confusion_table(labels.labels, preds, num_classes)
    noisy = privatize_table(t.counts, t.column_sums, lambda v: sampler.noisy(v, scale))
    metrics = metrics_from_table(noisy, t.column_sums, t.matched, t.n, referable_threshold)
    del t, noisy  # exact counts and noise are not kept, logged or persisted
    return metrics


def _predictions_hash(preds: dict[str, int]) -> str:
    return hashlib.sha256(json.dumps(sorted(preds.items()), separators=(",", ":")).encode()).hexdigest()


class PrivateScorer:
    """Scores one registered task against one registered dataset version.

    Registration (scope, dataset, version, task) is an operator action on the
    Ledger and must happen first; the scorer never creates accounting scopes."""

    def __init__(self, ledger: Ledger, *, task_id: str, version_id: str, labels: LabelSet,
                 privacy: PrivacyConfig, sampler: Sampler | None = None, simulation: bool = False):
        sampler = sampler or OpenDPSampler()
        if sampler.simulation and not simulation:
            raise ValueError("a seeded simulation sampler cannot be used for real releases")
        if simulation and not sampler.simulation:
            raise ValueError("simulation mode requires the simulation sampler")
        self.mode = SIMULATION if simulation else PRODUCTION
        if ledger.mode != self.mode:
            raise ValueError(f"a {self.mode} scorer requires a {self.mode}-mode ledger (got {ledger.mode})")
        self.binding = ledger.resolve(task_id, version_id, labels)
        self.scale, self.certified = calibrate_scale(privacy.epsilon_per_release, sampler.epsilon, L1_SENSITIVITY)
        self.charge: Fraction = privacy.epsilon_per_release
        # The ledger charges the configured ε; it must cover the certified cost.
        if units_ceil(self.certified) > to_units(self.charge):
            raise ValueError("certified privacy cost exceeds the configured charge")
        self.labels = labels
        self.ledger = ledger
        self.sampler = sampler
        self.simulation = simulation

    def _refuse(self, code: FailureCode, detail: str) -> Outcome:
        return Outcome(None, code, detail, simulation=self.simulation, mode=self.mode)

    def score(self, *, submission_id: str, participant_id: str, payload: Any) -> Outcome:
        b = self.binding
        try:
            preds = validate_predictions(payload, self.labels.item_ids, b.num_classes)
        except ReleaseRefused as exc:  # label-free, costs nothing
            log.info("refused", extra={"submission": submission_id, "code": str(exc.code)})
            return self._refuse(exc.code, exc.detail)

        try:
            r = self.ledger.reserve(binding=b, submission_id=submission_id, participant_id=participant_id,
                                    predictions_hash=_predictions_hash(preds), charge=self.charge,
                                    certified=self.certified, mode=self.mode)
        except Exception as exc:  # noqa: BLE001 - ledger unavailable: fail closed, release nothing
            log.error("ledger unavailable", extra={"submission": submission_id, "error": type(exc).__name__})
            return self._refuse(FailureCode.INTERNAL_ERROR, "scoring failed")
        if r.kind == "replay":
            if r.mode != self.mode:  # unreachable while the ledger enforces it; checked again here
                return self._refuse(FailureCode.INTERNAL_ERROR, "submission cannot be scored again")
            return Outcome(r.metrics, None, None, replayed=True, simulation=self.simulation, mode=self.mode)
        if r.kind == "exhausted":
            log.info("budget exhausted", extra={"submission": submission_id})
            return self._refuse(FailureCode.PRIVACY_BUDGET_EXHAUSTED, "privacy budget for this task is exhausted")
        if r.kind != "new":
            # conflict (identity reused with different owner/inputs/parameters), in_progress or failed
            log.warning("not released", extra={"submission": submission_id, "state": r.kind})
            return self._refuse(FailureCode.INTERNAL_ERROR, "submission cannot be scored again")

        rid = r.release_id
        assert rid is not None  # "new" reservations always carry an id
        try:
            metrics = compute_release(self.labels, preds, b.num_classes, b.referable_threshold,
                                      self.sampler, self.scale)
            self.ledger.release(rid, metrics)
        except Exception as exc:  # noqa: BLE001
            try:
                self.ledger.fail(rid)  # ε stays charged either way
            except Exception:  # noqa: BLE001 - row stays RESERVED, still charged; recover_stale handles it
                log.error("could not mark release failed", extra={"submission": submission_id})
            log.error("release failed", extra={"submission": submission_id, "error": type(exc).__name__})
            return self._refuse(FailureCode.INTERNAL_ERROR, "scoring failed")
        log.info("released", extra={"submission": submission_id, "epsilon": str(self.charge)})
        return Outcome(metrics, None, None, simulation=self.simulation, mode=self.mode)
