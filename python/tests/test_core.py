"""Contract tests for the causal, revision-aware Cumulative A/D Line.

The worked example's two lines are the cross-language acceptance anchor:

    original  net [30, -20, 0, 40, -30]  ->  line [30, 10, 10, 50, 20]
    corrected net [30, -10, 0, 40, -30]  ->  line [30, 20, 20, 60, 30]

The correction changes ONE session by +10 and every later level by +10. Both
language suites assert both lines exactly.
"""

from __future__ import annotations

import copy
import json

import pytest
from conftest import EARLY_CUTOFF, LATE_CUTOFF, WORKED, correction, inputs, resequence

from fintech_cumulative_ad_line import (
    MAX_SAFE_INTEGER,
    BreadthLineValidationError,
    calculate_cumulative_advance_decline_line,
)


def evaluate(contract: dict, events: list[dict], cutoff: str = LATE_CUTOFF) -> dict:
    return calculate_cumulative_advance_decline_line(contract, events, cutoff)


def lines(result: dict) -> list[int]:
    return [point["cumulative_line"] for point in result["points"]]


# --------------------------------------------------------------------------- #
# The worked example, exactly
# --------------------------------------------------------------------------- #
def test_resolved_line_matches_the_worked_example():
    contract, events = inputs()
    result = evaluate(contract, events)
    assert result["status"] == "resolved"
    assert [p["net_advances"] for p in result["points"]] == WORKED["original"]["net_advances"]
    assert lines(result) == WORKED["original"]["cumulative_line"]
    assert result["final_value"] == 20
    assert result["reasons"] == []


def test_visible_correction_recomputes_the_entire_suffix():
    """The defining property: one restated session moves every later level."""

    contract, events = inputs()
    events.append(correction())
    result = evaluate(contract, events)
    assert lines(result) == WORKED["correction"]["recomputed_line"] == [30, 20, 20, 60, 30]
    assert result["recompute_from_session"] == WORKED["correction"]["session_date"] == "2026-01-06"
    # One session's net changed by +10; four later levels moved by the same +10.
    assert WORKED["correction"]["delta"] == 10


def test_seed_translates_every_level_and_no_delta():
    """The level is a bookkeeping choice; the deltas are the measurement."""

    contract, events = inputs()
    baseline = evaluate(*inputs())
    contract["seed"] = 100
    shifted = evaluate(contract, events)
    assert lines(shifted) == [130, 110, 110, 150, 120]
    assert [p["net_advances"] for p in shifted["points"]] == [
        p["net_advances"] for p in baseline["points"]
    ]


# --------------------------------------------------------------------------- #
# Causality: the future cannot reach backwards
# --------------------------------------------------------------------------- #
def test_future_correction_is_ignored_at_an_earlier_cutoff():
    contract, events = inputs()
    events.append(correction())
    result = evaluate(contract, events, EARLY_CUTOFF)
    assert result["status"] == "resolved"
    assert result["ignored_future_event_count"] == 1
    assert lines(result) == [30, 10, 10, 50, 20]


@pytest.mark.parametrize(
    "patch",
    [
        {"series_id": "FUTURE-WRONG-SERIES"},
        {"event_id": "EV-001-R1"},
        {"ingest_sequence": 999},
        {"universe_size": 999},
        {"advances": "not-an-integer"},
        {"source_evidence_state": "unsupported"},
    ],
    ids=["series-break", "duplicate-id", "ingest-gap", "bad-partition", "malformed-count", "bad-state"],
)
def test_a_malformed_future_event_is_not_even_validated(patch: dict):
    """Skipping future events BEFORE validation is what makes this hold."""

    contract, events = inputs()
    events.append(correction(**patch))
    result = evaluate(contract, events, EARLY_CUTOFF)
    assert result["status"] == "resolved"
    assert result["ignored_future_event_count"] == 1
    assert result["final_value"] == 20


def test_seed_unavailable_at_cutoff_blocks_the_whole_line():
    contract, events = inputs()
    result = evaluate(contract, events, "2026-01-04T21:00:00Z")
    assert result["status"] == "incomplete"
    assert result["reasons"] == ["seed_not_yet_available"]
    assert result["blocked_from_session"] == "2026-01-05"


