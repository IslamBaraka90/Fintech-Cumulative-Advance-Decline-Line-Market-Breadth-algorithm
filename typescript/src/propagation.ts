/**
 * Correction propagation and rebasing — the operational half of a cumulative line.
 *
 * The calculator in `cumulativeAdvanceDeclineLine.ts` answers *"what is the line
 * as of one knowledge cutoff?"*. Running a cumulative series in production asks
 * two further questions that a single evaluation cannot:
 *
 * **"A correction landed. What did it cost me?"** Because the line is a running
 * sum, a restatement of one session shifts every later value by the same
 * constant. A chart that dropped 10 points overnight may have recorded no market
 * event at all. `diffLines` and `correctionImpact` separate the two: a **pure
 * restatement** is a constant offset on a tail, and its delta equals the change
 * in that one session's net advances. Anything else changed the *shape* of the
 * line and deserves a much closer look.
 *
 * **"Can I compare these two lines?"** Levels are not comparable across series —
 * the seed is a bookkeeping choice. `rebaseLine` re-expresses a line from a
 * different seed, and `lineDeltas` extracts the part that is genuinely
 * seed-independent. The invariant is worth stating explicitly:
 *
 *     rebaseLine(result, s)  changes every level by  (s - result.seed)
 *     lineDeltas(result)     is unchanged by any rebase
 *
 * Every function here refuses to work on an unpublishable result. A line whose
 * status is not `resolved` has no `points`, and inventing an analysis over its
 * diagnostic prefix would launder exactly the uncertainty the calculator was
 * careful to preserve.
 */

import {
  BreadthLineValidationError,
  calculateCumulativeAdvanceDeclineLine,
  type BreadthEvent,
  type BreadthLinePoint,
  type BreadthLineResult,
  type SeriesContract,
} from "./cumulativeAdvanceDeclineLine.ts";

export type DiffShape = "identical" | "constant_offset" | "reshaped" | "not_comparable";

export interface SessionDelta {
  session_date: string;
  session_sequence: number;
  before: number;
  after: number;
  delta: number;
  net_advances_changed: boolean;
}

export interface LineDiff {
  shape: DiffShape;
  first_divergent_session: string | null;
  tail_offset: number | null;
  changed_session_count: number;
  session_deltas: SessionDelta[];
  reasons: string[];
}

export interface CorrectionImpact {
  before_cutoff: string;
  after_cutoff: string;
  before_status: string;
  after_status: string;
  before_final_value: number | null;
  after_final_value: number | null;
  diff: LineDiff;
  recompute_from_session: string | null;
  ignored_future_event_count: number;
}

/**
 * Return a result's points, or throw if the result was never publishable.
 *
 * The calculator deliberately empties `points` for any non-resolved status and
 * moves the contiguous prefix to `causal_prefix_diagnostic`. Reading that prefix
 * here would quietly convert a diagnostic into an answer.
 */
function publishable(result: BreadthLineResult, label: string): BreadthLinePoint[] {
  if (result === null || typeof result !== "object") {
    throw new BreadthLineValidationError(`${label} must be a result object.`);
  }
  if (result.status !== "resolved") {
    throw new BreadthLineValidationError(
      `${label} has status '${result.status}'; only a resolved line can be analysed.`,
    );
  }
  if (!Array.isArray(result.points)) {
    throw new BreadthLineValidationError(`${label} is missing its points.`);
  }
  return [...result.points];
}

/**
 * The per-session net advances — the seed-independent content of the line.
 *
 * Two lines built from different seeds have different levels but identical
 * deltas. When comparing series, compare these.
 */
export function lineDeltas(result: BreadthLineResult): number[] {
  return publishable(result, "result").map((point) => point.net_advances);
}

/**
 * Re-express a resolved line as though it had started from `newSeed`.
 *
 * Every level moves by `newSeed - result.seed`; no delta changes. This is what
 * makes the seed a bookkeeping choice rather than a measurement, and it is the
 * only honest way to overlay two lines with different origins.
 */
export function rebaseLine(result: BreadthLineResult, newSeed: number): BreadthLineResult {
  const points = publishable(result, "result");
  if (!Number.isSafeInteger(newSeed)) {
    throw new BreadthLineValidationError("newSeed must be a safe integer.");
  }

  const offset = newSeed - result.seed;
  const rebased = points.map((point) => ({
    ...point,
    cumulative_line: point.cumulative_line + offset,
  }));
  return {
    ...result,
    seed: newSeed,
    points: rebased,
    final_value: rebased.length ? rebased[rebased.length - 1]!.cumulative_line : newSeed,
  };
}

