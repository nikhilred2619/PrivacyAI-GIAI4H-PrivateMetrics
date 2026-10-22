"""Host-side scoring: the data host runs this next to its sealed-execution worker.

For a clinical host task the platform never holds the reference labels. The
data host runs the participant's model in its sealed environment, obtains the
model's `predictions.json`, scores it here against labels that stay on the host,
and returns only the permitted metrics (conformance specification §2.3, the
"metrics" branch of `SealedRunResult`). The host is the trusted operator: it
holds the labels, the scorer and the privacy ledger.

    platform ── dispatch ──▶ host worker (sealed run, inherited containment)
                                  │ predictions (host-internal)
                                  ▼
                            HostScorer.finish()  ◀── host labels (local file)
                                  │                 host ledger (local SQLite)
                                  ▼
    platform ◀── {routeVersion, durationMs, metrics | failure}

What this module enforces:
  * only `metrics` or `failure` leave the host, never `predictions`; the body
    is checked against the exact permitted schema before it is returned;
  * a worker body that already carries `metrics` (for example a worker
    configured to score in plaintext) is refused rather than forwarded;
  * the dispatch must name this host's task. The dispatch must specify a
    route version exactly matching this component's registered route version.
    Missing, null, malformed, or mismatched route versions are refused before
    scoring;
  * every release goes through the production ledger (simulation is refused);
  * worker failures pass through with no charge, only with a code from the
    upstream closed taxonomy, and with a fixed detail string (the worker's own
    diagnostic detail stays on the host);
  * a pre-dispatch check refuses to run a model when the scope cannot pay for
    the release (no point exposing the host to a run whose result is refused).

What it does not do: run containers, authenticate the platform, or decide
who the participant is (the dispatch carries no participant identity; see
HOST_PARTICIPANT). Those belong to the host's worker and the platform.
"""

from __future__ import annotations

import csv
import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ROUTE_VERSION
from .adapter import to_sealed_run_result
from .config import PrivacyConfig, TaskConfig
from .errors import FAILURE_DETAIL_MAX_CHARS, WIRE_FAILURE_CODES
from .labels import LabelSet, check_group_factor
from .ledger import PRODUCTION, Ledger
from .scorer import PrivateScorer

METRIC_KEYS = ("qwk", "accuracy", "referableSensitivity", "referableSpecificity", "coverage")
# The dispatch message (conformance spec §2.1) carries no participant identity.
# Every host release is recorded against this fixed value; replay identity is
# still bound to the platform-assigned submission UUID, task, version, scope,
# predictions hash, ε and mode.
HOST_PARTICIPANT = "platform-dispatch"

CONFIG_KEYS = {"task_id", "dataset_id", "version_id", "scope_id", "labels_csv", "id_column", "label_column",
               "num_classes", "referable_threshold", "epsilon_per_release", "ledger", "group_factor"}
REQUIRED_CONFIG_KEYS = {"task_id", "dataset_id", "version_id", "scope_id", "labels_csv", "num_classes",
                        "referable_threshold", "epsilon_per_release", "ledger", "group_factor"}

_INTERNAL = "INTERNAL_ERROR"


class DispatchError(ValueError):
    """The dispatch is not one this host scorer may act on."""


@dataclass(frozen=True)
class HostConfig:
    """One host task. Paths are host-local; the labels file never leaves the host."""

    task_id: str
    dataset_id: str
    version_id: str
    scope_id: str
    labels_csv: Path
    num_classes: int
    referable_threshold: int
    epsilon_per_release: str
    ledger: Path
    group_factor: int
    id_column: str = "Image"
    label_column: str = "dr_grade"

    @classmethod
    def from_file(cls, path: Path | str) -> HostConfig:
        path = Path(path)
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("host config must be a JSON object")
        unknown = set(raw) - CONFIG_KEYS
        missing = REQUIRED_CONFIG_KEYS - set(raw)
        if unknown or missing:
            raise ValueError(f"host config: unknown keys {sorted(unknown)}, missing keys {sorted(missing)}")
        base = path.resolve().parent
        cfg = cls(
            task_id=raw["task_id"], dataset_id=raw["dataset_id"], version_id=raw["version_id"],
            scope_id=raw["scope_id"], labels_csv=(base / raw["labels_csv"]).resolve(),
            num_classes=raw["num_classes"], referable_threshold=raw["referable_threshold"],
            epsilon_per_release=str(raw["epsilon_per_release"]), ledger=(base / raw["ledger"]).resolve(),
            group_factor=raw["group_factor"], id_column=raw.get("id_column", "Image"),
            label_column=raw.get("label_column", "dr_grade"))
        TaskConfig(cfg.task_id, cfg.num_classes, cfg.referable_threshold)  # validates K and T
        PrivacyConfig(cfg.epsilon_per_release)  # validates ε (grid, range)
        return cfg

    def task(self) -> TaskConfig:
        return TaskConfig(self.task_id, self.num_classes, self.referable_threshold)

    def load_labels(self) -> LabelSet:
        return LabelSet(self.num_classes, read_labels_csv(self.labels_csv, self.id_column, self.label_column))


