"""Persistent, transactional privacy registry and budget ledger (SQLite).

Identity model (docs/ARCHITECTURE.md, "Accounting identity"):

  privacy scope   operator-assigned opaque ID; owns the ε cap. THE accounting key.
  dataset         operator-assigned ID; bound to exactly one scope, forever.
  dataset version immutable label set of a dataset (content fingerprint).
  task            evaluation task ID; bound to exactly one dataset, forever.
  release         one scored submission; charged to the scope.

Budget is never derived from label content. Changing labels (a new version),
renaming a task or registering another task on the same dataset all charge
the same scope. Registering the same content, or any overlapping item, under a
dataset in a *different* scope is refused, so copying or renaming a dataset
cannot open a fresh budget. Genuinely disjoint datasets may have separate
scopes; disjointness is checked on item identifiers only (limitation:
renamed items or one patient across datasets are not detected).

Amounts are integers in units of 1e-6 ε (budget.QUANTUM); sums are exact.
Every row counts towards the scope whatever its status: once reserved, noise
may have been drawn.

Concurrency: every mutation runs in BEGIN IMMEDIATE on a single SQLite file,
which serialises writers across threads and processes on one host. A shared
multi-host deployment needs an equivalent PostgreSQL implementation, which is
NOT provided or tested here (GAP_ANALYSIS G7).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .budget import fmt, from_units, parse_epsilon, to_units
from .config import TaskConfig, check_identifier
from .labels import LabelSet

SCHEMA_VERSION = 4
PRODUCTION = "production"
SIMULATION = "simulation"
MODES = (PRODUCTION, SIMULATION)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS privacy_scopes (
  scope_id     TEXT PRIMARY KEY,
  cap_units    INTEGER NOT NULL CHECK (cap_units > 0),
  created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cap_changes (
  scope_id     TEXT NOT NULL REFERENCES privacy_scopes(scope_id),
  old_units    INTEGER NOT NULL,
  new_units    INTEGER NOT NULL,
  approved_by  TEXT NOT NULL,
  reason       TEXT NOT NULL,
  changed_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
  dataset_id   TEXT PRIMARY KEY,
  scope_id     TEXT NOT NULL REFERENCES privacy_scopes(scope_id),
  created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_versions (
  dataset_id          TEXT NOT NULL REFERENCES datasets(dataset_id),
  version_id          TEXT NOT NULL,
  content_fingerprint TEXT NOT NULL,
  num_classes         INTEGER NOT NULL,
  item_count          INTEGER NOT NULL,
  group_factor        INTEGER NOT NULL CHECK (group_factor >= 1),
  created_at          REAL NOT NULL,
  PRIMARY KEY (dataset_id, version_id)
);
CREATE TABLE IF NOT EXISTS item_fingerprints (
  item_fp      TEXT NOT NULL,
  dataset_id   TEXT NOT NULL REFERENCES datasets(dataset_id),
  PRIMARY KEY (item_fp, dataset_id)
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id             TEXT PRIMARY KEY,
  dataset_id          TEXT NOT NULL REFERENCES datasets(dataset_id),
  num_classes         INTEGER NOT NULL,
  referable_threshold INTEGER NOT NULL,
  created_at          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
  release_id       TEXT PRIMARY KEY,
  submission_id    TEXT NOT NULL UNIQUE,
  participant_id   TEXT NOT NULL,
  task_id          TEXT NOT NULL REFERENCES tasks(task_id),
  dataset_id       TEXT NOT NULL,
  version_id       TEXT NOT NULL,
  scope_id         TEXT NOT NULL REFERENCES privacy_scopes(scope_id),
  predictions_hash TEXT NOT NULL,
  charged_units    INTEGER NOT NULL CHECK (charged_units > 0),
  certified_eps    TEXT NOT NULL,
  mode             TEXT NOT NULL CHECK (mode IN ('production','simulation')),
  status           TEXT NOT NULL CHECK (status IN ('RESERVED','RELEASED','FAILED_SPENT')),
  metrics_json     TEXT,
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS releases_by_scope ON releases(scope_id);
"""


class RegistryError(ValueError):
    """A registration that would weaken accounting (reset, rebind, overlap)."""


@dataclass(frozen=True)
class Binding:
    task_id: str
    dataset_id: str
    version_id: str
    scope_id: str
    num_classes: int
    referable_threshold: int
    group_factor: int = 1  # declared max evaluation items per patient for this version


@dataclass(frozen=True)
class Reservation:
    kind: str  # new | replay | exhausted | conflict | in_progress | failed
    release_id: str | None = None
    metrics: dict | None = None
    mode: str | None = None  # mode of the stored row, for replays


