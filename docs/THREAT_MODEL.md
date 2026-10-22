# Threat model — PrivacyAI-GIAI4H-PrivateMetrics

**Status: DRAFT — FOR ORGANIZER REVIEW. Not yet approved or integrated.** Route version `0.2.1` (draft; not registered or reviewed) (supersedes `0.2.0-draft`; changes in `SECURITY_FIX_SUMMARY.md`). Declaration 1 of 3 (conformance specification §3.1), consistent with `DISCLOSURE_PROFILE.md` and `OPERATIONAL_ENVELOPE.md` of the same version.
**Contributor:** Nikhil Reddy Donapati, Independent Researcher (PrivacyAI Research Lab).
**Upstream reviewed:** `FG-AI4H/oci-platform` at commit `5785db81b4193ff4e3d62738b3a3ea40f0f9aee8` (`UPSTREAM_REVIEW.md`; no material change since `1da3bbf`).

**Organizer feedback.** Organizer feedback received from Marc Lecoultre (ITU) on October 21, 2026, concerning the earlier Threat Model and Disclosure Profile drafts, has been incorporated into this revision. The feedback concerned the design direction; it is not an approval, test, integration or endorsement of this implementation. In summary:

1. The platform holds labels and scores **only for the public reference task**. For clinical tasks contributed by data hosts, the platform does not hold the labels: the **data host** runs the scorer against its own labels and returns metrics. This component therefore runs **on the data host** for host tasks.
2. For a host task the **data host is the trusted party**: it holds the labels, the evaluation data, the scorer and the privacy ledger. The platform receives only the permitted metrics and result metadata.
3. The Mode 1 predictions upload applies to the reference task. The platform cannot score predictions against host labels it never holds, so there is **no API-side scoring bypass for host tasks**.
4. Each host keeps its own ledger, so a single-host SQLite ledger may be sufficient if its concurrency and crash-recovery limits are declared.
5. Label-level DP is an acceptable privacy unit; where several items belong to one patient, the host must declare the group factor.
6. Other integration decisions can be resolved later.

Items that remain organizer or host decisions are marked **[DECISION]** (`GAP_ANALYSIS.md`).

## 1. What this component is

A differentially private (DP) **metrics release** for the GRADING scoring family, run by a data host at its scoring step. For one sealed run it:

1. receives the participant model's `predictions.json`, produced inside the host's sealed environment;
2. builds the confusion table against the host's reference labels, which never leave the host;
3. adds calibrated discrete-Laplace noise (OpenDP) to the label-dependent cells and derives the five GRADING metrics by post-processing;
4. charges the release to the host's persistent privacy ledger, owned by a **host-assigned privacy scope**, and refuses releases once the scope's cap would be exceeded;
5. returns only `{routeVersion, durationMs, metrics}` or a classified failure (`SealedRunResult`, metrics branch, spec §2.3).

It does not run models, change containment, train models or replace the platform. It is a Python library and CLI (`python -m privatemetrics`). It has **not been integrated** into any host's deployment or the platform (`GAP_ANALYSIS.md` G1).

### 1.1 Two settings

| | Reference task (`idrid-dr-grading`) | Clinical host task |
|---|---|---|
| Who holds the labels | The platform (public IDRiD grades, committed upstream) | The data host only |
| Who scores | The platform, in plaintext, with `scoring.ts` (sealed-run `predictions` branch and Mode 1 upload) | The data host, with this component |
| What crosses to the platform | Predictions (sealed run) or a predictions upload (Mode 1) | Noisy metrics or a failure code |
| Role of this component | **Demonstration only**: run host-side on the public labels to measure the fidelity gap (`REFERENCE_TASK_FIDELITY_REPORT.md`). Nothing secret is protected | The protection mechanism |
| Mode 1 | Exists; plaintext scoring of public labels; out of scope here | Does not exist: the platform holds no labels to score against |

## 2. Protected assets and privacy unit

| Asset | Protected here? | How |
|---|---|---|
| **Host reference labels** | **Yes: primary asset.** Privacy unit: **one evaluation item's label** | ε-label-DP per release (§7); composition enforced per privacy scope by the host's ledger (§7.6) |
| Labels of one patient with several items | Yes, at the group level | Group privacy: `g`·ε per release for a declared group factor `g` (§7.9) |
| Label-derived aggregates (confusion counts, prevalence among predicted items) | Yes | Released only through the noisy table |
| Exact counts, plaintext metrics, noise draws | Yes | In host memory for one release; never logged, persisted or returned (S12, `test_ledger_rows_record_certified_cost_not_above_charge`) |
| Per-item predictions | Not sent anywhere | They stay on the host; the host result never contains them (H2, H3, H4) |
| Images | **No formal guarantee from this component** | Inherited containment (§6); see §8.2 |
| Membership of an item in the evaluation set | **No** | Item identifiers and counts are known to the host's worker and assumed non-sensitive (A4) [DECISION G8] |
| The participant's model | Inherited | Reference containment, run by the host |

