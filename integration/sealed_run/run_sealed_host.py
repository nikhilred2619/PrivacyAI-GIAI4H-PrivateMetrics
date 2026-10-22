"""Host-side end to end with REAL sealed execution: upstream worker + this scorer.

What runs, unmodified, from the upstream checkout (`../upstream`, commit in
docs/UPSTREAM_COMMIT): `worker_eval.runner.SealedRunner` with a real Docker
client. It pulls each participant image by digest, runs it with the upstream
containment flags (no network, read-only root, non-root, all capabilities
dropped, no new privileges, memory/CPU/PID limits, size-capped tmpfs /output,
hard timeout), reads and validates `predictions.json`, and returns a
`SealedRunResult`. This component's `HostScorer.finish` then turns that into the
body the host would POST: noisy metrics or a classified failure.

What is NOT upstream / NOT production:
  * the registry is integration/sealed_run/minireg.py (local, in-memory),
    because public registries are unreachable here; the worker still pulls by
    digest and verifies RepoDigests against it;
  * the participant images are built locally, FROM scratch over a copy of a
    system Python on the host (or from PM_BASE_IMAGE where a public image can
    be pulled); their "model" is a toy rule over image bytes, not a clinical
    model;
  * the outbox POST to the platform is not made; the body is printed instead;
  * the AWS pieces (SQS pump, secrets, CloudWatch metrics) are not used.

Labels: the public reference fixture (fixtures/reference/labels.csv). They sit in
the host config directory, outside the input root, and are never mounted into
any container. The `label-probe` image searches its whole filesystem and
environment for them and reports what it finds to its own stdout (captured by
the worker for operators only).

Run:  python integration/sealed_run/run_sealed_host.py [--upstream ../upstream]
Writes docs/SEALED_RUN_TRANSCRIPT.md and experiments/results/sealed_run.json.
Exits 0 only if every check holds; 77 if Docker or the upstream checkout is
unavailable (reported as NOT EXECUTED, never as passed).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
NOT_EXECUTED = 77
TASK = "idrid-dr-grading"
EPS = "8"
SLEEPER_TIMEOUT_SEC = 5
CAP = "32"  # four releases

_PRELUDE = '''
import json, os, sys
from pathlib import Path
INPUT = Path(os.environ["OCI_INPUT_DIR"]); OUTPUT = Path(os.environ["OCI_OUTPUT_DIR"])
def items():
    idx = json.loads((INPUT / "index.json").read_text())
    return [(i["id"], INPUT / i["path"]) for i in idx["items"]]
def write(preds):
    (OUTPUT / "predictions.json").write_text(json.dumps({"predictions": preds}))
'''

MODELS = {
    # Toy "model": grade from the mean byte value of the image file. Not clinical.
    "toy-model": _PRELUDE + '''
preds = {}
for item_id, path in items():
    b = path.read_bytes()
    preds[item_id] = (sum(b[::97]) // max(1, len(b[::97]))) % 5
write(preds)
print("toy-model wrote", len(preds), "predictions")
''',
    # Looks everywhere it can for the reference labels and prints what it finds.
    "label-probe": _PRELUDE + '''
found = []
for top in ("/", ):
    for dirpath, dirnames, filenames in os.walk(top):
        if dirpath.startswith(("/proc", "/sys", "/dev")):
            dirnames[:] = []
            continue
        for f in filenames:
            p = os.path.join(dirpath, f)
            if p == "/model.py":  # this probe's own source mentions the column names
                continue
            try:
                with open(p, "rb") as fh:
                    head = fh.read(4096)
            except OSError:
                continue
            if b"dr_grade" in head or b"dme_risk" in head:
                found.append(p)
env_hits = [k for k, v in os.environ.items() if "grade" in v.lower() or "label" in k.lower()]
print("LABEL-PROBE files:", json.dumps(found), "env:", json.dumps(env_hits), flush=True)
write({item_id: 0 for item_id, _ in items()})
''',
    # Prints every input byte it can to stdout: the log channel must not reach the result.
    "input-echo": _PRELUDE + '''
for item_id, path in items():
    print("ECHO", item_id, path.read_bytes()[:64].hex(), flush=True)
write({item_id: 2 for item_id, _ in items()})
''',
    # Writes a predictions file that is not JSON.
    "malformed-output": _PRELUDE + '''
(OUTPUT / "predictions.json").write_text("{not json")
''',
    # Writes valid predictions, then exits non-zero: neither score nor predictions may result.
    "crash": _PRELUDE + '''
write({item_id: 1 for item_id, _ in items()})
sys.exit(3)
''',
    # Never finishes: must be killed at the dispatch timeout.
    "sleeper": _PRELUDE + '''
import time
time.sleep(600)
''',
    # Touches far more memory than the container limit.
    "memory-hog": _PRELUDE + '''
blocks = []
for _ in range(64):
    blocks.append(b"x" * (64 * 1024 * 1024))
write({item_id: 0 for item_id, _ in items()})
''',
    # Writes far more than the /output cap, slowly enough to be caught mid-write.
    "output-flood": _PRELUDE + '''
import time
chunk = bytes(1024 * 1024)
with (OUTPUT / "blob.bin").open("wb") as fh:
    for _ in range(128):
        fh.write(chunk)
        fh.flush()
        time.sleep(0.05)
''',
    # Tries to reach the network.
    "egress": _PRELUDE + '''
import socket
for host, port in (("1.1.1.1", 443), ("example.com", 80)):
    try:
        socket.create_connection((host, port), timeout=3).close()
        print("EGRESS SUCCEEDED", host, flush=True)
    except OSError as err:
        print("egress attempt failed:", type(err).__name__, err, flush=True)
write({item_id: 0 for item_id, _ in items()})
''',
}


def docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def build_base(tag: str) -> str:
    """The participant base image. PM_BASE_IMAGE (for example `python:3.13-alpine`)
    is used when set and pullable. Otherwise a FROM-scratch image is assembled
    from a system Python on this host (its binary, the shared libraries `ldd`
    lists, and its standard library), so no registry access is needed."""
    base = os.environ.get("PM_BASE_IMAGE")
    if base:
        subprocess.run(["docker", "pull", base], check=True, capture_output=True)
        return base
    py = next((Path(f"/usr/bin/python3.{m}") for m in (11, 12, 13, 14) if Path(f"/usr/bin/python3.{m}").exists()), None)
    if py is None:
        raise RuntimeError("no system /usr/bin/python3.x found to build a scratch base image; set PM_BASE_IMAGE")
    stdlib = Path("/usr/lib") / py.name
    libs = []
    for line in subprocess.run(["ldd", str(py)], capture_output=True, text=True, check=True).stdout.splitlines():
        parts = line.split()
        path = parts[2] if "=>" in parts and len(parts) > 2 else parts[0]
        if path.startswith("/"):
            libs.append(path)
    skip = {"test", "__pycache__", "site-packages", "dist-packages", "idlelib", "tkinter", "ensurepip", "lib2to3"}
    with tempfile.TemporaryDirectory() as d:
        ctx = Path(d)
        with tarfile.open(ctx / "rootfs.tar", mode="w") as tar:
            tar.add(py, arcname="usr/bin/python")
            for lib in libs:
                tar.add(os.path.realpath(lib), arcname=lib.lstrip("/"))
            tar.add(stdlib, arcname=str(stdlib).lstrip("/"),
                    filter=lambda ti: None if set(Path(ti.name).parts) & skip else ti)
        (ctx / "Dockerfile").write_text("FROM scratch\nADD rootfs.tar /\n"
                                        "ENV PYTHONHOME=/usr PYTHONDONTWRITEBYTECODE=1 PATH=/usr/bin\n")
        subprocess.run(["docker", "build", "-q", "-t", tag, str(ctx)], check=True, capture_output=True)
    return tag


def build_and_push(base: str, registry: str, name: str, code: str) -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as d:
        ctx = Path(d)
        (ctx / "model.py").write_text(code)
        (ctx / "Dockerfile").write_text(f'FROM {base}\nCOPY model.py /model.py\nCMD ["python", "/model.py"]\n')
        repo = f"{registry}/participant/{name}"
        subprocess.run(["docker", "build", "-q", "-t", f"{repo}:v1", str(ctx)], check=True, capture_output=True)
    push = subprocess.run(["docker", "push", f"{repo}:v1"], capture_output=True, text=True)
    if push.returncode != 0:
        # Keep registry diagnostics in the local integration-test output; the
        # participant and platform result paths never contain this text.
        raise RuntimeError(f"local integration registry push failed for {name}: "
                           f"{(push.stderr or push.stdout)[-2000:]}")
    out = push.stdout
    digest = next(tok for line in out.splitlines() if "digest:" in line for tok in line.split() if tok.startswith("sha256:"))
    # Remove local copies so the worker must pull by digest from the registry.
    subprocess.run(["docker", "rmi", "-f", f"{repo}:v1"], capture_output=True)
    return repo, digest


class _Metrics:
    def run_duration(self, seconds: float) -> None:
        pass


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", type=Path, default=ROOT.parent / "upstream")
    a = ap.parse_args()
    src = a.upstream / "apps" / "worker-eval" / "src"
    if not src.exists() or not docker_ok():
        print("NOT EXECUTED: needs a reachable Docker daemon and the upstream checkout "
              f"({src}); nothing was run.")
        return NOT_EXECUTED
    sys.path.insert(0, str(src))
    try:
        import docker
        from worker_eval.config import Settings
        from worker_eval.contract import SealedRunMessage
        from worker_eval.runner import SealedRunner
        from worker_eval.sandbox import MEMORY_BACKED_FS, filesystem_type, verify_output_root
    except ImportError as exc:
        print(f"NOT EXECUTED: integration dependencies missing ({exc.name}); install with pip install -e '.[integration]'")
        return NOT_EXECUTED
    from minireg import serve

    from privatemetrics import ROUTE_VERSION
    from privatemetrics.host import HostConfig, HostScorer, register_host_task

    # CI runs a real, short-lived OCI Distribution registry as a job service.
    # The fallback is the local stdlib mini-registry for offline development.
    # This setting affects test infrastructure only, never model containment.
    external_registry = os.environ.get("PM_INTEGRATION_REGISTRY", "").strip()
    if external_registry:
        import re
        if not re.fullmatch(r"(?:localhost|127\.0\.0\.1):[1-9][0-9]{0,4}", external_registry):
            raise RuntimeError("PM_INTEGRATION_REGISTRY must be localhost:<port> or 127.0.0.1:<port>")
        srv = None
        registry = external_registry
    else:
        srv, port = serve(0)
        registry = f"localhost:{port}"
    base = build_base("privatemetrics-int/pybase:local")
    images = {name: build_and_push(base, registry, name, code) for name, code in MODELS.items()}

    work = Path(tempfile.mkdtemp(prefix="pm-sealed-"))
    shm = Path("/dev/shm")
    memory_backed = filesystem_type(shm) in MEMORY_BACKED_FS
    out_root = Path(tempfile.mkdtemp(prefix="pm-runs-", dir=shm if memory_backed else None))
    os.chmod(out_root, 0o777)
    in_root = work / "inputs"
    task_in = in_root / TASK
    (task_in / "images").mkdir(parents=True)
    fixture_dir = a.upstream / "packages" / "database" / "seed" / "fixtures" / "idrid-grading-demo"
    labels_src = ROOT / "fixtures" / "reference" / "labels.csv"
    with labels_src.open(newline="") as fh:
        ids = [r["Image"] for r in csv.DictReader(fh)]
    for i in ids:
        shutil.copy(fixture_dir / f"{i}.jpg", task_in / "images" / f"{i}.jpg")
    (task_in / "index.json").write_text(json.dumps(
        {"numClasses": 5, "items": [{"id": i, "path": f"images/{i}.jpg"} for i in ids]}))
    for p in [in_root, *in_root.rglob("*")]:
        os.chmod(p, 0o755 if p.is_dir() else 0o644)

    host_dir = work / "host-private"  # outside the input root: never mounted
    host_dir.mkdir()
    shutil.copy(labels_src, host_dir / "labels.csv")
    cfg_raw = json.loads((ROOT / "fixtures" / "reference" / "host.json").read_text())
    cfg_raw.update(labels_csv="labels.csv", ledger="ledger.db", epsilon_per_release=EPS)
    (host_dir / "host.json").write_text(json.dumps(cfg_raw))
    cfg = HostConfig.from_file(host_dir / "host.json")
    register_host_task(cfg, epsilon_total=CAP)

    env = {
        "OCI_ENV": "dev", "AWS_REGION": "eu-central-1",
        "OCI_EVAL_QUEUE_URL": "https://sqs.invalid/q", "OCI_EVAL_DLQ_URL": "https://sqs.invalid/dlq",
        "OCI_OUTBOX_BASE_URL": "https://platform.invalid", "OCI_EVAL_OUTBOX_CREDENTIAL_SECRET": "/unused",
        "OCI_EVAL_MAX_RUN_SECONDS": "300", "OCI_EVAL_VISIBILITY_TIMEOUT_SECONDS": "900",
        "OCI_SANDBOX_NETWORK_MODE": "none", "OCI_SANDBOX_MEMORY_MIB": "512", "OCI_SANDBOX_CPUS": "1",
        "OCI_SANDBOX_PIDS_LIMIT": "128", "OCI_SANDBOX_OUTPUT_TMPFS_MIB": "16",
        "OCI_SANDBOX_READONLY_ROOTFS": "true", "OCI_SANDBOX_DROP_ALL_CAPABILITIES": "true",
        "OCI_SANDBOX_NO_NEW_PRIVILEGES": "true", "OCI_SANDBOX_RUN_AS_NON_ROOT": "true",
        "OCI_SANDBOX_REQUIRE_DIGEST": "true", "OCI_SANDBOX_PRUNE_IMAGE_AFTER_RUN": "true",
        "OCI_METRICS_NAMESPACE": "unused", "OCI_RUN_DURATION_METRIC": "unused",
        "OCI_EVAL_INPUT_ROOT": str(in_root), "OCI_EVAL_OUTPUT_ROOT": str(out_root),
        "OCI_SANDBOX_REQUIRE_TMPFS_OUTPUT": "true" if memory_backed else "false",
    }
    settings = Settings.from_env(env)
    verify_output_root(settings)
    weakened = settings.sandbox.weakened_controls()
    runner = SealedRunner(settings, docker.from_env(), _Metrics())
    cap = _Capture()
    logging.getLogger("worker_eval").addHandler(cap)
    logging.getLogger("worker_eval").setLevel(logging.DEBUG)

    host = HostScorer(cfg)
    runs: list[dict[str, Any]] = []
    # (image, is a redelivery of run #1)
    order = [("toy-model", False), ("label-probe", False), ("input-echo", False), ("egress", False),
             ("malformed-output", False), ("crash", False), ("sleeper", False), ("memory-hog", False),
             ("output-flood", False), ("toy-model", True), ("toy-model", False), ("toy-model", False)]
    first_dispatch = None
    for name, redelivery in order:
        repo, digest = images[name]
        sid = first_dispatch["submissionId"] if redelivery and first_dispatch else str(uuid.uuid4())
        disp = {"submissionId": sid, "taskSlug": TASK, "routeVersion": ROUTE_VERSION,
                "imageRef": f"{repo}@{digest}", "imageDigest": digest,
                "timeoutSec": SLEEPER_TIMEOUT_SEC if name == "sleeper" else 120,
                "callbackUrl": f"https://platform.invalid/v2/submissions/{sid}/result",
                "deadline": "2099-01-01T00:00:00Z"}
        first_dispatch = first_dispatch or disp
        pre = host.preflight(disp)
        before = len(cap.records)
        t0 = time.monotonic()
        if pre is None:
            worker_body = runner.run(SealedRunMessage.model_validate(disp)).to_payload()
            body = host.finish(disp, worker_body)
        else:  # the host refuses before the model runs: nothing is pulled or executed
            worker_body = {"not_run": "preflight refused"}
            body = pre
        wall = time.monotonic() - t0
        logs = [r for r in cap.records[before:]]
        stdout = " ".join(str(getattr(r, "stdout", "")) for r in logs if r.name == "worker_eval.participant")
        runs.append({"image": name, "submissionId": sid, "redelivery": redelivery,
                     "preflight": "proceed" if pre is None else pre["failure"]["code"],
                     "worker_result": {k: (f"<{len(v)} predictions, host-internal>" if k == "predictions" else v)
                                       for k, v in worker_body.items()},
                     "host_result": body, "wall_seconds": round(wall, 2),
                     "operator_stdout_excerpt": stdout[:300],
                     "spent_after": str(host.ledger.spent(cfg.scope_id))})

    from worker_eval.contract import SealedRunResult as UpstreamResult
    schema_ok = True
    for run in runs:
        try:
            UpstreamResult.model_validate(run["host_result"])
        except Exception:  # noqa: BLE001 - any validation error fails the check
            schema_ok = False
    label_text = labels_src.read_text()
    probe = next(r for r in runs if r["image"] == "label-probe")
    echo = next(r for r in runs if r["image"] == "input-echo")
    egress = next(r for r in runs if r["image"] == "egress")
    first, redo, fourth, refused = runs[0], runs[-3], runs[-2], runs[-1]
    spent_before_failures = runs[2]["spent_after"]

    def failed(image: str, code: str) -> bool:
        run = next(r for r in runs if r["image"] == image)
        return (run["host_result"].get("failure", {}).get("code") == code
                and run["spent_after"] == spent_before_failures and "metrics" not in run["host_result"])
    checks = {
        "sandbox envelope not weakened": weakened == [],
        "toy model: metrics returned, no predictions in host result":
            "metrics" in first["host_result"] and "predictions" not in first["host_result"],
        "label probe: no labels file visible inside the container": 'files: []' in probe["operator_stdout_excerpt"],
        "input echo: container stdout absent from the host result":
            "ECHO" not in json.dumps(echo["host_result"]) and "ECHO" in echo["operator_stdout_excerpt"],
        "egress: blocked and classified, passed through without charge":
            egress["host_result"].get("failure", {}).get("code") == "NETWORK_ATTEMPT_DETECTED"
            and egress["spent_after"] == spent_before_failures,
        "malformed output: MALFORMED_OUTPUT, no charge": failed("malformed-output", "MALFORMED_OUTPUT"),
        "container exits non-zero: NONZERO_EXIT, no charge": failed("crash", "NONZERO_EXIT"),
        "wall-clock limit: TIMEOUT, no charge": failed("sleeper", "TIMEOUT"),
        "memory limit: OOM_KILLED, no charge": failed("memory-hog", "OOM_KILLED"),
        "output-size cap: OUTPUT_TOO_LARGE, no charge": failed("output-flood", "OUTPUT_TOO_LARGE"),
        "every host result validates against the upstream SealedRunResult schema": schema_ok,
        "worker failure details stay on the host (fixed detail text only)":
            all(r["host_result"]["failure"]["detail"] in
                (f"{r['host_result']['failure']['code']} reported by the host's sealed-execution worker",
                 "privacy budget for this task is exhausted")
                for r in runs if "failure" in r["host_result"]),
        "redelivery of the first submission returns the stored release, free":
            redo["host_result"].get("metrics") == first["host_result"]["metrics"]
            and redo["spent_after"] == egress["spent_after"],
        "fourth scored release fills the cap": "metrics" in fourth["host_result"] and fourth["spent_after"] == CAP,
        "budget exhausted: preflight refuses and the model is never run":
            refused["preflight"] == "INTERNAL_ERROR" and refused["worker_result"] == {"not_run": "preflight refused"}
            and refused["spent_after"] == CAP,
        "no host result contains label rows or item ids":
            all(not any(line in json.dumps(r["host_result"]) for line in label_text.splitlines()[1:])
                and "IDRiD_" not in json.dumps(r["host_result"]) for r in runs),
        "budget charged once per scored release (4 x ε)": runs[-1]["spent_after"] == str(4 * int(EPS)),
    }
    if srv is not None:
        srv.shutdown()
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(out_root, ignore_errors=True)
    result = {"route_version": ROUTE_VERSION, "epsilon_per_release": EPS, "cap": CAP,
              "memory_backed_output": memory_backed, "weakened_controls": weakened, "runs": runs, "checks": checks}
    (ROOT / "experiments" / "results").mkdir(parents=True, exist_ok=True)
    (ROOT / "experiments" / "results" / "sealed_run.json").write_text(json.dumps(result, indent=1) + "\n")
    write_transcript(result)
    for k, v in checks.items():
        print(("PASS " if v else "FAIL ") + k)
    return 0 if all(checks.values()) else 1


def write_transcript(r: dict) -> None:
    L = ["# Sealed-run transcript (host side, real Docker)", "",
         f"**Status: DRAFT — NOT YET APPROVED OR INTEGRATED.** Route version `{r['route_version']}`.", "",
         "Generated by `integration/sealed_run/run_sealed_host.py`. Do not edit by hand. The upstream worker "
         "(`worker_eval.SealedRunner`, unmodified, real Docker daemon) ran each participant image; this "
         "component's `HostScorer` produced the body a host would return. See the script's docstring for what "
         "is local test infrastructure (registry, scratch base image, toy model) rather than production.", "",
         f"ε per release {r['epsilon_per_release']}, scope cap {r['cap']}, `/output` memory-backed: "
         f"{r['memory_backed_output']}, weakened sandbox controls: {r['weakened_controls'] or 'none'}. Worker "
         "sandbox settings for this run: network none, memory 512 MiB (swap off), 1 CPU, 128 PIDs, `/output` cap "
         f"16 MiB, read-only root, non-root, all capabilities dropped; dispatch timeout 120 s ({SLEEPER_TIMEOUT_SEC} s "
         "for `sleeper`). CPU and PID limits were applied but not exercised by any image.", "",
         "| # | Image | Preflight | Worker result (host-internal) | Host result (to platform) | ε spent after |",
         "|---|---|---|---|---|---|"]
    for i, run in enumerate(r["runs"], 1):
        w = run["worker_result"]
        wtxt = w.get("predictions") or w.get("not_run") or json.dumps(w.get("failure"))[:120]
        h = run["host_result"]
        htxt = json.dumps(h.get("metrics") or h.get("failure"))
        tag = " (redelivery of #1)" if run["redelivery"] else ""
        L.append(f"| {i} | `{run['image']}`{tag} | {run['preflight']} | {wtxt} | `{htxt}` | {run['spent_after']} |")
    L += ["", "## Checks", "", "| Check | Result |", "|---|---|"]
    L += [f"| {k} | {'PASS' if v else 'FAIL'} |" for k, v in r["checks"].items()]
    L += ["", "## Operator-only container output (excerpts)", "",
          "Captured by the upstream worker's participant logger; never part of a result body.", ""]
    for run in r["runs"]:
        excerpt = run["operator_stdout_excerpt"].strip().replace("\n", " | ")[:120] or "(not run)"
        L.append(f"* run {r['runs'].index(run) + 1}, `{run['image']}`: `{excerpt}`")
    (ROOT / "docs" / "SEALED_RUN_TRANSCRIPT.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    sys.exit(main())
