"""Failure codes. The first group mirrors the upstream closed taxonomy
(`SealedRunFailureCodeSchema`); the last is proposed (GAP_ANALYSIS G3).

Every code and its fixed detail string are independent of label values.
"""

from __future__ import annotations

import enum


class FailureCode(enum.StrEnum):
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    UNKNOWN_ITEM_IDS = "UNKNOWN_ITEM_IDS"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # Proposed addition; mapped to INTERNAL_ERROR on the wire until upstream adopts it.
    PRIVACY_BUDGET_EXHAUSTED = "PRIVACY_BUDGET_EXHAUSTED"


UPSTREAM_CODES = {c for c in FailureCode if c is not FailureCode.PRIVACY_BUDGET_EXHAUSTED}


class ReleaseRefused(Exception):
    """Raised for every refused release. `detail` is a fixed, value-free string."""

    def __init__(self, code: FailureCode, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# The complete upstream closed taxonomy (conformance specification §2.3;
# worker-eval `SealedRunFailureCode`). Worker failures are passed through the
# host scorer only if their code is in this set.
WIRE_FAILURE_CODES = frozenset({
    "TIMEOUT", "OOM_KILLED", "NONZERO_EXIT", "NO_OUTPUT", "MALFORMED_OUTPUT", "UNKNOWN_ITEM_IDS",
    "OUTPUT_TOO_LARGE", "NETWORK_ATTEMPT_DETECTED", "DIGEST_MISMATCH", "IMAGE_PULL_FAILED",
    "STARTUP_FAILED", "INTERNAL_ERROR",
})
FAILURE_DETAIL_MAX_CHARS = 4_000  # upstream worker-eval contract
