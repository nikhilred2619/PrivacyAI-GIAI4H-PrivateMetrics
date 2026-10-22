"""Adversarial and security tests S1-S14 (docs/SECURITY_TEST_REPORT.md).

These test THIS component only. Containment guarantees (network isolation,
read-only filesystem, log capture, resource limits) belong to the upstream
reference implementation and are not exercised here.
"""
import json
import logging
import multiprocessing as mp
import sqlite3
import threading

import pytest
from conftest import SCOPE, build, perfect, register, synthetic_labels

from privatemetrics.config import PrivacyConfig
from privatemetrics.errors import FailureCode
from privatemetrics.labels import LabelSet
from privatemetrics.ledger import Ledger
from privatemetrics.scorer import PrivateScorer


# S1 repeated evaluation requests are charged and capped
def test_S1_repeated_requests_spend_budget_until_blocked(make_scorer, labelset, ledger):
    s = make_scorer(eps="1", total="3")
    outs = [s.score(submission_id=f"sub-{i}", participant_id="p1", payload=perfect(labelset)) for i in range(5)]
    assert [o.metrics is not None for o in outs] == [True, True, True, False, False]
    assert outs[3].failure is FailureCode.PRIVACY_BUDGET_EXHAUSTED
    assert ledger.spent(SCOPE) == pytest.approx(3.0)


# S2 duplicate delivery / replay returns the stored release, no new noise, no new charge
def test_S2_replay_returns_identical_release_without_spending(make_scorer, labelset, ledger):
    s = make_scorer(eps="1", total="5")
    first = s.score(submission_id="sub-1", participant_id="p1", payload=perfect(labelset))
    again = [s.score(submission_id="sub-1", participant_id="p1", payload=perfect(labelset)) for _ in range(20)]
    assert all(o.replayed and o.metrics == first.metrics for o in again)
    assert ledger.spent(SCOPE) == pytest.approx(1.0)


def test_S2b_replay_with_different_predictions_is_refused(make_scorer, labelset):
    s = make_scorer()
    s.score(submission_id="sub-1", participant_id="p1", payload=perfect(labelset))
    other = {"predictions": {k: 0 for k in labelset.labels}}
    o = s.score(submission_id="sub-1", participant_id="p1", payload=other)
    assert o.metrics is None and o.failure is FailureCode.INTERNAL_ERROR


# S3 budget is global across participants (Sybil / collusion)
def test_S3_budget_is_shared_across_participants(make_scorer, labelset, ledger):
    s = make_scorer(eps="1", total="4")
    granted = sum(s.score(submission_id=f"s{i}", participant_id=f"sybil-{i}", payload=perfect(labelset)).metrics
                  is not None for i in range(10))
    assert granted == 4


def test_S3b_new_scorer_instance_shares_the_persistent_ledger(labelset, tmp_path):
    db = tmp_path / "l.db"
    for i in range(3):  # three separate scorer lifetimes (application restarts)
        s = build(Ledger(db), labelset, eps="1", cap="2")
        s.score(submission_id=f"s{i}", participant_id="p", payload=perfect(labelset))
    assert Ledger(db).spent(SCOPE) == pytest.approx(2.0)


def test_S3c_cap_cannot_be_raised_by_reregistering(labelset, tmp_path):
    db = tmp_path / "l.db"
    build(Ledger(db), labelset, eps="1", cap="2")
    with pytest.raises(ValueError):
        build(Ledger(db), labelset, eps="1", cap="50")


# S4 adaptive / differencing queries are each charged (no free probes)
def test_S4_differencing_queries_each_cost_budget(make_scorer, labelset, ledger):
    s = make_scorer(eps="0.5", total="1")
    base = perfect(labelset)
    probe = {"predictions": dict(base["predictions"], item_0000=4)}
    assert s.score(submission_id="a", participant_id="p", payload=base).metrics is not None
    assert s.score(submission_id="b", participant_id="p", payload=probe).metrics is not None
    third = s.score(submission_id="c", participant_id="p", payload={"predictions": {"item_0000": 0}})
    assert third.failure is FailureCode.PRIVACY_BUDGET_EXHAUSTED