def read_labels_csv(path: Path, id_col: str, label_col: str) -> dict[str, int]:
    out: dict[str, int] = {}
    with Path(path).open(newline="") as fh:
        for row in csv.DictReader(fh):
            item = row[id_col]
            if item in out:
                raise ValueError("duplicate item id in labels file")
            out[item] = int(row[label_col])
    return out


def read_patient_map(path: Path) -> dict[str, str]:
    """CSV with columns item_id, patient_id. Read once at registration, not stored."""
    out: dict[str, str] = {}
    with Path(path).open(newline="") as fh:
        for row in csv.DictReader(fh):
            item = row["item_id"]
            if item in out:
                # A later row must not silently re-assign an item: that could
                # understate the number of items per patient.
                raise ValueError("duplicate item id in patient map")
            out[item] = row["patient_id"]
    return out


def register_host_task(cfg: HostConfig, *, epsilon_total: str, patient_map: Path | None = None) -> dict[str, Any]:
    """Operator action on the host: register scope, dataset, version (with its
    declared group factor) and task in the host's production ledger."""
    labels = cfg.load_labels()
    observed = None
    if patient_map is not None:
        observed = check_group_factor(labels, read_patient_map(patient_map), cfg.group_factor)
    led = Ledger(cfg.ledger, mode=PRODUCTION)
    led.register_scope(cfg.scope_id, epsilon_total)
    led.register_dataset(cfg.dataset_id, cfg.scope_id)
    led.register_version(cfg.dataset_id, cfg.version_id, labels, group_factor=cfg.group_factor)
    led.register_task(cfg.task(), cfg.dataset_id)
    return {"registered": True, "scope": cfg.scope_id, "dataset": cfg.dataset_id, "version": cfg.version_id,
            "task": cfg.task_id, "items": labels.n, "group_factor": cfg.group_factor,
            "group_factor_checked_against_patient_map": observed is not None}


def check_result_body(body: Any, *, proposed_codes: bool = False) -> None:
    """The exact body a host may return (upstream `SealedRunResult`, metrics or
    failure branch). Raises ValueError on anything else, including a
    `predictions` key, extra keys, non-finite or out-of-range metrics."""
    if not isinstance(body, dict):
        raise ValueError("result body must be an object")
    allowed = {"routeVersion", "durationMs", "metrics", "failure"}
    if set(body) - allowed:
        raise ValueError("result body has keys outside the permitted schema")
    if body.get("routeVersion") != ROUTE_VERSION:
        raise ValueError("result body must carry this component's route version")
    d = body.get("durationMs")
    if isinstance(d, bool) or not isinstance(d, int) or d < 0:
        raise ValueError("durationMs must be a non-negative integer")
    if ("metrics" in body) == ("failure" in body):
        raise ValueError("exactly one of metrics or failure")
    if "metrics" in body:
        m = body["metrics"]
        if not isinstance(m, dict) or tuple(sorted(m)) != tuple(sorted(METRIC_KEYS)):
            raise ValueError("metrics must have exactly the five GRADING keys")
        for k, v in m.items():
            if isinstance(v, bool) or not isinstance(v, float) or not math.isfinite(v):
                raise ValueError("metric values must be finite floats")
            lo = -1.0 if k == "qwk" else 0.0
            if not lo <= v <= 1.0:
                raise ValueError("metric value out of range")
    else:
        f = body["failure"]
        if not isinstance(f, dict) or set(f) - {"code", "detail"} or "code" not in f:
            raise ValueError("failure must be {code, detail}")
        codes = WIRE_FAILURE_CODES | ({"PRIVACY_BUDGET_EXHAUSTED"} if proposed_codes else set())
        if f["code"] not in codes:
            raise ValueError("failure code outside the closed taxonomy")
        det = f.get("detail")
        if det is not None and (not isinstance(det, str) or len(det) > FAILURE_DETAIL_MAX_CHARS):
            raise ValueError("failure detail must be a string of at most 4000 characters")


def _failure(code: str, detail: str, duration_ms: int = 0) -> dict[str, Any]:
    return {"routeVersion": ROUTE_VERSION, "durationMs": int(duration_ms), "failure": {"code": code, "detail": detail}}


