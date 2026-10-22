# Security-fix summary: route `0.1.0-draft` → … → `0.2.0-draft` → `0.2.1-draft` → `0.2.1`

**Status: DRAFT — NOT YET APPROVED OR INTEGRATED.** This is an internal audit by the contributor (with AI assistance), not an independent audit.

D1–D7 were reproduced against `0.1.0-draft` and fixed in `0.1.1-draft`. D8 was reproduced against `0.1.1-draft` and fixed in `0.1.2-draft`. D9 was found and reproduced during the development of `0.2.0-draft` (host-side scoring) and fixed before that version was packaged. Each defect was reproduced before it was fixed. Each fix has regression tests and a mutation in `scripts/mutation_check.py` that re-introduces the defect and must make a test fail. Test results: `SECURITY_TEST_REPORT.md` (generated).

| ID | Defect in 0.1.0-draft | Reproduction (observed on 0.1.0-draft) | Fix in 0.1.1-draft | Regression tests | Remaining limitation |
|---|---|---|---|---|---|
| D1 | Ledger stored ε as `round(eps * 1e6)`, which can round **down** | ε = 2.5e-6 was charged 2e-6; 5 releases fit under a 1e-5 cap while the true spend was 1.25e-5 (25% over the cap) | `budget.py`: ε are exact rationals on a 1e-6 grid; off-grid, NaN, ±inf, ≤ 0, > 1000 and boolean values rejected, never rounded; integer-unit arithmetic | `tests/test_budget.py` (grid rejection, invalid values, exact units, cumulative releases near the cap, exact fill, smallest quantum) | Grid resolution 1e-6; finer ε is rejected rather than supported |
| D1b | The scorer accepted OpenDP's certified ε up to 1e-9 above the declared ε; the textbook scale `2/ε` over-certifies for some ε | Certified − declared = 4.4e-16 at ε = 3.7; certified 1.0000000000000002e-6 at ε = 1e-6 | `calibrate_scale`: raise the float scale by ULPs until OpenDP's map, read exactly, is ≤ the declared ε; the ledger refuses any charge below the certified cost; the certified ε is stored per release | `test_D1b_*`, `test_D1_ledger_refuses_to_charge_below_certified`, `test_ledger_rows_record_certified_cost_not_above_charge` | Relies on OpenDP's privacy map being correct |
| D2 | Budget key = content hash of task + labels | Changing one label created a fresh budget (4 releases where the cap allowed 2); the same labels under another task name gave 2 more | Operator-assigned privacy **scope** owns the cap; dataset → scope and task → dataset bindings immutable; versions immutable; identical content or overlapping item IDs refused across scopes; cap changes only via audited `change_cap` | `tests/test_identity.py` (identical labels, one label changed, renamed version, other task name, duplicate registration, copies, partial overlap, shared scope, disjoint scopes, Sybil participants, concurrency, reset attempts, cap audit, mismatched labels, old schema) | Disjointness checked by item ID only; patients across datasets and renamed items are not detected; the operator is trusted |
| D3 | A replay returned the stored release to **any** caller presenting the submission ID | Mallory reusing Alice's submission ID received Alice's metrics | Replay requires same participant, task, version, scope, predictions hash and ε; otherwise refused with no charge | `test_D3_other_participant_cannot_fetch_stored_release`, `test_S2b_*` | Participant identity comes from the platform; this component cannot authenticate callers |
| D4 | Replay under a changed ε configuration was silently served | Re-delivery after ε changed returned the cached release | Refused (ε is part of the replay identity) | `test_D4_replay_under_changed_epsilon_is_refused` | — |
| D5 | Crash recovery: stale `RESERVED` rows had no operator path, and an error in `fail()` could escape the scorer | Code review | `recover_stale` / `privatemetrics recover` (rows stay charged, never re-released); scorer catches ledger errors at every step and fails closed | `test_crashed_reservation_stays_charged_and_is_recoverable`, `test_release_persist_failure_returns_nothing`, S15 | — |
| D6 | Integration assumption: 0.1.0 assumed the host-resident `metrics` path; the organizer guidance relayed by the contributor says the platform holds labels and scores. The upstream platform scores at **two** call sites (sealed runs and Mode 1 uploads) | Code inspection of `submission-result.service.ts` and `evaluation.service.ts` | `ARCHITECTURE.md` §1, §4 now targets the trusted scoring step with options P1 (Python service), P2 (TypeScript port), P3 (host-resident); `to_evaluation_scores` adapter added; Mode 1 bypass declared | `test_evaluation_scores_for_platform_side_scoring` | **Not integrated**; Mode 1 bypass remains until the platform routes both paths through the budget **[DECISION G1]** |
| D7 | Documents stated accounting "per label set" via content hash, and claimed nothing about the i32 domain or calibration | Review | Threat model, disclosure profile, envelope, architecture, traceability and README rewritten for the scope model, exact accounting and OpenDP domain | Consistency review | — |

