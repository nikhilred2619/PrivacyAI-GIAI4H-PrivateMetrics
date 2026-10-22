/**
 * Evaluation scoring — pure functions, no NestJS / Prisma deps (ADR-0017).
 *
 * Given a task's HIDDEN ground truth (`imageId -> integer label`) and a
 * submission's predictions (`imageId -> integer label`), compute the four
 * headline metrics for ordinal-grade scoring:
 *
 *   - quadratic-weighted kappa (QWK) — the primary agreement metric for
 *     ordinal grades (IDRiD DR grading uses it);
 *   - accuracy — exact-match rate;
 *   - referable sensitivity / specificity — the binary split at the task's
 *     referable threshold T (grade ≥ T is "referable");
 *   - coverage — fraction of the ground-truth image set the submission
 *     actually predicted.
 *
 * Metrics are computed over the INTERSECTION of the two image sets
 * (`matched`); `coverage` reports how much of the ground truth that
 * intersection represents. Every returned number is rounded to 4 decimals.
 *
 * Correctness here must be trusted before any result is shown as
 * authoritative (ADR-0017 "Negative" consequence) — see `scoring.spec.ts`
 * for the hand-computed cases that pin these formulas.
 */

/* eslint-disable security/detect-object-injection --
 * This module is a pure numeric algorithm: every computed member access
 * below indexes a locally-constructed array (confusion matrix, marginal
 * histograms, aligned label arrays) with a bounded numeric loop counter or
 * a validated integer class label — never a user-controlled string key. The
 * object-injection sink does not apply. */

/** The four headline metrics + coverage, each rounded to 4 decimals. */
export interface EvaluationScores {
  /** Quadratic-weighted kappa over the matched set. */
  qwk: number;
  /** Exact-match accuracy over the matched set. */
  accuracy: number;
  /** Sensitivity (recall) for the referable class (label ≥ T). */
  referableSensitivity: number;
  /** Specificity (TN rate) for the non-referable class (label < T). */
  referableSpecificity: number;
  /** matched / |groundTruth| — fraction of the GT set predicted. */
  coverage: number;
}

/**
 * Thrown when a prediction (or ground-truth) label is not an integer in
 * `[0, numClasses - 1]`. Surfaced by the service as a 4xx and persisted on
 * the submission's `error`. The DTO layer already rejects non-integer /
 * negative grades; this is the second, task-aware guard (it knows N).
 */
export class ScoringError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ScoringError';
  }
}

export interface ScoringInput {
  /** HIDDEN ground truth: imageId -> integer label. */
  groundTruth: Record<string, number>;
  /** Submission predictions: imageId -> integer label. */
  predictions: Record<string, number>;
  /** Number of ordinal classes N (labels are 0..N-1). Default 5. */
  numClasses?: number;
  /** Referable threshold T (label ≥ T is referable). Default 2. */
  referableThreshold?: number;
}

/** Round to 4 decimals; `-0` is normalised to `0`. */
function round4(x: number): number {
  const r = Math.round((x + Number.EPSILON) * 1e4) / 1e4;
  return r === 0 ? 0 : r;
}

function assertValidLabel(label: number, numClasses: number, where: string, imageId: string): void {
  if (!Number.isInteger(label)) {
    throw new ScoringError(`${where} label for "${imageId}" must be an integer, got ${label}`);
  }
  if (label < 0 || label > numClasses - 1) {
    throw new ScoringError(
      `${where} label for "${imageId}" (${label}) is out of range [0, ${numClasses - 1}]`,
    );
  }
}

/**
 * Quadratic-weighted Cohen's kappa for two aligned ordinal-label arrays.
 *
 *   w[i][j]  = (i - j)^2 / (N - 1)^2
 *   O[i][j]  = count of (true = i, pred = j)
 *   E[i][j]  = histTrue[i] * histPred[j] / n
 *   kappa    = 1 - (Σ w·O) / (Σ w·E)
 *
 * Edge cases (per ADR-0017 spec):
 *   - n == 0                       → 0.0
 *   - Σ w·E == 0 (no disagreement possible) → 1.0
 *   - N <= 1 (degenerate weights)  → 1.0
 */