class Ledger:
    def __init__(self, path: Path | str, *, mode: str = PRODUCTION):
        """Open or create a ledger file. Every file has one immutable mode,
        fixed when it is created: a production ledger never holds simulation
        releases and a simulation ledger never holds production ones. Opening a
        file in the other mode is refused (no shared budget, no shared cache)."""
        if mode not in MODES:
            raise ValueError("mode must be 'production' or 'simulation'")
        self.path = str(path)
        self.mode = mode
        if not os.path.exists(self.path):
            # Owner-only from the start (SQLite gives its -wal/-shm files the same
            # mode). The ledger holds released metrics and predictions hashes.
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        with closing(conn):
            conn.execute("PRAGMA journal_mode=WAL")
            existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "label_sets" in existing:
                raise RegistryError("ledger file uses the superseded v1 schema; migrate it explicitly")
            if "meta" in existing:
                row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if row is None or row[0] != str(SCHEMA_VERSION):
                    raise RegistryError(
                        f"unsupported ledger schema version {row[0] if row else 'unknown'}; this release reads "
                        f"schema {SCHEMA_VERSION} only (v2 files cannot tell simulation from production releases; "
                        "v3 files carry no patient group factor). Create a new ledger file")
            conn.executescript(SCHEMA)  # executescript manages its own transaction
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() is None:
                    conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
                    conn.execute("INSERT INTO meta VALUES ('mode', ?)", (mode,))
                stored = conn.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            if stored is None or stored[0] != mode:
                raise RegistryError(f"ledger file is a {stored[0] if stored else 'unknown'}-mode ledger; "
                                    f"refusing to open it in {mode} mode")

    @contextmanager
    def _tx(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        with closing(conn):
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # --- registry (operator actions) -----------------------------------------

    def register_scope(self, scope_id: str, cap: object) -> None:
        check_identifier(scope_id, "scope_id")
        units = to_units(parse_epsilon(cap, name="cap"))
        with self._tx() as c:
            row = c.execute("SELECT cap_units FROM privacy_scopes WHERE scope_id=?", (scope_id,)).fetchone()
            if row is None:
                c.execute("INSERT INTO privacy_scopes VALUES (?,?,?)", (scope_id, units, time.time()))
            elif row[0] != units:
                raise RegistryError("scope already registered with a different cap; use change_cap")

    def change_cap(self, scope_id: str, new_cap: object, *, approved_by: str, reason: str) -> None:
        """Lowering is always allowed. Raising weakens protection and needs a
        named approver and reason; every change is recorded."""
        units = to_units(parse_epsilon(new_cap, name="cap"))
        with self._tx() as c:
            row = c.execute("SELECT cap_units FROM privacy_scopes WHERE scope_id=?", (scope_id,)).fetchone()
            if row is None:
                raise RegistryError("unknown scope")
            if units > row[0] and (not approved_by.strip() or not reason.strip()):
                raise RegistryError("raising a cap requires approved_by and reason")
            c.execute("UPDATE privacy_scopes SET cap_units=? WHERE scope_id=?", (units, scope_id))
            c.execute("INSERT INTO cap_changes VALUES (?,?,?,?,?,?)",
                      (scope_id, row[0], units, approved_by, reason, time.time()))

    def register_dataset(self, dataset_id: str, scope_id: str) -> None:
        check_identifier(dataset_id, "dataset_id")
        with self._tx() as c:
            if c.execute("SELECT 1 FROM privacy_scopes WHERE scope_id=?", (scope_id,)).fetchone() is None:
                raise RegistryError("unknown scope")
            row = c.execute("SELECT scope_id FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
            if row is None:
                c.execute("INSERT INTO datasets VALUES (?,?,?)", (dataset_id, scope_id, time.time()))
            elif row[0] != scope_id:
                raise RegistryError("dataset is bound to another scope; scope bindings are immutable")

    def register_version(self, dataset_id: str, version_id: str, labels: LabelSet, *, group_factor: int) -> None:
        """Register an immutable label set. `group_factor` is the host's
        declaration of the largest number of evaluation items that belong to
        one patient in this version (1 = one item per patient). It is part of
        the version and cannot be changed later; see labels.check_group_factor
        for validating it against a patient map."""
        check_identifier(version_id, "version_id")
        if isinstance(group_factor, bool) or not isinstance(group_factor, int) or not 1 <= group_factor <= labels.n:
            raise RegistryError("group_factor must be an integer between 1 and the number of items")
        fp = labels.content_fingerprint
        items = labels.item_fingerprints()
        with self._tx() as c:
            ds = c.execute("SELECT scope_id FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
            if ds is None:
                raise RegistryError("unknown dataset")
            scope = ds[0]
            row = c.execute("SELECT content_fingerprint, group_factor FROM dataset_versions "
                            "WHERE dataset_id=? AND version_id=?", (dataset_id, version_id)).fetchone()
            if row is not None:
                if row[0] != fp:
                    raise RegistryError("dataset version already registered with different labels; versions are immutable")
                if row[1] != group_factor:
                    raise RegistryError("dataset version already registered with a different group factor; "
                                        "versions are immutable")
                return  # idempotent re-registration
            same = c.execute(
                "SELECT d.scope_id FROM dataset_versions v JOIN datasets d USING(dataset_id) "
                "WHERE v.content_fingerprint=? AND d.scope_id<>?", (fp, scope)).fetchone()
            if same is not None:
                raise RegistryError("identical labels are registered under another privacy scope")
            c.execute("CREATE TEMP TABLE IF NOT EXISTS new_items (item_fp TEXT PRIMARY KEY)")
            c.execute("DELETE FROM new_items")
            c.executemany("INSERT INTO new_items VALUES (?)", [(x,) for x in items])
            overlap = c.execute(
                "SELECT COUNT(*) FROM item_fingerprints f JOIN datasets d USING(dataset_id) "
                "JOIN new_items n USING(item_fp) WHERE d.scope_id<>?", (scope,)).fetchone()[0]
            if overlap:
                raise RegistryError(f"{overlap} item(s) overlap a dataset in another privacy scope")
            c.execute("INSERT INTO dataset_versions VALUES (?,?,?,?,?,?,?)",
                      (dataset_id, version_id, fp, labels.num_classes, labels.n, group_factor, time.time()))
            c.executemany("INSERT OR IGNORE INTO item_fingerprints VALUES (?,?)", [(x, dataset_id) for x in items])

    def register_task(self, task: TaskConfig, dataset_id: str) -> None:
        with self._tx() as c:
            if c.execute("SELECT 1 FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone() is None:
                raise RegistryError("unknown dataset")
            row = c.execute("SELECT dataset_id, num_classes, referable_threshold FROM tasks WHERE task_id=?",
                            (task.task_id,)).fetchone()
            if row is None:
                c.execute("INSERT INTO tasks VALUES (?,?,?,?,?)",
                          (task.task_id, dataset_id, task.num_classes, task.referable_threshold, time.time()))
            elif row != (dataset_id, task.num_classes, task.referable_threshold):
                raise RegistryError("task already registered with a different dataset or configuration")

    def resolve(self, task_id: str, version_id: str, labels: LabelSet) -> Binding:
        """The accounting binding for scoring `task_id` against `version_id`,
        after checking that `labels` is exactly the registered version."""
        with self._tx() as c:
            row = c.execute(
                "SELECT t.dataset_id, d.scope_id, t.num_classes, t.referable_threshold, v.content_fingerprint, "
                "v.group_factor "
                "FROM tasks t JOIN datasets d USING(dataset_id) "
                "LEFT JOIN dataset_versions v ON v.dataset_id=t.dataset_id AND v.version_id=? WHERE t.task_id=?",
                (version_id, task_id)).fetchone()
        if row is None:
            raise RegistryError("unknown task")
        dataset_id, scope_id, k, thr, fp, gf = row
        if fp is None:
            raise RegistryError("unknown dataset version for this task")
        if fp != labels.content_fingerprint or k != labels.num_classes:
            raise RegistryError("labels do not match the registered dataset version")
        return Binding(task_id, dataset_id, version_id, scope_id, k, thr, gf)

    # --- releases --------------------------------------------------------------

    def reserve(self, *, binding: Binding, submission_id: str, participant_id: str, predictions_hash: str,
                charge: Fraction, certified: Fraction, mode: str) -> Reservation:
        if mode != self.mode:
            raise RegistryError(f"{mode} release refused on a {self.mode}-mode ledger")
        if certified > charge:
            raise ValueError("refusing to charge less than the certified privacy cost")
        need = to_units(charge)
        now = time.time()
        with self._tx() as c:
            row = c.execute(
                "SELECT release_id, participant_id, task_id, version_id, scope_id, predictions_hash, charged_units, "
                "status, metrics_json, mode FROM releases WHERE submission_id=?", (submission_id,)).fetchone()
            if row is not None:
                rid, pid, tid, vid, sid, ph, units, status, mj, rmode = row
                if rmode != mode:  # defence in depth: never serve a row across modes
                    return Reservation("conflict", rid, mode=rmode)
                if (pid, tid, vid, sid, ph, units) != (participant_id, binding.task_id, binding.version_id,
                                                       binding.scope_id, predictions_hash, need):
                    return Reservation("conflict", rid)
                if status == "RELEASED":
                    return Reservation("replay", rid, json.loads(mj), mode=rmode)
                return Reservation("in_progress" if status == "RESERVED" else "failed", rid)
            cap = c.execute("SELECT cap_units FROM privacy_scopes WHERE scope_id=?", (binding.scope_id,)).fetchone()
            if cap is None:
                raise RegistryError("unknown scope")
            spent = c.execute("SELECT COALESCE(SUM(charged_units),0) FROM releases WHERE scope_id=?",
                              (binding.scope_id,)).fetchone()[0]
            if spent + need > cap[0]:
                return Reservation("exhausted")
            rid = "rel_" + uuid.uuid4().hex
            c.execute("INSERT INTO releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, submission_id, participant_id, binding.task_id, binding.dataset_id, binding.version_id,
                       binding.scope_id, predictions_hash, need, str(certified), mode, "RESERVED", None, now, now))
            return Reservation("new", rid, mode=mode)

    def release(self, release_id: str, metrics: dict) -> None:
        with self._tx() as c:
            cur = c.execute("UPDATE releases SET status='RELEASED', metrics_json=?, updated_at=? "
                            "WHERE release_id=? AND status='RESERVED'",
                            (json.dumps(metrics, sort_keys=True), time.time(), release_id))
            if cur.rowcount != 1:
                raise RuntimeError("release row not in RESERVED state")

    def fail(self, release_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE releases SET status='FAILED_SPENT', updated_at=? WHERE release_id=? AND status='RESERVED'",
                      (time.time(), release_id))

    def recover_stale(self, older_than_s: float) -> int:
        """Operator crash recovery: RESERVED rows older than the threshold become
        FAILED_SPENT. Their ε stays charged; they are never re-released."""
        with self._tx() as c:
            cur = c.execute("UPDATE releases SET status='FAILED_SPENT', updated_at=? "
                            "WHERE status='RESERVED' AND created_at < ?", (time.time(), time.time() - older_than_s))
            return cur.rowcount

    # --- read-only views -------------------------------------------------------

    def spent(self, scope_id: str) -> Fraction:
        with self._tx() as c:
            v = c.execute("SELECT COALESCE(SUM(charged_units),0) FROM releases WHERE scope_id=?",
                          (scope_id,)).fetchone()[0]
        return from_units(v)

    def submission_status(self, submission_id: str) -> str | None:
        """RESERVED, RELEASED, FAILED_SPENT, or None if the submission is unknown."""
        with self._tx() as c:
            row = c.execute("SELECT status FROM releases WHERE submission_id=?", (submission_id,)).fetchone()
        return None if row is None else str(row[0])

    def remaining(self, scope_id: str) -> Fraction:
        with self._tx() as c:
            cap = c.execute("SELECT cap_units FROM privacy_scopes WHERE scope_id=?", (scope_id,)).fetchone()
            v = c.execute("SELECT COALESCE(SUM(charged_units),0) FROM releases WHERE scope_id=?",
                          (scope_id,)).fetchone()[0]
        if cap is None:
            raise RegistryError("unknown scope")
        return from_units(cap[0] - v)

    def rows(self) -> list[dict]:
        conn = sqlite3.connect(self.path)
        with closing(conn):
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM releases ORDER BY created_at")]

    def scope_summary(self) -> list[dict]:
        conn = sqlite3.connect(self.path)
        with closing(conn):
            q = ("SELECT s.scope_id, s.cap_units, COALESCE(SUM(r.charged_units),0), COUNT(r.release_id), "
                 "COALESCE(SUM(r.charged_units * v.group_factor),0), "
                 "(SELECT COALESCE(MAX(v2.group_factor),1) FROM dataset_versions v2 JOIN datasets d2 USING(dataset_id) "
                 " WHERE d2.scope_id=s.scope_id) "
                 "FROM privacy_scopes s LEFT JOIN releases r USING(scope_id) "
                 "LEFT JOIN dataset_versions v ON v.dataset_id=r.dataset_id AND v.version_id=r.version_id "
                 "GROUP BY s.scope_id ORDER BY s.scope_id")
            # spent/cap are label-level ε (one item's label). patient_level_spent is
            # the sum of g × ε over releases, where g is the group factor declared
            # for the version each release used: an upper bound on the ε spent
            # on any one patient. patient_level_cap_bound = max g × cap.
            return [{"scope": s, "cap": fmt(from_units(cap)), "spent": fmt(from_units(sp)), "releases": n,
                     "patient_level_spent": fmt(from_units(psp)),
                     "patient_level_cap_bound": fmt(from_units(cap * gmax))}
                    for s, cap, sp, n, psp, gmax in conn.execute(q)]
