"""Tests for the propagation surface: rebasing, diffing, and correction impact."""

from __future__ import annotations

import pytest
from conftest import EARLY_CUTOFF, LATE_CUTOFF, WORKED, correction, inputs, resequence

from fintech_cumulative_ad_line import (
    BreadthLineValidationError,
    calculate_cumulative_advance_decline_line,
    correction_impact,
    diff_lines,
    line_deltas,
    rebase_line,
)


def evaluate(contract: dict, events: list[dict], cutoff: str = LATE_CUTOFF) -> dict:
    return calculate_cumulative_advance_decline_line(contract, events, cutoff)


def lines(result: dict) -> list[int]:
    return [point["cumulative_line"] for point in result["points"]]


# --------------------------------------------------------------------------- #
# Rebasing: the level is arbitrary, the deltas are not
# --------------------------------------------------------------------------- #
def test_rebase_shifts_every_level_by_a_constant():
    result = evaluate(*inputs())
    rebased = rebase_line(result, 100)
    assert lines(rebased) == [130, 110, 110, 150, 120]
    assert rebased["seed"] == 100
    assert rebased["final_value"] == 120


def test_rebase_leaves_the_deltas_invariant():
    """The whole point: rebasing cannot change the measurement."""

    result = evaluate(*inputs())
    assert line_deltas(rebase_line(result, -5_000)) == line_deltas(result)
    assert line_deltas(result) == WORKED["original"]["net_advances"]


def test_rebasing_matches_recomputing_from_that_seed():
    """Rebasing is not an approximation — it equals a full recompute."""

    contract, events = inputs()
    contract["seed"] = 100
    recomputed = evaluate(contract, events)
    rebased = rebase_line(evaluate(*inputs()), 100)
    assert lines(rebased) == lines(recomputed)
    assert rebased["final_value"] == recomputed["final_value"]


def test_rebase_to_zero_is_idempotent_on_a_zero_seeded_line():
    result = evaluate(*inputs())
    assert lines(rebase_line(result, 0)) == lines(result)


def test_rebase_rejects_a_non_integer_seed():
    result = evaluate(*inputs())
    for bad in (1.5, "100", True, None):
        with pytest.raises(BreadthLineValidationError):
            rebase_line(result, bad)


# --------------------------------------------------------------------------- #
# Diffing: restatement vs. reshaping
# --------------------------------------------------------------------------- #
def test_identical_lines_diff_to_nothing():
    diff = diff_lines(evaluate(*inputs()), evaluate(*inputs()))
    assert diff["shape"] == "identical"
    assert diff["first_divergent_session"] is None
    assert diff["tail_offset"] == 0
    assert diff["changed_session_count"] == 0
    assert all(d["delta"] == 0 for d in diff["session_deltas"])


def test_a_correction_is_a_constant_offset_on_the_tail():
    """The headline case, straight from the worked example."""

    contract, events = inputs()
    events.append(correction())
    diff = diff_lines(evaluate(*inputs()), evaluate(contract, events))

    assert diff["shape"] == "constant_offset"
    assert diff["first_divergent_session"] == "2026-01-06"
    assert diff["tail_offset"] == WORKED["correction"]["delta"] == 10
    assert diff["changed_session_count"] == 4  # sessions 2..5 all moved
    assert diff["reasons"] == ["restated_session:2026-01-06"]

    # Exactly one session's net advances changed; the other four inherited it.
    changed = [d for d in diff["session_deltas"] if d["net_advances_changed"]]
    assert [d["session_date"] for d in changed] == ["2026-01-06"]
    assert [d["delta"] for d in diff["session_deltas"]] == [0, 10, 10, 10, 10]


def test_a_pure_seed_change_is_a_level_shift_not_a_restatement():
    """Distinguishing a bookkeeping change from a data change."""

    contract, events = inputs()
    contract["seed"] = 100
    diff = diff_lines(evaluate(*inputs()), evaluate(contract, events))
    assert diff["shape"] == "constant_offset"
    assert diff["first_divergent_session"] == "2026-01-05"  # shifts from the very first
    assert diff["tail_offset"] == 100
    assert diff["reasons"] == ["level_shift_only"]
    assert not any(d["net_advances_changed"] for d in diff["session_deltas"])


