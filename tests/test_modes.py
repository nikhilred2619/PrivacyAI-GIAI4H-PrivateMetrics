"""D8 regression tests: simulation results can never become production results.

Covers the ledger file mode, per-row mode in replay validation, the scorer's
mode checks, the adapter's provenance check, and restarts (a fresh Ledger /
PrivateScorer on the same file, which is what an application restart does)."""

import json
import sqlite3

import pytest
from conftest import SCOPE, build, perfect, register

from privatemetrics.adapter import to_evaluation_scores, to_sealed_run_result
from privatemetrics.config import PrivacyConfig
from privatemetrics.errors import FailureCode
from privatemetrics.ledger import PRODUCTION, SCHEMA_VERSION, SIMULATION, Ledger, RegistryError
from privatemetrics.mechanism import SimulationSampler
from privatemetrics.scorer import Outcome, PrivateScorer, _predictions_hash

TASK, VERSION = "idrid-dr-grading", "v1"


def prod_scorer(led, labels, eps="1"):
    return PrivateScorer(led, task_id=TASK, version_id=VERSION, labels=labels, privacy=PrivacyConfig(eps))


def sim_scorer(led, labels, eps="1", seed=0):
    return PrivateScorer(led, task_id=TASK, version_id=VERSION, labels=labels, privacy=PrivacyConfig(eps),
                         sampler=SimulationSampler(seed), simulation=True)


# --- ledger file mode -----------------------------------------------------------

def test_new_ledger_defaults_to_production_and_records_mode(tmp_path):
    led = Ledger(tmp_path / "l.db")
    assert led.mode == PRODUCTION
    with sqlite3.connect(tmp_path / "l.db") as c:
        meta = dict(c.execute("SELECT key, value FROM meta"))
    assert meta == {"schema_version": str(SCHEMA_VERSION), "mode": PRODUCTION}


def test_invalid_ledger_mode_rejected(tmp_path):
    with pytest.raises(ValueError):
        Ledger(tmp_path / "l.db", mode="staging")
    with pytest.raises(ValueError):
        Ledger(tmp_path / "l.db", mode="")


def test_simulation_file_cannot_be_reopened_as_production_after_restart(tmp_path, labelset):
    db = tmp_path / "sim.db"
    s = build(Ledger(db, mode=SIMULATION), labelset, simulation=True)
    assert s.score(submission_id="s1", participant_id="p", payload=perfect(labelset)).metrics is not None
    # "restart": new objects, same file
    with pytest.raises(RegistryError):
        Ledger(db)
    with pytest.raises(RegistryError):
        Ledger(db, mode=PRODUCTION)
    assert Ledger(db, mode=SIMULATION).mode == SIMULATION


def test_production_file_cannot_be_reopened_as_simulation_after_restart(tmp_path, labelset):
    db = tmp_path / "prod.db"
    s = build(Ledger(db), labelset)
    assert s.score(submission_id="s1", participant_id="p", payload=perfect(labelset)).metrics is not None
    with pytest.raises(RegistryError):
        Ledger(db, mode=SIMULATION)
    assert Ledger(db).spent(SCOPE) == 1


def test_file_without_stored_mode_is_refused(tmp_path):
    db = tmp_path / "l.db"
    Ledger(db)
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM meta WHERE key='mode'")
    with pytest.raises(RegistryError):
        Ledger(db)
    with pytest.raises(RegistryError):
        Ledger(db, mode=SIMULATION)


def test_v2_schema_file_is_refused(tmp_path):
    """v2 ledgers had no mode column: their rows cannot be classified, so they are not reused."""
    db = tmp_path / "v2.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("INSERT INTO meta VALUES ('schema_version', '2')")
    for mode in (PRODUCTION, SIMULATION):
        with pytest.raises(RegistryError):
            Ledger(db, mode=mode)


# --- scorer / ledger pairing ----------------------------------------------------

def test_simulation_scorer_refused_on_production_ledger(ledger, labelset):
    register(ledger, labelset)
    with pytest.raises(ValueError):
        sim_scorer(ledger, labelset)


def test_production_scorer_refused_on_simulation_ledger(sim_ledger, labelset):
    register(sim_ledger, labelset)
    with pytest.raises(ValueError):
        prod_scorer(sim_ledger, labelset)


