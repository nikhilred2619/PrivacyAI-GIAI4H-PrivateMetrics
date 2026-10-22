"""Task and privacy configuration, validated once."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .budget import parse_epsilon

MAX_ITEMS = 100_000  # upstream payload cap (conformance spec §2.2)
ITEM_ID_MAX_CHARS = 200  # upstream ITEM_ID_MAX_CHARS
ID_MAX_CHARS = 80


def check_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= ID_MAX_CHARS or not value.isprintable():
        raise ValueError(f"{name} must be a printable string of 1-{ID_MAX_CHARS} characters")
    return value


@dataclass(frozen=True)
class TaskConfig:
    task_id: str
    num_classes: int = 5
    referable_threshold: int = 2

    def __post_init__(self) -> None:
        check_identifier(self.task_id, "task_id")
        if isinstance(self.num_classes, bool) or not isinstance(self.num_classes, int) or not 2 <= self.num_classes <= 64:
            raise ValueError("num_classes must be an integer in [2, 64]")
        if (isinstance(self.referable_threshold, bool) or not isinstance(self.referable_threshold, int)
                or not 0 < self.referable_threshold < self.num_classes):
            raise ValueError("referable_threshold must be an integer in [1, num_classes-1]")


@dataclass(frozen=True)
class PrivacyConfig:
    """Per-release ε. The cap is a property of the privacy scope in the
    ledger, not of a scorer, so it cannot be changed by starting a scorer with
    a different configuration."""

    epsilon_per_release: Fraction

    def __init__(self, epsilon_per_release: object):
        object.__setattr__(self, "epsilon_per_release", parse_epsilon(epsilon_per_release, name="epsilon_per_release"))