export function quadraticWeightedKappa(
  trueLabels: readonly number[],
  predLabels: readonly number[],
  numClasses: number,
): number {
  const n = trueLabels.length;
  if (n === 0) return 0;
  if (numClasses <= 1) return 1;

  const N = numClasses;
  const denomW = (N - 1) * (N - 1);

  const observed: number[][] = Array.from({ length: N }, () => new Array<number>(N).fill(0));
  const histTrue = new Array<number>(N).fill(0);
  const histPred = new Array<number>(N).fill(0);

  for (let k = 0; k < n; k++) {
    const i = trueLabels[k]!;
    const j = predLabels[k]!;
    observed[i]![j]! += 1;
    histTrue[i]! += 1;
    histPred[j]! += 1;
  }

  let numerator = 0;
  let denominator = 0;
  for (let i = 0; i < N; i++) {
    for (let j = 0; j < N; j++) {
      const w = ((i - j) * (i - j)) / denomW;
      const expected = (histTrue[i]! * histPred[j]!) / n;
      numerator += w * observed[i]![j]!;
      denominator += w * expected;
    }
  }

  if (denominator === 0) return 1;
  return 1 - numerator / denominator;
}

/**
 * Score a submission against a task's ground truth. Pure — no I/O.
 *
 * Aligns on the ground-truth image set: `matched` = imageIds present in
 * BOTH maps; all metrics except `coverage` are computed over `matched`
 * only. Prediction labels are validated against `[0, N-1]` (throws
 * `ScoringError` on a bad label); ground-truth labels are validated
 * defensively too.
 */
export function scoreSubmission(input: ScoringInput): EvaluationScores {
  const numClasses = input.numClasses ?? 5;
  const referableThreshold = input.referableThreshold ?? 2;

  const groundTruthIds = Object.keys(input.groundTruth);

  // Validate every prediction label against the task's class range. A
  // malformed label anywhere in the submission is a hard reject, not a
  // silently-dropped row.
  for (const [imageId, label] of Object.entries(input.predictions)) {
    assertValidLabel(label, numClasses, 'prediction', imageId);
  }
  // Defensive: ground truth is validated at task-creation time, but a bad
  // label would corrupt the QWK matrix indexing, so guard here too.
  for (const imageId of groundTruthIds) {
    assertValidLabel(input.groundTruth[imageId]!, numClasses, 'ground-truth', imageId);
  }

  // Intersection, in a deterministic order (sorted by imageId).
  const matchedIds = groundTruthIds
    .filter((id) => Object.prototype.hasOwnProperty.call(input.predictions, id))
    .sort();

  const coverage = groundTruthIds.length === 0 ? 0 : matchedIds.length / groundTruthIds.length;

  const trueLabels: number[] = [];
  const predLabels: number[] = [];
  for (const id of matchedIds) {
    trueLabels.push(input.groundTruth[id]!);
    predLabels.push(input.predictions[id]!);
  }

  const matchedCount = matchedIds.length;

  // Accuracy — exact-match rate over the matched set.
  let exact = 0;
  for (let k = 0; k < matchedCount; k++) {
    if (trueLabels[k] === predLabels[k]) exact += 1;
  }
  const accuracy = matchedCount === 0 ? 0 : exact / matchedCount;

  // Referable split at T.
  let tp = 0; // true ≥ T & pred ≥ T
  let tn = 0; // true < T & pred < T
  let pos = 0; // true ≥ T
  let neg = 0; // true < T
  for (let k = 0; k < matchedCount; k++) {
    const t = trueLabels[k]!;
    const p = predLabels[k]!;
    const trueReferable = t >= referableThreshold;
    const predReferable = p >= referableThreshold;
    if (trueReferable) {
      pos += 1;
      if (predReferable) tp += 1;
    } else {
      neg += 1;
      if (!predReferable) tn += 1;
    }
  }
  const referableSensitivity = pos ? tp / pos : 0;
  const referableSpecificity = neg ? tn / neg : 0;

  const qwk = quadraticWeightedKappa(trueLabels, predLabels, numClasses);

  return {
    qwk: round4(qwk),
    accuracy: round4(accuracy),
    referableSensitivity: round4(referableSensitivity),
    referableSpecificity: round4(referableSpecificity),
    coverage: round4(coverage),
  };
}
