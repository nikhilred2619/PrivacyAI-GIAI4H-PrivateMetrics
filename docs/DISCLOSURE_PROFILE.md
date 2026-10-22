# Disclosure profile — PrivacyAI-GIAI4H-PrivateMetrics

**Status: DRAFT — FOR ORGANIZER REVIEW. Not yet approved or integrated.** Route version `0.2.1` (draft; not registered or reviewed). Declaration 2 of 3 (conformance specification §3.2), consistent with `THREAT_MODEL.md` and `OPERATIONAL_ENVELOPE.md` of the same version. Incorporates the organizer feedback summarised at the top of `THREAT_MODEL.md`. **[DECISION]** marks items the organizers or hosts decide (`GAP_ANALYSIS.md`).

## 1. Where things are

For a **clinical host task**, the labels, the evaluation images, the sealed-execution worker, this scorer and its privacy ledger are all on the **data host**. The host is the trusted operator. Only the permitted aggregate result leaves the host.

```
platform ── dispatch (submission UUID, task, route version, image digest, …) ──▶ host
host:  sealed run of the model (inherited containment)  →  predictions (host-internal)
host:  HostScorer: validate → reserve ε in host ledger → noise → metrics → record
host ── {routeVersion, durationMs, metrics}  or  {routeVersion, durationMs, failure} ──▶ platform
```

For the **public reference task**, the platform holds the (public) labels and scores in plaintext itself. This component is used there only to measure the fidelity gap locally (`REFERENCE_TASK_FIDELITY_REPORT.md`).

## 2. What each party observes (host task)

