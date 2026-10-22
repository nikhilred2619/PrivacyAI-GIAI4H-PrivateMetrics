# PrivacyAI-GIAI4H-PrivateMetrics

Host-side, label-differentially-private metric release for the **GI-AI4H Benchmarking Challenge, Phase 1, sealed-execution track**. An independent solution-provider contribution by Nikhil Reddy Donapati (Independent Researcher, PrivacyAI Research Lab).

> **Status: DRAFT — NOT YET APPROVED OR INTEGRATED.** Route version `0.2.1` (draft; not registered or reviewed). Not reviewed, approved or integrated into any host deployment or the GI-AI4H platform. Not endorsed by ITU, WHO, WIPO or the GI-AI4H organizers. Every result in this repository was produced locally by the contributor; none comes from the platform. Changes since `0.1.0`: `docs/SECURITY_FIX_SUMMARY.md`.

## What it does

For a clinical task, the **data host** holds the reference labels and runs the participant's model in its sealed environment. This component runs on the host, next to the reference sealed-execution worker, and turns the model's predictions into the only thing that leaves the host:

1. it builds the confusion table from the predictions and the host's labels (which never leave the host);
2. it adds discrete-Laplace noise (OpenDP) to the label-dependent cells, projects each prediction column back onto its known total, and derives the five GRADING metrics (QWK, accuracy, referable sensitivity and specificity, coverage);
3. it charges every release, exactly, to the host's **persistent privacy budget**, owned by a host-assigned privacy scope and shared by every dispatch, task and dataset version in that scope; and refuses releases (and model runs) once it is spent;
4. it returns `{routeVersion, durationMs, metrics}` or a classified failure, checked against the exact permitted schema. Never predictions, item IDs or labels.