## D8: simulation-to-production replay (fixed in `0.1.2-draft`)

| Field | Detail |
|---|---|
| Defect in `0.1.1-draft` | Ledger rows did not record whether a release came from a simulation run (seeded, reproducible noise) or production. Simulation and production could use the same ledger file. A production scorer started later on that file served the stored simulation release as a production **replay** of the same submission ID, and the adapter accepted it because the outcome only carried the scorer's own `simulation` flag. Simulation releases were also charged to the production scope's budget. |
| Reproduction (observed on `0.1.1-draft`, this session) | Simulation scorer (seed fixed) released `sub-1` on ledger file L; process objects discarded; production scorer (`simulation=False`) on L, same submission ID, participant and predictions. Observed: `production replay: True`, `simulation flag: False`, `same values: True`; `to_sealed_run_result` and `to_evaluation_scores` both produced a publishable body (`True True`); simulation spend counted in the production scope: `1` (ε). |
| Fix in `0.1.2-draft` | **Ledger file:** immutable mode stored in `meta` at creation; opening in the other mode, or a file with no mode, is refused; schema version 3, and v2/v1 files are refused. **Rows:** new `mode` column (`CHECK` constraint); `reserve(..., mode=)` is required and must equal the ledger's mode; a stored row of the other mode is a conflict, never a replay. **Scorer:** refuses a ledger of the other mode; replay path re-checks the row's mode. **Outcome/adapter:** `Outcome.mode` (derived from, and required to agree with, `simulation`); both adapter functions emit only `production` outcomes. |
| Regression tests | `tests/test_modes.py`: file mode recorded and defaulting to production; invalid mode; simulation file not reopenable as production after restart and the reverse; file without stored mode refused; v2 file refused; scorer/ledger mode pairing both ways; `reserve` refuses the other mode; **cross-mode replay after restart releases nothing** (simulation row planted in a production file under the same submission ID); cross-mode row is a ledger-level conflict; production row not served in simulation; same submission ID in both modes stays independent; simulation spend never touches the production scope; simulation replay stays simulation after restart and is still refused by the adapter; outcome provenance validation; adapter refuses real, hand-built and tampered simulation outcomes; production outcomes still publish |
| Mutation check | Five D8 mutants: ledger serves rows across modes; ledger and scorer cross-mode checks both removed; ledger file mode not enforced; scorer accepts other-mode ledger; adapter trusts the simulation flag only. Results: `SECURITY_TEST_REPORT.md` |
| API change | `Ledger.reserve` now takes a required `mode` keyword. `Ledger(path)` still defaults to production; simulation callers pass `mode="simulation"`. The CLI is production-only and unchanged. |
| Remaining limitation | The stored mode relies on ledger-file integrity (A7): a party with write access to the file can rewrite it. That party is the trusted operator. Ledger files created by `0.1.1-draft` must be recreated; they are refused, not migrated, because their rows cannot be classified. |

Also added in `0.1.1-draft`: ruff and mypy configuration and CI steps (both pass), schema versioning that refuses to reuse a v1 ledger file silently, and mechanism tests for empty matched sets, single-column predictions and undefined denominators.

## `0.2.0-draft`: host-side architecture (organizer feedback)