def test_ledger_reserve_refuses_other_mode_directly(ledger, sim_ledger, labelset):
    from fractions import Fraction
    register(ledger, labelset)
    register(sim_ledger, labelset)
    for led, wrong in ((ledger, SIMULATION), (sim_ledger, PRODUCTION)):
        b = led.resolve(TASK, VERSION, labelset)
        with pytest.raises(RegistryError):
            led.reserve(binding=b, submission_id="x", participant_id="p", predictions_hash="h",
                        charge=Fraction(1), certified=Fraction(1), mode=wrong)
        assert led.rows() == []


# --- cross-mode replay ----------------------------------------------------------

def _copy_simulation_row_into(prod_db, sim_db, submission_id):
    """Simulate a forged / legacy / hand-migrated row: a simulation release
    sitting in a production ledger file under a production-looking submission ID."""
    with sqlite3.connect(sim_db) as s:
        row = s.execute("SELECT * FROM releases WHERE submission_id=?", (submission_id,)).fetchone()
    assert row is not None
    with sqlite3.connect(prod_db) as p:
        p.execute(f"INSERT INTO releases VALUES ({','.join('?' * len(row))})", row)


def test_cross_mode_replay_after_restart_releases_nothing(tmp_path, labelset):
    """The v0.1.1 reproduction: simulate, restart in production, replay the same ID."""
    sim_db, prod_db = tmp_path / "sim.db", tmp_path / "prod.db"
    payload = perfect(labelset)
    sim = build(Ledger(sim_db, mode=SIMULATION), labelset, simulation=True, seed=7)
    so = sim.score(submission_id="sub-1", participant_id="p", payload=payload)
    assert so.metrics is not None and so.mode == SIMULATION

    register(Ledger(prod_db), labelset)
    _copy_simulation_row_into(prod_db, sim_db, "sub-1")

    # restart: brand-new production objects on the production file
    out = prod_scorer(Ledger(prod_db), labelset).score(submission_id="sub-1", participant_id="p", payload=payload)
    assert out.metrics is None and out.failure is FailureCode.INTERNAL_ERROR
    assert not out.replayed and out.mode == PRODUCTION
    assert out.metrics != so.metrics
    # the simulated values never surface through the adapter either
    body = to_sealed_run_result(out, route_version="x", duration_ms=1)
    assert "metrics" not in body
    assert json.dumps(so.metrics) not in json.dumps(body)


def test_cross_mode_row_is_a_conflict_at_the_ledger(tmp_path, labelset):
    sim_db, prod_db = tmp_path / "sim.db", tmp_path / "prod.db"
    build(Ledger(sim_db, mode=SIMULATION), labelset, simulation=True).score(
        submission_id="sub-1", participant_id="p", payload=perfect(labelset))
    register(Ledger(prod_db), labelset)
    _copy_simulation_row_into(prod_db, sim_db, "sub-1")
    s = prod_scorer(Ledger(prod_db), labelset)
    r = Ledger(prod_db).reserve(binding=s.binding, submission_id="sub-1", participant_id="p",
                                predictions_hash=_predictions_hash(dict(labelset.labels)),
                                charge=s.charge, certified=s.certified, mode=PRODUCTION)
    assert r.kind == "conflict" and r.metrics is None and r.mode == SIMULATION


def test_production_row_never_replayed_in_simulation(tmp_path, labelset):
    """Reverse direction: a production release copied into a simulation file is not served there."""
    sim_db, prod_db = tmp_path / "sim.db", tmp_path / "prod.db"
    po = build(Ledger(prod_db), labelset).score(submission_id="sub-2", participant_id="p",
                                               payload=perfect(labelset))
    assert po.metrics is not None
    register(Ledger(sim_db, mode=SIMULATION), labelset)
    with sqlite3.connect(prod_db) as p:
        row = p.execute("SELECT * FROM releases WHERE submission_id='sub-2'").fetchone()
    with sqlite3.connect(sim_db) as s:
        s.execute(f"INSERT INTO releases VALUES ({','.join('?' * len(row))})", row)
    out = sim_scorer(Ledger(sim_db, mode=SIMULATION), labelset).score(
        submission_id="sub-2", participant_id="p", payload=perfect(labelset))
    assert out.metrics is None and not out.replayed


