import pytest

from privatemetrics.config import PrivacyConfig, TaskConfig
from privatemetrics.labels import LabelSet
from privatemetrics.ledger import SIMULATION, Ledger
from privatemetrics.mechanism import SimulationSampler
from privatemetrics.scorer import PrivateScorer

# Synthetic stand-in for the reference task: same size and class balance as the
# published idrid-dr-grading slice (aggregate counts only), synthetic item IDs.
REFERENCE_CLASS_COUNTS = [7, 5, 6, 6, 6]
SCOPE = "scope-idrid"


def synthetic_labels(counts=REFERENCE_CLASS_COUNTS, prefix="item"):
    labels, i = {}, 0
    for grade, c in enumerate(counts):
        for _ in range(c):
            labels[f"{prefix}_{i:04d}"] = grade
            i += 1
    return LabelSet(len(counts), labels)


def register(ledger, labels, *, cap="5", scope=SCOPE, dataset="ds-idrid", version="v1", task="idrid-dr-grading"):
    ledger.register_scope(scope, cap)
    ledger.register_dataset(dataset, scope)
    ledger.register_version(dataset, version, labels, group_factor=1)
    ledger.register_task(TaskConfig(task, labels.num_classes, 2), dataset)


def build(ledger, labels, *, eps="1", cap="5", scope=SCOPE, dataset="ds-idrid", version="v1",
          task="idrid-dr-grading", simulation=False, seed=0):
    register(ledger, labels, cap=cap, scope=scope, dataset=dataset, version=version, task=task)
    sampler = SimulationSampler(seed) if simulation else None
    return PrivateScorer(ledger, task_id=task, version_id=version, labels=labels, privacy=PrivacyConfig(eps),
                         sampler=sampler, simulation=simulation)


@pytest.fixture
def labelset():
    return synthetic_labels()


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.db")


@pytest.fixture
def sim_ledger(tmp_path):
    """Simulation runs get their own ledger file, opened in simulation mode."""
    return Ledger(tmp_path / "simulation.db", mode=SIMULATION)


@pytest.fixture
def make_scorer(labelset, ledger, sim_ledger):
    def make(eps="1", total="5", *, labels=None, simulation=False, seed=0, led=None, **kw):
        default = sim_ledger if simulation else ledger
        return build(led or default, labels or labelset, eps=eps, cap=total, simulation=simulation, seed=seed, **kw)
    return make


def perfect(labels: LabelSet):
    return {"predictions": dict(labels.labels)}
