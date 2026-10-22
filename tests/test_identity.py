"""Regression tests for D2: privacy accounting identity independent of label content."""
import threading

import pytest
from conftest import SCOPE, build, perfect, register, synthetic_labels

from privatemetrics.config import PrivacyConfig, TaskConfig
from privatemetrics.labels import LabelSet
from privatemetrics.ledger import Ledger, RegistryError
from privatemetrics.scorer import PrivateScorer


def spend(sc, labels, n, prefix):
    return sum(sc.score(submission_id=f"{prefix}{i}", participant_id="p", payload=perfect(labels)).metrics
               is not None for i in range(n))


def scorer(ledger, labels, task="idrid-dr-grading", version="v1", eps="1"):
    return PrivateScorer(ledger, task_id=task, version_id=version, labels=labels, privacy=PrivacyConfig(eps))


def test_same_dataset_identical_labels_reregistered(ledger, labelset):
    s = build(ledger, labelset, cap="2")
    assert spend(s, labelset, 2, "a") == 2
    register(ledger, labelset, cap="2")  # idempotent re-registration
    assert spend(scorer(ledger, labelset), labelset, 2, "b") == 0


def test_D2_one_label_changed_new_version_shares_budget(ledger, labelset):
    s = build(ledger, labelset, cap="2")
    assert spend(s, labelset, 2, "a") == 2
    changed = LabelSet(5, dict(labelset.labels, item_0000=4))
    ledger.register_version("ds-idrid", "v2", changed, group_factor=1)
    assert spend(scorer(ledger, changed, version="v2"), changed, 3, "b") == 0
    assert ledger.spent(SCOPE) == 2


def test_version_ids_are_immutable(ledger, labelset):
    register(ledger, labelset)
    with pytest.raises(RegistryError):
        ledger.register_version("ds-idrid", "v1", LabelSet(5, dict(labelset.labels, item_0000=4)), group_factor=1)


def test_changed_version_identifier_same_labels_shares_budget(ledger, labelset):
    s = build(ledger, labelset, cap="1")
    assert spend(s, labelset, 1, "a") == 1
    ledger.register_version("ds-idrid", "v1-renamed", labelset, group_factor=1)
    assert spend(scorer(ledger, labelset, version="v1-renamed"), labelset, 2, "b") == 0


def test_D2b_same_dataset_under_another_task_name_shares_budget(ledger, labelset):
    s = build(ledger, labelset, cap="2")
    assert spend(s, labelset, 1, "a") == 1
    ledger.register_task(TaskConfig("renamed-task", 5, 2), "ds-idrid")
    assert spend(scorer(ledger, labelset, task="renamed-task"), labelset, 3, "b") == 1


def test_task_cannot_be_rebound_to_another_dataset(ledger, labelset):
    register(ledger, labelset)
    ledger.register_scope("scope-other", "5")
    ledger.register_dataset("ds-other", "scope-other")
    with pytest.raises(RegistryError):
        ledger.register_task(TaskConfig("idrid-dr-grading", 5, 2), "ds-other")


def test_duplicate_dataset_registration_under_another_scope_refused(ledger, labelset):
    register(ledger, labelset)
    ledger.register_scope("scope-fresh", "100")
    with pytest.raises(RegistryError):
        ledger.register_dataset("ds-idrid", "scope-fresh")  # rebinding a dataset


def test_copy_under_new_dataset_and_scope_refused(ledger, labelset):
    register(ledger, labelset)
    ledger.register_scope("scope-fresh", "100")
    ledger.register_dataset("ds-copy", "scope-fresh")
    with pytest.raises(RegistryError, match="identical labels"):
        ledger.register_version("ds-copy", "v1", labelset, group_factor=1)


def test_minimally_modified_copy_in_new_scope_refused_by_item_overlap(ledger, labelset):
    register(ledger, labelset)
    ledger.register_scope("scope-fresh", "100")
    ledger.register_dataset("ds-copy", "scope-fresh")
    tweaked = LabelSet(5, dict(labelset.labels, item_0000=4))
    with pytest.raises(RegistryError, match="overlap"):
        ledger.register_version("ds-copy", "v1", tweaked, group_factor=1)