Not a defect fix: the organizer feedback (top of `THREAT_MODEL.md`) moved scoring of clinical tasks to the data host. New in this version: `privatemetrics.host` (`HostConfig`, `HostScorer`, registration), CLI `host-register` / `host-preflight` / `host-score` / `version`, `python -m privatemetrics`, a declared and checked patient **group factor** (ledger schema 4; schema-3 files are refused, not migrated), patient-level spend reporting, and the real-Docker integration in `integration/sealed_run/`. Every earlier control (D1–D8) is unchanged and still tested; the earlier mutants still fail their tests.

New host-side controls, each with tests and a mutant in `scripts/mutation_check.py`:

| ID | Control | Failure it prevents | Tests | Mutant |
|---|---|---|---|---|
| H1 | A worker result that already carries `metrics` is refused, never forwarded | A worker configured to score in plaintext would publish exact label-derived values | H4c | "H1 worker metrics forwarded" |
| H2 | Exact result-schema gate on every outgoing body | Any bug that adds `predictions`, labels, extra fields or out-of-range values to a result | H4b, H4g | "H2 result schema gate removed" |
| H3 | Dispatch must name this task and this route version (in `0.2.0-draft` an absent version was still accepted; required since D10) | Scoring a run under declarations that do not apply to it | H5 | "H3 route version not checked" |
| H4 | Preflight refuses a dispatch the scope cannot pay for | Running a participant's model on host data for a result that will be refused | H6b | "H4 preflight ignores the budget" |
| H5 | Group factor checked against a patient map; immutable per version | An understated factor understates patient-level ε | H7, H7b | "H5 understated group factor accepted" |

## D9: one submission, two draws through a non-canonical submission ID (fixed in `0.2.0-draft`)

| Field | Detail |
|---|---|
| Defect (first version of `HostScorer.check_dispatch`, never packaged) | The check accepted any textual form of a valid UUID (`str(UUID(sid)) == sid.lower()`), but the ledger keys releases on the string as given. The same submission presented once in lower case and once in upper case was two different ledger identities. |
| Reproduction (observed during development) | Registered a synthetic host task (cap 5, ε 1), called `finish` for submission `u` and then for `u.upper()` with identical predictions: two different metric sets were returned (accuracy 0.9333 and 0.8778 in the observed run) and the scope's spend was 2. A redelivery that should have been a free replay bought a second, independent noise draw. |
| Reachability | Only when a dispatch for an already-scored submission arrives with a differently cased ID, for example a replayed or altered message. The platform itself uses one form per submission. |
| Fix | Only the canonical lower-case form is accepted (`str(UUID(sid)) == sid`); anything else is refused before scoring, with no charge. |
| Regression tests | `test_H5_dispatch_for_another_task_route_or_bad_id_is_refused` (upper-case case) |
| Mutation check | "D9 non-canonical submission UUID accepted" |

## Other corrections in `0.2.0-draft`

* `ARCHITECTURE.md` (0.1.x) claimed that projection onto column totals lowered accuracy RMSE compared with clipping. On the real reference labels with the `strong` fixture, clipping alone gave **lower** accuracy RMSE and **higher** QWK RMSE. The claim is corrected and the choice is an open item (G18).
* The 0.1.x documents described a platform-side scorer and a Mode 1 bypass for protected tasks; both are obsolete for host tasks and removed.
* CLI configuration and registration errors are reported as `{"error", "detail"}` on stderr with exit code 1 instead of a Python traceback.

## `0.2.1-draft`: hardening after a source audit

Each item was reproduced on `0.2.0-draft` (commit `c3a2f29`) with a script before it was fixed; each fix has regression tests and a mutant that makes them fail.

