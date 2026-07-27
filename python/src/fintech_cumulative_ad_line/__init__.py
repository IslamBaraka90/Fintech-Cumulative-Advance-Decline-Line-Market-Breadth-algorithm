"""Fintech Cumulative Advance/Decline Line — causal, revision-aware breadth line.

A small, well-specified, cross-language reference implementation of the
Cumulative A/D Line (``line[n] = line[n-1] + advances - declines``) built as a
**projection over an append-only event archive** rather than a mutable series:
corrections arrive as revision events, the line is rebuilt from an explicit seed,
and nothing that was unavailable at the knowledge cutoff can influence the answer.

Because the line is a running sum, a break in the chain is not a missing point —
it invalidates every point after it. This implementation drops the suffix rather
than summing across the hole, and never publishes a level it cannot support.

Companion article (canonical): https://thefintechbuilder.com/market-breadth-and-internals/advance-decline-breadth/cumulative-advance-decline-line/
Catalog topic id: D04-F01-A03  (Domain D04 — Market Breadth and Internals / Family D04-F01 — Advance/Decline Breadth)
"""

from __future__ import annotations

from .core import (
    MAX_SAFE_INTEGER,
    BreadthLinePoint,
    BreadthLineResult,
    BreadthLineValidationError,
    calculate_cumulative_advance_decline_line,
)
from .propagation import (
    CorrectionImpact,
    LineDiff,
    SessionDelta,
    correction_impact,
    diff_lines,
    line_deltas,
    rebase_line,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "MAX_SAFE_INTEGER",
    "BreadthLineValidationError",
    "BreadthLinePoint",
    "BreadthLineResult",
    "calculate_cumulative_advance_decline_line",
    "CorrectionImpact",
    "LineDiff",
    "SessionDelta",
    "correction_impact",
    "diff_lines",
    "line_deltas",
    "rebase_line",
]