## 3. Adversaries

| Adversary | Assumed capability | In scope? |
|---|---|---|
| **Model developer** | Knows the task configuration and this route's declarations. Controls model code fully; the model sees **all** images jointly inside the sealed container. Submits adaptively, within the platform's quotas. | **Yes: primary** |
| **Colluding / Sybil participants** | Many identities, each within its per-participant quota; pooled results | **Yes**: the budget is per privacy scope and shared by every dispatch to the host's task |
| **Platform operator** (for host tasks) | Dispatches runs, receives and stores every released result, sees result metadata and durations | **Yes, as an observer of releases**: it sees only what the model developer sees, plus every participant's results. The DP guarantee covers all released values together, because the budget is global per scope. Not protected: what the platform learns from dispatch metadata it already holds |
| **Network observer** | Sees traffic between host and platform | Relies on TLS of the host's outbox (inherited) |
| **Solution provider** (this contributor) | Writes open code; does not operate it | Yes: no data, keys, seeds, telemetry or results flow to the provider |
| **Data host** (operator of this component for its task) | Holds labels, images, ledger and configuration | **No: trusted.** DP cannot protect data from the party that holds it. A host that misconfigures or bypasses the scorer is outside this model (§9) |

## 4. Attacks considered

Test IDs refer to `SECURITY_TEST_REPORT.md`, which records their current results.