# S5 small subgroups: a single referable item does not get exact disclosure
def test_S5_single_item_subgroup_is_noised(labelset, ledger):
    lbl = LabelSet(5, {"only": 3, **{f"z{i}": 0 for i in range(9)}})
    s = build(ledger, lbl, eps="1", cap="1000")
    vals = {s.score(submission_id=f"q{i}", participant_id="p",
                    payload={"predictions": {"only": 0}}).metrics["accuracy"] for i in range(60)}
    assert len(vals) > 1  # not a deterministic 0/1 answer about that one label


# S6-S9 malformed inputs are refused before labels are read, at no budget cost
@pytest.mark.parametrize("payload,code", [
    (None, FailureCode.MALFORMED_OUTPUT),
    ([], FailureCode.MALFORMED_OUTPUT),
    ({"predictions": {}, "extra": 1}, FailureCode.MALFORMED_OUTPUT),
    ({"predictions": {"item_0000": 1.5}}, FailureCode.MALFORMED_OUTPUT),
    ({"predictions": {"item_0000": True}}, FailureCode.MALFORMED_OUTPUT),
    ({"predictions": {"item_0000": -1}}, FailureCode.MALFORMED_OUTPUT),   # S7 out of range
    ({"predictions": {"item_0000": 5}}, FailureCode.MALFORMED_OUTPUT),
    ({"predictions": {"not-an-item": 0}}, FailureCode.UNKNOWN_ITEM_IDS),  # S8 unknown ids
    ({"predictions": {"": 0}}, FailureCode.MALFORMED_OUTPUT),
    ({"predictions": {"x" * 201: 0}}, FailureCode.MALFORMED_OUTPUT),
])
def test_S6_S9_malformed_inputs_refused_without_cost(make_scorer, labelset, ledger, payload, code):
    o = make_scorer().score(submission_id="m", participant_id="p", payload=payload)
    assert o.metrics is None and o.failure is code
    assert ledger.spent(SCOPE) == 0


def test_S9b_oversize_payload_refused(make_scorer):
    big = {"predictions": {f"i{j}": 0 for j in range(100_001)}}
    assert make_scorer().score(submission_id="big", participant_id="p", payload=big).failure is FailureCode.OUTPUT_TOO_LARGE


def test_S8b_missing_items_allowed_and_reported_as_coverage(make_scorer, labelset):
    half = {"predictions": dict(list(labelset.labels.items())[:15])}
    assert make_scorer().score(submission_id="h", participant_id="p", payload=half).metrics["coverage"] == 0.5


# S10 concurrent consumption never overspends (threads)
def test_S10_concurrent_threads_never_overspend(labelset, tmp_path):
    db = tmp_path / "l.db"
    s = build(Ledger(db), labelset, eps="1", cap="7")
    results = []
    lock = threading.Lock()

    def worker(i):
        o = s.score(submission_id=f"t{i}", participant_id=f"p{i % 3}", payload=perfect(labelset))
        with lock:
            results.append(o.metrics is not None)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(results) == 7
    assert Ledger(db).spent(SCOPE) == pytest.approx(7.0)


def _proc_worker(db, i, q):
    lbl = synthetic_labels()
    s = build(Ledger(db), lbl, eps="1", cap="5")
    q.put(s.score(submission_id=f"proc{i}", participant_id="p", payload=perfect(lbl)).metrics is not None)


def test_S10b_concurrent_processes_never_overspend(tmp_path):
    db = str(tmp_path / "l.db")
    Ledger(db)
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=_proc_worker, args=(db, i, q)) for i in range(12)]
    [p.start() for p in ps]
    [p.join(120) for p in ps]
    granted = sum(q.get(timeout=5) for _ in ps)
    assert granted == 5
    assert Ledger(db).spent(SCOPE) == pytest.approx(5.0)


