/** Tests for the propagation surface: rebasing, diffing, and correction impact. */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BreadthLineValidationError,
  calculateCumulativeAdvanceDeclineLine,
  type BreadthEvent,
  type BreadthLineResult,
  type SeriesContract,
} from "../src/cumulativeAdvanceDeclineLine.ts";
import { correctionImpact, diffLines, lineDeltas, rebaseLine } from "../src/propagation.ts";
import { EARLY_CUTOFF, LATE_CUTOFF, WORKED, correction, inputs, resequence } from "./fixtures.ts";

const evaluate = (
  contract: SeriesContract,
  events: BreadthEvent[],
  cutoff = LATE_CUTOFF,
): BreadthLineResult => calculateCumulativeAdvanceDeclineLine(contract, events, cutoff);

const lines = (result: BreadthLineResult) => result.points.map((p) => p.cumulative_line);

// --- rebasing: the level is arbitrary, the deltas are not ------------------ //
test("rebase shifts every level by a constant", () => {
  const rebased = rebaseLine(evaluate(...inputs()), 100);
  assert.deepEqual(lines(rebased), [130, 110, 110, 150, 120]);
  assert.equal(rebased.seed, 100);
  assert.equal(rebased.final_value, 120);
});

test("rebase leaves the deltas invariant", () => {
  const r = evaluate(...inputs());
  assert.deepEqual(lineDeltas(rebaseLine(r, -5000)), lineDeltas(r));
  assert.deepEqual(lineDeltas(r), WORKED.original.net_advances);
});

test("rebasing matches recomputing from that seed", () => {
  const [c, e] = inputs();
  c.seed = 100;
  const recomputed = evaluate(c, e);
  const rebased = rebaseLine(evaluate(...inputs()), 100);
  assert.deepEqual(lines(rebased), lines(recomputed));
  assert.equal(rebased.final_value, recomputed.final_value);
});

test("rebase to zero is idempotent on a zero-seeded line", () => {
  const r = evaluate(...inputs());
  assert.deepEqual(lines(rebaseLine(r, 0)), lines(r));
});

test("rebase rejects a non-integer seed", () => {
  const r = evaluate(...inputs());
  for (const bad of [1.5, "100", NaN, Infinity] as unknown[]) {
    assert.throws(() => rebaseLine(r, bad as number), BreadthLineValidationError);
  }
});

// --- diffing: restatement vs. reshaping ------------------------------------ //
test("identical lines diff to nothing", () => {
  const diff = diffLines(evaluate(...inputs()), evaluate(...inputs()));
  assert.equal(diff.shape, "identical");
  assert.equal(diff.first_divergent_session, null);
  assert.equal(diff.tail_offset, 0);
  assert.equal(diff.changed_session_count, 0);
  assert.ok(diff.session_deltas.every((d) => d.delta === 0));
});

test("a correction is a constant offset on the tail", () => {
  const [c, e] = inputs();
  e.push(correction());
  const diff = diffLines(evaluate(...inputs()), evaluate(c, e));

  assert.equal(diff.shape, "constant_offset");
  assert.equal(diff.first_divergent_session, "2026-01-06");
  assert.equal(diff.tail_offset, WORKED.correction.delta);
  assert.equal(diff.tail_offset, 10);
  assert.equal(diff.changed_session_count, 4); // sessions 2..5 all moved
  assert.deepEqual(diff.reasons, ["restated_session:2026-01-06"]);

  // Exactly one session's net advances changed; the other four inherited it.
  assert.deepEqual(
    diff.session_deltas.filter((d) => d.net_advances_changed).map((d) => d.session_date),
    ["2026-01-06"],
  );
  assert.deepEqual(
    diff.session_deltas.map((d) => d.delta),
    [0, 10, 10, 10, 10],
  );
});

test("a pure seed change is a level shift not a restatement", () => {
  const [c, e] = inputs();
  c.seed = 100;
  const diff = diffLines(evaluate(...inputs()), evaluate(c, e));
  assert.equal(diff.shape, "constant_offset");
  assert.equal(diff.first_divergent_session, "2026-01-05"); // shifts from the very first
  assert.equal(diff.tail_offset, 100);
  assert.deepEqual(diff.reasons, ["level_shift_only"]);
  assert.ok(!diff.session_deltas.some((d) => d.net_advances_changed));
});

