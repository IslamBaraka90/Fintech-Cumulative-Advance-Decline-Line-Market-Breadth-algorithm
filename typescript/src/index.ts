/**
 * Fintech Cumulative Advance/Decline Line — causal, revision-aware breadth line.
 *
 * A small, well-specified, cross-language reference implementation of the
 * Cumulative A/D Line (`line[n] = line[n-1] + advances - declines`) built as a
 * **projection over an append-only event archive** rather than a mutable series:
 * corrections arrive as revision events, the line is rebuilt from an explicit
 * seed, and nothing unavailable at the knowledge cutoff can influence the answer.
 *
 * Because the line is a running sum, a break in the chain is not a missing point —
 * it invalidates every point after it. This implementation drops the suffix rather
 * than summing across the hole, and never publishes a level it cannot support.
 *
 * Companion article (canonical):
 * https://thefintechbuilder.com/market-breadth-and-internals/advance-decline-breadth/cumulative-advance-decline-line/
 * Catalog topic id: D04-F01-A03
 */

export {
  BreadthLineValidationError,
  calculateCumulativeAdvanceDeclineLine,
  type Action,
  type BreadthEvent,
  type BreadthLinePoint,
  type BreadthLineResult,
  type ExpectedSession,
  type ResultStatus,
  type SeriesContract,
} from "./cumulativeAdvanceDeclineLine.ts";

export {
  correctionImpact,
  diffLines,
  lineDeltas,
  rebaseLine,
  type CorrectionImpact,
  type DiffShape,
  type LineDiff,
  type SessionDelta,
} from "./propagation.ts";
