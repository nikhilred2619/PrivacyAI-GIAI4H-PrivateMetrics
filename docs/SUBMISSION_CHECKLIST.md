# Submission-readiness checklist

**Status: DRAFT — NOT YET APPROVED OR INTEGRATED.** Route version `0.2.1` (draft; not registered or reviewed). Prepared 22 October 2026. Phase 1 closes 18 December 2026, 23:59 UTC (conformance specification §8).

"Complete" means done and evidenced in this repository. Nothing that depends on the organizers, a host or an independent reviewer is marked complete without their confirmation. **Submission readiness is not conformance approval.** Independent organizer review and adversarial evaluation must still happen (spec §6).

| Item | Status | Evidence / what is missing |
|---|---|---|
| Verified critical defects fixed | **Complete** | D1–D14 and C1 with reproductions or upstream evidence, fixes, regression tests and mutants (`SECURITY_FIX_SUMMARY.md`) |
| Full test suite run in a supported environment | **Complete (contributor-run)** | `VERIFICATION_SUMMARY.md`; clean installs below |
| Security mutation checks | **Complete** | 27 of 27 mutants caught (`experiments/results/mutation-check.json`) |
| Certified costs match ledger accounting | **Complete** | G4 (`tests/test_guarantees.py`): recorded certified ε = OpenDP's map, ≤ charge, exact sums |
| Experimental claims match reproducible data | **Complete** | Fidelity report generated from `experiments/results/reference_fidelity.json`; an independent second run agreed in 150 of 150 cells; declarations quote that file |
| Host-side integration demonstrated in the available environment | **Complete locally** | `HOST_DEMO_TRANSCRIPT.md` (CLI); `SEALED_RUN_TRANSCRIPT.md` (unmodified upstream worker, real Docker, 9 images, 17 of 17 checks). Not a deployment at a real data host |
| Three declarations consistent | **Complete (draft)** | `THREAT_MODEL.md`, `DISCLOSURE_PROFILE.md`, `OPERATIONAL_ENVELOPE.md` for route `0.2.1`; frozen only once review begins |
| Envelope separates enforced, inherited, measured and not-implemented limits | **Complete** | `OPERATIONAL_ENVELOPE.md`; every ENFORCED row names a test |
| Repository clean and reproducible | **Complete** | No data beyond the public reference grades, no credentials, ledgers, caches or virtual environments in the archive; clean-checkout installs pass (below) |
| Remaining organizer decisions identified | **Complete** | `GAP_ANALYSIS.md`; integration point in `REQUIREMENTS_TRACEABILITY.md` |
| Reference-task fidelity demonstration | **Complete locally; partial against spec §4** | Synthetic predictions (no baseline predictions are published upstream); no platform run |
| GitHub Actions CI | **Written; workflow-equivalent commands run locally; not run on GitHub** | `.github/workflows/ci.yml` (locked 3.13 job, ranges 3.11 job; skipped Docker or registry tests fail CI). Needs a published repository |
| Public repository link | **Not done** | Needs the contributor's decision and GitHub access; nothing has been published |
| Organizer integration | **Not done** | Worker hook (G1); route registration and `routeVersion` in dispatches (G16): today's platform dispatches carry no route version and are refused. Registry-format declarations are ready (`registry/`) |
| Independent adversarial evaluation | **Not done** | G19 |
| Formal conformance approval | **Not done** | The route would be `DECLARED` on submission; it is not `UNDER_REVIEW` or `APPROVED` |

## Clean-install results (commit `60777ec`)

Each run unpacked the committed tree into an empty directory, created a new virtual environment, set `PM_REQUIRE_SEALED_RUN=1` and `PM_REQUIRE_REGISTRY_CHECK=1` as in CI, ran `python -m pytest -q -rs` and then `python scripts/verify_all.py` (ruff, mypy, full suite with JUnit report, mutation check, fixture check, CLI host demonstration, real-Docker sealed run, registry-schema validation, reduced fidelity rerun). 22 October 2026, contributor's machine.

| Environment | verify_all steps | Tests | Mutants | Sealed-run checks |
|---|---|---|---|---|
| Python 3.13.16, `requirements-lock.txt` | 9 of 9 PASS | 247 passed, 0 failed, 0 skipped | 27 of 27 caught | 17 of 17 |
| Python 3.11.17, version ranges (`.[dev,integration,plots]`) | 9 of 9 PASS | 247 passed, 0 failed, 0 skipped | 27 of 27 caught | 17 of 17 |

## Before sending

1. Publish the repository (contributor action) and put its link into the email.
2. Run `python scripts/verify_all.py` on the published commit and check `docs/VERIFICATION_SUMMARY.md`.
3. Send the Operational Envelope and the link to Marc Lecoultre (spec §9), with the other declarations linked.
