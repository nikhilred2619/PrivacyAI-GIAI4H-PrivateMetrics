# Upstream review — what this route relies on

**Status: DRAFT — NOT YET APPROVED OR INTEGRATED.** Route version `0.2.1` (draft; not registered or reviewed).

Repository: `FG-AI4H/oci-platform`. Re-checked on 22 October 2026 three times (latest 15:05 US Central); no new commits after `5785db8`.

| | Commit |
|---|---|
| Previously reviewed | `1da3bbfe3042a4b6b83016e14acf06639d62a569` |
| Reviewed for this version | `5785db81b4193ff4e3d62738b3a3ea40f0f9aee8` (`docs/UPSTREAM_COMMIT`) |

## Changes between the two commits

`git log 1da3bbf..5785db8` lists two commits: `c1e1ceb` (certifi 2026.10.19 in `apps/worker-eval/uv.lock`, a dependency security update) and `5785db8` (a web-app supervisors' inbox). Neither touches `docs/challenge/`, `apps/worker-eval/src/`, `apps/api/src/modules/evaluation/` or the seed fixture. **No material change for this route.** The conformance specification is still the version of 20 August 2026. `scoring.ts` is byte-identical (SHA-256 `b78ad609…bf47ba1e`).

## Specification clauses and the upstream code behind them

| Clause | What it says | Upstream code checked | How this route uses it |
|---|---|---|---|
| §2.1 Dispatch | Submission ID, task, route and route version, digest-pinned image, timeout, callback, deadline | `worker_eval.contract.SealedRunMessage` (fields `submissionId` (UUID), `taskSlug`, `routeId`, `routeVersion`, `imageRef`, `imageDigest`, `timeoutSec`, `callbackUrl`, `deadline`). **No participant identity.** | `HostScorer.check_dispatch` reads `submissionId`, `taskSlug`, `routeVersion` only |
| §2.2 Model execution | `/input` read-only with `index.json`, `/output` writable and capped, `predictions.json` schema, ≤ 100,000 items | `worker_eval.sandbox`, `worker_eval.inputs`, `worker_eval.predictions.read_predictions` | Reused unmodified; predictions are re-validated host-side against the host's labels |
| §2.3 Results | Predictions (platform scores) **or** metrics (host scored against its own labels); never both; idempotent; closed failure taxonomy | `SealedRunResult` (Pydantic and Zod); API `submission-result.service.ts` stores a `metrics` result "as-is" tagged GRADING; replay identity is a fingerprint over the five metric values (`sealed-run.ts`); a route-version gate applies when the dispatch recorded one | The host returns the `metrics` branch. A redelivery returns identical metrics, so the API treats it as a no-op |
| §2.4 Containment | No network, read-only root, non-root, capabilities dropped, limits, timeout, by digest, no host mounts, capped `/output`, stdout/stderr operator-only | `worker_eval.sandbox.build_container_kwargs`, `execute`, `logging_setup.log_participant_output` | Inherited. Exercised with a real Docker daemon in `integration/sealed_run/` (`SEALED_RUN_TRANSCRIPT.md`) |
| §3 Declarations | Threat model, disclosure profile, operational envelope; caps enforced | — | `THREAT_MODEL.md`, `DISCLOSURE_PROFILE.md`, `OPERATIONAL_ENVELOPE.md` |
| §4 Reference task | Public DR-grading slice, published plaintext baseline, measured fidelity gap | Seed task `idrid-dr-grading` (`demo.sql`: K = 5, T = 2, 30 ground-truth grades); fixture `test-labels.hidden.csv`; `scoring.ts`. **No baseline predictions are published** | `REFERENCE_TASK_FIDELITY_REPORT.md` with synthetic prediction fixtures, disclosed as such |
| §5 GRADING | QWK, accuracy, referable sensitivity/specificity, coverage | `scoring.ts` | Same formulas; zero-noise parity tested |
| §6 Review | Technical review and adversarial testing; failed reviews published | — | `SECURITY_TEST_REPORT.md`, mutation check |
| §9 Submission | Declarations plus a reference to a runnable implementation | — | `SUBMISSION_CHECKLIST.md` |

Nothing in this repository modifies the upstream platform.

## Compatibility notes for this route version

* **`routeVersion` in dispatches (confirmed in code).** `apps/api/src/modules/evaluation/evaluation.service.ts` creates container submissions with `routeId: null, routeVersion: null` and publishes a `SealedRunMessage` without either key; `SealedRunMessageSchema` (`packages/shared-types`) marks both optional "until WP5"; the worker model (`worker_eval.contract.SealedRunMessage`) accepts their absence. Route `0.2.1` requires the version, so this host scorer refuses every current dispatch until the platform sends it (G16; `test_H5e_current_upstream_dispatch_shape_is_refused_proposed_shape_is_accepted`).
* **Route registry exists.** `route-registry.service.ts` with `CreateEvaluationRouteRequestSchema` and `CreateRouteVersionRequestSchema` (`packages/shared-types/src/evaluation-route.ts`). The version must match `^\d+\.\d+\.\d+$`, so the earlier wire string `0.2.1-draft` could never have been registered; the route version is now `0.2.1`. `registry/evaluation-route.json` and `registry/route-version.json` parse with those schemas (zod 4.4.2 as pinned upstream; `registry/validate_declarations.py`). The registry's `OperationalEnvelopeSchema` wants single `maxRuntimeSec`/`maxMemoryMb` values; this route declares the reference worker's dev limits (1800 s, 1024 MB, `infra/cdk/lib/environments.ts`) as values a host's worker enforces, and marks them a host decision.
* **Failure detail.** The API logs `failure.detail` for operators only. Upstream worker details embed participant-influenced numbers (exit status, bytes written, run time); the host scorer replaces the detail with fixed text (D14).
* **Result fingerprint.** The API's replay fingerprint covers the five metric values (`sealed-run.ts`); a redelivered host result carries identical metrics and is a no-op there.
* **`metrics` branch typed as GRADING.** The API tags host-supplied metrics as GRADING; this route supports GRADING only, so the two agree.