/**
 * Compare two resolved evaluations of the same line, session by session.
 *
 * The classification is the point:
 *
 * - `identical` — nothing moved.
 * - `constant_offset` — every session from the first divergence onward moved by
 *   the *same* amount, and only one session's net advances changed. That is a
 *   textbook restatement: the market did not move, the data did.
 * - `reshaped` — more than one session changed, or the offsets differ. The
 *   line's shape changed and a tail offset is not a sufficient summary.
 * - `not_comparable` — the two evaluations do not describe the same session
 *   calendar, so a session-by-session diff would be meaningless.
 *
 * Lines with different seeds are compared *after* an implicit rebase: a pure
 * seed change is reported as a `constant_offset` starting at the first session,
 * with no net-advances change — the level shifted, the market did not.
 */
export function diffLines(before: BreadthLineResult, after: BreadthLineResult): LineDiff {
  const left = publishable(before, "before");
  const right = publishable(after, "after");

  const reasons: string[] = [];
  const sameCalendar =
    left.length === right.length &&
    left.every((point, index) => point.session_date === right[index]!.session_date);
  if (!sameCalendar) {
    return {
      shape: "not_comparable",
      first_divergent_session: null,
      tail_offset: null,
      changed_session_count: 0,
      session_deltas: [],
      reasons: ["session_calendars_differ"],
    };
  }

  const sessionDeltas: SessionDelta[] = left.map((old, index) => {
    const now = right[index]!;
    return {
      session_date: old.session_date,
      session_sequence: old.session_sequence,
      before: old.cumulative_line,
      after: now.cumulative_line,
      delta: now.cumulative_line - old.cumulative_line,
      net_advances_changed: old.net_advances !== now.net_advances,
    };
  });

  const divergent = sessionDeltas.filter((d) => d.delta !== 0);
  let shape: DiffShape;
  let first: string | null;
  let tail: number | null;

  if (divergent.length === 0) {
    shape = "identical";
    first = null;
    tail = 0;
  } else {
    first = divergent[0]!.session_date;
    const start = sessionDeltas.indexOf(divergent[0]!);
    const tailDeltas = new Set(sessionDeltas.slice(start).map((d) => d.delta));
    const restated = sessionDeltas.filter((d) => d.net_advances_changed);
    if (tailDeltas.size === 1 && restated.length <= 1) {
      shape = "constant_offset";
      tail = divergent[0]!.delta;
      reasons.push(
        restated.length ? `restated_session:${restated[0]!.session_date}` : "level_shift_only",
      );
    } else {
      shape = "reshaped";
      tail = null;
      reasons.push(`multiple_sessions_restated:${restated.length}`);
    }
  }

  return {
    shape,
    first_divergent_session: first,
    tail_offset: tail,
    changed_session_count: divergent.length,
    session_deltas: sessionDeltas,
    reasons,
  };
}

/**
 * Evaluate the same archive at two knowledge cutoffs and diff the results.
 *
 * This is the question an operator actually has after a correction lands: *how
 * much of the archive between these two instants changed what I had published?*
 * Both evaluations are causal, so the "before" line is exactly what was knowable
 * then — not today's data truncated, which is the usual bug.
 *
 * Throws if either cutoff produced an unpublishable line; a diff against a
 * diagnostic prefix would be a comparison of two different kinds of object.
 */
export function correctionImpact(
  contract: SeriesContract,
  events: BreadthEvent[],
  beforeCutoff: string,
  afterCutoff: string,
): CorrectionImpact {
  const before = calculateCumulativeAdvanceDeclineLine(contract, events, beforeCutoff);
  const after = calculateCumulativeAdvanceDeclineLine(contract, events, afterCutoff);
  return {
    before_cutoff: beforeCutoff,
    after_cutoff: afterCutoff,
    before_status: before.status,
    after_status: after.status,
    before_final_value: before.final_value,
    after_final_value: after.final_value,
    diff: diffLines(before, after),
    recompute_from_session: after.recompute_from_session,
    ignored_future_event_count: before.ignored_future_event_count,
  };
}