test("two restated sessions are reshaped not offset", () => {
  const [c, e] = inputs();
  e.push(correction());
  e.push(
    correction({
      event_id: "EV-004-R2",
      supersedes_event_id: "EV-004-R1",
      ingest_sequence: 7,
      session_sequence: 4,
      session_date: "2026-01-08",
      effective_at: "2026-01-08T21:00:00Z",
      available_at: "2026-01-10T15:00:00Z",
      advances: 60, // was 65 / 25 (net +40); now 60 / 30 (net +30)
      declines: 30,
    }),
  );
  const after = evaluate(c, e);
  const diff = diffLines(evaluate(...inputs()), after);

  assert.equal(diff.shape, "reshaped");
  assert.equal(diff.tail_offset, null);
  assert.deepEqual(diff.reasons, ["multiple_sessions_restated:2"]);
  assert.deepEqual(
    diff.session_deltas.map((d) => d.delta),
    [0, 10, 10, 0, 0],
  );
  assert.deepEqual(
    diff.session_deltas.filter((d) => d.net_advances_changed).map((d) => d.session_date),
    ["2026-01-06", "2026-01-08"],
  );
  // The endpoint is unchanged even though the line's shape is not.
  assert.equal(after.final_value, 20);
  assert.equal(evaluate(...inputs()).final_value, 20);
});

test("different calendars are not comparable", () => {
  const [c, e] = inputs();
  const shortContract = { ...c, expected_sessions: c.expected_sessions.slice(0, 4) };
  const shortEvents = resequence(e.filter((x) => x.session_sequence !== 5));
  const diff = diffLines(evaluate(...inputs()), evaluate(shortContract, shortEvents));
  assert.equal(diff.shape, "not_comparable");
  assert.deepEqual(diff.reasons, ["session_calendars_differ"]);
  assert.deepEqual(diff.session_deltas, []);
});

// --- refusing to analyse an unpublishable line ----------------------------- //
test("helpers refuse a non-resolved line", () => {
  const [c, e] = inputs();
  const broken = evaluate(c, resequence(e.filter((x) => x.session_sequence !== 3)));
  assert.equal(broken.status, "incomplete");
  assert.ok(broken.causal_prefix_diagnostic.length); // a prefix exists ...
  // ... and is still not an answer.
  assert.throws(() => lineDeltas(broken), BreadthLineValidationError);
  assert.throws(() => rebaseLine(broken, 5), BreadthLineValidationError);
});

test("diff refuses an unpublishable side", () => {
  const [c, e] = inputs();
  e[1]!.is_final = false;
  const provisional = evaluate(c, e);
  assert.throws(() => diffLines(evaluate(...inputs()), provisional), BreadthLineValidationError);
  assert.throws(() => diffLines(provisional, evaluate(...inputs())), BreadthLineValidationError);
});

// --- correction impact: two causal cutoffs over one archive ---------------- //
test("correction impact reports the before and after lines", () => {
  const [c, e] = inputs();
  e.push(correction());
  const impact = correctionImpact(c, e, EARLY_CUTOFF, LATE_CUTOFF);

  assert.equal(impact.before_status, "resolved");
  assert.equal(impact.after_status, "resolved");
  assert.equal(impact.before_final_value, 20);
  assert.equal(impact.after_final_value, 30);
  assert.equal(impact.ignored_future_event_count, 1); // the correction, at the early cutoff
  assert.equal(impact.recompute_from_session, "2026-01-06");
  assert.equal(impact.diff.shape, "constant_offset");
  assert.equal(impact.diff.tail_offset, 10);
});

test("correction impact over a quiet window finds nothing", () => {
  const [c, e] = inputs();
  e.push(correction());
  const impact = correctionImpact(c, e, "2026-01-10T00:00:00Z", EARLY_CUTOFF);
  assert.equal(impact.diff.shape, "identical");
  assert.equal(impact.before_final_value, 20);
  assert.equal(impact.after_final_value, 20);
});

test("correction impact is causal on both sides", () => {
  const [c, e] = inputs();
  e.push(correction());
  const impact = correctionImpact(c, e, EARLY_CUTOFF, LATE_CUTOFF);
  // If the before-side had leaked the correction, its final value would be 30.
  assert.notEqual(impact.before_final_value, impact.after_final_value);
  assert.equal(impact.before_final_value, 20);
});

test("correction impact throws when a cutoff is unpublishable", () => {
  const [c, e] = inputs();
  e.push(correction());
  assert.throws(
    () => correctionImpact(c, e, "2026-01-04T21:00:00Z", LATE_CUTOFF),
    BreadthLineValidationError,
  );
});