| Attack | Treatment | Evidence |
|---|---|---|
| Repeated queries | Each release charges ε to the scope; cap enforced exactly | S1, `test_D1_cumulative_fractional_releases_never_exceed_cap`, H6 |
| Sybil / collusion | Budget per scope, not per participant or dispatch | S3, H6 |
| Adaptive / differencing queries | Adaptive composition (§7.6); every probe is charged | S4 |
| Small subgroups | Sensitivity is per label, independent of cell sizes | Proof §7.3; S5 |
| Result redelivery / duplicate dispatch | Stored release returned (identical metrics, so the platform's result fingerprint treats it as a no-op); no new noise, no new charge; survives restarts | S2, H8, `SEALED_RUN_TRANSCRIPT.md` run 5 |
| Redelivery with different predictions (non-deterministic model re-run) | Refused; no second draw | S2b, H8b |
| **Submission ID in a non-canonical form** (upper-case UUID for an already-scored submission) | Refused: only the canonical lower-case UUID is accepted, so one submission cannot obtain two draws (D9) | H5, mutation check |
| Dispatch for another task, or without the exact route version (absent, null, malformed or different) | Refused before scoring (D10) | H5, H5b, H5c; mutation check |
| Re-running a model for a submission that can never be released (in progress, failed, crashed) | Preflight refuses; the model is not run (D12) | H6d |
| Replay under changed privacy parameters | Refused | `test_D4_replay_under_changed_epsilon_is_refused` |
| **Dataset-version attack** (edit one label to get a "new" dataset) | A new version of the same dataset charges the same scope | `test_D2_one_label_changed_new_version_shares_budget`, H6c |
| **Budget-reset attempts** (re-register with a larger cap; rebind a dataset or task; copy or tweak a dataset into a fresh scope; overwrite a version) | All refused by the registry; cap increases need a named approver and are recorded | `tests/test_identity.py` |
| Understated group factor | Refused at registration when a patient map is supplied (a map that lists an item twice is refused, D11); immutable once registered | H7, H7b, H7d |
| Numerical undercharging (rounding, float error) | Exact rational ε on a 1e-6 grid; off-grid values rejected; noise scale calibrated so that OpenDP's certified cost ≤ the charged ε | `tests/test_budget.py` |
| Concurrency races | One `BEGIN IMMEDIATE` transaction per reservation (SQLite, one host) | S10, S10b, H8d |
| Crash between reservation and release | Reserve before sampling; the row stays charged; `recover` marks stale rows `FAILED_SPENT`, never re-released | S11, H8c, `HOST_DEMO_TRANSCRIPT.md` §10 |
| Ledger unavailable or write failure | Fail closed: no metrics returned | S15, H8e |
| Running a model when no release is possible | Host preflight refuses the dispatch before the model runs | H6b, `SEALED_RUN_TRANSCRIPT.md` run 7 |
| **Plaintext metrics escaping** (a worker configured to score, or a bug producing extra fields) | The host scorer refuses a worker body that carries `metrics`; every outgoing body is checked against the exact permitted schema and replaced by `INTERNAL_ERROR` otherwise | H4c, H4b, H4g; mutation check |
| Predictions, item IDs or labels in the result | Never present; checked by schema | H2, H3, H4, `SEALED_RUN_TRANSCRIPT.md` checks |
| Container stdout/stderr echoing inputs | Inherited: operator log only; absent from the host result | `SEALED_RUN_TRANSCRIPT.md` run 3 (`input-echo`) |
| Network exfiltration from the model | Inherited: no network; classified `NETWORK_ATTEMPT_DETECTED`, passed through with no charge | `SEALED_RUN_TRANSCRIPT.md` run 4 (`egress`) |
| Resource abuse by the model (time, memory, output size), malformed output, crashes | Inherited worker limits; failure codes passed through with no charge | `SEALED_RUN_TRANSCRIPT.md` runs 5–9 (`TIMEOUT`, `OOM_KILLED`, `OUTPUT_TOO_LARGE`, `MALFORMED_OUTPUT`, `NONZERO_EXIT`) |
| Reading the host ledger from another local account | New ledger files are created owner-only (`0600`) (D13, hardening) | H8f |
| Label access from inside the container | Labels are not mounted; the host keeps them outside the input root | `SEALED_RUN_TRANSCRIPT.md` run 2 (`label-probe`) |
| Malformed, out-of-range, unknown-ID or oversize predictions | Validated before labels are read; no charge | S6–S9 |
| Worker failure codes as a channel | Passed through only if in the closed taxonomy; no charge. The code itself remains an inherited channel (§8.3) | H4e, H4f |
| Participant-influenced values in worker failure details (exit status, bytes written, run time) reaching the platform | Detail replaced by fixed text; the worker's detail stays in the host's logs (D14) | H4h; mutation check |
| Logs and error text | Identifiers, ε and status only; fixed label-independent messages | S12, S13 |
| Floating-point noise attacks (Mironov 2012) | Integer-valued exact discrete sampler (OpenDP) | Design; `test_opendp_privacy_map_equals_declared_epsilon` |
| Seeded or reproducible production noise | Production rejects the seeded sampler | S14 |
| Simulation-to-production replay | One immutable mode per ledger file and per row; only production outcomes leave; the host scorer is production-only | `tests/test_modes.py`, H9 |

## 5. Accounting identity

The budget belongs to a **privacy scope**, an opaque ID chosen by the host. It is never derived from label content.

* A **dataset** is bound to exactly one scope, permanently.
* A **dataset version** is an immutable label set of a dataset with an immutable declared **group factor**. New versions (for example corrected labels) charge the same scope.
* A **task** is bound to exactly one dataset, permanently. Renamed or additional tasks on the same dataset charge the same scope.
* Registering identical labels, or any item whose identifier already belongs to a dataset in another scope, is **refused**. Overlapping datasets must share a scope.
* Disjointness is checked **on item identifiers only**. Renamed items, or one patient appearing in two datasets under different item IDs, are **not detected**; separating such datasets is the host's responsibility.
* A scope's cap can only be changed through `change_cap`. Lowering is always allowed; raising needs a named approver and reason; every change is recorded.
* Each host has its own ledger and scopes. Two hosts holding copies of the same patients' labels would each spend their own budget; that is outside one host's control (§8.4).

## 6. Inherited from the reference implementation

The host runs the reference sealed-execution worker (`apps/worker-eval`); this component neither provides nor changes containment. From conformance specification §2.4 and the upstream code:

* sealed container: no network, read-only root filesystem, only `/output` writable and size-capped (memory-backed), non-root, all capabilities dropped, no new privileges, memory/CPU/process limits, hard timeout, pull and verify by digest, no host mounts besides `/input` (read-only) and `/output`; container removed and image pruned afterwards;
* container stdout/stderr captured for operators only;
* **labels never enter the sandbox**: the host keeps them outside the input root, and the scorer reads them from the host's own configuration (assumption A1);
* the API stores a host's `metrics` result as-is and treats a result with identical metrics as a no-op replay.

**Observed, not certified.** `integration/sealed_run/run_sealed_host.py` drives the unmodified upstream `SealedRunner` with a real Docker daemon and nine participant images (toy model, label probe, input echo, egress, malformed output, non-zero exit, sleeper, memory hog, output flood) and passes every result through `HostScorer`. Observed: no label file visible in the container; container output kept out of results; network blocked; timeout, memory and output-size limits enforced; failures passed through with no charge; every host body valid against the upstream `SealedRunResult` schema. Results: `SEALED_RUN_TRANSCRIPT.md`. That run used a local registry and locally built images; it is evidence that the two pieces fit and that the inherited limits act, not a certification of any host's containment or deployment. CPU and PID limits were configured but not exercised.

## 7. Formal guarantee

### 7.1 Notation

Item set `I`, `|I| = n`. Labels `y : I → {0..K−1}` (protected, host-held). Predictions `p : I → {0..K−1} ∪ {⊥}`, a function of images and public inputs only (A1). Matched set `J = {i : p_i ≠ ⊥}`, `m = |J|`; column counts `c_b`; active columns `B⁺ = {b : c_b > 0}`. Released statistic: `C[a,b] = |{i ∈ J : y_i = a, p_i = b}|` for `b ∈ B⁺`.

### 7.2 Neighbouring relation

`y ≃ y′` iff they differ in the label of exactly one item, with item set, images and predictions unchanged (replace-one, label level). This is **label differential privacy**. It is not image-level, record-level or membership DP. Patient-level protection follows from group privacy (§7.9).

### 7.3 Lemma 1 (sensitivity)

If `y ≃ y′` differ at item `i`: for `i ∉ J`, `C(y) = C(y′)`; for `i ∈ J` with `p_i = b`, one cell of column `b` decreases by 1 and one increases by 1. So `‖C(y) − C(y′)‖₁ ≤ 2`. `B⁺`, `c_b`, `m` and `n` depend only on predictions and the item set, so they are equal for `y` and `y′`. Checked exhaustively on 300 random instances in `test_sensitivity_of_table_is_two_for_every_single_label_change`. ∎

### 7.4 Per-release guarantee

`Ĉ = C + Z` with `Z` i.i.d. discrete Laplace, `Pr[Z = z] ∝ exp(−|z|/s)`. The geometric/discrete-Laplace mechanism argument (Ghosh, Roughgarden & Sundararajan 2009; Canonne, Kamath & Steinke 2020) gives `Pr[Ĉ(y) = ĉ] ≤ exp(2/s) · Pr[Ĉ(y′) = ĉ]`, i.e. `(2/s)`-label-DP with δ = 0.

**Implementation check.** Noise is sampled by OpenDP 0.16.0 `make_laplace` over `vector_domain(atom_domain(T=i32))` with `l1_distance(T=i32)`. `budget.calibrate_scale` starts at the textbook scale `2/ε` and raises the floating-point scale one ULP at a time until OpenDP's own privacy map at `d_in = 2`, read exactly as a rational, is **≤ the configured ε**. The certified value is stored with each release (`certified_eps`). Counts are at most 100,000, far inside the i32 range; with ε ≥ 1e-6 the scale is at most about 2·10⁶, and saturation at the i32 bounds would need a draw around 10³ scale lengths from zero.

### 7.5 Post-processing

Released metrics are a deterministic function of `Ĉ` and of `(c_b, m, n)`, which are equal across neighbours, so they are ε-label-DP. Coverage `m/n` depends on predictions only and is released exactly. With no matched items, no label-dependent value is released; ε is still charged.

### 7.6 Composition and accounting

Basic sequential composition holds under adaptive choice of submissions for pure DP (Dwork & Roth 2014, Thm 3.16; Dwork, Rothblum & Vadhan 2010). Releases charged to one scope compose to `Σ εⱼ`. The host's ledger enforces `Σ εⱼ ≤ cap` **exactly**:

* ε values are rationals on a fixed grid of 1e-6; off-grid, non-finite, non-positive and out-of-range values are rejected, never rounded;
* amounts are stored as integers in 1e-6 units, so sums are exact;
* each release is charged the configured ε, which is ≥ OpenDP's certified cost (asserted at scorer start-up and again in the ledger).

Releases on **different scopes** compose only if the underlying label sets are disjoint; the registry enforces this for item identifiers, not for patients (§5).

### 7.7 Assumptions

| ID | Assumption | Enforced by |
|---|---|---|
| A1 | Labels never enter the model sandbox; predictions do not depend on labels | Inherited containment run by the host; labels outside the input root (`SEALED_RUN_TRANSCRIPT.md`) |
| A2 | Production noise is fresh and unpredictable; never re-drawn for the same submission; no production result carries simulation noise | OpenDP sampler (CSPRNG, `opendp._data.fill_bytes`); release cache keyed on the canonical submission ID; seeded sampler refused; ledger-file and per-row mode checks |
| A3 | Every scored release on a protected host label set passes through the host's ledger | **The host's deployment**: the host runs only this scorer for the task and does not return predictions or plaintext metrics. The scorer refuses worker metrics, but cannot stop a host from deploying something else. The platform cannot score host labels it does not hold |
| A4 | Item set, item identifiers and item count are non-sensitive | Host task configuration **[DECISION G8]** |
| A5 | The declared group factor `g` is at least the true maximum number of items per patient | Host declaration; checked against a patient map when one is supplied (H7) |
| A6 | No other participant-visible output depends on labels | Fixed error codes; worker failure codes do not see labels |
| A7 | The ledger file is not modified, replaced or rolled back outside this component | Host (trusted). See §8.4 on backups |

### 7.8 Simulation and production separation

Simulation runs use a seeded, reproducible sampler, so a simulation result published as production would carry no guarantee. Separation is enforced at four points: an immutable mode per ledger file; a mode per release row checked on every replay; scorer/ledger mode pairing; and an adapter that emits only production outcomes. The host scorer opens its ledger in production mode only and refuses a simulation ledger (H9).

### 7.9 Group privacy (patients with several items)

If one patient contributes up to `g` items, changing that patient's labels changes at most `g` items. For a pure ε-DP mechanism under replace-one adjacency, datasets that differ in `g` items satisfy `Pr[M(y) ∈ S] ≤ exp(gε) Pr[M(y′) ∈ S]` (Dwork & Roth 2014, Thm 2.2). So each release is **`g`·ε-DP at the patient level**, and `k` releases on a scope cost at most `Σ g_vⱼ · εⱼ`, where `g_v` is the factor declared for the version a release used.

* The factor is declared per dataset version, stored in the ledger, immutable, and reported: `privatemetrics ledger` prints `patient_level_spent` (`Σ g·ε`) and `patient_level_cap_bound` (`max g × cap`).
* With a patient map (`item_id,patient_id`), registration refuses a factor below the observed maximum. The map is read and discarded; nothing about patients is stored.
* Without a map, the factor is the host's unverified declaration. The reference configuration declares `g = 2` for the IDRiD slice because patients are not identified upstream (both eyes of one patient is the plausible maximum); this is an **assumption, not a verified fact**.
* Group privacy does not cover a patient whose items appear in **several datasets or several hosts** under different identifiers (§5, §8.4).

### 7.10 What is proven, what is tested, what rests on the host

| # | Property | Formal basis | Checked in this repository (empirical, not a proof) | Needs the trusted host? |
|---|---|---|---|---|
| 1 | Replace-one label adjacency | Definition §7.2 | — | No |
| 2 | Confusion-table L1 sensitivity ≤ 2; prediction-only statistics unchanged | Lemma 1, §7.3 | G5: every single-label change on 300 random instances (`tests/test_guarantees.py`); `test_sensitivity_of_table_is_two_for_every_single_label_change` | No |
| 3 | Discrete-Laplace calibration | OpenDP privacy map at d_in = 2; ULP calibration §7.4 | `test_D1b_*`, G4 | No |
| 4 | Certified cost ≤ charged ε, recorded per release | Assertion at start-up and in the ledger | G4: recorded `certified_eps` equals OpenDP's map at the calibrated scale for 7 ε values, never above the charge; `test_ledger_rows_record_certified_cost_not_above_charge` | No |
| 5 | Exact cumulative accounting | Integer units §7.6 | G4 (ledger sum = k · ε exactly), `tests/test_budget.py`, S1, S10, H8d | Ledger integrity (A7) |
| 6 | Adaptive composition | Dwork–Rothblum–Vadhan (pure DP) | — (formal only) | Every release goes through the ledger (A3) |
| 7 | Post-processing is free and deterministic | §7.5 | G3 | No |
| 8 | Coverage is label-independent | §7.5 (`m/n` depends on predictions only) | G2: identical released coverage for random label sets and fresh noise | Predictions do not depend on labels (A1) |
| 9 | Patient-level `g`·ε | Group privacy §7.9 | H7, H7b (declaration checked against a map when given; reporting) | The declared `g` is correct (A5); without a map it is not verified |
| 10 | No fresh budget from re-registering, versioning, copying or overlapping data | Registry rules §5 | `tests/test_identity.py`, H6c | Item identifiers identify items; patients across datasets are not detected |

**Exact privacy-loss check (G1).** On three small instances (K = 2 and K = 3, ε = 1 and 2) the full release distribution was computed exactly under the discrete-Laplace pmf, for two neighbouring label sets, enumerating every noise vector within a radius whose excluded mass is below 10⁻⁶. The largest probability ratio over outputs with probability ≥ 10⁻⁴ was 4.88 at ε = 2 (bound e² = 7.39), and 1.44 and 1.95 at ε = 1 (bound e = 2.72). This checks the implementation of sensitivity, projection and metric derivation on those instances; it is consistent with, but does not replace, the proof.

**What cannot be enforced without trusting the host:** that the labels used are the registered ones and are not shown to the model (A1); that every release passes through this ledger (A3); that the declared group factor is right when no map is given (A5); and that the ledger is not rolled back or edited (A7). No test in this repository can rule out a host that breaks these.

## 8. Limitations and residual risks

### 8.1 Utility on the 30-item reference task (measured)

Measured locally with 5,000 releases per setting on the public reference labels and synthetic prediction fixtures (`REFERENCE_TASK_FIDELITY_REPORT.md`):

* at ε = 1 a strong fixture's accuracy is released about 0.21 too low on average (SD 0.125), QWK about 0.36 too low;
* at ε = 4 accuracy is within ±0.10 in 96% of releases, QWK in 70%;
* at ε = 8 (the declared reference-demonstration ε) both are within ±0.10 in at least 99% of releases, and about half of all releases (53%) equal the plaintext values exactly.

Useful numbers on 30 items need an ε that gives weak protection to any single label. At ε = 1 the accuracy SD falls to 0.019 at 300 items and 0.006 at 1,000 (`EXPERIMENT_REPORT.md` §2): host tasks with hundreds or thousands of items are the intended operating point.

### 8.2 Images

The model sees all images jointly, so an image can influence every prediction. No image-level DP is claimed. Prediction-only quantities (coverage) are released exactly; image information reaching them is bounded only by inherited containment, quotas and output precision.

### 8.3 Inherited side channels

A model can choose its failure mode by image content, which leaks a few bits about images per run through the failure code, independent of labels. `durationMs` in a host result is the worker's run time: the model controls it and can make it depend on the images, so it is a timing channel from the model to the platform (label-independent; the upstream API keeps it out of participant views). It is kept because the result schema requires it; coarsening it would be a contract change **[DECISION G11]**. The scorer's own runtime is measured, not constant-time, and is not part of the result.

### 8.4 Other residual risks

* **Single-host ledger.** SQLite with `BEGIN IMMEDIATE` serialises writers on one host (threads and processes, tested). It must live on a local file system: network file systems with unreliable locking are not supported. A host running several scorer machines needs one shared ledger, which is **not implemented** (G7).
* **Backups and rollback.** Restoring an older copy of the ledger would refund spent budget. Backups must be append-only or restored only after reconciling with the release log; the component cannot detect a rollback (A7).
* **Cross-host and cross-dataset patients.** Each host spends its own budget; one patient present at two hosts, or in two datasets under different identifiers, is not tracked.
* **Budget exhaustion is a denial of service** for later participants; allocation policy **[DECISION G4]**.
* **Participant identity.** The dispatch carries none; releases are recorded against a fixed value. Replay identity is the platform-assigned submission UUID plus inputs and parameters. A platform that reused submission IDs across participants would be outside this model.
* **Reference task.** Its labels are public by design; the demonstration shows behaviour and utility, not protection.

## 9. Out of scope

A compromised or dishonest data host, scorer host or ledger file; a host deployment that bypasses this scorer; the platform's own handling of dispatch metadata; container-level side channels beyond the inherited controls; image-, membership- and cross-host patient-level privacy; encrypted-computation, confidential-computing and federated tracks; attacks on OpenDP or the operating system's random number generator; the public reference task's plaintext scoring paths on the platform (including Mode 1).

## References

Chaudhuri & Hsu (2011) COLT · Ghazi et al. (2021) NeurIPS · Ghosh, Roughgarden & Sundararajan (2009) STOC · Canonne, Kamath & Steinke (2020) NeurIPS · Dwork & Roth (2014) · Dwork, Rothblum & Vadhan (2010) FOCS · Mironov (2012) CCS · Duchi et al. (2008) ICML.
