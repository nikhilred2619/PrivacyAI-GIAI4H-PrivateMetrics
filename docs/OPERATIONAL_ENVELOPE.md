# Operational envelope — PrivacyAI-GIAI4H-PrivateMetrics

**Status: DRAFT — FOR ORGANIZER REVIEW. Not yet approved or integrated.** Route version `0.2.1` (draft; not registered or reviewed). Declaration 3 of 3 (conformance specification §3.3), consistent with `THREAT_MODEL.md` and `DISCLOSURE_PROFILE.md` of the same route version.

**How to read the limits.** Every technical limit carries one label:

| Label | Meaning |
|---|---|
| **ENFORCED** | Implemented in this component and covered by a named test (most also by a mutant in `scripts/mutation_check.py`) |
| **INHERITED** | Applied by the reference sealed-execution worker the host runs, not by this component. Where this repository observed it with a real Docker daemon, the run is named (`SEALED_RUN_TRANSCRIPT.md`) |
| **MEASURED** | An observation on the contributor's machine. Not a cap and not a conformance guarantee |
| **NOT IMPLEMENTED** | Not provided; stated so that nobody assumes it |

Items that need organizer or host action are marked **[INTEGRATION]** or **[DECISION]** (`GAP_ANALYSIS.md`).

## 1. Contribution identity and route version

