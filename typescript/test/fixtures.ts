/**
 * Shared fixture access — the same JSON the Python suite drives off, which is
 * what makes the cross-language parity claim checkable rather than aspirational.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import type { BreadthEvent, SeriesContract } from "../src/cumulativeAdvanceDeclineLine.ts";

const load = (name: string) =>
  JSON.parse(readFileSync(fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url)), "utf8"));

export const FIXTURE = load("ad_line_fixtures.json");
export const WORKED = load("worked_example.json");

/** The archive is fully known by this instant, correction included. */
export const LATE_CUTOFF = "2026-01-11T00:00:00Z";
/** One hour before the correction becomes available (it lands 2026-01-10T14:00:00Z). */
export const EARLY_CUTOFF = "2026-01-09T23:00:00Z";

/** A fresh, deep-cloned (contract, events) pair — never share mutable fixtures. */
export function inputs(): [SeriesContract, BreadthEvent[]] {
  return [structuredClone(FIXTURE.contract), structuredClone(FIXTURE.base_events)];
}

/** The fixture's revision-2 event for 2026-01-06, optionally patched. */
export function correction(patch: Record<string, unknown> = {}): BreadthEvent {
  return { ...structuredClone(FIXTURE.synthetic_correction), ...patch } as BreadthEvent;
}

/** Renumber ingest_sequence after removing an event, so the archive stays contiguous. */
export function resequence(events: BreadthEvent[]): BreadthEvent[] {
  return events.map((event, index) => ({ ...event, ingest_sequence: index + 1 }));
}
