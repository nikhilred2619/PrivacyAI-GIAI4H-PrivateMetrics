"""Regression tests for D1/D1b: exact ε accounting, never undercharged."""
from decimal import Decimal
from fractions import Fraction

import pytest
from conftest import SCOPE, build, perfect

from privatemetrics.budget import QUANTUM, BudgetValueError, calibrate_scale, parse_epsilon, to_units, units_ceil
from privatemetrics.config import PrivacyConfig
from privatemetrics.mechanism import OpenDPSampler


def test_D1_original_bug_reproduced_by_old_rounding():
    """The superseded ledger used round(eps * 1e6): 2.5e-6 became 2 units."""
    assert round(2.5e-6 * 1_000_000) == 2  # banker's rounding down: the original defect


@pytest.mark.parametrize("bad", ["2.5e-6", 2.5e-6, "1e-7", 1 / 3, "0.3333333", Fraction(1, 3)])
def test_D1_values_off_the_accounting_grid_are_rejected(bad):
    with pytest.raises(BudgetValueError):
        parse_epsilon(bad)


@pytest.mark.parametrize("bad", [0, -1, "0", "-0.5", float("nan"), float("inf"), "nan", "Infinity", True, None,
                                 "abc", [], "1001"])
def test_D1_invalid_values_rejected(bad):
    with pytest.raises(BudgetValueError):
        parse_epsilon(bad)


@pytest.mark.parametrize("ok,units", [("0.000001", 1), (1e-6, 1), ("0.1", 100_000), (0.1, 100_000),
                                      (Decimal("2.5"), 2_500_000), (3, 3_000_000), ("1000", 1_000_000_000)])
def test_D1_grid_values_are_exact(ok, units):
    assert to_units(parse_epsilon(ok)) == units


def test_D1_ceil_never_rounds_down():
    assert units_ceil(Fraction(25, 10_000_000)) == 3   # 2.5e-6 -> 3 units, never 2
    assert units_ceil(Fraction(2, 1_000_000)) == 2


@pytest.mark.parametrize("eps", ["0.000001", "0.000003", "0.1", "0.3", "0.7", "1", "1.1", "2.3", "3.7", "8", "1000"])
def test_D1b_calibrated_scale_certified_cost_never_exceeds_declared(eps):
    declared = parse_epsilon(eps)
    s = OpenDPSampler()
    scale, certified = calibrate_scale(declared, s.epsilon)
    assert Fraction(s.epsilon(scale)) == certified <= declared
    assert units_ceil(certified) <= to_units(declared)
    # The naive textbook scale over-certifies for some values (the original D1b defect):
    naive = Fraction(s.epsilon(2 / float(declared)))
    assert certified <= declared and (naive <= declared or scale > 2 / float(declared))


def test_D1b_naive_scale_exceeds_declared_for_small_epsilon():
    s = OpenDPSampler()
    assert Fraction(s.epsilon(2 / 1e-6)) > Fraction(1, 1_000_000)  # why calibration is needed


def test_D1_cumulative_fractional_releases_never_exceed_cap(ledger, labelset):
    sc = build(ledger, labelset, eps="0.3", cap="1")
    granted = [sc.score(submission_id=f"f{i}", participant_id="p", payload=perfect(labelset)).metrics is not None
               for i in range(5)]
    assert granted == [True, True, True, False, False]
    assert ledger.spent(SCOPE) == Fraction(9, 10) <= 1


def test_D1_boundary_exact_fill(ledger, labelset):
    sc = build(ledger, labelset, eps="0.25", cap="1")
    granted = sum(sc.score(submission_id=f"b{i}", participant_id="p", payload=perfect(labelset)).metrics is not None
                  for i in range(6))
    assert granted == 4 and ledger.spent(SCOPE) == 1


def test_D1_smallest_quantum_release(ledger, labelset):
    sc = build(ledger, labelset, eps="0.000001", cap="0.000003")
    granted = sum(sc.score(submission_id=f"q{i}", participant_id="p", payload=perfect(labelset)).metrics is not None
                  for i in range(5))
    assert granted == 3 and ledger.spent(SCOPE) == 3 * QUANTUM


def test_D1_ledger_refuses_to_charge_below_certified(ledger, labelset):
    sc = build(ledger, labelset)
    with pytest.raises(ValueError):
        ledger.reserve(binding=sc.binding, submission_id="x", participant_id="p", predictions_hash="h",
                       charge=Fraction(1), certified=Fraction(1) + QUANTUM, mode="production")


def test_D1_privacy_config_rejects_float_noise():
    with pytest.raises(BudgetValueError):
        PrivacyConfig(0.1 + 0.2)  # 0.30000000000000004 is not on the 1e-6 grid
    assert PrivacyConfig("0.3").epsilon_per_release == Fraction(3, 10)
