/**
 * Contract tests for the causal, revision-aware Cumulative A/D Line.
 *
 * The worked example's two lines are the cross-language acceptance anchor:
 *
 *     original  net [30, -20, 0, 40, -30]  ->  line [30, 10, 10, 50, 20]
 *     corrected net [30, -10, 0, 40, -30]  ->  line [30, 20, 20, 60, 30]
 *
 * The correction changes ONE session by +10 and every later level by +10. Both
 * language suites assert both lines exactly.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BreadthLineValidationError,
  calculateCumulativeAdvanceDeclineLine,
  type BreadthEvent,
  type BreadthLineResult,
  type SeriesContract,
} from "../src/cumulativeAdvanceDeclineLine.ts";
import { EARLY_CUTOFF, LATE_CUTOFF, WORKED, correction, inputs, resequence } from "./fixtures.ts";

const evaluate = (
  contract: SeriesContract,
  events: BreadthEvent[],
  cutoff = LATE_CUTOFF,
): BreadthLineResult => calculateCumulativeAdvanceDeclineLine(contract, events, cutoff);

const lines = (result: BreadthLineResult) => result.points.map((p) => p.cumulative_line);

// --- the worked example, exactly ------------------------------------------- //
test("resolved line matches the worked example", () => {
  const r = evaluate(...inputs());
  assert.equal(r.status, "resolved");
  assert.deepEqual(
    r.points.map((p) => p.net_advances),
    WORKED.original.net_advances,
  );
  assert.deepEqual(lines(r), WORKED.original.cumulative_line);
  assert.equal(r.final_value, 20);
  assert.deepEqual(r.reasons, []);
});

test("visible correction recomputes the entire suffix", () => {
  const [c, e] = inputs();
  e.push(correction());
  const r = evaluate(c, e);
  assert.deepEqual(lines(r), WORKED.correction.recomputed_line);
  assert.deepEqual(lines(r), [30, 20, 20, 60, 30]);
  assert.equal(r.recompute_from_session, WORKED.correction.session_date);
  assert.equal(r.recompute_from_session, "2026-01-06");
  assert.equal(WORKED.correction.delta, 10);
});

test("seed translates every level and no delta", () => {
  const baseline = evaluate(...inputs());
  const [c, e] = inputs();
  c.seed = 100;
  const shifted = evaluate(c, e);
  assert.deepEqual(lines(shifted), [130, 110, 110, 150, 120]);
  assert.deepEqual(
    shifted.points.map((p) => p.net_advances),
    baseline.points.map((p) => p.net_advances),
  );
});

// --- causality: the future cannot reach backwards -------------------------- //
test("future correction is ignored at an earlier cutoff", () => {
  const [c, e] = inputs();
  e.push(correction());
  const r = evaluate(c, e, EARLY_CUTOFF);
  assert.equal(r.status, "resolved");
  assert.equal(r.ignored_future_event_count, 1);
  assert.deepEqual(lines(r), [30, 10, 10, 50, 20]);
});

for (const [name, patch] of Object.entries({
  "series break": { series_id: "FUTURE-WRONG-SERIES" },
  "duplicate id": { event_id: "EV-001-R1" },
  "ingest gap": { ingest_sequence: 999 },
  "bad partition": { universe_size: 999 },
  "malformed count": { advances: "not-an-integer" },
  "bad source state": { source_evidence_state: "unsupported" },
})) {
  test(`a malformed future event is not even validated (${name})`, () => {
    const [c, e] = inputs();
    e.push(correction(patch));
    const r = evaluate(c, e, EARLY_CUTOFF);
    assert.equal(r.status, "resolved");
    assert.equal(r.ignored_future_event_count, 1);
    assert.equal(r.final_value, 20);
  });
}

test("seed unavailable at cutoff blocks the whole line", () => {
  const [c, e] = inputs();
  const r = evaluate(c, e, "2026-01-04T21:00:00Z");
  assert.equal(r.status, "incomplete");
  assert.deepEqual(r.reasons, ["seed_not_yet_available"]);
  assert.equal(r.blocked_from_session, "2026-01-05");
});

// --- never splice a suffix across a break ---------------------------------- //
test("missing middle session returns only a diagnostic prefix", () => {
  const [c, e] = inputs();
  const r = evaluate(c, resequence(e.filter((x) => x.session_sequence !== 3)));
  assert.equal(r.status, "incomplete");
  assert.deepEqual(r.missing_sessions, ["2026-01-07"]);
  assert.deepEqual(r.points, []);
  assert.equal(r.final_value, null);
  assert.deepEqual(
    r.causal_prefix_diagnostic.map((p) => p.session_sequence),
    [1, 2],
  );
  assert.equal(r.blocked_from_session, "2026-01-07");
});

test("cancelled session is not zero-filled", () => {
  const [c, e] = inputs();
  e.push(
    correction({
      event_id: "EV-003-R2",
      supersedes_event_id: "EV-003-R1",
      session_sequence: 3,
      session_date: "2026-01-07",
      action: "cancel",
      available_at: "2026-01-10T14:00:00Z",
    }),
  );
  const r = evaluate(c, e);
  assert.equal(r.status, "incomplete");
  assert.deepEqual(r.cancelled_sessions, ["2026-01-07"]);
  assert.deepEqual(r.points, []);
  assert.equal(r.causal_prefix_diagnostic.length, 2);
});

test("provisional source taints every dependent level", () => {
  const [c, e] = inputs();
  e[1]!.is_final = false;
  const r = evaluate(c, e);
  assert.equal(r.status, "incomplete");
  assert.equal(r.contains_provisional, true);
  assert.deepEqual(r.points, []);
  assert.deepEqual(
    r.causal_prefix_diagnostic.map((p) => p.source_is_provisional),
    [false, true, false, false, false],
  );
  assert.deepEqual(
    r.causal_prefix_diagnostic.map((p) => p.line_is_provisional),
    [false, true, true, true, true],
  );
});

for (const state of ["incomplete", "ambiguous", "unsupported"] as const) {
  test(`non-ready daily evidence propagates its own status (${state})`, () => {
    const [c, e] = inputs();
    e[2]!.source_evidence_state = state;
    const r = evaluate(c, e);
    assert.equal(r.status, state);
    assert.deepEqual(r.points, []);
    assert.equal(r.final_value, null);
    assert.deepEqual(
      r.causal_prefix_diagnostic.map((p) => p.session_sequence),
      [1, 2],
    );
  });
}

test("empty declared calendar is a resolved seed state", () => {
  const [c] = inputs();
  c.expected_sessions = [];
  const r = evaluate(c, []);
  assert.equal(r.status, "resolved");
  assert.deepEqual(r.points, []);
  assert.equal(r.final_value, c.seed);
});

// --- never guess a lineage ------------------------------------------------- //
test("branching revision lineage is ambiguous", () => {
  const [c, e] = inputs();
  e.push(
    correction(),
    correction({
      event_id: "EV-002-R2-B",
      ingest_sequence: 7,
      available_at: "2026-01-10T14:00:01Z",
    }),
  );
  const r = evaluate(c, e);
  assert.equal(r.status, "ambiguous");
  assert.deepEqual(r.reasons, ["non_unique_revision_lineage:2026-01-06"]);
});

test("duplicate event identity is ambiguous", () => {
  const [c, e] = inputs();
  e.push(correction({ event_id: "EV-001-R1" }));
  const r = evaluate(c, e);
  assert.equal(r.status, "ambiguous");
  assert.deepEqual(r.reasons, ["duplicate_event_id"]);
});

// --- continuity contract -------------------------------------------------- //
for (const field of [
  "series_id",
  "continuity_id",
  "venue_id",
  "universe_id",
  "calendar_id",
  "session_type",
  "comparison_basis",
] as const) {
  test(`identity drift requires a series break (${field})`, () => {
    const [c, e] = inputs();
    e[2]![field] = "DIFFERENT";
    const r = evaluate(c, e);
    assert.equal(r.status, "unsupported");
    assert.ok(r.reasons.includes("series_break_required"));
  });
}

test("event outside the declared calendar is unsupported", () => {
  const [c, e] = inputs();
  e[0]!.session_date = "2026-01-03";
  assert.ok(evaluate(c, e).reasons.includes("event_outside_expected_calendar"));
});

test("ingest sequence gap is unsupported", () => {
  const [c, e] = inputs();
  e[2]!.ingest_sequence = 8;
  assert.ok(evaluate(c, e).reasons.includes("event_sequence_not_contiguous"));
});

test("events out of availability order are unsupported", () => {
  const [c, e] = inputs();
  e[2]!.available_at = "2026-01-05T21:00:04Z";
  e[2]!.effective_at = "2026-01-05T21:00:03Z";
  assert.ok(evaluate(c, e).reasons.includes("events_not_in_ingest_order"));
});

test("expected calendar must be contiguous", () => {
  const [c, e] = inputs();
  c.expected_sessions[2]!.session_sequence = 9;
  assert.deepEqual(evaluate(c, e).reasons, ["expected_session_sequence_not_contiguous"]);
});

test("unsupported metric is explicit", () => {
  const [c, e] = inputs();
  c.metric = "volume_weighted_line";
  const r = evaluate(c, e);
  assert.equal(r.status, "unsupported");
  assert.deepEqual(r.reasons, ["unsupported_metric"]);
});

// --- raise vs. status ----------------------------------------------------- //
test("unreconciled partition is unsupported", () => {
  const [c, e] = inputs();
  e[0]!.universe_size = 101;
  assert.ok(evaluate(c, e).reasons.includes("unreconciled_issue_partition"));
});

test("a ready source cannot hide unclassified issues", () => {
  const [c, e] = inputs();
  e[2]!.unclassified = 1;
  e[2]!.excluded = 1;
  const r = evaluate(c, e);
  assert.equal(r.status, "unsupported");
  assert.ok(r.reasons.includes("ready_source_contains_unclassified_issues"));
});

test("cumulative overflow is rejected", () => {
  const [c, e] = inputs();
  c.seed = Number.MAX_SAFE_INTEGER;
  assert.throws(() => evaluate(c, e), BreadthLineValidationError);
});

for (const [field, value] of [
  ["available_at", "bad"],
  ["effective_at", "2026-01-05"],
  ["session_date", "2026-02-30"],
  ["session_date", "not-a-date"],
] as const) {
  test(`malformed time value raises (${field}=${value})`, () => {
    const [c, e] = inputs();
    (e[0] as unknown as Record<string, unknown>)[field] = value;
    assert.throws(() => evaluate(c, e), BreadthLineValidationError);
  });
}

test("available_at before effective_at raises", () => {
  const [c, e] = inputs();
  e[0]!.available_at = "2026-01-05T20:00:00Z";
  e[0]!.effective_at = "2026-01-05T21:00:00Z";
  assert.throws(() => evaluate(c, e), BreadthLineValidationError);
});

test("inputs are never mutated", () => {
  const [c, e] = inputs();
  const before = JSON.stringify([c, e]);
  evaluate(c, e);
  assert.equal(JSON.stringify([c, e]), before);
});

test("result carries the audit trail", () => {
  const r = evaluate(...inputs());
  assert.equal(r.metric, "cumulative_issue_count_advance_decline_line");
  assert.equal(r.knowledge_cutoff, LATE_CUTOFF);
  assert.equal(r.seed, 0);
  const first = r.points[0]!;
  assert.equal(first.event_id, "EV-001-R1");
  assert.equal(first.revision_number, 1);
  assert.equal(first.source_evidence_state, "ready");
  assert.ok(first.universe_snapshot_id);
  assert.equal(
    first.advances + first.declines + first.unchanged + first.excluded + first.unclassified,
    first.universe_size,
  );
});
