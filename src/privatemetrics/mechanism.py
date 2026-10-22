"""Noise for the label-dependent cells.

Production: OpenDP's discrete Laplace over an integer vector with the L1
metric. OpenDP samples from a cryptographically secure RNG (documented in
`opendp._data.fill_bytes`), exactly, over the integers: no floating-point
Laplace (Mironov 2012). The privacy map is asked for ε at d_in = 2 and must
not exceed the configured ε.

Simulation: a seeded NumPy sampler of the same distribution (difference of two
geometric variables). It exists only to make experiments reproducible and is
refused unless the caller explicitly runs in simulation mode.
"""

from __future__ import annotations

import math
from typing import Protocol

L1_SENSITIVITY = 2  # replace-one label change, THREAT_MODEL Lemma 1


class Sampler(Protocol):
    simulation: bool

    def epsilon(self, scale: float) -> float: ...

    def noisy(self, values: list[int], scale: float) -> list[int]: ...


class OpenDPSampler:
    simulation = False

    def __init__(self) -> None:
        import opendp.prelude as dp

        dp.enable_features("contrib")
        self._dp = dp
        self._cache: dict[float, object] = {}

    def measurement(self, scale: float):
        if scale not in self._cache:
            dp = self._dp
            self._cache[scale] = dp.m.make_laplace(
                dp.vector_domain(dp.atom_domain(T=int)), dp.l1_distance(T=int), scale=scale)
        return self._cache[scale]

    def epsilon(self, scale: float) -> float:
        """ε from OpenDP's own privacy map at the declared sensitivity."""
        return float(self.measurement(scale).map(L1_SENSITIVITY))

    def noisy(self, values: list[int], scale: float) -> list[int]:
        if not values:
            return []
        return [int(v) for v in self.measurement(scale)(values)]


class SimulationSampler:
    """Seeded sampler for experiments only. Never used for real releases."""

    simulation = True

    def __init__(self, seed: int):
        import numpy as np

        self._rng = np.random.default_rng(seed)

    def epsilon(self, scale: float) -> float:
        return L1_SENSITIVITY / scale

    def noisy(self, values: list[int], scale: float) -> list[int]:
        if not values:
            return []
        p = 1.0 - math.exp(-1.0 / scale)
        n = len(values)
        z = self._rng.geometric(p, n) - self._rng.geometric(p, n)
        return [int(v) + int(d) for v, d in zip(values, z, strict=True)]


def discrete_laplace_variance(scale: float) -> float:
    """Variance of discrete Laplace with Pr[z] ∝ exp(-|z|/scale)."""
    a = math.exp(-1.0 / scale)
    return 2 * a / (1 - a) ** 2