| Party | Before a run | During | After |
|---|---|---|---|
| **Model developer** | Task configuration (K, T), this route's declarations including ε per release and the scope's cap. Whether remaining budget is shown is **[DECISION G6]** | Nothing (inherited sealed execution) | The five GRADING metrics computed from the **noisy** table, or a failure code. Never labels, predictions, item-level outcomes, exact counts, noise, seeds or other participants' results |
| **Platform** (operator and API) | Dispatch metadata it creates | Nothing from inside the host | For each run: the result body above (noisy metrics or a classified failure code with fixed detail text), `routeVersion`, `durationMs` (the worker's run time). Never labels, predictions, item identifiers, exact counts, noise, ledger contents or the host's configuration |
| **Data host** (trusted) | Its labels, images, configuration and ledger | Container stdout/stderr (operator log, inherited); this component's logs: submission ID, outcome or failure code, ε. **No values** | Ledger file (created owner-only, mode `0600`) with rows: release ID, submission, task, dataset, version, scope, predictions hash, ε charged, OpenDP-certified ε, mode, status, timestamps, and **the released noisy metrics** (kept so a redelivery returns identical values). Not exact counts, plaintext metrics, noise or predictions |
| **Solution provider** (this contributor) | Public code | — | Nothing. No telemetry, data, keys, seeds or results |
| **Network observer** | — | — | Host-to-platform traffic protected by the host's TLS (inherited) |

## 3. Released and private values

| Value | Leaves the host? |
|---|---|
| `qwk`, `accuracy`, `referableSensitivity`, `referableSpecificity` | Yes, computed from the noisy, column-projected table; clamped to the valid range; rounded to 4 decimals |
| `coverage` | Yes, **exact**: it depends only on predictions and the item count, so it carries no label information |
| `routeVersion` | Yes, this component's route version |
| `durationMs` | Yes, copied from the worker's result (model run time); the scorer's own time is not added |
| `failure.code`, `failure.detail` | Yes for failures: the worker's code if it is in the closed taxonomy, with a **fixed** detail (`<CODE> reported by the host's sealed-execution worker`); the worker's own operator detail, which can carry participant-influenced values such as exit status or bytes written, stays on the host. Otherwise `INTERNAL_ERROR` with a fixed label-free detail |
| Per-item predictions, item identifiers, labels | **No** (checked by an exact schema gate before anything is returned) |
| Noisy table, exact counts, noise draws, calibrated scale | No |
| Label and item fingerprints, patient map | No (host-internal; the patient map is not even stored) |
| Whether a result was a replay | Not in the body; a replay returns the same numbers, which the platform's result fingerprint treats as a no-op |

## 4. How the per-host global budget works

* **Owner:** a privacy scope, a host-assigned opaque ID with a cap `ε_total`, in the host's own ledger. Every release on any task or dataset version bound to that scope is charged to it, whichever participant's model is being scored.
* **Charge per release:** the configured `ε_release`, a decimal on a 1e-6 grid. It is never rounded; off-grid values are rejected. OpenDP's certified cost for the calibrated noise is ≤ the charge and is recorded with each release.
* **Arithmetic:** integers in 1e-6 units, so sums are exact.
* **Reservation:** before any noise is drawn, in one serialised transaction. A reserved release stays charged even if it later fails or the process crashes.
* **Preflight:** before the model runs, the host checks that the scope can pay; if not, the run is refused and the model is never executed.
* **Per-participant quotas** (upstream: 3 per task per week, 10 per task) remain. They pace submissions; the scope cap is the privacy control.
* **Patient level:** each version carries a declared group factor `g`; the ledger reports `Σ g·ε` per scope (`THREAT_MODEL.md` §7.9).

| Parameter | Value |
|---|---|
| Privacy unit | One item's reference label, replace-one |
| Patient-level guarantee | `g`·ε per release for a declared group factor `g` |
| δ | 0 |
| `ε_release`, host task | **[DECISION G4]**, chosen by the host. Measured guidance: at ε = 1, accuracy SD ≈ 0.019 at 300 items and 0.006 at 1,000 (`EXPERIMENT_REPORT.md` §2) |
| `ε_release`, reference demonstration | 8 (`REFERENCE_TASK_FIDELITY_REPORT.md` §7); public labels, so a utility choice, not a protection claim |
| `ε_total` per scope | **[DECISION G4]**, set by the host at registration |

## 5. Dataset identity and related versions

* The host registers, in order: a **scope** (with its cap), a **dataset** bound to that scope, one or more immutable **versions** (label set plus group factor), and **tasks** bound to the dataset.
* Corrected or changed labels are a new version of the same dataset. They charge the same scope; the budget does not reset.
* Another task name on the same dataset charges the same scope.
* Registering identical labels, or any overlapping item identifier, under a dataset in another scope is refused.
* Disjointness is checked on item identifiers only, not patients (`THREAT_MODEL.md` §5).
* Cap changes go through `change_cap`. Lowering is free; raising needs a named approver and reason; all changes are recorded.

## 6. Redelivery and duplicates

* The same submission UUID (canonical lower-case form), task, version, scope, predictions and ε returns the stored release: identical metrics, no new noise, no new charge. This survives restarts.
* The same submission with any of those different is refused with no charge and no stored values returned. This includes a model that produces different predictions when re-run.
* A submission ID in another textual form (for example upper case) is refused, so one submission cannot obtain two draws.
* A submission whose release crashed after reservation is never released later, even after `recover`; preflight refuses to run its model again.
* Simulation and production never share a ledger file, and a stored row of one mode is never served in the other.

## 7. After budget exhaustion

The release is blocked: no metrics, exact or noisy. Preflight refuses new dispatches before the model runs; a result arriving anyway is refused. On the wire this is `INTERNAL_ERROR` with the fixed detail "privacy budget for this task is exhausted", because the taxonomy is closed; a dedicated `PRIVACY_BUDGET_EXHAUSTED` code is proposed **[DECISION G3]** and available behind `--proposed-codes`.

## 8. Failure responses and metadata

| Situation | Body returned | Charge |
|---|---|---|
| Worker failure (`TIMEOUT`, `OOM_KILLED`, `NETWORK_ATTEMPT_DETECTED`, …) | Same code, fixed detail; the worker's operator detail stays on the host | None |
| Worker failure with a code outside the taxonomy, malformed worker body, or a worker body carrying `metrics` | `INTERNAL_ERROR`, fixed detail | None |
| Malformed, out-of-range, unknown IDs, oversize predictions | `MALFORMED_OUTPUT`, `UNKNOWN_ITEM_IDS`, `OUTPUT_TOO_LARGE` | None |
| Dispatch for another task; route version absent, null, malformed or different; non-canonical submission ID | `INTERNAL_ERROR` "dispatch refused by host scorer" (before the model runs) | None |
| Redelivery of a submission that is in progress or whose release failed or crashed | `INTERNAL_ERROR` "submission cannot be scored again" (preflight: before the model runs) | None |
| Budget exhausted | `INTERNAL_ERROR` (proposed `PRIVACY_BUDGET_EXHAUSTED`) | None |
| Identity conflict, in-progress or previously failed submission | `INTERNAL_ERROR` "submission cannot be scored again" | None (the original row keeps its charge) |
| Ledger unavailable, scoring or persistence failure, schema gate failure | `INTERNAL_ERROR`, no metrics | Charged if a reservation had been made |

Detail strings written by this component are fixed and never contain values. Error outcomes do not depend on label values: labels are validated at registration, not per release.

## 9. Trust anchor and keys

**Contractual and operational, at the data host.** The guarantee rests on the host operating the scorer and ledger as declared, on the inherited containment keeping labels out of the sandbox, and on every scored release for the protected label set passing through the host's ledger (A3). There is no hardware attestation, encrypted computation or independent certification. No keys are introduced; production randomness comes from OpenDP's CSPRNG-backed sampler and is not stored. The platform and the model developer must trust the host's declaration that it runs this route version as configured.

## 10. Reproducibility and verification

* A production release cannot be reproduced; reproducing the noise would remove it.
* Verifiable instead: open code and pinned dependencies; OpenDP's privacy map, recorded per release as `certified_eps` and checked ≤ charge; exact ledger sums per scope (`privatemetrics ledger`); statistical plausibility of each release against the exact table for an auditor with label access; the registry's binding, group-factor and cap-change history.
* The **fidelity gap** on the public reference task is reproducible by anyone: `python experiments/reference_fidelity.py` (fresh production noise; summary statistics reproduce within their confidence intervals) or `--simulation SEED` (bit-for-bit).
* Simulations use a seeded sampler, are labelled `SIMULATION`, use their own ledger files, and can never be returned as host results.
* The host-side flow is reproducible end to end with `python scripts/host_demo.py` (`HOST_DEMO_TRANSCRIPT.md`), and with a real Docker daemon via `integration/sealed_run/run_sealed_host.py` (`SEALED_RUN_TRANSCRIPT.md`).

## 11. Unresolved decisions

Host-side worker integration and route-version naming (G1, G16), failure code (G3), ε values and allocation (G4), result metadata (G6), multi-machine hosts (G7), identifier sensitivity (G8), patient maps and group factors (G9). None of the simulation/production separation or the host-side result gate depends on an organizer decision. See `GAP_ANALYSIS.md`.