class HostScorer:
    """Production host scorer for one registered host task."""

    def __init__(self, cfg: HostConfig, *, proposed_codes: bool = False):
        self.cfg = cfg
        self.proposed_codes = proposed_codes
        self.labels = cfg.load_labels()  # host-local; never sent anywhere
        self.ledger = Ledger(cfg.ledger, mode=PRODUCTION)
        self.scorer = PrivateScorer(self.ledger, task_id=cfg.task_id, version_id=cfg.version_id,
                                    labels=self.labels, privacy=PrivacyConfig(cfg.epsilon_per_release))

    # -- dispatch ---------------------------------------------------------------

    def check_dispatch(self, dispatch: Any) -> str:
        """Return the submission ID if this host may score the dispatch."""
        if not isinstance(dispatch, Mapping):
            raise DispatchError("dispatch must be an object")
        sid = dispatch.get("submissionId")
        # Canonical lower-case form only. Accepting "ABC…" and "abc…" as different
        # strings for the same UUID would let a redelivery bypass the replay
        # cache and buy a fresh noise draw with a second charge.
        try:
            if not isinstance(sid, str) or str(uuid.UUID(sid)) != sid:
                raise ValueError
        except ValueError:
            raise DispatchError("submissionId must be a canonical lower-case UUID") from None
        if dispatch.get("taskSlug") != self.cfg.task_id:
            raise DispatchError("dispatch is for another task")
        # Required and exact: a result is only meaningful under the declarations
        # of the route version that produced it (spec §3, §7). An absent, null or
        # different version is refused before anything is scored.
        rv = dispatch.get("routeVersion")
        if not isinstance(rv, str) or not 1 <= len(rv) <= 64:
            raise DispatchError("dispatch must name a route version")
        if rv != ROUTE_VERSION:
            raise DispatchError("dispatch names another route version")
        return sid

    def preflight(self, dispatch: Any) -> dict[str, Any] | None:
        """Before the model runs. None = proceed; otherwise the failure body to
        return instead of running the model.

        * a redelivered submission whose release exists may proceed (its replay
          is free);
        * a submission that is in progress, or whose release failed or crashed,
          can never be released, so the model is not run again for it;
        * a new submission proceeds only if the scope can pay for it.

        Advisory, not a reservation: two new dispatches can both pass when one
        ε remains; the second is then refused when it is scored."""
        try:
            sid = self.check_dispatch(dispatch)
        except DispatchError:
            return _failure(_INTERNAL, "dispatch refused by host scorer")
        status = self.ledger.submission_status(sid)
        if status == "RELEASED":
            return None
        if status is not None:
            return _failure(_INTERNAL, "submission cannot be scored again")
        if self.ledger.remaining(self.scorer.binding.scope_id) < self.scorer.charge:
            code = "PRIVACY_BUDGET_EXHAUSTED" if self.proposed_codes else _INTERNAL
            return _failure(code, "privacy budget for this task is exhausted")
        return None

    # -- after the sealed run ------------------------------------------------------

    def finish(self, dispatch: Any, worker_body: Any) -> dict[str, Any]:
        """Turn the worker's `SealedRunResult` (predictions or failure) into the
        body the host returns (metrics or failure). Never returns predictions."""
        try:
            sid = self.check_dispatch(dispatch)
        except DispatchError:
            return _failure(_INTERNAL, "dispatch refused by host scorer")
        body = self._finish(sid, worker_body)
        try:
            check_result_body(body, proposed_codes=self.proposed_codes)
        except ValueError:
            return _failure(_INTERNAL, "scoring failed")  # fail closed: nothing else leaves
        return body

    def _finish(self, sid: str, wb: Any) -> dict[str, Any]:
        if not isinstance(wb, Mapping):
            return _failure(_INTERNAL, "worker result is not an object")
        d = wb.get("durationMs", 0)
        duration = d if isinstance(d, int) and not isinstance(d, bool) and d >= 0 else 0
        present = [k for k in ("predictions", "metrics", "failure") if k in wb and wb[k] is not None]
        if len(present) != 1:
            return _failure(_INTERNAL, "worker result must carry exactly one of predictions or failure", duration)
        if present[0] == "metrics":
            # A worker must never score in plaintext on a protected task. Forwarding
            # its metrics would publish exact label-derived values.
            return _failure(_INTERNAL, "worker result carried metrics; refused by host scorer", duration)
        if present[0] == "failure":
            f = wb["failure"]
            code = f.get("code") if isinstance(f, Mapping) else None
            if code not in WIRE_FAILURE_CODES:
                return _failure(_INTERNAL, "worker failure code outside the closed taxonomy", duration)
            # The worker's `detail` is operator text for the host. Upstream builds it
            # from fixed templates, but several embed participant-influenced values
            # (exit status, bytes written, run time), and the model has seen the
            # host's images. Only the code leaves the host; the detail is replaced
            # by a fixed string and stays in the host worker's own logs.
            return {"routeVersion": ROUTE_VERSION, "durationMs": duration,
                    "failure": {"code": code, "detail": f"{code} reported by the host's sealed-execution worker"}}
        out = self.scorer.score(submission_id=sid, participant_id=HOST_PARTICIPANT,
                                payload={"predictions": wb["predictions"]})
        return to_sealed_run_result(out, route_version=ROUTE_VERSION, duration_ms=duration,
                                    proposed_codes=self.proposed_codes)
