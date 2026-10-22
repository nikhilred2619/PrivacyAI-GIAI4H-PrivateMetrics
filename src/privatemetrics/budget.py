"""Exact privacy-budget arithmetic.

ε values are exact rationals (`fractions.Fraction`) on a fixed accounting grid
of 1e-6 (QUANTUM). Configuration values must lie exactly on the grid; anything
else is rejected rather than rounded, so the ledger can never record less than
the configured cost. Ledger amounts are integers in units of QUANTUM, so sums
are exact and repeated fractional releases cannot drift past a cap.

The noise scale is chosen so that OpenDP's own privacy map (the certified
cost) is at or below the configured ε; the ledger then charges the configured
ε, which is therefore >= the certified cost.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from fractions import Fraction

QUANTUM = Fraction(1, 1_000_000)  # accounting resolution: 1e-6 ε
MAX_EPSILON = Fraction(1000)       # sanity bound on any single configured value
MAX_NUDGES = 256


class BudgetValueError(ValueError):
    pass


def parse_epsilon(value: object, *, name: str = "epsilon") -> Fraction:
    """Exact ε from int, str, Decimal, Fraction or float.

    A float is read through its shortest round-trip decimal form (`repr`), so
    `0.1` means exactly 1/10. Rejected: bool, NaN, ±inf, values <= 0, values
    above MAX_EPSILON, and values not an exact multiple of QUANTUM."""
    if isinstance(value, bool) or value is None:
        raise BudgetValueError(f"{name} must be a number")
    try:
        if isinstance(value, Fraction):
            f = value
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise BudgetValueError(f"{name} must be finite")
            f = Fraction(Decimal(repr(value)))
        elif isinstance(value, (int, Decimal, str)):
            d = Decimal(value) if not isinstance(value, Decimal) else value
            if not d.is_finite():
                raise BudgetValueError(f"{name} must be finite")
            f = Fraction(d)
        else:
            raise BudgetValueError(f"{name} has unsupported type {type(value).__name__}")
    except (InvalidOperation, ValueError) as exc:
        if isinstance(exc, BudgetValueError):
            raise
        raise BudgetValueError(f"{name} is not a valid number") from exc
    if f <= 0:
        raise BudgetValueError(f"{name} must be > 0")
    if f > MAX_EPSILON:
        raise BudgetValueError(f"{name} must be <= {MAX_EPSILON}")
    if (f / QUANTUM).denominator != 1:
        raise BudgetValueError(f"{name} must be a multiple of the accounting precision 1e-6")
    return f


def to_units(eps: Fraction) -> int:
    q = eps / QUANTUM
    if q.denominator != 1:
        raise BudgetValueError("value is not on the accounting grid")
    return q.numerator


def units_ceil(eps: Fraction) -> int:
    """Smallest number of QUANTUM units that covers eps (never rounds down)."""
    q = eps / QUANTUM
    return -(-q.numerator // q.denominator)


def from_units(units: int) -> Fraction:
    return units * QUANTUM


def fmt(eps: Fraction) -> str:
    return str(Decimal(eps.numerator) / Decimal(eps.denominator))


def calibrate_scale(declared: Fraction, privacy_map, sensitivity: int = 2) -> tuple[float, Fraction]:
    """Return (scale, certified ε) with certified <= declared.

    Starts at the textbook scale Δ/ε and moves the float scale up one ULP at a
    time until the mechanism's privacy map, read exactly as a rational, does
    not exceed the declared ε. Raises if that does not happen quickly."""
    scale = float(Fraction(sensitivity) / declared)
    for _ in range(MAX_NUDGES):
        certified = Fraction(privacy_map(scale))
        if certified <= declared:
            return scale, certified
        scale = math.nextafter(scale, math.inf)
    raise BudgetValueError("could not calibrate a noise scale within the declared epsilon")