def test_partial_overlap_refused(ledger, labelset):
    register(ledger, labelset)
    ledger.register_scope("scope-fresh", "100")
    ledger.register_dataset("ds-partial", "scope-fresh")
    part = LabelSet(5, {"item_0001": 1, "new_1": 2, "new_2": 0})
    with pytest.raises(RegistryError, match="1 item"):
        ledger.register_version("ds-partial", "v1", part, group_factor=1)


def test_overlapping_datasets_may_share_a_scope(ledger, labelset):
    register(ledger, labelset, cap="3")
    ledger.register_dataset("ds-subset", SCOPE)
    sub = LabelSet(5, dict(list(labelset.labels.items())[:10]))
    ledger.register_version("ds-subset", "v1", sub, group_factor=1)
    ledger.register_task(TaskConfig("subset-task", 5, 2), "ds-subset")
    s = build(ledger, labelset, cap="3")
    assert spend(s, labelset, 2, "a") == 2
    assert spend(scorer(ledger, sub, task="subset-task"), sub, 3, "b") == 1  # shared cap of 3


def test_genuinely_disjoint_datasets_have_independent_scopes(ledger, labelset):
    s1 = build(ledger, labelset, cap="1")
    other = synthetic_labels(prefix="hospital_b")
    s2 = build(ledger, other, cap="1", scope="scope-b", dataset="ds-b", task="task-b")
    assert spend(s1, labelset, 2, "a") == 1
    assert spend(s2, other, 2, "b") == 1
    assert ledger.spent(SCOPE) == 1 and ledger.spent("scope-b") == 1


def test_multiple_participants_share_scope(ledger, labelset):
    s = build(ledger, labelset, cap="3")
    got = sum(s.score(submission_id=f"m{i}", participant_id=f"user{i}", payload=perfect(labelset)).metrics
              is not None for i in range(8))
    assert got == 3


def test_concurrent_queries_across_two_tasks_on_one_scope(tmp_path, labelset):
    db = tmp_path / "l.db"
    s1 = build(Ledger(db), labelset, cap="6")
    Ledger(db).register_task(TaskConfig("alias-task", 5, 2), "ds-idrid")
    s2 = scorer(Ledger(db), labelset, task="alias-task")
    out, lock = [], threading.Lock()

    def go(sc, i):
        r = sc.score(submission_id=f"c{i}", participant_id="p", payload=perfect(labelset))
        with lock:
            out.append(r.metrics is not None)

    ts = [threading.Thread(target=go, args=((s1, s2)[i % 2], i)) for i in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(out) == 6 and Ledger(db).spent(SCOPE) == 6


def test_budget_reset_attempts_all_fail(ledger, labelset):
    s = build(ledger, labelset, cap="1")
    spend(s, labelset, 1, "a")
    with pytest.raises(RegistryError):
        ledger.register_scope(SCOPE, "100")                       # re-register with a bigger cap
    with pytest.raises(RegistryError):
        ledger.change_cap(SCOPE, "100", approved_by="", reason="")  # unapproved raise
    with pytest.raises(RegistryError):
        ledger.register_version("ds-idrid", "v1", LabelSet(5, {"x": 0}), group_factor=1)  # overwrite version
    assert ledger.spent(SCOPE) == 1 and ledger.remaining(SCOPE) == 0


def test_cap_changes_are_audited_and_lowering_is_free(ledger, labelset):
    build(ledger, labelset, cap="5")
    ledger.change_cap(SCOPE, "2", approved_by="", reason="")  # lowering needs no approval
    ledger.change_cap(SCOPE, "4", approved_by="WG-Data review 2026-11", reason="larger host task")
    import sqlite3
    rows = sqlite3.connect(ledger.path).execute("SELECT old_units, new_units, approved_by FROM cap_changes").fetchall()
    assert rows == [(5_000_000, 2_000_000, ""), (2_000_000, 4_000_000, "WG-Data review 2026-11")]


def test_scoring_unregistered_or_mismatched_labels_refused(ledger, labelset):
    register(ledger, labelset)
    with pytest.raises(RegistryError):
        scorer(ledger, LabelSet(5, dict(labelset.labels, item_0000=4)))  # labels != registered v1
    with pytest.raises(RegistryError):
        scorer(ledger, labelset, task="no-such-task")
    with pytest.raises(RegistryError):
        scorer(ledger, labelset, version="v9")


def test_old_schema_file_is_not_silently_reused(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE label_sets (label_set_id TEXT PRIMARY KEY)")
    c.commit()
    c.close()
    with pytest.raises(RegistryError):
        Ledger(db)