def test_two_restated_sessions_are_reshaped_not_offset():
    """Two corrections do not summarise as one tail offset.

    Here the second restatement (-10 at session 4) exactly cancels the first
    (+10 at session 2), so the *final* value is unchanged at 20 while two
    interior levels moved. A tail offset would have reported 0 and hidden both.
    """

    contract, events = inputs()
    events.append(correction())
    events.append(
        correction(
            event_id="EV-004-R2",
            supersedes_event_id="EV-004-R1",
            ingest_sequence=7,
            session_sequence=4,
            session_date="2026-01-08",
            effective_at="2026-01-08T21:00:00Z",
            available_at="2026-01-10T15:00:00Z",
            advances=60,  # was 65 / 25 (net +40); now 60 / 30 (net +30)
            declines=30,
        )
    )
    after = evaluate(contract, events)
    diff = diff_lines(evaluate(*inputs()), after)

    assert diff["shape"] == "reshaped"
    assert diff["tail_offset"] is None
    assert diff["reasons"] == ["multiple_sessions_restated:2"]
    assert [d["delta"] for d in diff["session_deltas"]] == [0, 10, 10, 0, 0]
    assert [d["session_date"] for d in diff["session_deltas"] if d["net_advances_changed"]] == [
        "2026-01-06",
        "2026-01-08",
    ]
    # The endpoint is unchanged even though the line's shape is not.
    assert after["final_value"] == evaluate(*inputs())["final_value"] == 20


def test_different_calendars_are_not_comparable():
    contract, events = inputs()
    short = resequence([e for e in events if e["session_sequence"] != 5])
    short_contract = {**contract, "expected_sessions": contract["expected_sessions"][:4]}
    diff = diff_lines(evaluate(*inputs()), evaluate(short_contract, short))
    assert diff["shape"] == "not_comparable"
    assert diff["reasons"] == ["session_calendars_differ"]
    assert diff["session_deltas"] == []


# --------------------------------------------------------------------------- #
# Refusing to analyse an unpublishable line
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("helper", [line_deltas, lambda r: rebase_line(r, 5)])
def test_helpers_refuse_a_non_resolved_line(helper):
    """Reading causal_prefix_diagnostic here would launder the uncertainty."""

    contract, events = inputs()
    events = resequence([e for e in events if e["session_sequence"] != 3])
    broken = evaluate(contract, events)
    assert broken["status"] == "incomplete"
    assert broken["causal_prefix_diagnostic"]  # a prefix exists ...
    with pytest.raises(BreadthLineValidationError):  # ... and is still not an answer
        helper(broken)


def test_diff_refuses_an_unpublishable_side():
    contract, events = inputs()
    events[1]["is_final"] = False
    provisional = evaluate(contract, events)
    with pytest.raises(BreadthLineValidationError):
        diff_lines(evaluate(*inputs()), provisional)
    with pytest.raises(BreadthLineValidationError):
        diff_lines(provisional, evaluate(*inputs()))


# --------------------------------------------------------------------------- #
# Correction impact: two causal cutoffs over one archive
# --------------------------------------------------------------------------- #
def test_correction_impact_reports_the_before_and_after_lines():
    contract, events = inputs()
    events.append(correction())
    impact = correction_impact(contract, events, EARLY_CUTOFF, LATE_CUTOFF)

    assert impact["before_status"] == impact["after_status"] == "resolved"
    assert impact["before_final_value"] == 20
    assert impact["after_final_value"] == 30
    assert impact["ignored_future_event_count"] == 1  # the correction, at the early cutoff
    assert impact["recompute_from_session"] == "2026-01-06"
    assert impact["diff"]["shape"] == "constant_offset"
    assert impact["diff"]["tail_offset"] == 10


def test_correction_impact_over_a_quiet_window_finds_nothing():
    contract, events = inputs()
    events.append(correction())
    impact = correction_impact(contract, events, "2026-01-10T00:00:00Z", EARLY_CUTOFF)
    assert impact["diff"]["shape"] == "identical"
    assert impact["before_final_value"] == impact["after_final_value"] == 20


def test_correction_impact_is_causal_on_both_sides():
    """The 'before' line is what was knowable then, not today's data truncated."""

    contract, events = inputs()
    events.append(correction())
    impact = correction_impact(contract, events, EARLY_CUTOFF, LATE_CUTOFF)
    # If the before-side had leaked the correction, its final value would be 30.
    assert impact["before_final_value"] != impact["after_final_value"]
    assert impact["before_final_value"] == 20


def test_correction_impact_raises_when_a_cutoff_is_unpublishable():
    contract, events = inputs()
    events.append(correction())
    with pytest.raises(BreadthLineValidationError):
        correction_impact(contract, events, "2026-01-04T21:00:00Z", LATE_CUTOFF)
