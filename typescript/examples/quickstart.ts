/**
 * Quickstart: a cumulative line, a correction, and what the correction cost.
 *
 * Run:  node --experimental-strip-types examples/quickstart.ts
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  calculateCumulativeAdvanceDeclineLine,
  type BreadthEvent,
  type BreadthLineResult,
  type SeriesContract,
} from "../src/cumulativeAdvanceDeclineLine.ts";
import { correctionImpact, diffLines, lineDeltas, rebaseLine } from "../src/propagation.ts";

const FIXTURE = JSON.parse(
  readFileSync(fileURLToPath(new URL("../test/fixtures/ad_line_fixtures.json", import.meta.url)), "utf8"),
);
const CONTRACT: SeriesContract = FIXTURE.contract;
const EVENTS: BreadthEvent[] = FIXTURE.base_events;
const CORRECTION: BreadthEvent = FIXTURE.synthetic_correction;

const EARLY = "2026-01-09T23:00:00Z"; // before the correction was published
const LATE = "2026-01-11T00:00:00Z"; // after it was published

const nets = (r: BreadthLineResult) => r.points.map((p) => p.net_advances);
const lines = (r: BreadthLineResult) => r.points.map((p) => p.cumulative_line);

// 1) The line as first published.
const original = calculateCumulativeAdvanceDeclineLine(CONTRACT, EVENTS, LATE);
console.log(`original (${original.status}):`);
console.log(`  net  [${nets(original)}]`);
console.log(`  line [${lines(original)}]  final=${original.final_value}`);

// 2) A correction to session 2 arrives. ONE session changes; every later level moves.
const archive = [...structuredClone(EVENTS), structuredClone(CORRECTION)];
const corrected = calculateCumulativeAdvanceDeclineLine(CONTRACT, archive, LATE);
console.log(`\nafter correction to ${CORRECTION.session_date}:`);
console.log(`  net  [${nets(corrected)}]`);
console.log(`  line [${lines(corrected)}]  final=${corrected.final_value}`);
console.log(`  recompute from: ${corrected.recompute_from_session}`);

// 3) The diff names it as a restatement, not a market move.
const diff = diffLines(original, corrected);
console.log(
  `\ndiff: ${diff.shape} of ${diff.tail_offset! > 0 ? "+" : ""}${diff.tail_offset} from ` +
    `${diff.first_divergent_session} (${diff.changed_session_count} levels moved, [${diff.reasons}])`,
);

// 4) The same archive read at two knowledge cutoffs — both causal.
const impact = correctionImpact(CONTRACT, archive, EARLY, LATE);
console.log(
  `\nas of ${EARLY}: final=${impact.before_final_value} ` +
    `(ignored ${impact.ignored_future_event_count} future event)`,
);
console.log(`as of ${LATE}: final=${impact.after_final_value}`);
console.log("  -> the line 'moved' because data landed, not because the market did");

// 5) The level is a bookkeeping choice; the deltas are the measurement.
const rebased = rebaseLine(original, 1000);
console.log(`\nrebased to 1000: [${lines(rebased)}]`);
console.log(
  `deltas unchanged: ${JSON.stringify(lineDeltas(rebased)) === JSON.stringify(lineDeltas(original))}`,
);

// 6) A break in the chain drops the suffix rather than summing across the hole.
const gapped = structuredClone(EVENTS)
  .filter((event) => event.session_sequence !== 3)
  .map((event, index) => ({ ...event, ingest_sequence: index + 1 }));
const broken = calculateCumulativeAdvanceDeclineLine(CONTRACT, gapped, LATE);
console.log(
  `\nsession 3 missing -> status=${broken.status} points=[${broken.points}] final=${broken.final_value}`,
);
console.log(
  `  diagnostic prefix only: sessions [${broken.causal_prefix_diagnostic.map((p) => p.session_sequence)}]`,
);