# S11 failure after reservation: budget stays spent, no retry with fresh noise
def test_S11_crash_after_reserve_keeps_budget_spent(make_scorer, labelset, ledger, monkeypatch):
    s = make_scorer(eps="1", total="5")
    import privatemetrics.scorer as sc
    monkeypatch.setattr(sc, "metrics_from_table", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    o = s.score(submission_id="x", participant_id="p", payload=perfect(labelset))
    assert o.failure is FailureCode.INTERNAL_ERROR
    monkeypatch.undo()
    retry = s.score(submission_id="x", participant_id="p", payload=perfect(labelset))
    assert retry.metrics is None  # the same submission never gets a second draw
    assert ledger.spent(SCOPE) == pytest.approx(1.0)
    assert ledger.rows()[0]["status"] == "FAILED_SPENT"


# S12 restricted disclosure: logs and ledger hold no labels, counts or plaintext metrics
def test_S12_logs_and_ledger_hold_no_sensitive_values(make_scorer, labelset, ledger, caplog, tmp_path):
    caplog.set_level(logging.DEBUG, logger="privatemetrics")
    s = make_scorer(eps="1", total="2")
    s.score(submission_id="s", participant_id="p", payload=perfect(labelset))
    s.score(submission_id="s2", participant_id="p", payload={"predictions": {"bad": 0}})
    text = caplog.text + json.dumps([{k: v for k, v in r.__dict__.items() if k not in ("msg", "args")}
                                     for r in caplog.records], default=str)
    assert "item_00" not in text  # no item ids or per-item labels
    assert "qwk" not in text and "accuracy" not in text  # no released or plaintext metric values
    db = sqlite3.connect(ledger.path)
    cols = [r[1] for r in db.execute("PRAGMA table_info(releases)")]
    assert not {"labels", "counts", "noise", "plaintext"} & set(cols)
    dump = "\n".join(db.iterdump())
    assert "item_00" not in dump  # no item ids, hence no per-item labels


# S13 refusal outcomes never depend on label values
def test_S13_refusals_identical_for_different_label_sets(tmp_path):
    a = synthetic_labels([7, 5, 6, 6, 6])
    b = synthetic_labels([0, 0, 0, 0, 30])
    bad = [None, {"predictions": {"item_0000": 9}}, {"predictions": {"zzz": 1}}]
    out = []
    for lbl, name in ((a, "a"), (b, "b")):
        s = build(Ledger(tmp_path / f"{name}.db"), lbl, eps="1", cap="1")
        out.append([(o.failure, o.detail) for o in (s.score(submission_id=f"r{i}", participant_id="p", payload=x)
                                                     for i, x in enumerate(bad))])
    assert out[0] == out[1]


def test_S13b_label_validation_happens_at_registration(labelset):
    with pytest.raises(ValueError):
        LabelSet(5, {"a": 7})


# S14 simulation randomness can never be used for a real release
def test_S14_seeded_sampler_refused_in_production(labelset, ledger):
    from privatemetrics.mechanism import SimulationSampler
    register(ledger, labelset)
    with pytest.raises(ValueError):
        PrivateScorer(ledger, task_id="idrid-dr-grading", version_id="v1", labels=labelset,
                      privacy=PrivacyConfig("1"), sampler=SimulationSampler(1))


def test_S14b_simulation_outcomes_cannot_be_sent_to_platform(make_scorer, labelset):
    from privatemetrics.adapter import to_sealed_run_result
    o = make_scorer(simulation=True).score(submission_id="s", participant_id="p", payload=perfect(labelset))
    with pytest.raises(ValueError):
        to_sealed_run_result(o, route_version="x", duration_ms=1)


def test_production_releases_are_not_reproducible(make_scorer, labelset, tmp_path):
    """Two independent production releases of the same input should usually differ."""
    lbl = labelset
    vals = set()
    for i in range(10):
        s = build(Ledger(tmp_path / f"r{i}.db"), lbl, eps="1", cap="1")
        vals.add(json.dumps(s.score(submission_id="same", participant_id="p", payload=perfect(lbl)).metrics))
    assert len(vals) > 1


# S15 ledger unavailable: fail closed, nothing released
def test_S15_ledger_failure_fails_closed(make_scorer, labelset, monkeypatch):
    s = make_scorer()
    def broken(**kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(s.ledger, "reserve", broken)
    o = s.score(submission_id="z", participant_id="p", payload=perfect(labelset))
    assert o.metrics is None and o.failure is FailureCode.INTERNAL_ERROR