Guarantee: each release is **ε-label-DP** (replace-one on one item's label, δ = 0), noise scale calibrated so that OpenDP's certified ε ≤ the charged ε; **g·ε at patient level** for a declared group factor g. Releases compose additively per scope and the ledger enforces the cap. Proofs and assumptions: `docs/THREAT_MODEL.md` §7. Images get no formal guarantee from this component.

For the public reference task, the platform scores in plaintext itself; this component is run on the reference task only to measure its fidelity gap.

## Architecture at a glance

```
platform ──dispatch (submission UUID, task, routeVersion, image digest)──▶ data host
  data host:  host-preflight      → refuse early (wrong task/version, no budget, unreleasable submission)
              upstream SealedRunner (unchanged) → sealed container → predictions.json (stays on the host)
              host-score          → validate → reserve ε in host ledger → OpenDP discrete Laplace
                                    → project columns → GRADING metrics → record → exact schema gate
platform ◀──{routeVersion, durationMs, metrics | failure}── data host
```

Privacy mechanism in one paragraph: the confusion table of (true label, prediction) counts changes by at most 2 in L1 norm when one item's label changes, while column totals, the matched count and coverage depend only on predictions. Each label-dependent cell gets discrete-Laplace noise at a scale calibrated against OpenDP's own privacy map, each column is projected back onto its known total, and the five metrics are computed from that table (post-processing, no extra cost). The host ledger charges ε per release to the scope, exactly, and refuses releases beyond the cap. Details: `docs/ARCHITECTURE.md`, `docs/THREAT_MODEL.md` §7.

## Results so far (contributor-run, local, not on the platform)

* **Reference task, real public labels, synthetic prediction fixtures, 5,000 releases per setting** (`docs/REFERENCE_TASK_FIDELITY_REPORT.md`, with figures). Strong fixture, mean gap (SD) of released minus plaintext:

  | ε | accuracy | QWK |
  |---|---|---|
  | 8 (declared for the demo) | −0.005 (0.012) | −0.012 (0.023) |
  | 4 | −0.031 (0.037) | −0.078 (0.059) |
  | 2 | −0.088 (0.072) | −0.185 (0.117) |
  | 1 | −0.207 (0.125) | −0.356 (0.221) |

  ε = 8 is **not** strong per-label privacy: 53% of releases at ε = 8 equal the plaintext exactly. It is a utility choice for a 30-item demo on labels that are public anyway. Three independent 5,000-trial runs agree: each with the previous one in all 150 cells within 4 standard errors.
* **Larger host tasks** (simulation, `docs/EXPERIMENT_REPORT.md`): at ε = 1, accuracy SD ≈ 0.019 at 300 items and 0.006 at 1,000. Not a clinical result.
* **Zero noise = upstream `scoring.ts`** exactly (205 random cases plus the reference fixtures, run with Node).
* **Exact privacy-loss check** on small instances: worst-case output probability ratio below e^ε in every case (`tests/test_guarantees.py`, `docs/THREAT_MODEL.md` §7.10).
* **Real sealed execution**: the unmodified upstream worker ran nine participant images with a real Docker daemon (normal run, label probe, input echo, network attempt, malformed output, crash, timeout, out-of-memory, output flood) and this component produced the host results; 17 of 17 checks passed (`docs/SEALED_RUN_TRANSCRIPT.md`). Local registry, locally built images, toy model.
* **Verification**: `python scripts/verify_all.py` → `docs/VERIFICATION_SUMMARY.md`.

## Quick start

Python 3.11+. Node 22+ is optional (parity test). Docker is optional (real sealed run).

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-lock.txt && pip install -e . --no-deps   # exact versions (Python 3.12+)
# or: pip install -e ".[dev,integration,plots]"                       # compatible ranges (Python 3.11+)
git clone https://github.com/FG-AI4H/oci-platform ../upstream && git -C ../upstream checkout "$(cat docs/UPSTREAM_COMMIT)"

python scripts/host_demo.py          # end-to-end host flow through the CLI -> docs/HOST_DEMO_TRANSCRIPT.md
python -m pytest -q                  # all tests
```

## Host-side CLI

```sh
python -m privatemetrics host-register  --config host.json --epsilon-total 40 --patient-map patients.csv
python -m privatemetrics host-preflight --config host.json --dispatch dispatch.json        # exit 3: do not run
python -m privatemetrics host-score     --config host.json --dispatch dispatch.json --worker-result worker.json
python -m privatemetrics ledger  --ledger ledger.db              # label- and patient-level spend per scope
python -m privatemetrics recover --ledger ledger.db --older-than 3600
python -m privatemetrics version
```

`host.json` (paths relative to the file; all on the host):

```json
{"task_id": "host-dr-grading", "dataset_id": "hospital-dr-2026", "version_id": "2026-10",
 "scope_id": "hospital-dr-scope", "labels_csv": "labels.csv", "id_column": "Image", "label_column": "dr_grade",
 "num_classes": 5, "referable_threshold": 2, "epsilon_per_release": "1", "ledger": "ledger.db", "group_factor": 2}
```

`dispatch.json` is the platform's dispatch message (spec §2.1); `worker.json` is the upstream worker's `SealedRunResult` (`predictions` or `failure`). ε values are decimal strings on a 1e-6 grid; other values are rejected. Exit codes: 0 metrics or proceed, 2 failure body, 3 preflight refusal, 1 configuration error. Full contract: `docs/OPERATIONAL_ENVELOPE.md`.

## Verification

```sh
python scripts/verify_all.py                       # all of the below that can run here -> docs/VERIFICATION_SUMMARY.md
ruff check . && mypy src
python scripts/mutation_check.py                   # each disabled control must make a named test fail
python experiments/reference_fidelity.py           # fidelity gap on the reference task (about 2 minutes; regenerates the report)
python experiments/plot_fidelity.py                # figures from the committed results
python experiments/run_experiments.py              # simulation experiments -> docs/EXPERIMENT_REPORT.md
python integration/sealed_run/run_sealed_host.py   # real Docker sealed run (needs Docker and ../upstream)
python scripts/security_report.py                  # -> docs/SECURITY_TEST_REPORT.md, experiments/results/test-results.json
```

## Documents

| | |
|---|---|
| `docs/THREAT_MODEL.md` | Declaration 1: adversaries, guarantee, proofs, out of scope |
| `docs/DISCLOSURE_PROFILE.md` | Declaration 2: who sees what, trust anchor, parameters |
| `docs/OPERATIONAL_ENVELOPE.md` | Declaration 3: operations, enforced and inherited limits, fidelity gap |
| `docs/REFERENCE_TASK_FIDELITY_REPORT.md` | Generated: fidelity gap on the reference task |
| `docs/ARCHITECTURE.md` | Host-side placement, pipeline, approaches compared |
| `docs/GAP_ANALYSIS.md` | Open organizer and host decisions |
| `docs/SECURITY_FIX_SUMMARY.md` | Defects D1–D13 and host-side controls, with reproductions and tests |
| `docs/VERIFICATION_SUMMARY.md` | Generated: what was run, and its result |
| `docs/SECURITY_TEST_REPORT.md` | Generated: every test and mutant, with its result |
| `docs/HOST_DEMO_TRANSCRIPT.md`, `docs/SEALED_RUN_TRANSCRIPT.md` | Generated: demonstration transcripts |
| `docs/REQUIREMENTS_TRACEABILITY.md` | Conformance traceability and submission-readiness matrix; the integration point to approve |
| `docs/UPSTREAM_REVIEW.md` | Upstream commit reviewed, the code each clause relies on, compatibility notes |
| `registry/*.json` | The declarations in the platform's route-registry format, validated against its Zod schemas (not registered) |
| `examples/host-config/` | Synthetic host configuration example and the decisions a host must make |
| `docs/SUBMISSION_CHECKLIST.md` | What is complete and what is not |
| `docs/EXPERIMENT_REPORT.md` | Generated: simulation experiments (sample size, imbalance, mechanisms) |

## Data

No private or patient-level data is included. `fixtures/reference/labels.csv` holds the **public** IDRiD grades of the upstream reference slice (CC BY 4.0, provenance in `fixtures/reference/README.md`); prediction fixtures are synthetic. Host-like data in tests and the demo is synthetic and generated at run time. `demo-host/` and `*.db` are git-ignored.

## License

BSD 3-Clause (`LICENSE`). Third-party material: `NOTICE.md`.
