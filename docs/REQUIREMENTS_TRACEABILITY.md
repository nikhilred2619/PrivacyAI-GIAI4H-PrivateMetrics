# Conformance traceability and submission-readiness matrix

**Status: DRAFT — NOT YET APPROVED OR INTEGRATED.** Route version `0.2.1` (draft; not registered or reviewed).

Source: conformance specification (version of 20 August 2026) and participant FAQ at upstream commit `5785db81b4193ff4e3d62738b3a3ea40f0f9aee8` (`UPSTREAM_REVIEW.md`), and the organizer feedback summarised in `THREAT_MODEL.md`.

**Verification status vocabulary.** *Contributor-tested*: implemented here and tested by the contributor (results in `SECURITY_TEST_REPORT.md`, `VERIFICATION_SUMMARY.md`). *Inherited (observed)*: provided by the upstream worker and observed in this repository's real-Docker run. *Inherited*: provided upstream, not exercised here. *Not implemented*. *Organizer-dependent*. **Nothing in this matrix has been independently verified**: no organizer review or adversarial evaluation has taken place (G19).

| Requirement | Implementation | Test or experiment | Declaration | Verification status | Remaining limitation | Organizer decision |
|---|---|---|---|---|---|---|
| §1 Evaluate a model on host data without exposing data to the developer or the model to the host | `host.py`, `scorer.py`; containment by the upstream worker | H-series; `SEALED_RUN_TRANSCRIPT.md` | TM §1, §6 | Contributor-tested; containment inherited (observed) | Not deployed at any host | G1 |
| §2.1 Dispatch (digest, timeout, callback, deadline) | Upstream worker; `HostScorer.check_dispatch` checks task, exact route version, canonical UUID | H5, H5b, H5c; mutants D9, D10, H3 | OE §4, §8 | Contributor-tested (checks); inherited (dispatch handling) | Today's platform dispatches carry a null route version | G16 |
| §2.2 `predictions.json` schema, ranges, unknown IDs, ≤ 100,000 items | `validation.py`; upstream `read_predictions` | S6–S9, S8b, S9b; run 5 | OE §5 | Contributor-tested; inherited (observed) | — | — |
| §2.3 Return metrics **or** predictions, never both, never patient-level data or model internals | `HostScorer.finish`, `check_result_body` | H2–H4g; 12 real-run bodies validated against upstream `SealedRunResult`; mutants H1, H2 | OE §13; DP §3 | Contributor-tested | — | — |
| §2.3 Idempotent results | Ledger replay cache keyed on canonical submission, inputs, ε, mode | S2, S2b, H8, H8b; run 10 | DP §6; OE §8 | Contributor-tested | — | — |
| §2.3 Closed failure taxonomy; text from code | `errors.py`, `host.py` | H4e, H4f, S13; runs 4–9 | OE §15; DP §8 | Contributor-tested; worker codes inherited (observed) | Budget exhaustion maps to `INTERNAL_ERROR` | G3 |
| §2.4 Containment: network, read-only root, non-root, capabilities, limits, timeout, by digest, capped `/output` | Upstream worker | Runs 2–9 (`label-probe`, `input-echo`, `egress`, timeout, OOM, output cap) | TM §6; OE §10, §12, §13 | Inherited (observed for network, timeout, memory, output cap, log isolation); CPU and PID limits inherited, not exercised | Local registry, locally built images | — |
| §2.4 stdout/stderr never returned | Upstream worker | Run 3 (`input-echo`) | TM §6 | Inherited (observed) | — | — |
| §3.1 Threat model with out-of-scope items and the assumption the guarantee rests on | `docs/THREAT_MODEL.md` | — | TM §3, §7.7, §7.10, §9 | Draft | Frozen only once review begins | — |
| §3.2 Disclosure profile: who sees what, trust anchor, keys, reproducibility | `docs/DISCLOSURE_PROFILE.md` | — | DP §2–§10 | Draft | — | G6 |
| §3.3 Operational envelope; caps enforced, not just documented | `docs/OPERATIONAL_ENVELOPE.md` | Every ENFORCED row names its test | OE (all) | Contributor-tested for ENFORCED rows | Rows marked INHERITED, MEASURED or NOT IMPLEMENTED are not this component's caps | — |
| §3.3 Fidelity gap against the plaintext baseline | `experiments/reference_fidelity.py` | `REFERENCE_TASK_FIDELITY_REPORT.md` (5,000 releases per setting, reproduced) | OE §17 | Contributor-measured | Synthetic predictions; no platform run | G4 |
| §4 Reference task: produce a result, measure and record the gap, observe the envelope | Host scorer on the public reference labels | `HOST_DEMO_TRANSCRIPT.md` (reference section), fidelity report, real run | OE §17 | Partly: done locally; not on the platform, no real model | Upstream publishes no baseline predictions | G1 |
| §5 Scoring families | GRADING | `tests/test_parity.py` | OE §3 | Contributor-tested for GRADING | CLASSIFICATION not implemented | G13 |
| §6 Technical review and adversarial testing | Contributor's adversarial tests and mutation check | `SECURITY_TEST_REPORT.md`, `tests/test_guarantees.py` | TM §4 | Contributor-tested only | **Independent review not started** | G19 |
| §7 Score published with route and version | `routeVersion` in every body; dispatch version checked | H4, H5b | OE §13 | Contributor-tested | — | G16 |
| §9 Declarations plus a runnable implementation | This repository | `VERIFICATION_SUMMARY.md` | — | Package ready | **Not published**; link pending | Contributor action |
| Feedback: host-side scoring; host trusted; no Mode 1 concern for host tasks | `host.py`; declarations | H-series | TM §1.1, DP §1 | Contributor-tested | Host wiring not upstream | G1 |
| Feedback: per-host SQLite ledger with declared limits | `ledger.py` | S10, S10b, H8d, H8f | OE §14 | Contributor-tested | Rollback undetectable; single machine | G7, G17 |
| Feedback: group factor for multi-item patients | `labels.py`, `ledger.py`, `host.py` | H7, H7b, H7d | TM §7.9; OE §9 | Contributor-tested | Unverified without a patient map | G9 |
| Feedback: envelope with measured fidelity gap and a repository link | `docs/OPERATIONAL_ENVELOPE.md` | — | OE §17 | Envelope done | Link pending publication | — |
| FAQ: quotas bound label reconstruction per participant | Global ε cap per host scope, not resettable by renaming, versioning or copying | S3, H6, H6c, `tests/test_identity.py` | TM §5; DP §4 | Contributor-tested | Allocation among participants | G4 |
| Formal guarantee (label-DP per release, composition, group privacy) | `mechanism.py`, `budget.py`, `postprocess.py`, `ledger.py` | G1–G5, `tests/test_budget.py` | TM §7, §7.10 | Proof + contributor checks | Rests on A1, A3, A5, A7 (trusted host) | G2 (accepted per feedback) |

TM = `THREAT_MODEL.md`, DP = `DISCLOSURE_PROFILE.md`, OE = `OPERATIONAL_ENVELOPE.md`. Run numbers refer to `SEALED_RUN_TRANSCRIPT.md`.

## Integration point to approve

One decision unblocks deployment: **where the host's worker calls this component** (G1). The proposal (`ARCHITECTURE.md` §4) is two calls around the unmodified upstream `SealedRunner.run`:

1. `HostScorer.preflight(dispatch)` before the model runs; a non-null result is posted instead of running the model;
2. `HostScorer.finish(dispatch, runner_result.to_payload())` after the run; its result is posted instead of the worker's own body.

Alongside it, on the platform side (G16): register the route and version from `registry/*.json` (validated against upstream's Zod schemas), record which route a host task uses, and set `routeId` and `routeVersion` in the submission row and the dispatched `SealedRunMessage` (both fields already exist). Also open: the failure code for budget exhaustion (G3) and the ε and cap policy for host tasks (G4).
