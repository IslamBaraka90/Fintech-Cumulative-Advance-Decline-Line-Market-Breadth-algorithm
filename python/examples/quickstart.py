"""Quickstart: a cumulative line, a correction, and what the correction cost.

Run:  python examples/quickstart.py
"""

import copy
import json
from pathlib import Path

from fintech_cumulative_ad_line import (
    calculate_cumulative_advance_decline_line,
    correction_impact,
    diff_lines,
    line_deltas,
    rebase_line,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
FIXTURE = json.loads((FIXTURES / "ad_line_fixtures.json").read_text(encoding="utf-8"))
CONTRACT, EVENTS = FIXTURE["contract"], FIXTURE["base_events"]
CORRECTION = FIXTURE["synthetic_correction"]

EARLY = "2026-01-09T23:00:00Z"  # before the correction was published
LATE = "2026-01-11T00:00:00Z"   # after it was published

# 1) The line as first published.
original = calculate_cumulative_advance_decline_line(CONTRACT, EVENTS, LATE)
print(f"original ({original['status']}):")
print(f"  net  {[p['net_advances'] for p in original['points']]}")
print(f"  line {[p['cumulative_line'] for p in original['points']]}  final={original['final_value']}")

# 2) A correction to session 2 arrives. ONE session changes; every later level moves.
archive = copy.deepcopy(EVENTS) + [copy.deepcopy(CORRECTION)]
corrected = calculate_cumulative_advance_decline_line(CONTRACT, archive, LATE)
print(f"\nafter correction to {CORRECTION['session_date']}:")
print(f"  net  {[p['net_advances'] for p in corrected['points']]}")
print(f"  line {[p['cumulative_line'] for p in corrected['points']]}  final={corrected['final_value']}")
print(f"  recompute from: {corrected['recompute_from_session']}")

# 3) The diff names it as a restatement, not a market move.
diff = diff_lines(original, corrected)
print(f"\ndiff: {diff['shape']} of {diff['tail_offset']:+d} from {diff['first_divergent_session']} "
      f"({diff['changed_session_count']} levels moved, {diff['reasons']})")

# 4) The same archive read at two knowledge cutoffs — both causal.
impact = correction_impact(CONTRACT, archive, EARLY, LATE)
print(f"\nas of {EARLY}: final={impact['before_final_value']} "
      f"(ignored {impact['ignored_future_event_count']} future event)")
print(f"as of {LATE}: final={impact['after_final_value']}")
print("  -> the line 'moved' because data landed, not because the market did")

# 5) The level is a bookkeeping choice; the deltas are the measurement.
rebased = rebase_line(original, 1000)
print(f"\nrebased to 1000: {[p['cumulative_line'] for p in rebased['points']]}")
print(f"deltas unchanged: {line_deltas(rebased) == line_deltas(original)}")

# 6) A break in the chain drops the suffix rather than summing across the hole.
gapped = [e for e in copy.deepcopy(EVENTS) if e["session_sequence"] != 3]
for index, event in enumerate(gapped, start=1):
    event["ingest_sequence"] = index
broken = calculate_cumulative_advance_decline_line(CONTRACT, gapped, LATE)
print(f"\nsession 3 missing -> status={broken['status']} points={broken['points']} "
      f"final={broken['final_value']}")
print(f"  diagnostic prefix only: sessions {[p['session_sequence'] for p in broken['causal_prefix_diagnostic']]}")
