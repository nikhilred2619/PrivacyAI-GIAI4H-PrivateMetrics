"""Upstream wire format: the body of POST /v2/submissions/:id/result
(`SealedRunResultSchema`). Exactly one of metrics or failure."""

from __future__ import annotations

from typing import Any

from .errors import UPSTREAM_CODES, FailureCode
from .ledger import PRODUCTION
from .scorer import Outcome


def _require_production(outcome: Outcome) -> None:
    if outcome.simulation or outcome.mode != PRODUCTION:
        raise ValueError("simulation outcomes are never sent to the platform")


def to_sealed_run_result(outcome: Outcome, *, route_version: str, duration_ms: int,
                         proposed_codes: bool = False) -> dict[str, Any]:
    _require_production(outcome)
    body: dict[str, Any] = {"routeVersion": route_version, "durationMs": int(duration_ms)}
    if outcome.metrics is not None:
        body["metrics"] = {k: outcome.metrics[k] for k in
                           ("qwk", "accuracy", "referableSensitivity", "referableSpecificity", "coverage")}
        return body
    assert outcome.failure is not None
    code = outcome.failure
    if code not in UPSTREAM_CODES and not proposed_codes:
        code = FailureCode.INTERNAL_ERROR  # closed upstream taxonomy (GAP_ANALYSIS G3)
    body["failure"] = {"code": str(code), "detail": outcome.detail}
    return body


def to_evaluation_scores(outcome: Outcome) -> dict[str, float]:
    """The upstream `EvaluationScores` object alone (the `metrics` value of a
    result). Raises if there is nothing releasable or the outcome is not production."""
    _require_production(outcome)
    if outcome.metrics is None:
        raise ValueError("no released metrics")
    return {k: outcome.metrics[k] for k in ("qwk", "accuracy", "referableSensitivity", "referableSpecificity", "coverage")}
