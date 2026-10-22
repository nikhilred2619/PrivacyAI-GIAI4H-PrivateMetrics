"""Label-free validation of a predictions payload.

Mirrors the upstream rules (conformance spec §2.2, worker-eval `predictions.py`):
integer labels in [0, K-1], no unknown item IDs, at most 100,000 entries,
IDs of 1-200 characters. Missing items are allowed (reduced coverage).
Nothing here reads reference labels, so no outcome can depend on them.
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from typing import Any

from .config import ITEM_ID_MAX_CHARS, MAX_ITEMS
from .errors import FailureCode, ReleaseRefused


def validate_predictions(payload: Any, item_ids: Set[str], num_classes: int) -> dict[str, int]:
    if not isinstance(payload, Mapping) or set(payload.keys()) != {"predictions"}:
        raise ReleaseRefused(FailureCode.MALFORMED_OUTPUT, "payload must be an object with exactly one key 'predictions'")
    preds = payload["predictions"]
    if not isinstance(preds, Mapping):
        raise ReleaseRefused(FailureCode.MALFORMED_OUTPUT, "'predictions' must be an object")
    if len(preds) > MAX_ITEMS:
        raise ReleaseRefused(FailureCode.OUTPUT_TOO_LARGE, "more than 100000 predictions")
    out: dict[str, int] = {}
    unknown = 0
    for item_id, label in preds.items():
        if not isinstance(item_id, str) or not 1 <= len(item_id) <= ITEM_ID_MAX_CHARS:
            raise ReleaseRefused(FailureCode.MALFORMED_OUTPUT, "item id must be a string of 1-200 characters")
        if isinstance(label, bool) or not isinstance(label, int):
            raise ReleaseRefused(FailureCode.MALFORMED_OUTPUT, "prediction labels must be integers")
        if not 0 <= label < num_classes:
            raise ReleaseRefused(FailureCode.MALFORMED_OUTPUT, "prediction label out of range")
        if item_id not in item_ids:
            unknown += 1
            continue
        out[item_id] = label
    if unknown:
        # Upstream reports which IDs are unknown; the IDs are the participant's own
        # input and public, so this does not depend on labels. The count is enough here.
        raise ReleaseRefused(FailureCode.UNKNOWN_ITEM_IDS, "predictions contain unknown item ids")
    return out
