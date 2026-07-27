# Fintech Cumulative Advance/Decline Line — Market Breadth Algorithm

> A canonical, well-specified, **cross-language (Python + TypeScript)** reference
> implementation of the **Cumulative Advance/Decline Line** — built as a
> **projection over an append-only event archive** rather than a mutable series.
> Because the line is a running sum, one restated session silently offsets *every*
> later value, and a gap invalidates everything after it. This implementation
> refuses to look ahead, refuses to sum across a hole, and ships a
> **correction-propagation** surface that tells you whether your chart moved
> because the market did or because the data did.

<p>
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="TypeScript" src="https://img.shields.io/badge/typescript-5.7%2B-3178c6">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Tests" src="https://img.shields.io/badge/tests-59%20py%20%2F%2058%20ts-brightgreen">
</p>

**📖 Full article (canonical):** **[Cumulative Advance/Decline Line — The Fintech Builder](https://thefintechbuilder.com/market-breadth-and-internals/advance-decline-breadth/cumulative-advance-decline-line/)**

This repository is the runnable, production-oriented companion to that article.
The article teaches the concept; this repo is the code you install and build on.

🧭 **Browse all algorithms:** [Awesome FinTech Algorithms](https://github.com/IslamBaraka90/Fintech-Algorithms-Awesome) — the full index of the library.
🗂️ **This algorithm's domain:** [Market Breadth and Internals](https://thefintechbuilder.com/domains/market-breadth-and-internals/) › **Advance-Decline Breadth**

| | |
|---|---|
| **Catalog topic** | `D04-F01-A03` |
| **Domain** | D04 — Market Breadth and Internals |
| **Family** | D04-F01 — Advance/Decline Breadth |
| **Difficulty** | 2 / 5 |
| **Languages** | Python, TypeScript |
| **Builds on** | [Net Advances](https://github.com/IslamBaraka90/Fintech-Net-Advances-Market-Breadth-algorithm) — this is its running sum |

---

## Table of contents

- [What is the A/D Line?](#what-is-the-ad-line)
- [Two consequences of one recurrence](#two-consequences-of-one-recurrence)
- [The three refusals](#the-three-refusals)
- [Status reference](#status-reference)
- [Why this implementation](#why-this-implementation)
- [Install](#install)
- [Quickstart](#quickstart)
- [Worked example (exact)](#worked-example-exact)
- [Propagation: what did the correction cost?](#propagation-what-did-the-correction-cost)
- [Contract, event & result shapes](#contract-event--result-shapes)
- [API reference](#api-reference)
- [Edge cases & limitations](#edge-cases--limitations)
- [Testing](#testing)
- [Related algorithms](#related-algorithms)
- [License](#license)

---

## What is the A/D Line?

```
line[n] = line[n-1] + (advances[n] - declines[n])
```

A running sum of [Net Advances](https://thefintechbuilder.com/market-breadth-and-internals/advance-decline-breadth/net-advances/).
Where Net Advances describes *one session* and the
[A/D Ratio](https://thefintechbuilder.com/market-breadth-and-internals/advance-decline-breadth/advance-decline-ratio/)
normalizes it, the A/D Line accumulates. That is what makes it useful — a slow
divergence between a rising index and a falling A/D Line is a classic
narrowing-participation signal — and it is also what makes it the hardest of the
three to compute honestly.

## Two consequences of one recurrence

**1. State is history.** Every published value depends on every prior session. So
a correction to one session in the past is not an edit — it's a **recomputation
of the entire suffix**. Treating the line as a mutable series that you append to
is the original sin here; this implementation treats it as a *projection over an
append-only event archive*, rebuilt from the seed on every evaluation.

**2. The level is arbitrary; only the changes are real.** The line begins at a
`seed` that is a bookkeeping choice. A line seeded at 0 and one seeded at 100
describe the same market. Absolute levels across two series are therefore
**never** comparable — which is why the contract carries `seed_lineage_id`, and
why [`rebase_line`](#propagation-what-did-the-correction-cost) exists.

## The three refusals

### 1. Never look ahead

Events whose `available_at` is past the `knowledge_cutoff` are skipped **before
validation**. That ordering matters more than it looks: it means a *malformed*
future event cannot break an earlier query, and a future *correction* cannot leak
into it. The test suite asserts this with six different kinds of poisoned future
event — a series break, a duplicate id, an ingest gap, a broken partition, a
malformed count, an unsupported source state — and the earlier answer is
unchanged in every case.

### 2. Never splice a suffix

If a session is missing, cancelled, or not yet final, the cumulative chain is
broken. The sessions *after* the break are dropped entirely rather than summed
across the hole:

```python
# session 3 of 5 is missing from the archive
result["status"]                    # "incomplete"
result["points"]                    # []        <- nothing is publishable
result["final_value"]               # None
result["causal_prefix_diagnostic"]  # sessions 1-2, for diagnosis only
result["blocked_from_session"]      # "2026-01-07"
```

A sum across a gap looks exactly like a real level and is not one. Note that even
the *contiguous prefix* isn't published — it moves to
`causal_prefix_diagnostic`, so nothing downstream can mistake a partial line for
a complete one. A **cancelled** session is likewise not a session with net `0`;
zero-filling it would invent a flat day.

### 3. Never guess a lineage

Each session's revision chain must be unique and correctly linked
(`revision_number` 1..n, each event naming its parent via `supersedes_event_id`).
Two events competing for the same revision are `ambiguous`, even if one looks
more plausible — repairing correction order is the source steward's job.

## Status reference

| `status` | meaning | `points` |
|---|---|---|
| `resolved` | every declared session has one final, reconciled revision | the line |
| `incomplete` | a session is missing, cancelled, provisional, or the seed isn't available yet | `[]` + diagnostic prefix |
| `ambiguous` | a unique revision chain cannot be selected | `[]` + diagnostic prefix |
| `unsupported` | the archive cannot belong to this continuity contract | `[]` |

The `unsupported` / `incomplete` split is the important one. `incomplete` means
*"come back when the data arrives"*. `unsupported` means *"this archive must
never be spliced into this line at all"* — a different `universe_id`,
`comparison_basis`, or `calendar_id` is a different measurement, and continuing
the sum across it would silently concatenate two incompatible series.

## Why this implementation

- **Event-sourced, not mutable** — corrections are appended as revision events;
  the line is a pure function of (contract, archive, cutoff).
- **`recompute_from_session`** names the earliest session a caller must recompute
  from, so a downstream cache knows exactly what it must invalidate.
- **Sticky provisionality** — `line_is_provisional` is inherited forward: a
  running sum cannot un-inherit doubt about one of its terms.
- **Raise vs. status is deliberate.** Malformed values (bad timestamps,
  impossible dates, `available_at` before `effective_at`) **raise**; modeled
  data-quality problems return a **status**.
- **Inputs are never mutated** — the archive is append-only, and the calculator
  treats it as read-only (asserted in both suites).
- **Cross-language parity** — both suites assert the worked example's two exact
  integer lines.

## Install

**Python**

```bash
pip install fintech-cumulative-ad-line
```

**TypeScript / JavaScript (Node ≥ 20)**

```bash
npm install fintech-cumulative-ad-line
```

## Quickstart

**Python**

```python
from fintech_cumulative_ad_line import calculate_cumulative_advance_decline_line

result = calculate_cumulative_advance_decline_line(contract, events, knowledge_cutoff)

if result["status"] == "resolved":
    publish(result["points"], final=result["final_value"])
else:
    flag(result["status"], result["reasons"], blocked_at=result["blocked_from_session"])

if result["recompute_from_session"]:
    invalidate_cache_from(result["recompute_from_session"])
```

**TypeScript**

```ts
import { calculateCumulativeAdvanceDeclineLine } from "fintech-cumulative-ad-line";

const result = calculateCumulativeAdvanceDeclineLine(contract, events, knowledgeCutoff);
if (result.status === "resolved") publish(result.points);
```

## Worked example (exact)

Five sessions, `seed = 0`, then one correction to session 2 (2026-01-06) that
lands on 2026-01-10T14:00:00Z:

| | 01-05 | 01-06 | 01-07 | 01-08 | 01-09 | final |
|---|--:|--:|--:|--:|--:|--:|
| net advances (original) | `+30` | `−20` | `0` | `+40` | `−30` | |
| **line (original)** | **30** | **10** | **10** | **50** | **20** | `20` |
| net advances (corrected) | `+30` | `−10` | `0` | `+40` | `−30` | |
| **line (corrected)** | **30** | **20** | **20** | **60** | **30** | `30` |
| delta | `0` | `+10` | `+10` | `+10` | `+10` | `+10` |

**One session's net advances changed by +10. Four levels moved by +10.** That
row of deltas is the whole algorithm in one line, and both language suites assert
every integer in this table.

## Propagation: what did the correction cost?

This repo's "beyond the tutorial" surface — the operational questions a single
evaluation can't answer.

| helper | answers |
|---|---|
| `correction_impact` | evaluate one archive at two cutoffs and diff — both sides causal |
| `diff_lines` | classify the change: `identical` / `constant_offset` / `reshaped` / `not_comparable` |
| `rebase_line` | re-express a line from a different seed, so two lines can be overlaid |
| `line_deltas` | the seed-independent content — what you should actually compare |

The classification is the point. A **pure restatement** shows up as a *constant
offset on a tail*, and the offset equals the change in that one session's net
advances. Anything else changed the line's **shape**:

```python
diff = diff_lines(original, corrected)

diff["shape"]                    # "constant_offset"
diff["first_divergent_session"]  # "2026-01-06"
diff["tail_offset"]              # 10
diff["changed_session_count"]    # 4
diff["reasons"]                  # ["restated_session:2026-01-06"]
```

Running the bundled example prints the same thing in both languages:

```
as of 2026-01-09T23:00:00Z: final=20 (ignored 1 future event)
as of 2026-01-11T00:00:00Z: final=30
  -> the line 'moved' because data landed, not because the market did
```

Two details worth noting:

- A **pure seed change** is reported as `constant_offset` with
  `reasons: ["level_shift_only"]` and *no* net-advances change. Bookkeeping, not
  data.
- `reshaped` is not a weaker version of `constant_offset`. When two sessions are
  restated in opposite directions the *endpoint can be unchanged* while interior
  levels move — a tail offset would report `0` and hide both corrections. The
  test suite pins exactly that case.

Every helper **refuses** a non-`resolved` line. A diagnostic prefix exists on
those results, and reading it here would launder precisely the uncertainty the
calculator was careful to preserve.

## Contract, event & result shapes

**Continuity contract** — what is being measured, and where the line starts:

- *Identity (must match every event):* `series_id`, `continuity_id`, `venue_id`,
  `universe_id`, `calendar_id`, `session_type`, `comparison_basis`
- *Seed:* `seed`, `seed_lineage_id`, `seed_effective_at`, `seed_available_at`
- *Calendar:* `expected_sessions[]` — `{session_sequence, session_date}`, which
  must be contiguous from 1 and strictly increasing in date
- *Metric:* `cumulative_issue_count_advance_decline_line`

**Event** — one revision of one session, append-only:

- *Lineage:* `event_id`, `ingest_sequence`, `revision_number`,
  `supersedes_event_id`, `action` (`upsert` | `cancel`)
- *Placement:* `session_sequence`, `session_date`
- *Timing:* `effective_at`, `available_at`, `is_final`
- *Evidence:* `source_evidence_state` (`ready` | `incomplete` | `ambiguous` |
  `unsupported`), `universe_snapshot_id`
- *Counts (on `upsert`):* `advances`, `declines`, `unchanged`, `excluded`,
  `unclassified`, `universe_size`

The partition invariant is enforced per event:

```
advances + declines + unchanged + excluded + unclassified == universe_size
```

…and a `ready` source may not carry `unclassified > 0` — that combination claims
completeness it doesn't have.

**Result:** `status`, `reasons`, `knowledge_cutoff`, `seed`, `points[]`,
`final_value`, `causal_prefix_diagnostic[]`, `missing_sessions[]`,
`cancelled_sessions[]`, `contains_provisional`, `ignored_future_event_count`,
`recompute_from_session`, `blocked_from_session`, `metric`.

**Point:** the session identity, the selected `event_id` / `revision_number` /
`effective_at` / `available_at`, all six counts, `net_advances`,
`cumulative_line`, `source_is_provisional`, `line_is_provisional`, and
`universe_snapshot_id` — enough to audit any published level back to its source
event.

## API reference

| Purpose | Python | TypeScript |
|---|---|---|
| Evaluate the line | `calculate_cumulative_advance_decline_line(c, e, cutoff)` | `calculateCumulativeAdvanceDeclineLine(c, e, cutoff)` |
| Impact of a correction | `correction_impact(c, e, before, after)` | `correctionImpact(c, e, before, after)` |
| Diff two lines | `diff_lines(before, after)` | `diffLines(before, after)` |
| Rebase to a new seed | `rebase_line(result, seed)` | `rebaseLine(result, seed)` |
| Seed-independent deltas | `line_deltas(result)` | `lineDeltas(result)` |
| Errors | `BreadthLineValidationError` | `BreadthLineValidationError` |

## Edge cases & limitations

- **An empty declared calendar is `resolved`**, not an error — the line is
  exactly the seed.
- **A cancelled session is not net `0`.** Zero-filling would invent a flat day;
  it breaks the chain instead.
- **`seed_not_yet_available`** blocks the entire line: if the starting level
  wasn't knowable at the cutoff, no level after it was either.
- **Events are never sorted.** `ingest_sequence` must be contiguous and
  `available_at` non-decreasing; an out-of-order archive is `unsupported` rather
  than silently repaired.
- **Cumulative overflow raises** — the running sum is bounds-checked against the
  safe-integer range at every step.
- **Levels are not comparable across series.** Always compare `line_deltas`, or
  `rebase_line` both sides to a common seed first.
- **Not a signal.** Breadth describes participation. A/D Line divergence is a
  well-known *observation*, not a forecast.

## Testing

**Python** (59 tests)

```bash
cd python && pip install -e ".[dev]" && pytest
```

**TypeScript** (58 tests, zero runtime dependencies)

```bash
cd typescript && npm install && npm test && npm run build
```

Both suites walk the same `ad_line_fixtures.json` and `worked_example.json`, and
assert the worked example's two integer lines exactly. That shared pair of files
is what makes the cross-language parity claim checkable rather than aspirational.

## Related algorithms

- `D04-F01-A01` — [Net Advances](https://github.com/IslamBaraka90/Fintech-Net-Advances-Market-Breadth-algorithm) (the per-session term this sums)
- `D04-F01-A02` — [Advance/Decline Ratio](https://github.com/IslamBaraka90/Fintech-Advance-Decline-Ratio-Market-Breadth-algorithm) (the scale-free alternative)
- `D04-F01-A04` — [Normalized A/D Line](https://thefintechbuilder.com/market-breadth-and-internals/advance-decline-breadth/normalized-advance-decline-line/) ·
  `A05` — [Absolute Breadth Index](https://thefintechbuilder.com/market-breadth-and-internals/advance-decline-breadth/absolute-breadth-index/)
- `D04-F02-A01` — [Traditional McClellan Oscillator](https://thefintechbuilder.com/market-breadth-and-internals/mcclellan-family/traditional-mcclellan-oscillator/)
- `D07-F01-A02` — [EMA](https://github.com/IslamBaraka90/Fintech-EMA-Exponential-Moving-Average-algorithm) (the smoothing the McClellan family uses)

Full index: **[Awesome FinTech Algorithms](https://github.com/IslamBaraka90/Fintech-Algorithms-Awesome)**.

## License

[MIT](./LICENSE) © The Fintech Builder. Part of the
[100 FinTech Algorithms](https://thefintechbuilder.com) library.
