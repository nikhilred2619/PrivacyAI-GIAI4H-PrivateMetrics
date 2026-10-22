# Gap analysis — what remains to be decided or built

**Status: DRAFT — FOR ORGANIZER REVIEW. Not yet approved or integrated.** Route version `0.2.1` (draft; not registered or reviewed). Updated for the organizer feedback summarised at the top of `THREAT_MODEL.md`. Nothing here has been agreed beyond that feedback; no item is marked resolved on the basis of an approval that has not been given.

## Resolved by the organizer feedback (Marc Lecoultre, ITU, 21 October 2026)

| ID | Earlier question | Resolution | What changed in `0.2.0-draft` |
|---|---|---|---|
| G1 (part) | Where does the DP mechanism run? | On the **data host**, which scores against its own labels and returns metrics. The platform scores only the public reference task | `privatemetrics.host` (`HostScorer`, CLI `host-*`), host-side architecture in `ARCHITECTURE.md` |
| G1 (part) | Mode 1 predictions upload as a budget bypass | Not a host-task concern: the platform holds no host labels to score against. Mode 1 applies only to the public reference task | Removed from the host-task threat model; reference-task Mode 1 is out of scope (`THREAT_MODEL.md` §9) |
| G2 | Privacy unit | Label-level DP is acceptable; the host must declare a group factor where patients have several items | Group factor per version, validated against a patient map, reported at patient level (`THREAT_MODEL.md` §7.9) |
| G7 (part) | Shared PostgreSQL ledger | Each host keeps its own ledger; SQLite may suffice if its limits are declared. PostgreSQL is not to be built without a demonstrated need | SQLite kept; limits declared in `OPERATIONAL_ENVELOPE.md` §16 |
| G10 | Reference-task labels | Public by design; the platform scores the reference task | Real public labels copied with provenance; fidelity measured on them (`REFERENCE_TASK_FIDELITY_REPORT.md`) |
| G5 | Pre-dispatch budget check | Can now be done host-side without any API change | `HostScorer.preflight` / `host-preflight` |

## Open items

| ID | Question | Why it matters | Current draft position | Decides |
|---|---|---|---|---|
| G1 | **Host-side wiring.** The host's worker must call `host-preflight` before `SealedRunner.run` and `host-score` on its result before the outbox POST | Without it the component is not in the path. A host could also misconfigure its worker to return predictions or plaintext metrics | Wiring shown against the unmodified upstream runner with real Docker (`integration/sealed_run/`); not part of upstream `worker-eval`. Options: a hook in `worker-eval`, or a wrapper process owned by the host | Organizers + hosts |
| G3 | **Budget-exhaustion failure code** (closed taxonomy) | A blocked release should not look like a platform error | `INTERNAL_ERROR` with a fixed detail; `PRIVACY_BUDGET_EXHAUSTED` available behind `--proposed-codes` | Organizers (`shared-types`) |
| G4 | **ε per release and cap per scope, allocation** among participants | Utility vs. protection; a cap allows cap/ε releases for everyone together | Reference demonstration ε = 8 (public labels; measured). Host tasks: chosen by the host; measured guidance in `EXPERIMENT_REPORT.md` §2 | Hosts, with organizers |
| G6 | **Result metadata** ("noisy, ε = …", remaining budget) | Readers must know values are noisy | Declared at route level only; no per-result field (the result schema is closed) | Organizers |
| G7 | **Hosts with several scorer machines** | SQLite serialises writers on one machine only | Not supported; one ledger file on one host. A shared store would need equivalent transactions and is not implemented or tested | Hosts (only if needed) |
| G8 | **Item identifiers for host tasks.** Sensitive? | Overlap detection hashes item IDs without a key | Identifiers assumed non-sensitive (A4); keyed hashes if hosts need them | Hosts |
| G9 | **Patient maps.** Will hosts supply `item_id,patient_id` maps? | Without a map the group factor is an unverified declaration | Map optional; when supplied, the declaration is checked; never stored | Hosts |
| G11 | **Failure outcomes as an image channel** (inherited) | A model can pick its failure by image content | Out of scope; noted for reviewers | Organizers |
| G12 | **Undefined denominators** return 0 (upstream convention) | Indistinguishable from a real 0 | Kept for compatibility with `scoring.ts` | Organizers |
| G13 | **CLASSIFICATION family** | Same mechanism extends to nominal labels; the API currently tags host metrics as GRADING only | GRADING only | Organizers |
| G15 | **Who registers scopes and approves cap increases at a host** | The registry prevents accidental resets but trusts its operator | Host operator role; approvals recorded with name and reason | Hosts |
| G16 | **Route version in dispatches.** The host scorer requires `routeVersion` equal to `0.2.1` (exact string) and refuses absent, null, malformed or different values before the model runs. **Confirmed upstream behaviour** (`5785db8`): `evaluation.service.ts` creates container submissions with `routeId: null, routeVersion: null` and publishes a `SealedRunMessage` without either key ("No route registry yet (WP5)"), so **every current dispatch would be refused**. Upstream already has a route registry (`route-registry.service.ts`; `CreateRouteVersionRequestSchema` requires `MAJOR.MINOR.PATCH`), which is why the wire string is now `0.2.1` rather than `0.2.1-draft` | Results are meaningful only under the declarations of the route version that produced them; accepting a null version would let a run be scored under declarations nobody named | **Required organizer-side change (minimal):** (1) register the route (`registry/evaluation-route.json`) and version (`registry/route-version.json`; both validated against upstream's Zod schemas); (2) record which route a host task uses; (3) at dispatch, set `routeId` and `routeVersion` on the submission row and in the `SealedRunMessage` from the registry (both fields already exist and are optional in `SealedRunMessageSchema`). The API's existing check then matches the host's `routeVersion` against the dispatched one. Nothing in this repository can make that change | Organizers |
| G17 | **Ledger backups and rollback** | Restoring an old ledger copy, or editing it, refunds budget; no SQLite transaction test can detect this | Declared as a host operating rule (`OPERATIONAL_ENVELOPE.md` §14); rollback detection is NOT IMPLEMENTED (it would need an anchor outside the host, for example the platform's own count of scored submissions per task) | Hosts; organizers if an external anchor is wanted |
| G18 | **Post-processing choice** | Projection biases accuracy downward on the reference fixtures; clipping alone had lower accuracy RMSE there but higher QWK RMSE | Projection kept (consistent table); alternatives are free under post-processing and could be evaluated | Contributor, with reviewer input |
| G19 | **Independent review** | Spec §6 requires paired technical and adversarial review | Not started; all evidence here is the contributor's own | Organizers |

## Removed or obsolete

* **Mode 1 bypass for protected tasks** (earlier G1): obsolete for host tasks, per the feedback.
* **Integration options P1 (platform-side Python service) and P2 (TypeScript port in the API)**: not needed for host tasks, since the platform does not hold host labels. The library-level `score` command remains for tests and experiments only.
* **G14 (copy recipients)**: an addressing question for the contributor, not a design gap.