| | |
|---|---|
| Name | PrivacyAI-GIAI4H-PrivateMetrics |
| Track | GI-AI4H Benchmarking Challenge, Phase 1, sealed-execution track |
| Route version | `0.2.1` (`privatemetrics.ROUTE_VERSION`; `python -m privatemetrics version`). MAJOR.MINOR.PATCH as the platform's route registry requires; draft status is the registry's review status, not part of the string |
| Contributor | Nikhil Reddy Donapati, Independent Researcher (PrivacyAI Research Lab) |
| Status | DRAFT — not submitted for review, not approved, not integrated, not independently evaluated, not endorsed by ITU, WHO, WIPO or the GI-AI4H organizers |
| Upstream reviewed | `FG-AI4H/oci-platform` `5785db81b4193ff4e3d62738b3a3ea40f0f9aee8`, re-checked 22 October 2026 (`UPSTREAM_REVIEW.md`) |
| Licence | BSD 3-Clause (`LICENSE`); third-party material in `NOTICE.md` |
| Registry form | `registry/evaluation-route.json`, `registry/route-version.json` (the platform's `CreateEvaluationRouteRequest` / `CreateRouteVersionRequest`; not registered) |

## 2. Permitted operations

| Operation | Who | Command | Output |
|---|---|---|---|
| Register a host task | Host operator | `python -m privatemetrics host-register --config host.json --epsilon-total E [--patient-map P]` | Scope, dataset, version (labels + group factor), task in the host ledger; refusals for rebinding, overwriting, cross-scope copies or overlaps, understated group factor, duplicate patient-map rows |
| Preflight a dispatch | Host worker, before the sealed run | `host-preflight --config host.json --dispatch d.json` | `{"proceed": true}` (exit 0) or a failure body (exit 3) |
| Score a sealed run | Host worker, after the sealed run | `host-score --config host.json --dispatch d.json --worker-result w.json` | `SealedRunResult` body: metrics (exit 0) or failure (exit 2) |
| Ledger status | Host operator | `ledger --ledger L.db` | Per scope: cap, ε spent, releases, patient-level ε spent and bound |
| Crash recovery | Host operator | `recover --ledger L.db --older-than SECONDS` | Stale `RESERVED` rows → `FAILED_SPENT` (still charged, never released). Use a threshold above the longest scoring time: a threshold of 0 also fails in-flight releases (they then return no metrics) |
| Route version | Anyone | `version` | `{"routeVersion": "0.2.1"}` |

No other operation is offered. Library-level `register` / `score` (explicit participant ID) exist for tests and experiments only. Exit code 1 = configuration or registration error, reported as `{"error", "detail"}` on stderr with no label content.

## 3. Supported model format and scoring family

| Item | Value | Label |
|---|---|---|
| Model format | Any image the upstream worker runs (digest-pinned container writing `predictions.json`); nothing about the model changes | INHERITED |
| Scoring family | **GRADING only**: `qwk`, `accuracy`, `referableSensitivity`, `referableSpecificity`, `coverage`, upstream `scoring.ts` formulas and `round4` | ENFORCED (zero-noise parity: `tests/test_parity.py`, 205 random cases; all reference fixtures) |
| K (classes), T (referable threshold) | K in 2–64; T in 1..K−1 | ENFORCED at configuration (`TaskConfig`) |
| CLASSIFICATION and other families | — | NOT IMPLEMENTED (G13) |
| Undefined denominators | 0, as upstream (G12) | ENFORCED (parity) |

## 4. Host-side architecture and required host configuration

```
platform ──dispatch──▶ host worker (upstream worker-eval, unmodified)
                        │ 1. HostScorer.preflight(dispatch)  ── refuse → failure body, model not run
                        │ 2. SealedRunner.run(dispatch)       sealed container, /input read-only, no labels
                        │ 3. predictions.json                 host-internal
                        │ 4. HostScorer.finish(dispatch, worker result)
                        │      validate → reserve ε → OpenDP noise → project → metrics → record → schema gate
platform ◀──{routeVersion, durationMs, metrics | failure}── host outbox
```

Steps 1 and 4 are this component; steps 2–3 and the outbox are the upstream worker. The host wires them together **[INTEGRATION, G1]**; `integration/sealed_run/run_sealed_host.py` shows the wiring around the unmodified upstream `SealedRunner`. The platform's API already accepts the `metrics` branch for host-resident scoring (`UPSTREAM_REVIEW.md`). For the public reference task the platform scores in plaintext itself; this component is not in that path.

Required host configuration (`host.json`, all keys checked; unknown keys refused):

| Key | Meaning |
|---|---|
| `task_id`, `dataset_id`, `version_id`, `scope_id` | Task slug as dispatched; host-assigned identifiers |
| `labels_csv`, `id_column`, `label_column` | Host-local labels file, outside the worker's input root |
| `num_classes`, `referable_threshold` | K and T |
| `epsilon_per_release` | ε per release (decimal on a 1e-6 grid) **[DECISION G4]** |
| `ledger` | Path of the host's SQLite ledger on a local file system |
| `group_factor` | Declared maximum items per patient for this version |

The dispatch must carry `routeVersion` equal to `0.2.1` (string, exact match). Absent, null, malformed or different values are refused before anything runs. The platform currently leaves `routeVersion` null until its route registry exists, so **current platform dispatches would be refused** until the platform sends the agreed value **[DECISION G16]**.

Trust: the data host is trusted (labels, images, worker, scorer, ledger, configuration). Not trusted: model developers and colluding participants; the platform as an observer of results; network observers. Assumptions A1–A7: `THREAT_MODEL.md` §7.7.

## 5. Permitted prediction payload

The upstream `predictions.json`, as handed over by the worker in `SealedRunResult.predictions`: `{ "predictions": { "<itemId>": 0, "<itemId>": 3 } }`.

| Rule | Value | Label | Test |
|---|---|---|---|
| Shape | Object with exactly one key `predictions`, itself an object | ENFORCED | `test_S6_S9_malformed_inputs_refused_without_cost` |
| Labels | Integers in `[0, K−1]`; booleans rejected | ENFORCED | same |
| Item IDs | Strings of 1–200 characters, all in the host's label set (`UNKNOWN_ITEM_IDS` otherwise) | ENFORCED | same |
| Missing items | Allowed; reported as coverage | ENFORCED | `test_S8b_missing_items_allowed_and_reported_as_coverage` |
| Size | ≤ 100,000 predictions (`OUTPUT_TOO_LARGE`) | ENFORCED | `test_S9b_oversize_payload_refused` |
| File handling (`O_NOFOLLOW`, regular file, size cap), invalid JSON | Upstream `read_predictions` | INHERITED; observed: run 5 (`MALFORMED_OUTPUT`) | upstream suite |

A refused payload costs no budget.

## 6. Arithmetic precision

* Confusion counts are exact integers. Noise is added to integers by OpenDP's exact discrete sampler over i32; counts ≤ 100,000 are far inside the i32 range.
* Post-processing (simplex projection, metric formulas) uses IEEE-754 double precision; it is deterministic (G3) and has no privacy cost.
* Released values are clamped (QWK in [−1, 1], others in [0, 1]) and rounded to 4 decimals with upstream `round4`. The schema gate rejects non-finite or out-of-range values (ENFORCED, H4b).
* ε arithmetic is exact: rationals on a 1e-6 grid; ledger sums in integer units (ENFORCED, `tests/test_budget.py`, G4).

## 7. Privacy calibration

| Item | Value | Label | Test |
|---|---|---|---|
| Mechanism | Discrete Laplace, OpenDP 0.16.0 `make_laplace`, L1 sensitivity 2 (replace-one label) | ENFORCED | `test_opendp_privacy_map_equals_declared_epsilon`; G5 (sensitivity); G1 (exact privacy loss on small instances) |
| Calibration | Scale raised by ULPs from 2/ε until OpenDP's map is ≤ ε; scorer refuses to start otherwise; certified ε stored per release | ENFORCED | `test_D1b_*`, G4; mutation check |
| Randomness | OpenDP CSPRNG; seeded simulation sampler refused in production and on production ledgers | ENFORCED | S14, `tests/test_modes.py`, H9 |
| ε per release | Exact decimal on a 1e-6 grid in (0, 1000]; off-grid, NaN, ±inf, ≤ 0, boolean values rejected | ENFORCED | `tests/test_budget.py` |
| Host-task ε | Chosen by the host **[DECISION G4]** | — | — |
| Reference-demonstration ε | 8 (§17) | Declared | — |

## 8. Privacy budget rules

| Rule | Value | Label | Test |
|---|---|---|---|
| Charge per release | The configured ε, ≥ OpenDP's certified cost | ENFORCED | G4, `test_ledger_rows_record_certified_cost_not_above_charge` |
| Global cap per privacy scope | Σ ε ≤ cap, exact, across every dispatch, task and version bound to the scope; cap ≤ 1000 | ENFORCED (transactional, one host) | S1, S3, S10, H6, H6c |
| Accounting identity | Scope owns the cap; dataset → scope and task → dataset immutable; versions immutable; identical or overlapping items across scopes refused | ENFORCED | `tests/test_identity.py` |
| Cap changes | Only via `change_cap`; raising needs approver and reason; recorded | ENFORCED | `test_cap_changes_are_audited_and_lowering_is_free` |
| Dispatch checks | Task must match; `routeVersion` required and exact; submission ID a canonical lower-case UUID | ENFORCED | H5, H5b, H5c; mutants D9, D10, H3 |
| Preflight | No model run for a new submission the scope cannot pay for, or for a submission that is in progress, failed or crashed. Advisory, not a reservation: two concurrent new dispatches can both pass with one ε left; the second is then refused at scoring | ENFORCED | H6b, H6d; run 12 |
| Redelivery | Same submission, inputs and ε → stored release, no charge; anything else refused | ENFORCED | S2, S2b, H8, H8b; run 10 |
| Composition | Basic, adaptive, pure DP: k releases cost Σ εⱼ | Proof (`THREAT_MODEL.md` §7.6) | — |

## 9. Patient-group handling

| Item | Value | Label | Test |
|---|---|---|---|
| Declaration | `group_factor` g ≥ 1 per dataset version, required | ENFORCED | H1c, H7b |
| Validation | With `--patient-map` (`item_id,patient_id`): refused if any item is unmapped, an item is listed twice, or g < observed maximum. The map is read and discarded | ENFORCED | H7, H7d |
| Immutability | Re-registering a version with another g is refused | ENFORCED | H7b |
| Reporting | `ledger`: `patient_level_spent` = Σ g·ε, `patient_level_cap_bound` = max g × cap | ENFORCED | H7b |
| Without a patient map | g is the host's unverified declaration | — | — |
| Reference task | g = 2 declared; **unverified** (patients not identified upstream) | Declared | — |
| Patients across datasets or hosts | Not tracked | NOT IMPLEMENTED | — |

## 10. Model runtime limits

| Limit | Value | Label |
|---|---|---|
| Wall-clock timeout | min(dispatch `timeoutSec`, host `OCI_EVAL_MAX_RUN_SECONDS`), hard kill | INHERITED; observed: run 7 (`TIMEOUT` after about 5.3 s at a 5 s limit) |
| Dispatch deadline | A run past its deadline is abandoned before starting | INHERITED (not exercised here) |
| Network | None | INHERITED; observed: run 4 (`NETWORK_ATTEMPT_DETECTED`) |

## 11. Scorer runtime measurements

| Item | Value | Label |
|---|---|---|
| Full host release, reference task (30 items), including ledger transactions | Median about 9 ms, 95th percentile about 13 ms (`REFERENCE_TASK_FIDELITY_REPORT.md` §6) | MEASURED |
| Production release by item count | Tens of milliseconds or less up to 1,000 items; about one second at 100,000 (`EXPERIMENT_REPORT.md` §5; varies with machine and load) | MEASURED |
| Scorer timeout | — | NOT IMPLEMENTED (runtime is not part of the result) |
| Ledger lock wait | SQLite busy timeout 30 s per transaction; on expiry the scorer fails closed | ENFORCED (fail-closed path: S15, H8e) |

## 12. Memory usage

| Item | Value | Label |
|---|---|---|
| Model container memory (swap off), CPUs, PIDs | Host worker configuration (`OCI_SANDBOX_MEMORY_MIB`, `OCI_SANDBOX_CPUS`, `OCI_SANDBOX_PIDS_LIMIT`) | INHERITED; memory observed: run 8 (`OOM_KILLED` at 512 MiB). CPU and PID limits configured but not exercised |
| Scorer memory | Peak traced allocation about 0.1 MiB per release on the reference task and up to 1,000 items; about 11 MiB at 100,000 items; whole experiment process about 62 MiB RSS | MEASURED |
| Scorer memory cap | — | NOT IMPLEMENTED |
| Concurrent scorer processes | Any number on one host share one ledger safely | ENFORCED by transactions (S10b, H8d) |

## 13. Output limits

| Limit | Value | Label | Test |
|---|---|---|---|
| Model `/output` | Memory-backed, size-capped (`OCI_SANDBOX_OUTPUT_TMPFS_MIB`) | INHERITED; observed: run 9 (`OUTPUT_TOO_LARGE` at a 16 MiB cap) | upstream suite |
| Predictions | ≤ 100,000 entries | ENFORCED | S9b |
| Host result body | Exactly `routeVersion`, `durationMs`, and one of `metrics` (five finite floats in range) or `failure` (closed-taxonomy code, detail ≤ 4,000 characters). Anything else becomes `INTERNAL_ERROR`. A worker body already carrying `metrics` is refused | ENFORCED | H4, H4b, H4c, H4g; all 12 bodies of the real run validated against upstream `SealedRunResult` |

Example body (`POST /v2/submissions/:id/result`):

```json
{"routeVersion": "0.2.1", "durationMs": 61234,
 "metrics": {"qwk": 0.9015, "accuracy": 0.823, "referableSensitivity": 0.9219,
             "referableSpecificity": 0.9631, "coverage": 1.0}}
```

Never `predictions`. `durationMs` is the worker's model run time. A redelivered submission returns identical metrics, which the platform's result fingerprint treats as a no-op.

## 14. SQLite transaction and backup requirements

| Topic | Requirement or behaviour | Label |
|---|---|---|
| Storage | One SQLite file per host ledger, on a **local** file system (WAL mode); network file systems unsupported | Requirement |
| File permissions | New ledger files are created owner-only (`0600`) | ENFORCED (H8f) |
| Schema | Version 4, immutable mode `production`; older files refused | ENFORCED (`tests/test_modes.py`, H7c) |
| Locking | Every mutation in `BEGIN IMMEDIATE`; serialises writers across threads and processes on one host | ENFORCED (S10, S10b, H8d) |
| Ordering | Reserve (charged) → noise → record `RELEASED`; failure after reservation leaves the row charged and returns no metrics | ENFORCED (S11, H8c, H8e) |
| Several machines sharing one ledger | — | NOT IMPLEMENTED (G7) |
| Backups | Use SQLite's online backup, or copy the file and its `-wal` while no scorer runs. **Never restore an older copy over a live ledger**: it refunds spent budget | Requirement on the host |
| Rollback detection | — | NOT IMPLEMENTED. A trusted host that restores or edits its ledger is outside the threat model (A7); no SQLite transaction test can detect it |

## 15. Failure-code behaviour

| Code | Raised by | When | Charge |
|---|---|---|---|
| `TIMEOUT`, `OOM_KILLED`, `NONZERO_EXIT`, `NO_OUTPUT`, `NETWORK_ATTEMPT_DETECTED`, `DIGEST_MISMATCH`, `IMAGE_PULL_FAILED`, `STARTUP_FAILED` | Upstream worker; code passed through with a fixed detail (observed: runs 4, 6–8); the worker's operator detail stays on the host | Model or run failure | None |
| `MALFORMED_OUTPUT`, `UNKNOWN_ITEM_IDS`, `OUTPUT_TOO_LARGE` | Worker (runs 5, 9) or this component | Invalid or oversize output | None |
| `INTERNAL_ERROR` | This component | Dispatch refused; submission not releasable; worker body malformed, carrying `metrics` or an unknown code; budget exhausted; identity conflict; ledger or scoring failure; schema gate failure | Only if a reservation had been made |
| `PRIVACY_BUDGET_EXHAUSTED` | This component, only with `--proposed-codes` | Budget exhausted | None |

Detail strings returned by this component are fixed and value-free (S12, S13, H4f, H4h).

## 16. Known limitations

* Not integrated into any host deployment or the platform (G1); route-version string not agreed (G16). The real-Docker run used a local registry, locally built images and a toy model.
* Single-host ledger; rollback by the host operator is undetectable (§14).
* Group factor unverified without a patient map; cross-dataset and cross-host patients not tracked.
* Utility on 30 items needs a large ε; released values are biased downward (§17).
* Images have no formal guarantee; failure codes are an inherited image channel (G11).
* No scorer timeout or memory cap; CPU and PID limits of the worker not exercised here.
* No independent review yet (G19).

Details: `THREAT_MODEL.md` §8–9, `GAP_ANALYSIS.md`.

## 17. Reference-task fidelity at the declared ε

Measured locally on the public reference labels (`fixtures/reference/labels.csv`) with **synthetic prediction fixtures** (not model inference; no baseline predictions are published upstream), 5,000 releases per setting, production OpenDP noise, route `0.2.1`. Three independent 5,000-trial runs exist with the same mechanism; this one agreed with the previous one in all 150 cells within 4 standard errors (largest z = 2.54), and that one with its predecessor (largest z = 2.94). Full tables, CIs, plots, bias analysis and limitations: `REFERENCE_TASK_FIDELITY_REPORT.md`. Plaintext baseline: upstream `scoring.ts` (exact zero-noise parity).

**Declared reference-demonstration ε = 8**: the smallest tested ε at which at least 80% of `strong`-fixture releases are within ±0.10 for both accuracy and QWK.

| Fixture (plaintext accuracy / QWK) | ε | Accuracy mean gap (SD) | QWK mean gap (SD) | Within ±0.10: accuracy / QWK |
|---|---|---|---|---|
| strong (0.800 / 0.953) | 8 | −0.005 (0.012) | −0.012 (0.023) | 100% / 99% |
| moderate (0.667 / 0.804) | 8 | −0.004 (0.012) | −0.009 (0.024) | 100% / 99% |
| strong-partial (0.833 / 0.958) | 8 | −0.008 (0.016) | −0.015 (0.027) | 100% / 98% |
| strong | 4 | −0.031 (0.037) | −0.078 (0.059) | 96% / 70% |
| strong | 2 | −0.088 (0.072) | −0.185 (0.117) | 57% / 25% |
| strong | 1 | −0.207 (0.125) | −0.356 (0.221) | 19% / 9% |

Coverage is exact at every ε. **ε = 8 is not strong individual-label privacy**: at ε = 8, 53% of releases equal the plaintext values exactly. It is a utility choice for a 30-item demonstration on labels that are public anyway. For host tasks, the simulation in `EXPERIMENT_REPORT.md` §2 (ε = 1: accuracy SD ≈ 0.019 at 300 items, 0.006 at 1,000) suggests a small ε becomes usable at hundreds to thousands of items; this is a simulation, not a clinical result **[DECISION G4]**.

Privacy expenditure: k releases at ε cost k·ε against the scope (label level) and k·g·ε at patient level. At ε = 8, ten releases cost 80 (label level) and 160 with g = 2.

## 18. Reproduction procedure

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-lock.txt && pip install -e . --no-deps    # or: pip install -e ".[dev,integration]"
git clone https://github.com/FG-AI4H/oci-platform ../upstream && git -C ../upstream checkout "$(cat docs/UPSTREAM_COMMIT)"

python scripts/verify_all.py          # everything below, with a summary -> docs/VERIFICATION_SUMMARY.md
python -m pytest -q                   # all tests (the real-Docker test skips, reported, without Docker)
ruff check . && mypy src
python scripts/mutation_check.py      # each disabled control must make a named test fail
python scripts/host_demo.py           # host flow via the CLI -> docs/HOST_DEMO_TRANSCRIPT.md
python experiments/reference_fidelity.py && python experiments/plot_fidelity.py
python integration/sealed_run/run_sealed_host.py   # needs Docker -> docs/SEALED_RUN_TRANSCRIPT.md
python scripts/security_report.py     # -> docs/SECURITY_TEST_REPORT.md, experiments/results/test-results.json
python registry/validate_declarations.py   # registry/*.json against the platform's own Zod schemas (Node, npm)
```

Results in the repository were produced by these commands on the contributor's machine. They are contributor-reported, not results from the GI-AI4H platform or an independent reviewer.