def test_same_submission_id_in_both_modes_is_independent(tmp_path, labelset):
    """Reusing a simulation submission ID in production gets a fresh production
    release (fresh noise, production budget), never the simulated values."""
    payload = perfect(labelset)
    sim = build(Ledger(tmp_path / "sim.db", mode=SIMULATION), labelset, simulation=True, seed=3)
    so = sim.score(submission_id="shared", participant_id="p", payload=payload)
    prod_led = Ledger(tmp_path / "prod.db")
    po = build(prod_led, labelset).score(submission_id="shared", participant_id="p", payload=payload)
    assert so.mode == SIMULATION and po.mode == PRODUCTION and not po.replayed
    assert prod_led.spent(SCOPE) == 1
    # production replay (after restart) returns the production values, not the simulated ones
    again = prod_scorer(Ledger(tmp_path / "prod.db"), labelset).score(
        submission_id="shared", participant_id="p", payload=payload)
    assert again.replayed and again.metrics == po.metrics and again.mode == PRODUCTION
    assert Ledger(tmp_path / "prod.db").spent(SCOPE) == 1


def test_simulation_spend_never_touches_production_scope(tmp_path, labelset):
    prod_db = tmp_path / "prod.db"
    register(Ledger(prod_db), labelset, cap="2")
    sim = build(Ledger(tmp_path / "sim.db", mode=SIMULATION), labelset, cap="50", simulation=True)
    for i in range(10):
        assert sim.score(submission_id=f"s{i}", participant_id="p", payload=perfect(labelset)).metrics
    assert Ledger(prod_db).spent(SCOPE) == 0
    assert Ledger(prod_db).rows() == []
    p = prod_scorer(Ledger(prod_db), labelset)
    assert all(p.score(submission_id=f"q{i}", participant_id="p", payload=perfect(labelset)).metrics
               for i in range(2))


def test_simulation_replay_stays_simulation_after_restart(tmp_path, labelset):
    db = tmp_path / "sim.db"
    first = build(Ledger(db, mode=SIMULATION), labelset, simulation=True, seed=5).score(
        submission_id="s", participant_id="p", payload=perfect(labelset))
    again = sim_scorer(Ledger(db, mode=SIMULATION), labelset, seed=99).score(
        submission_id="s", participant_id="p", payload=perfect(labelset))
    assert again.replayed and again.metrics == first.metrics
    assert again.mode == SIMULATION and again.simulation
    with pytest.raises(ValueError):
        to_sealed_run_result(again, route_version="x", duration_ms=1)
    with pytest.raises(ValueError):
        to_evaluation_scores(again)


# --- outcome provenance and adapter ---------------------------------------------

def test_outcome_mode_derived_from_flag():
    assert Outcome(None, FailureCode.INTERNAL_ERROR, "x").mode == PRODUCTION
    assert Outcome(None, FailureCode.INTERNAL_ERROR, "x", simulation=True).mode == SIMULATION


@pytest.mark.parametrize("kw", [
    {"simulation": True, "mode": PRODUCTION},
    {"simulation": False, "mode": SIMULATION},
    {"mode": "staging"},
])
def test_outcome_rejects_inconsistent_provenance(kw):
    with pytest.raises(ValueError):
        Outcome({"qwk": 1.0}, None, None, **kw)


def test_adapter_refuses_simulation_outcomes_however_built(sim_ledger, labelset):
    real = build(sim_ledger, labelset, simulation=True).score(
        submission_id="s", participant_id="p", payload=perfect(labelset))
    forged = Outcome(dict(real.metrics), None, None, simulation=True)
    for o in (real, forged):
        with pytest.raises(ValueError):
            to_sealed_run_result(o, route_version="x", duration_ms=1)
        with pytest.raises(ValueError):
            to_evaluation_scores(o)


def test_adapter_refuses_outcome_whose_mode_was_tampered(sim_ledger, labelset):
    o = build(sim_ledger, labelset, simulation=True).score(
        submission_id="s", participant_id="p", payload=perfect(labelset))
    object.__setattr__(o, "simulation", False)  # bypass the frozen dataclass: mode still says simulation
    with pytest.raises(ValueError):
        to_sealed_run_result(o, route_version="x", duration_ms=1)
    with pytest.raises(ValueError):
        to_evaluation_scores(o)


def test_production_outcomes_still_publish(ledger, labelset):
    o = build(ledger, labelset).score(submission_id="s", participant_id="p", payload=perfect(labelset))
    assert o.mode == PRODUCTION
    assert to_sealed_run_result(o, route_version="x", duration_ms=1)["metrics"] == o.metrics
    assert to_evaluation_scores(o)
