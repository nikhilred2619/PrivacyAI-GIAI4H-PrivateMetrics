"""Host-side scoring (route 0.2.1): the data host holds labels, scorer and ledger.

H-series tests. Labels used here are synthetic "private host" labels written
to a temporary directory at test time; nothing private is in the repository."""

import csv
import json
import multiprocessing as mp
import random
import sqlite3
import uuid
from pathlib import Path

import pytest

from privatemetrics import ROUTE_VERSION
from privatemetrics.cli import main
from privatemetrics.errors import WIRE_FAILURE_CODES
from privatemetrics.host import (
    HOST_PARTICIPANT,
    METRIC_KEYS,
    HostConfig,
    HostScorer,
    check_result_body,
    register_host_task,
)
from privatemetrics.labels import LabelSet, check_group_factor
from privatemetrics.ledger import SIMULATION, Ledger, RegistryError

TASK = "host-dr-grading"


def write_private_host(root: Path, *, n=60, eps="1", group_factor=2, seed=11, version="v1", labels=None):
    """Synthetic private host data: labels CSV, patient map (two eyes per
    patient), and a host config. Returns (config path, labels dict)."""
    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    labels = labels or {f"host_{i:05d}": rng.randrange(5) for i in range(n)}
    with (root / "labels.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Image", "dr_grade"])
        w.writerows(labels.items())
    with (root / "patients.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["item_id", "patient_id"])
        w.writerows((k, f"patient_{i // 2}") for i, k in enumerate(sorted(labels)))
    cfg = {"task_id": TASK, "dataset_id": "host-ds", "version_id": version, "scope_id": "host-scope",
           "labels_csv": "labels.csv", "num_classes": 5, "referable_threshold": 2,
           "epsilon_per_release": eps, "ledger": "ledger.db", "group_factor": group_factor}
    (root / "host.json").write_text(json.dumps(cfg))
    return root / "host.json", labels


def dispatch(sid=None, **kw):
    d = {"submissionId": sid or str(uuid.uuid4()), "taskSlug": TASK, "routeVersion": ROUTE_VERSION,
         "imageRef": "registry.example/m@sha256:" + "a" * 64, "imageDigest": "sha256:" + "a" * 64,
         "timeoutSec": 60, "callbackUrl": "https://platform.example/v2/submissions/x/result",
         "deadline": "2099-01-01T00:00:00Z"}
    d.update(kw)
    return d


def worker(preds, ms=1234):
    return {"routeVersion": None, "durationMs": ms, "predictions": preds}


@pytest.fixture
def host(tmp_path):
    cfg_path, labels = write_private_host(tmp_path / "host")
    cfg = HostConfig.from_file(cfg_path)
    register_host_task(cfg, epsilon_total="5", patient_map=tmp_path / "host" / "patients.csv")
    return cfg, labels


# --- H1 trusted host-side label access -----------------------------------------------

def test_H1_labels_are_read_only_from_the_host_config(host):
    cfg, labels = host
    hs = HostScorer(cfg)
    assert hs.labels.labels == labels and cfg.labels_csv.parent.name == "host"


def test_H1b_scorer_refuses_labels_that_differ_from_the_registered_version(host):
    cfg, labels = host
    changed = dict(labels)
    k = next(iter(changed))
    changed[k] = (changed[k] + 1) % 5
    with cfg.labels_csv.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Image", "dr_grade"])
        w.writerows(changed.items())
    with pytest.raises(RegistryError):
        HostScorer(cfg)


def test_H1c_host_config_rejects_unknown_and_missing_keys(tmp_path):
    p, _ = write_private_host(tmp_path)
    raw = json.loads(p.read_text())
    p.write_text(json.dumps({**raw, "mode": "simulation"}))
    with pytest.raises(ValueError):
        HostConfig.from_file(p)
    raw.pop("group_factor")
    p.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        HostConfig.from_file(p)


# --- H2/H3/H4 what leaves the host ------------------------------------------------------

def _no_leak(body: dict, labels: dict, preds: dict) -> None:
    text = json.dumps(body)
    assert "predictions" not in body
    for item in list(labels)[:50] + list(preds)[:50]:
        assert item not in text  # no item IDs, so no per-item labels or predictions


def test_H2_H3_metrics_body_carries_no_labels_ids_or_predictions(host):
    cfg, labels = host
    preds = dict(labels)
    body = HostScorer(cfg).finish(dispatch(), worker(preds))
    assert set(body) == {"routeVersion", "durationMs", "metrics"}
    assert set(body["metrics"]) == set(METRIC_KEYS)
    _no_leak(body, labels, preds)


def test_H4_exact_result_schema_and_upstream_pydantic(host):
    cfg, labels = host
    body = HostScorer(cfg).finish(dispatch(), worker(dict(labels), ms=777))
    check_result_body(body)
    assert body["routeVersion"] == ROUTE_VERSION and body["durationMs"] == 777  # worker time, not scorer time
    up = Path(__file__).resolve().parents[2] / "upstream" / "apps" / "worker-eval" / "src"
    if not up.exists():
        pytest.skip("UNVERIFIED: upstream worker-eval checkout not found")
    import sys
    sys.path.insert(0, str(up))
    from worker_eval.contract import SealedRunResult
    SealedRunResult.model_validate(body)


@pytest.mark.parametrize("bad", [
    {"routeVersion": ROUTE_VERSION, "durationMs": 1, "predictions": {"a": 1}},
    {"routeVersion": ROUTE_VERSION, "durationMs": 1, "metrics": {k: 0.5 for k in METRIC_KEYS}, "labels": {}},
    {"routeVersion": ROUTE_VERSION, "durationMs": 1, "metrics": {k: 0.5 for k in METRIC_KEYS},
     "failure": {"code": "TIMEOUT"}},
    {"routeVersion": ROUTE_VERSION, "durationMs": 1, "metrics": {**{k: 0.5 for k in METRIC_KEYS}, "n": 30.0}},
    {"routeVersion": ROUTE_VERSION, "durationMs": 1, "metrics": {**{k: 0.5 for k in METRIC_KEYS}, "qwk": 1.5}},
    {"routeVersion": ROUTE_VERSION, "durationMs": 1, "metrics": {**{k: 0.5 for k in METRIC_KEYS}, "qwk": float("nan")}},
    {"routeVersion": "other", "durationMs": 1, "metrics": {k: 0.5 for k in METRIC_KEYS}},
    {"routeVersion": ROUTE_VERSION, "durationMs": -1, "metrics": {k: 0.5 for k in METRIC_KEYS}},
    {"routeVersion": ROUTE_VERSION, "durationMs": 1, "failure": {"code": "MADE_UP"}},
    {"routeVersion": ROUTE_VERSION, "durationMs": 1, "failure": {"code": "TIMEOUT", "detail": "x" * 4001}},
])
def test_H4b_result_schema_check_rejects_everything_else(bad):
    with pytest.raises(ValueError):
        check_result_body(bad)


def test_H4c_worker_metrics_are_never_forwarded(host):
    """A worker that scored in plaintext must not get its exact metrics out."""
    cfg, _ = host
    exact = {k: 0.8123 for k in METRIC_KEYS}
    body = HostScorer(cfg).finish(dispatch(), {"durationMs": 5, "metrics": exact})
    assert body["failure"]["code"] == "INTERNAL_ERROR" and "metrics" not in body
    assert "0.8123" not in json.dumps(body)
    assert Ledger(cfg.ledger).rows() == []


@pytest.mark.parametrize("wb", [
    None, [], "x", {"durationMs": 1},
    {"durationMs": 1, "predictions": {"a": 1}, "failure": {"code": "TIMEOUT"}},
])
def test_H4d_malformed_worker_bodies_fail_closed_without_charge(host, wb):
    cfg, _ = host
    body = HostScorer(cfg).finish(dispatch(), wb)
    assert body["failure"]["code"] == "INTERNAL_ERROR"
    assert Ledger(cfg.ledger).rows() == []


@pytest.mark.parametrize("code", sorted(WIRE_FAILURE_CODES))
def test_H4e_worker_failures_pass_through_without_charge(host, code):
    cfg, _ = host
    body = HostScorer(cfg).finish(dispatch(), {"durationMs": 9, "failure": {"code": code, "detail": "d"}})
    assert body == {"routeVersion": ROUTE_VERSION, "durationMs": 9,
                    "failure": {"code": code, "detail": f"{code} reported by the host's sealed-execution worker"}}
    assert Ledger(cfg.ledger).rows() == []


@pytest.mark.parametrize("detail", [
    "container exited with status 173",                               # participant-chosen exit status
    "/output reached 18874371 bytes, above the configured cap",       # participant-chosen size
    "hard-killed after 5321 ms at the configured wall-clock cap",
    "LABEL IDRiD_001=4 " + "x" * 3000,                                # arbitrary text
])
def test_H4h_worker_failure_detail_never_leaves_the_host(host, detail):
    """D14: only the failure code is forwarded; the worker's operator detail,
    which can carry participant-influenced values, is replaced by fixed text."""
    cfg, _ = host
    body = HostScorer(cfg).finish(dispatch(), {"durationMs": 9, "failure": {"code": "NONZERO_EXIT", "detail": detail}})
    assert body["failure"] == {"code": "NONZERO_EXIT", "detail": "NONZERO_EXIT reported by the host's sealed-execution worker"}
    for token in ("173", "18874371", "5321", "IDRiD", "LABEL"):
        assert token not in json.dumps(body)


def test_H8g_concurrent_redelivery_of_one_submission_charges_once(host):
    """The same dispatch arriving on several threads at once: one charge, and
    every metrics body returned is the same release."""
    import threading
    cfg, labels = host
    d = dispatch()
    out: list[dict] = []
    lock = threading.Lock()

    def go():
        body = HostScorer(cfg).finish(d, worker(dict(labels)))
        with lock:
            out.append(body)
    ts = [threading.Thread(target=go) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    metrics = [json.dumps(b["metrics"], sort_keys=True) for b in out if "metrics" in b]
    assert len(out) == 8 and len(set(metrics)) == 1
    assert all("metrics" in b or b["failure"]["detail"] == "submission cannot be scored again" for b in out)
    assert Ledger(cfg.ledger).spent("host-scope") == 1
    assert HostScorer(cfg).finish(d, worker(dict(labels)))["metrics"] == json.loads(metrics[0])


def test_H5d_route_version_is_registrable_by_the_upstream_route_registry():
    """The platform's registry accepts only MAJOR.MINOR.PATCH (shared-types
    CreateRouteVersionRequestSchema), so the string this scorer requires must
    be one the platform can register and dispatch."""
    import re
    assert re.fullmatch(r"\d+\.\d+\.\d+", ROUTE_VERSION)


def _upstream_contract():
    import sys
    up = Path(__file__).resolve().parents[2] / "upstream" / "apps" / "worker-eval" / "src"
    if not up.exists():
        pytest.skip("UNVERIFIED: upstream worker-eval checkout not found")
    sys.path.insert(0, str(up))
    from worker_eval import contract
    return contract


def test_H5e_current_upstream_dispatch_shape_is_refused_proposed_shape_is_accepted(host):
    """Compatibility (G16). Today the platform publishes no routeId/routeVersion
    (evaluation.service.ts, 'No route registry yet'). That message is valid for
    the upstream worker but is refused here. The proposed message, with the
    registered routeId and routeVersion filled in, is accepted."""
    contract = _upstream_contract()
    cfg, labels = host
    sid = str(uuid.uuid4())
    today = {"submissionId": sid, "taskSlug": TASK, "imageRef": "registry.example/m@sha256:" + "a" * 64,
             "imageDigest": "sha256:" + "a" * 64, "timeoutSec": 1800,
             "callbackUrl": f"https://platform.example/v2/submissions/{sid}/result", "deadline": "2099-01-01T00:00:00Z"}
    contract.SealedRunMessage.model_validate(today)  # the worker accepts it
    hs = HostScorer(cfg)
    assert hs.preflight(today)["failure"]["code"] == "INTERNAL_ERROR"
    proposed = dict(today, routeId=str(uuid.uuid4()), routeVersion=ROUTE_VERSION)
    contract.SealedRunMessage.model_validate(proposed)
    assert hs.preflight(proposed) is None
    assert "metrics" in hs.finish(proposed, worker(dict(labels)))


def test_H4f_worker_failure_with_unknown_code_is_not_forwarded(host):
    cfg, _ = host
    body = HostScorer(cfg).finish(dispatch(), {"durationMs": 9, "failure": {"code": "LABELS_ARE_3_4_0"}})
    assert body["failure"]["code"] == "INTERNAL_ERROR" and "LABELS" not in json.dumps(body)


# --- H5 dispatch checks -------------------------------------------------------------------

@pytest.mark.parametrize("d", [
    dispatch(taskSlug="other-task"),
    dispatch(routeVersion="0.1.2-draft"),
    dispatch(sid="not-a-uuid"),
    dispatch(sid=str(uuid.uuid4()).upper()),
    {"taskSlug": TASK},
    None,
])
def test_H5_dispatch_for_another_task_route_or_bad_id_is_refused(host, d):
    cfg, labels = host
    hs = HostScorer(cfg)
    assert hs.preflight(d)["failure"]["code"] == "INTERNAL_ERROR"
    assert hs.finish(d, worker(dict(labels)))["failure"]["code"] == "INTERNAL_ERROR"
    assert Ledger(cfg.ledger).rows() == []


@pytest.mark.parametrize("rv", ["<absent>", None, "", 0, 1.0, True, [ROUTE_VERSION], {"v": ROUTE_VERSION},
                                ROUTE_VERSION + " ", " " + ROUTE_VERSION, ROUTE_VERSION + "-draft", "v" + ROUTE_VERSION, "0.2.0",
                                "0.1.2-draft", "x" * 65])
def test_H5b_route_version_is_required_and_exact(host, rv):
    """D10: an absent, null, malformed or different route version is refused before scoring."""
    cfg, labels = host
    d = dispatch()
    if rv == "<absent>":
        d.pop("routeVersion")
    else:
        d["routeVersion"] = rv
    hs = HostScorer(cfg)
    assert hs.preflight(d) == {"routeVersion": ROUTE_VERSION, "durationMs": 0,
                               "failure": {"code": "INTERNAL_ERROR", "detail": "dispatch refused by host scorer"}}
    assert hs.finish(d, worker(dict(labels)))["failure"]["code"] == "INTERNAL_ERROR"
    assert Ledger(cfg.ledger).rows() == []


def test_H5c_exact_route_version_is_accepted(host):
    cfg, labels = host
    hs = HostScorer(cfg)
    d = dispatch(routeVersion=ROUTE_VERSION)
    assert hs.preflight(d) is None
    assert "metrics" in hs.finish(d, worker(dict(labels)))


def test_H6d_preflight_refuses_submissions_that_can_never_be_released(host):
    """D12: no model run for a submission that is in progress, failed or crashed."""
    cfg, labels = host
    hs = HostScorer(cfg)
    from privatemetrics.scorer import _predictions_hash
    sid = str(uuid.uuid4())
    hs.ledger.reserve(binding=hs.scorer.binding, submission_id=sid, participant_id=HOST_PARTICIPANT,
                      predictions_hash=_predictions_hash(dict(labels)), charge=hs.scorer.charge,
                      certified=hs.scorer.certified, mode="production")
    assert hs.preflight(dispatch(sid))["failure"]["detail"] == "submission cannot be scored again"  # in progress
    Ledger(cfg.ledger).recover_stale(0)
    assert hs.preflight(dispatch(sid))["failure"]["detail"] == "submission cannot be scored again"  # crashed
    done = dispatch()
    hs.finish(done, worker(dict(labels)))
    assert hs.preflight(done) is None  # released: a redelivery may proceed, its replay is free


def test_H7d_patient_map_with_duplicate_items_is_refused(tmp_path):
    """D11: a repeated item row must not silently re-assign the item to another patient."""
    from privatemetrics.host import read_patient_map
    cfg_path, labels = write_private_host(tmp_path / "h")
    pm = tmp_path / "h" / "patients.csv"
    with pm.open("a", newline="") as fh:
        csv.writer(fh).writerow([sorted(labels)[0], "patient_other"])
    with pytest.raises(ValueError):
        read_patient_map(pm)
    with pytest.raises(ValueError):
        register_host_task(HostConfig.from_file(cfg_path), epsilon_total="5", patient_map=pm)


def test_H8f_new_ledger_files_are_owner_only(tmp_path):
    import os
    import stat
    led = Ledger(tmp_path / "fresh.db")
    assert stat.S_IMODE(os.stat(led.path).st_mode) == 0o600


# --- H6 local scoring, shared budget, revisions -----------------------------------------------

def test_H6_global_budget_shared_by_all_dispatches(host):
    cfg, labels = host
    hs = HostScorer(cfg)
    out = [hs.finish(dispatch(), worker(dict(labels))) for _ in range(7)]
    assert ["metrics" in b for b in out] == [True] * 5 + [False] * 2
    assert all(r["participant_id"] == HOST_PARTICIPANT for r in Ledger(cfg.ledger).rows())
    assert Ledger(cfg.ledger).spent("host-scope") == 5


def test_H6b_preflight_refuses_to_run_a_model_when_budget_is_exhausted(host):
    cfg, labels = host
    hs = HostScorer(cfg)
    sids = [str(uuid.uuid4()) for _ in range(5)]
    for s in sids:
        assert hs.preflight(dispatch(s)) is None
        assert "metrics" in hs.finish(dispatch(s), worker(dict(labels)))
    assert hs.preflight(dispatch())["failure"]["code"] == "INTERNAL_ERROR"
    proposed = HostScorer(cfg, proposed_codes=True).preflight(dispatch())
    assert proposed["failure"]["code"] == "PRIVACY_BUDGET_EXHAUSTED"
    assert hs.preflight(dispatch(sids[0])) is None  # a redelivery may proceed: its replay is free


def test_H6c_dataset_revision_charges_the_same_scope(tmp_path):
    cfg_path, labels = write_private_host(tmp_path / "h")
    cfg = HostConfig.from_file(cfg_path)
    register_host_task(cfg, epsilon_total="3")
    HostScorer(cfg).finish(dispatch(), worker(dict(labels)))
    revised = dict(labels)
    k = next(iter(revised))
    revised[k] = (revised[k] + 1) % 5
    cfg2_path, _ = write_private_host(tmp_path / "h2", labels=revised, version="v2")
    raw = json.loads(cfg2_path.read_text())
    raw["ledger"] = str(cfg.ledger)
    raw["task_id"] = TASK + "-v2"
    cfg2_path.write_text(json.dumps(raw))
    cfg2 = HostConfig.from_file(cfg2_path)
    register_host_task(cfg2, epsilon_total="3")
    hs2 = HostScorer(cfg2)
    out = [hs2.finish(dispatch(taskSlug=cfg2.task_id), worker(dict(revised))) for _ in range(3)]
    assert ["metrics" in b for b in out] == [True, True, False]
    assert Ledger(cfg.ledger).spent("host-scope") == 3


# --- H7 group factor ------------------------------------------------------------------------

def test_H7_group_factor_validated_against_patient_map(tmp_path):
    cfg_path, labels = write_private_host(tmp_path / "h", group_factor=1)
    lset = LabelSet(5, labels)
    pm = {r["item_id"]: r["patient_id"] for r in csv.DictReader((tmp_path / "h" / "patients.csv").open())}
    assert check_group_factor(lset, pm, 2) == 2
    with pytest.raises(ValueError):
        check_group_factor(lset, pm, 1)  # understated: two eyes per patient
    with pytest.raises(ValueError):
        register_host_task(HostConfig.from_file(cfg_path), epsilon_total="5", patient_map=tmp_path / "h" / "patients.csv")
    pm.pop(next(iter(pm)))
    with pytest.raises(ValueError):
        check_group_factor(lset, pm, 2)  # unmapped item


def test_H7b_group_factor_is_immutable_and_reported_at_patient_level(host):
    cfg, labels = host
    led = Ledger(cfg.ledger)
    with pytest.raises(RegistryError):
        led.register_version(cfg.dataset_id, cfg.version_id, cfg.load_labels(), group_factor=1)
    for bad in (0, -1, True, 2.0, 10_000):
        with pytest.raises(RegistryError):
            led.register_version(cfg.dataset_id, "v-bad", cfg.load_labels(), group_factor=bad)
    HostScorer(cfg).finish(dispatch(), worker(dict(labels)))
    row = led.scope_summary()[0]
    assert row["spent"] == "1" and row["patient_level_spent"] == "2" and row["patient_level_cap_bound"] == "10"
    assert led.resolve(cfg.task_id, cfg.version_id, cfg.load_labels()).group_factor == 2


def test_H7c_v3_ledger_without_group_factor_is_refused(tmp_path):
    db = tmp_path / "v3.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("INSERT INTO meta VALUES ('schema_version', '3')")
        c.execute("INSERT INTO meta VALUES ('mode', 'production')")
    with pytest.raises(RegistryError):
        Ledger(db)


# --- H8 replay, restarts, crash recovery, concurrency ------------------------------------------

def test_H8_replay_across_restarts_is_identical_and_free(host):
    cfg, labels = host
    d = dispatch()
    first = HostScorer(cfg).finish(d, worker(dict(labels)))
    again = HostScorer(cfg).finish(d, worker(dict(labels)))  # new objects: a restart
    assert again == first and Ledger(cfg.ledger).spent("host-scope") == 1


def test_H8b_redelivery_with_different_predictions_is_refused(host):
    """A non-deterministic model rerun on redelivery cannot buy a second draw."""
    cfg, labels = host
    d = dispatch()
    HostScorer(cfg).finish(d, worker(dict(labels)))
    changed = dict(labels)
    k = next(iter(changed))
    changed[k] = (changed[k] + 1) % 5
    body = HostScorer(cfg).finish(d, worker(changed))
    assert body["failure"]["code"] == "INTERNAL_ERROR" and Ledger(cfg.ledger).spent("host-scope") == 1


def test_H8c_crash_after_reservation_stays_charged_and_never_releases(host):
    cfg, labels = host
    hs = HostScorer(cfg)
    sid = str(uuid.uuid4())
    from privatemetrics.scorer import _predictions_hash
    r = hs.ledger.reserve(binding=hs.scorer.binding, submission_id=sid, participant_id=HOST_PARTICIPANT,
                          predictions_hash=_predictions_hash(dict(labels)), charge=hs.scorer.charge,
                          certified=hs.scorer.certified, mode="production")
    assert r.kind == "new"  # process "dies" here, before noise is released
    assert Ledger(cfg.ledger).recover_stale(0) == 1
    body = HostScorer(cfg).finish(dispatch(sid), worker(dict(labels)))
    assert body["failure"]["code"] == "INTERNAL_ERROR" and Ledger(cfg.ledger).spent("host-scope") == 1


def _score_proc(cfg_path: str, preds: dict, q) -> None:
    hs = HostScorer(HostConfig.from_file(cfg_path))
    q.put("metrics" in hs.finish(dispatch(), worker(preds)))


def test_H8d_concurrent_host_processes_never_overspend(tmp_path):
    cfg_path, labels = write_private_host(tmp_path / "h")
    register_host_task(HostConfig.from_file(cfg_path), epsilon_total="4")
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_score_proc, args=(str(cfg_path), dict(labels), q)) for _ in range(10)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    results = [q.get(timeout=5) for _ in procs]
    assert sum(results) == 4 and Ledger(tmp_path / "h" / "ledger.db").spent("host-scope") == 4


def test_H8e_ledger_failure_fails_closed(host, monkeypatch):
    cfg, labels = host
    hs = HostScorer(cfg)

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(hs.ledger, "release", boom)
    body = hs.finish(dispatch(), worker(dict(labels)))
    assert body["failure"]["code"] == "INTERNAL_ERROR" and "metrics" not in body
    assert Ledger(cfg.ledger).spent("host-scope") == 1  # reserved ε stays charged


# --- H9 simulation/production --------------------------------------------------------------

def test_H9_host_scorer_refuses_a_simulation_ledger(tmp_path):
    cfg_path, _ = write_private_host(tmp_path / "h")
    Ledger(tmp_path / "h" / "ledger.db", mode=SIMULATION)
    cfg = HostConfig.from_file(cfg_path)
    with pytest.raises(RegistryError):
        register_host_task(cfg, epsilon_total="5")
    with pytest.raises(RegistryError):
        HostScorer(cfg)


# --- H10 CLI (python -m privatemetrics) ----------------------------------------------------------

def test_H10_cli_host_flow(tmp_path, capsys):
    cfg_path, labels = write_private_host(tmp_path / "h", eps="2")
    pm = tmp_path / "h" / "patients.csv"
    assert main(["host-register", "--config", str(cfg_path), "--epsilon-total", "4", "--patient-map", str(pm)]) == 0
    assert json.loads(capsys.readouterr().out)["group_factor_checked_against_patient_map"] is True
    d, w = tmp_path / "d.json", tmp_path / "w.json"
    w.write_text(json.dumps(worker(dict(labels))))
    codes = []
    for _ in range(3):
        d.write_text(json.dumps(dispatch()))
        pre = main(["host-preflight", "--config", str(cfg_path), "--dispatch", str(d)])
        capsys.readouterr()
        codes.append(pre)
        if pre == 0:
            assert main(["host-score", "--config", str(cfg_path), "--dispatch", str(d), "--worker-result", str(w)]) == 0
            assert set(json.loads(capsys.readouterr().out)["metrics"]) == set(METRIC_KEYS)
    assert codes == [0, 0, 3]
    assert main(["version"]) == 0
    assert json.loads(capsys.readouterr().out) == {"routeVersion": ROUTE_VERSION}


def test_H4g_schema_check_is_the_last_gate(host, monkeypatch):
    """Even if a bug upstream of the gate produced a body with predictions, it
    would not leave the host."""
    cfg, labels = host
    import privatemetrics.host as h

    def leaky(*a, **k):
        return {"routeVersion": ROUTE_VERSION, "durationMs": 1, "metrics": {k: 0.5 for k in METRIC_KEYS},
                "predictions": dict(labels)}
    monkeypatch.setattr(h, "to_sealed_run_result", leaky)
    body = HostScorer(cfg).finish(dispatch(), worker(dict(labels)))
    assert body["failure"]["code"] == "INTERNAL_ERROR" and "predictions" not in body


def test_H10b_example_host_config_registers_and_scores(tmp_path):
    """The documented example works end to end on synthetic data."""
    import shutil
    import subprocess
    import sys
    src = Path(__file__).resolve().parents[1] / "examples" / "host-config"
    work = tmp_path / "ex"
    shutil.copytree(src, work)
    subprocess.run([sys.executable, "-B", str(work / "make_synthetic.py"), "--items", "200"], check=True,
                   capture_output=True)
    cfg = HostConfig.from_file(work / "host.example.json")
    out = register_host_task(cfg, epsilon_total="20", patient_map=work / "patients.csv")
    assert out["group_factor_checked_against_patient_map"] is True
    labels = cfg.load_labels().labels
    d = dispatch(taskSlug=cfg.task_id)
    assert "metrics" in HostScorer(cfg).finish(d, worker(dict(labels)))
