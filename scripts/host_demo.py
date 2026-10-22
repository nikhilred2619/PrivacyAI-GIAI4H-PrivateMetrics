"""Host-side demonstration through the real CLI (`python -m privatemetrics ...`).

Every step runs the installed command in a fresh process (so every step is also
a restart) and records the exact command, exit code and output in
docs/HOST_DEMO_TRANSCRIPT.md. Steps assert their expected outcome; the script
exits non-zero if any does not hold.

Data:
  * PRIVATE HOST DATA: synthetic labels and a patient map generated at run time
    into <workdir>/host-private/ (default demo-host/, git-ignored). They stand in
    for a hospital's labels; nothing real or private is in the repository.
  * PUBLIC REFERENCE DATA: fixtures/reference/ (IDRiD grades, CC BY 4.0), used
    in the last part to score the reference task at its declared ε.

The sealed model run itself is not part of this script: `worker.json` stands in
for the upstream worker's predictions result. integration/sealed_run/ runs the
real upstream worker with Docker.

This is a local demonstration. It is not a deployment on, or a result from, the
GI-AI4H platform.

Usage: python scripts/host_demo.py [--workdir demo-host] [--no-transcript]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK = "host-dr-grading"
EPS, CAP = "1", "3"


class Demo:
    def __init__(self, work: Path):
        self.work = work
        self.log: list[str] = []
        self.failures: list[str] = []

    def section(self, title: str, text: str = "") -> None:
        self.log += ["", f"## {title}", ""] + ([text, ""] if text else [])

    def run(self, *args: str, expect: int | None = 0) -> tuple[int, str]:
        cmd = [sys.executable, "-m", "privatemetrics", *args]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=self.work)
        shown = " ".join(["python -m privatemetrics", *[a.replace(str(self.work) + "/", "") for a in args]])
        out = (r.stdout + r.stderr).strip()
        self.log += ["```console", f"$ {shown}", out, f"[exit {r.returncode}]", "```", ""]
        if expect is not None and r.returncode != expect:
            self.failures.append(f"{shown}: exit {r.returncode}, expected {expect}")
        return r.returncode, r.stdout

    def check(self, ok: bool, what: str) -> None:
        self.log.append(f"* {'PASS' if ok else 'FAIL'}: {what}")
        if not ok:
            self.failures.append(what)


def write_private_host(d: Path) -> dict[str, int]:
    rng = random.Random(4242)
    d.mkdir(parents=True, exist_ok=True)
    labels = {f"case_{i:05d}": rng.choices(range(5), weights=[40, 20, 20, 10, 10])[0] for i in range(400)}
    with (d / "labels.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Image", "dr_grade"])
        w.writerows(labels.items())
    with (d / "patients.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["item_id", "patient_id"])
        w.writerows((k, f"patient_{i // 2:05d}") for i, k in enumerate(sorted(labels)))  # two eyes each
    (d / "host.json").write_text(json.dumps({
        "task_id": TASK, "dataset_id": "hospital-dr-2026", "version_id": "2026-10", "scope_id": "hospital-dr-scope",
        "labels_csv": "labels.csv", "id_column": "Image", "label_column": "dr_grade", "num_classes": 5,
        "referable_threshold": 2, "epsilon_per_release": EPS, "ledger": "ledger.db", "group_factor": 2}, indent=1))
    return labels


def model_predictions(labels: dict[str, int], seed: int) -> dict[str, int]:
    """A participant model's output as the worker would hand it over (synthetic)."""
    rng = random.Random(seed)
    return {k: v if rng.random() < 0.75 else min(4, max(0, v + rng.choice((-1, 1)))) for k, v in labels.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", type=Path, default=ROOT / "demo-host")
    ap.add_argument("--no-transcript", action="store_true")
    a = ap.parse_args()
    work = a.workdir.resolve()
    if work.exists():
        shutil.rmtree(work)
    priv = work / "host-private"
    labels = write_private_host(priv)
    cfg = str(priv / "host.json")
    demo = Demo(work)

    demo.section("1. Host-side task configuration",
                 "The host's configuration (`host-private/host.json`) names the task, the dataset and version, the "
                 "privacy scope, the per-release ε, the declared group factor and the paths of the labels file and "
                 "ledger. Both paths are host-local.")
    demo.log += ["```json", (priv / "host.json").read_text(), "```"]

    demo.section("2–3. Trusted label registration, dataset and privacy-scope registration",
                 "A declared group factor of 1 is refused, because the patient map shows two items per patient. "
                 "The correct declaration (2) is accepted. The patient map is read and discarded.")
    bad = json.loads((priv / "host.json").read_text())
    bad["group_factor"] = 1
    (priv / "host-g1.json").write_text(json.dumps(bad))
    rc, _ = demo.run("host-register", "--config", str(priv / "host-g1.json"), "--epsilon-total", CAP,
                     "--patient-map", str(priv / "patients.csv"), expect=None)
    demo.check(rc != 0, "an understated group factor is refused at registration")
    (priv / "host-g1.json").unlink()
    rc, out = demo.run("host-register", "--config", cfg, "--epsilon-total", CAP, "--patient-map",
                       str(priv / "patients.csv"))
    demo.check(rc == 0 and json.loads(out)["group_factor_checked_against_patient_map"], "registration with g = 2")

    def dispatch(sid: str) -> Path:
        p = work / f"dispatch-{sid[:8]}.json"
        p.write_text(json.dumps({"submissionId": sid, "taskSlug": TASK, "routeVersion": route,
                                 "imageRef": "registry.example/team/model@sha256:" + "c" * 64,
                                 "imageDigest": "sha256:" + "c" * 64, "timeoutSec": 600,
                                 "callbackUrl": f"https://platform.example/v2/submissions/{sid}/result",
                                 "deadline": "2099-01-01T00:00:00Z"}))
        return p

    def worker(preds: dict[str, int], name: str) -> Path:
        p = work / f"{name}.json"
        p.write_text(json.dumps({"routeVersion": None, "durationMs": 61234, "predictions": preds}))
        return p

    route = json.loads(subprocess.run([sys.executable, "-m", "privatemetrics", "version"], capture_output=True,
                                      text=True, check=True).stdout)["routeVersion"]
    sid_a = str(uuid.uuid4())
    preds_a = model_predictions(labels, 1)
    w_a = worker(preds_a, "worker-a")

    demo.section("4. Participant prediction input",
                 "The platform's dispatch (spec §2.1) and the worker's result after the sealed run "
                 f"(`worker-a.json`: {len(preds_a)} predictions, host-internal; not reproduced here).")
    demo.log += ["```json", dispatch(sid_a).read_text(), "```"]

    demo.section("5–8. Preflight, scoring with production OpenDP noise, ledger reservation, metric release",
                 "Preflight checks the dispatch and the remaining budget before the model runs. `host-score` "
                 "validates the predictions, reserves ε in the ledger (one `BEGIN IMMEDIATE` transaction), draws "
                 "OpenDP discrete-Laplace noise, post-processes and records the release, and prints the only body "
                 "that leaves the host.")
    rc, _ = demo.run("host-preflight", "--config", cfg, "--dispatch", str(dispatch(sid_a)))
    rc, out = demo.run("host-score", "--config", cfg, "--dispatch", str(dispatch(sid_a)), "--worker-result", str(w_a))
    first = json.loads(out)
    demo.check(set(first) == {"routeVersion", "durationMs", "metrics"}, "only routeVersion, durationMs, metrics")
    demo.check(not any(k in out for k in list(labels)[:400]), "no item IDs (hence no labels or predictions) in output")

    demo.section("9. Replay behaviour",
                 "Redelivering the same submission (a fresh process: a restart) returns the stored release, with no "
                 "new noise and no new charge. Redelivery with different predictions is refused.")
    rc, out = demo.run("host-score", "--config", cfg, "--dispatch", str(dispatch(sid_a)), "--worker-result", str(w_a))
    demo.check(json.loads(out)["metrics"] == first["metrics"], "identical metrics on replay")
    w_changed = worker(model_predictions(labels, 99), "worker-a-rerun")
    rc, out = demo.run("host-score", "--config", cfg, "--dispatch", str(dispatch(sid_a)), "--worker-result",
                       str(w_changed), expect=2)
    rc, out = demo.run("ledger", "--ledger", str(priv / "ledger.db"))
    row = json.loads(out.splitlines()[0])
    demo.check(row["spent"] == "1" and row["patient_level_spent"] == "2", "1 release charged; patient-level ε = 2 × 1")

    demo.section("10. Crash between reservation and release, then recovery",
                 "A host process that dies after reserving ε but before releasing is simulated by reserving directly "
                 "in the ledger and exiting. `recover` marks the row `FAILED_SPENT`; it stays charged and the "
                 "submission is never released.")
    sid_c = str(uuid.uuid4())
    crash = ("import json,sys\nfrom privatemetrics.host import HostConfig, HostScorer, HOST_PARTICIPANT\n"
             "from privatemetrics.scorer import _predictions_hash\n"
             f"h = HostScorer(HostConfig.from_file({cfg!r}))\n"
             f"p = json.load(open({str(work / 'worker-c.json')!r}))['predictions']\n"
             f"r = h.ledger.reserve(binding=h.scorer.binding, submission_id={sid_c!r}, participant_id=HOST_PARTICIPANT,"
             " predictions_hash=_predictions_hash(p), charge=h.scorer.charge, certified=h.scorer.certified,"
             " mode='production')\nprint(r.kind)\nimport os; os._exit(0)  # crash: never releases\n")
    w_c = worker(model_predictions(labels, 3), "worker-c")
    r = subprocess.run([sys.executable, "-c", crash], capture_output=True, text=True, cwd=work)
    demo.log += ["```console", "$ python -c '<reserve, then os._exit before release>'", r.stdout.strip(), "```", ""]
    rc, out = demo.run("recover", "--ledger", str(priv / "ledger.db"), "--older-than", "0")
    demo.check(json.loads(out)["recovered"] == 1, "one stale reservation recovered")
    rc, out = demo.run("host-score", "--config", cfg, "--dispatch", str(dispatch(sid_c)), "--worker-result", str(w_c),
                       expect=2)
    demo.check("metrics" not in json.loads(out), "a crashed submission is never released afterwards")

    demo.section("11. Budget exhaustion",
                 f"Cap {CAP}, ε {EPS} per release: two releases are charged so far (one released, one crashed). The "
                 "next new submission takes the last unit; after that, preflight refuses before any model is run.")
    sid_d, sid_e = str(uuid.uuid4()), str(uuid.uuid4())
    w_d = worker(model_predictions(labels, 4), "worker-d")
    demo.run("host-preflight", "--config", cfg, "--dispatch", str(dispatch(sid_d)))
    rc, out = demo.run("host-score", "--config", cfg, "--dispatch", str(dispatch(sid_d)), "--worker-result", str(w_d))
    rc, out = demo.run("host-preflight", "--config", cfg, "--dispatch", str(dispatch(sid_e)), expect=3)
    demo.check(json.loads(out)["failure"]["detail"] == "privacy budget for this task is exhausted",
               "preflight refuses once the scope cannot pay")
    rc, out = demo.run("host-score", "--config", cfg, "--dispatch", str(dispatch(sid_e)), "--worker-result", str(w_d),
                       expect=2)
    rc, out = demo.run("ledger", "--ledger", str(priv / "ledger.db"))
    row = json.loads(out.splitlines()[0])
    demo.check(row["spent"] == CAP and row["releases"] == 3, "spend equals the cap; never above it")
    with sqlite3.connect(priv / "ledger.db") as c:
        statuses = sorted(s for (s,) in c.execute("SELECT status FROM releases"))
    demo.log.append(f"* Ledger rows by status: {statuses}")

    demo.section("12. Output against the upstream contract",
                 "Each host body is validated against the upstream worker's own Pydantic `SealedRunResult` when the "
                 "upstream checkout is present.")
    up = ROOT.parent / "upstream" / "apps" / "worker-eval" / "src"
    if up.exists():
        sys.path.insert(0, str(up))
        from worker_eval.contract import SealedRunResult
        SealedRunResult.model_validate(first)
        demo.check(True, "metrics body validates against upstream `SealedRunResult`")
    else:
        demo.log.append("* UNVERIFIED: upstream checkout not found; contract validation not executed")

    demo.section("Reference task (public data) at its declared ε",
                 "The public reference labels and the `strong` synthetic prediction fixture, scored host-side with "
                 "`fixtures/reference/host.json` (ε = 8, group factor 2, unverified). The labels are public: this "
                 "shows the mechanism on the reference task, not protection.")
    ref_cfg = json.loads((ROOT / "fixtures" / "reference" / "host.json").read_text())
    ref_cfg.update(labels_csv=str(ROOT / "fixtures" / "reference" / "labels.csv"), ledger="reference-ledger.db")
    ref_dir = work / "reference"
    ref_dir.mkdir()
    (ref_dir / "host.json").write_text(json.dumps(ref_cfg))
    demo.run("host-register", "--config", str(ref_dir / "host.json"), "--epsilon-total", "80")
    preds = json.loads((ROOT / "fixtures" / "reference" / "predictions" / "strong.json").read_text())["predictions"]
    wr = worker(preds, "worker-reference")
    sid_r = str(uuid.uuid4())
    disp = {"submissionId": sid_r, "taskSlug": "idrid-dr-grading", "routeVersion": route}
    (work / "dispatch-reference.json").write_text(json.dumps(disp))
    rc, out = demo.run("host-score", "--config", str(ref_dir / "host.json"), "--dispatch",
                       str(work / "dispatch-reference.json"), "--worker-result", str(wr))
    demo.log.append("* Plaintext baseline for this fixture (upstream `scoring.ts`): qwk 0.9531, accuracy 0.8000, "
                    "referableSensitivity 0.9444, referableSpecificity 0.9167, coverage 1.0 "
                    "(`docs/REFERENCE_TASK_FIDELITY_REPORT.md` §2).")
    demo.log.append("* At ε = 8 the noise scale is 0.25, so a single release often equals the plaintext values "
                    "exactly (the share is measured in the fidelity report, §4). That is the cost of choosing a "
                    "large ε for usable numbers on 30 items.")

    header = ["# Host-side demonstration transcript", "",
              f"**Status: DRAFT — NOT YET APPROVED OR INTEGRATED.** Route version `{route}`.", "",
              "Generated by `scripts/host_demo.py`; do not edit by hand. Every command below was executed in a fresh "
              "process and its real output captured. Metric values come from fresh production noise and change on "
              "every run. Private host data is synthetic and generated at run time. This is a local demonstration, "
              "not a deployment on the GI-AI4H platform."]
    result = "ALL CHECKS PASSED" if not demo.failures else "FAILURES: " + "; ".join(demo.failures)
    body = "\n".join([*header, "", f"**Result: {result}**", *demo.log]) + "\n"
    if not a.no_transcript:
        (ROOT / "docs" / "HOST_DEMO_TRANSCRIPT.md").write_text(body)
    print(result)
    return 0 if not demo.failures else 1


if __name__ == "__main__":
    sys.exit(main())