# --------------------------------------------------------------------------- #
# Never splice a suffix across a break
# --------------------------------------------------------------------------- #
def test_missing_middle_session_returns_only_a_diagnostic_prefix():
    contract, events = inputs()
    events = resequence([e for e in events if e["session_sequence"] != 3])
    result = evaluate(contract, events)
    assert result["status"] == "incomplete"
    assert result["missing_sessions"] == ["2026-01-07"]
    # The two known sessions are NOT publishable, and 01-08/01-09 are dropped
    # entirely rather than summed across the hole.
    assert result["points"] == []
    assert result["final_value"] is None
    assert [p["session_sequence"] for p in result["causal_prefix_diagnostic"]] == [1, 2]
    assert result["blocked_from_session"] == "2026-01-07"


def test_cancelled_session_is_not_zero_filled():
    """A cancelled session is not a session with net 0 — it breaks the chain."""

    contract, events = inputs()
    events.append(
        correction(
            event_id="EV-003-R2",
            supersedes_event_id="EV-003-R1",
            session_sequence=3,
            session_date="2026-01-07",
            action="cancel",
            available_at="2026-01-10T14:00:00Z",
        )
    )
    result = evaluate(contract, events)
    assert result["status"] == "incomplete"
    assert result["cancelled_sessions"] == ["2026-01-07"]
    assert result["points"] == []
    assert len(result["causal_prefix_diagnostic"]) == 2


def test_provisional_source_taints_every_dependent_level():
    """line_is_provisional is sticky: a running sum cannot un-inherit doubt."""

    contract, events = inputs()
    events[1]["is_final"] = False
    result = evaluate(contract, events)
    assert result["status"] == "incomplete"
    assert result["contains_provisional"] is True
    assert result["points"] == []
    assert [p["source_is_provisional"] for p in result["causal_prefix_diagnostic"]] == [
        False, True, False, False, False,
    ]
    assert [p["line_is_provisional"] for p in result["causal_prefix_diagnostic"]] == [
        False, True, True, True, True,
    ]


@pytest.mark.parametrize("state", ["incomplete", "ambiguous", "unsupported"])
def test_non_ready_daily_evidence_propagates_its_own_status(state: str):
    contract, events = inputs()
    events[2]["source_evidence_state"] = state
    result = evaluate(contract, events)
    assert result["status"] == state
    assert result["points"] == []
    assert result["final_value"] is None
    assert [p["session_sequence"] for p in result["causal_prefix_diagnostic"]] == [1, 2]


def test_empty_declared_calendar_is_a_resolved_seed_state():
    """No sessions is not an error — the line is exactly the seed."""

    contract, _ = inputs()
    contract["expected_sessions"] = []
    result = evaluate(contract, [])
    assert result["status"] == "resolved"
    assert result["points"] == []
    assert result["final_value"] == contract["seed"]


# --------------------------------------------------------------------------- #
# Never guess a lineage
# --------------------------------------------------------------------------- #
def test_branching_revision_lineage_is_ambiguous():
    """Two events claiming revision 2 of one session: unresolvable, not a guess."""

    contract, events = inputs()
    first = correction()
    second = correction(
        event_id="EV-002-R2-B", ingest_sequence=7, available_at="2026-01-10T14:00:01Z"
    )
    events.extend([first, second])
    result = evaluate(contract, events)
    assert result["status"] == "ambiguous"
    assert result["reasons"] == ["non_unique_revision_lineage:2026-01-06"]


def test_duplicate_event_identity_is_ambiguous():
    contract, events = inputs()
    events.append(correction(event_id="EV-001-R1"))
    result = evaluate(contract, events)
    assert result["status"] == "ambiguous"
    assert result["reasons"] == ["duplicate_event_id"]


