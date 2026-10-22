"""Reference label sets (one immutable *version* of a dataset's labels).

Labels are validated once, here, so per-release behaviour never branches on
label values (THREAT_MODEL A6). Two fingerprints are derived for the ledger's
registry. Neither decides which budget is charged; the operator-assigned
privacy scope does (ledger.py):

* `content_fingerprint`: hash over the (item, label) pairs. Used only to keep
  a registered version immutable and to detect the same content being
  registered under another dataset identity.
* `item_fingerprints`: hash per item identifier, used to detect overlap
  between datasets placed in different privacy scopes. Item identifiers are
  treated as non-sensitive (assumption A4); for host tasks with sensitive
  identifiers these should become keyed hashes [DECISION G8].

Fingerprints stay inside the trusted boundary and are never published: for a
small item set a published hash of labels could be brute-forced.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from .config import ITEM_ID_MAX_CHARS, MAX_ITEMS


def item_fingerprint(item_id: str) -> str:
    return hashlib.sha256(b"privatemetrics-item\x00" + item_id.encode()).hexdigest()


@dataclass(frozen=True)
class LabelSet:
    num_classes: int
    labels: Mapping[str, int] = field(repr=False)  # never printed

    def __post_init__(self) -> None:
        if isinstance(self.num_classes, bool) or not isinstance(self.num_classes, int) or not 2 <= self.num_classes <= 64:
            raise ValueError("num_classes must be an integer in [2, 64]")
        if not isinstance(self.labels, Mapping) or not self.labels:
            raise ValueError("label set is empty")
        if len(self.labels) > MAX_ITEMS:
            raise ValueError("label set exceeds 100000 items")
        for item_id, label in self.labels.items():
            if not isinstance(item_id, str) or not 1 <= len(item_id) <= ITEM_ID_MAX_CHARS:
                raise ValueError("invalid item id in label set")
            if isinstance(label, bool) or not isinstance(label, int) or not 0 <= label < self.num_classes:
                raise ValueError("label set contains a label outside [0, K-1]")
        object.__setattr__(self, "labels", dict(self.labels))

    @property
    def item_ids(self) -> frozenset[str]:
        return frozenset(self.labels)

    @property
    def n(self) -> int:
        return len(self.labels)

    @property
    def content_fingerprint(self) -> str:
        body = json.dumps({"K": self.num_classes, "labels": sorted(self.labels.items())}, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()

    def item_fingerprints(self) -> list[str]:
        return sorted(item_fingerprint(i) for i in self.labels)


def check_group_factor(labels: LabelSet, item_to_patient: Mapping[str, str], declared: int) -> int:
    """Validate a host's group-factor declaration against its patient map.

    `item_to_patient` maps every labelled item to an opaque patient key. It is
    read here and discarded: nothing about patients is stored. Returns the
    observed maximum number of items per patient. Raises ValueError if any
    labelled item is unmapped, or if the observed maximum exceeds `declared`
    (the declaration would understate the patient-level ε)."""
    if isinstance(declared, bool) or not isinstance(declared, int) or declared < 1:
        raise ValueError("group factor must be a positive integer")
    missing = labels.item_ids - set(item_to_patient)
    if missing:
        raise ValueError(f"{len(missing)} labelled item(s) have no patient mapping")
    counts: dict[str, int] = {}
    for item_id in labels.labels:
        key = item_to_patient[item_id]
        if not isinstance(key, str) or not key:
            raise ValueError("patient keys must be non-empty strings")
        counts[key] = counts.get(key, 0) + 1
    observed = max(counts.values())
    if observed > declared:
        raise ValueError(f"declared group factor {declared} is below the observed maximum of {observed} items per patient")
    return observed