| ID | Defect in `0.2.0-draft` | Reproduction (observed) | Fix | Tests | Mutant |
|---|---|---|---|---|---|
| D10 | `routeVersion` was optional: an absent or null value was accepted, so a result could be produced without naming the declarations it was made under | A dispatch with `routeVersion` removed, and one with `null`, both returned metrics | Required: a string of 1–64 characters equal to the route version, checked in preflight and before scoring | H5b (14 negative cases), H5c (positive) | "D10 absent route version accepted"; "H3 route version not checked" |
| D11 | A patient map listing an item twice silently kept the last row | A map with an extra row re-assigning one item to another patient was accepted; such rows can lower the observed items-per-patient and so understate the group factor | Duplicate item rows are refused | H7d | "D11 duplicate patient-map rows accepted" |
| D12 | Preflight let a dispatch proceed for a submission that could never be released (in progress, failed or crashed), so the model ran on host data for nothing | A crashed (recovered `FAILED_SPENT`) submission passed preflight | Preflight proceeds only for unknown submissions the scope can pay for, or already released ones (free replay) | H6d | "D12 preflight runs models for unreleasable submissions" |
| D13 | New ledger files were world-readable (`0644`) under a default umask | Observed mode `0o644` on a fresh ledger | New ledger files are created `0600`; SQLite gives its `-wal`/`-shm` files the same mode. Hardening: the ledger holds released noisy metrics and predictions hashes, not labels | H8f | "D13 world-readable ledger file" |

Also in `0.2.1-draft`:

* **Guarantee checks** (`tests/test_guarantees.py`): exact privacy-loss computation on small instances (worst ratio 4.88 vs e² = 7.39 at ε = 2; 1.44 and 1.95 vs e = 2.72 at ε = 1), sensitivity, coverage label-independence, determinism of post-processing, and certified cost equal to OpenDP's map and to the ledger record. `THREAT_MODEL.md` §7.10 separates what is proven, what is tested and what rests on the host.
* **Real-Docker integration extended** to malformed output, non-zero exit, timeout, out-of-memory and output-size cap; every host body validated against the upstream `SealedRunResult` schema (16 of 16 checks passed).
* **Fidelity reproduced:** a fresh 5,000-trial run agreed with the earlier run in all 150 cells within 4 standard errors; it is now the committed run, the earlier one is kept for comparison. Figures added.
* `scripts/verify_all.py` (one-command verification with `docs/VERIFICATION_SUMMARY.md`), `experiments/results/test-results.json` (machine-readable test results).
* The local test registry for the sealed run serves HTTPS with a throwaway certificate (Docker's HTTP fallback for localhost registries failed intermittently).

## Route `0.2.1`: final fixes before organizer review

Within route `0.2.1`, before any registration or review (declarations are frozen only once review begins, spec §3):

| ID | Issue | Evidence | Fix | Tests | Mutant |
|---|---|---|---|---|---|
| C1 | **Unregistrable route-version string.** The platform's route registry accepts only `MAJOR.MINOR.PATCH` (`CreateRouteVersionRequestSchema`, upstream `5785db8`), so `0.2.1-draft`, which the scorer required to match exactly, could never be dispatched by the platform | Upstream schema; the validator rejects `0.2.1-draft` ("expected MAJOR.MINOR.PATCH") | Wire route version `0.2.1`; draft status stays the registry's review status. Strict exact matching unchanged | H5d, R1, R2, R3 | — |
| D14 | **Worker failure detail forwarded to the platform.** Upstream worker details embed participant-influenced values (exit status 0–255, bytes written, run time); the model has seen the host's images | Upstream `failures.py` templates; test with crafted details | Only the code leaves the host; detail replaced by fixed text | H4h, H4e | "D14 worker failure detail forwarded" |

Added without code-path changes:

* `test_H8g_concurrent_redelivery_of_one_submission_charges_once` (8 threads, one submission: one charge, one release).
* `test_H5e_current_upstream_dispatch_shape_is_refused_proposed_shape_is_accepted` (today's upstream message shape refused; the proposed shape, validated with the upstream worker's model, accepted).
* `registry/` declarations in the platform's registry format, validated against upstream's Zod schemas by `registry/validate_declarations.py` (tests R1–R3; R3 checks that the declared fidelity gap equals the committed measurement).
* `examples/host-config/` (synthetic data; every value the host's decision).
* CI: a locked-dependency job (Python 3.13, `requirements-lock.txt`) and a version-range job (Python 3.11); skipped tests listed (`-rs`); a skipped real-Docker or registry-schema test fails CI (`PM_REQUIRE_SEALED_RUN`, `PM_REQUIRE_REGISTRY_CHECK`); OpenDP import check; registry validation step.
* Fidelity: a third independent 5,000-trial run, now the committed one; agreed with the previous run in 150 of 150 cells (largest z = 2.54).
