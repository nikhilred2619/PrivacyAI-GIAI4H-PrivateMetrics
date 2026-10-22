"""Regression tests for D3/D4 and transactional behaviour: ownership of
cached releases, parameter changes, restarts and crash recovery."""
import sqlite3
import time

import pytest
from conftest import SCOPE, build, perfect

from privatemetrics.config import PrivacyConfig
from privatemetrics.errors import FailureCode
from privatemetrics.ledger import Ledger
from privatemetrics.scorer import PrivateScorer


def test_D3_other_participant_cannot_fetch_stored_release(ledger, labelset):
    s = build(ledger, labelset)
    a = s.score(submission_id="shared", participant_id="alice", payload=perfect(labelset))
    m = s.score(submission_id="shared", participant_id="mallory", payload=perfect(labelset))
    assert a.metrics is not None and m.metrics is None and m.failure is FailureCode.INTERNAL_ERROR
    assert ledger.spent(SCOPE) == 1  # the refused attempt spends nothing


def test_D4_replay_under_changed_epsilon_is_refused(ledger, labelset):
    build(ledger, labelset, eps="1").score(submission_id="s", participant_id="p", payload=perfect(labelset))
    other = PrivateScorer(ledger, task_id="idrid-dr-grading", version_id="v1", labels=labelset,
                          privacy=PrivacyConfig("0.5"))
    r = other.score(submission_id="s", participant_id="p", payload=perfect(labelset))
    assert r.metrics is None and not r.replayed


def test_replay_survives_restart(tmp_path, labelset):
    db = tmp_path / "l.db"
    first = build(Ledger(db), labelset).score(submission_id="s", participant_id="p", payload=perfect(labelset))
    again = PrivateScorer(Ledger(db), task_id="idrid-dr-grading", version_id="v1", labels=labelset,
                          privacy=PrivacyConfig("1")).score(submission_id="s", participant_id="p",
                                                            payload=perfect(labelset))
    assert again.replayed and again.metrics == first.metrics and Ledger(db).spent(SCOPE) == 1


def test_crashed_reservation_stays_charged_and_is_recoverable(ledger, labelset, monkeypatch):
    s = build(ledger, labelset)
    import privatemetrics.ledger as L
    monkeypatch.setattr(L.Ledger, "release", lambda self, rid, m: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O")))
    monkeypatch.setattr(L.Ledger, "fail", lambda self, rid: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O")))
    o = s.score(submission_id="c", participant_id="p", payload=perfect(labelset))
    assert o.metrics is None and o.failure is FailureCode.INTERNAL_ERROR
    monkeypatch.undo()
    assert ledger.rows()[0]["status"] == "RESERVED" and ledger.spent(SCOPE) == 1
    assert s.score(submission_id="c", participant_id="p", payload=perfect(labelset)).metrics is None  # in progress
    time.sleep(0.01)
    assert ledger.recover_stale(0) == 1
    assert ledger.rows()[0]["status"] == "FAILED_SPENT" and ledger.spent(SCOPE) == 1
    assert s.score(submission_id="c", participant_id="p", payload=perfect(labelset)).metrics is None  # never re-drawn


def test_release_persist_failure_returns_nothing(ledger, labelset, monkeypatch):
    s = build(ledger, labelset)
    import privatemetrics.ledger as L
    monkeypatch.setattr(L.Ledger, "release", lambda self, rid, m: (_ for _ in ()).throw(sqlite3.OperationalError("full")))
    o = s.score(submission_id="p1", participant_id="p", payload=perfect(labelset))
    assert o.metrics is None and ledger.rows()[0]["status"] == "FAILED_SPENT"


def test_ledger_rows_record_certified_cost_not_above_charge(ledger, labelset):
    from fractions import Fraction
    build(ledger, labelset, eps="3.7").score(submission_id="s", participant_id="p", payload=perfect(labelset))
    row = ledger.rows()[0]
    assert Fraction(row["certified_eps"]) <= Fraction(row["charged_units"], 1_000_000) == Fraction(37, 10)


def test_registration_requires_existing_scope(ledger, labelset):
    with pytest.raises(ValueError):
        ledger.register_dataset("ds", "missing-scope")