# --------------------------------------------------------------------------- #
# Continuity contract: what may not be spliced into this line at all
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "field",
    ["series_id", "continuity_id", "venue_id", "universe_id", "calendar_id", "session_type", "comparison_basis"],
)
def test_identity_drift_requires_a_series_break(field: str):
    """Changing what is being measured mid-line is not a correction."""

    contract, events = inputs()
    events[2][field] = "DIFFERENT"
    result = evaluate(contract, events)
    assert result["status"] == "unsupported"
    assert "series_break_required" in result["reasons"]


def test_event_outside_the_declared_calendar_is_unsupported():
    contract, events = inputs()
    events[0]["session_date"] = "2026-01-03"
    assert "event_outside_expected_calendar" in evaluate(contract, events)["reasons"]


def test_ingest_sequence_gap_is_unsupported():
    contract, events = inputs()
    events[2]["ingest_sequence"] = 8
    assert "event_sequence_not_contiguous" in evaluate(contract, events)["reasons"]


def test_events_out_of_availability_order_are_unsupported():
    contract, events = inputs()
    events[2]["available_at"] = "2026-01-05T21:00:04Z"
    events[2]["effective_at"] = "2026-01-05T21:00:03Z"
    assert "events_not_in_ingest_order" in evaluate(contract, events)["reasons"]


def test_expected_calendar_must_be_contiguous():
    contract, events = inputs()
    contract["expected_sessions"][2]["session_sequence"] = 9
    assert evaluate(contract, events)["reasons"] == ["expected_session_sequence_not_contiguous"]


def test_unsupported_metric_is_explicit():
    contract, events = inputs()
    contract["metric"] = "volume_weighted_line"
    result = evaluate(contract, events)
    assert result["status"] == "unsupported"
    assert result["reasons"] == ["unsupported_metric"]


# --------------------------------------------------------------------------- #
# Raise vs. status: a caller bug is not a data gap
# --------------------------------------------------------------------------- #
def test_unreconciled_partition_is_unsupported():
    contract, events = inputs()
    events[0]["universe_size"] = 101
    assert "unreconciled_issue_partition" in evaluate(contract, events)["reasons"]


def test_a_ready_source_cannot_hide_unclassified_issues():
    contract, events = inputs()
    events[2]["unclassified"] = 1
    events[2]["excluded"] = 1
    result = evaluate(contract, events)
    assert result["status"] == "unsupported"
    assert "ready_source_contains_unclassified_issues" in result["reasons"]


def test_cumulative_overflow_is_rejected():
    contract, events = inputs()
    contract["seed"] = MAX_SAFE_INTEGER
    with pytest.raises(BreadthLineValidationError):
        evaluate(contract, events)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("available_at", "bad"),
        ("effective_at", "2026-01-05"),
        ("session_date", "2026-02-30"),
        ("session_date", "not-a-date"),
    ],
)
def test_malformed_time_values_raise(field: str, value: str):
    contract, events = inputs()
    events[0][field] = value
    with pytest.raises(BreadthLineValidationError):
        evaluate(contract, events)


def test_available_at_before_effective_at_raises():
    contract, events = inputs()
    events[0]["available_at"] = "2026-01-05T20:00:00Z"
    events[0]["effective_at"] = "2026-01-05T21:00:00Z"
    with pytest.raises(BreadthLineValidationError):
        evaluate(contract, events)


def test_inputs_are_never_mutated():
    """The archive is append-only; the calculator must treat it as read-only."""

    contract, events = inputs()
    before = json.dumps([contract, events], sort_keys=True)
    evaluate(contract, events)
    assert json.dumps([contract, events], sort_keys=True) == before


def test_result_carries_the_audit_trail():
    contract, events = inputs()
    result = evaluate(contract, events)
    assert result["metric"] == "cumulative_issue_count_advance_decline_line"
    assert result["knowledge_cutoff"] == LATE_CUTOFF
    assert result["seed"] == 0
    first = result["points"][0]
    assert first["event_id"] == "EV-001-R1"
    assert first["revision_number"] == 1
    assert first["source_evidence_state"] == "ready"
    assert first["universe_snapshot_id"]
    assert (
        first["advances"] + first["declines"] + first["unchanged"]
        + first["excluded"] + first["unclassified"] == first["universe_size"]
    )
