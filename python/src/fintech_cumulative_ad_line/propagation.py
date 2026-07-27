"""Correction propagation and rebasing — the operational half of a cumulative line.

The calculator in :mod:`.core` answers *"what is the line as of one knowledge
cutoff?"*. Running a cumulative series in production asks two further questions
that a single evaluation cannot:

**"A correction landed. What did it cost me?"**  Because the line is a running
sum, a restatement of one session shifts every later value by the same constant.
A chart that dropped 10 points overnight may have recorded no market event at
all. :func:`diff_lines` and :func:`correction_impact` separate the two: a
**pure restatement** is a constant offset on a tail, and its ``delta`` equals the
change in that one session's net advances. Anything else changed the *shape* of
the line and deserves a much closer look.

**"Can I compare these two lines?"**  Levels are not comparable across series —
the seed is a bookkeeping choice. :func:`rebase_line` re-expresses a line from a
different seed, and :func:`line_deltas` extracts the part that is genuinely
seed-independent. The invariant is worth stating explicitly:

    rebase_line(result, s)  changes every level by  (s - result["seed"])
    line_deltas(result)     is unchanged by any rebase

Every function here refuses to work on an unpublishable result. A line whose
status is not ``resolved`` has no ``points``, and inventing an analysis over its
diagnostic prefix would launder exactly the uncertainty :mod:`.core` was careful
to preserve.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Sequence, TypedDict

from .core import (
    BreadthLinePoint,
    BreadthLineResult,
    BreadthLineValidationError,
    calculate_cumulative_advance_decline_line,
)


DiffShape = Literal["identical", "constant_offset", "reshaped", "not_comparable"]


class SessionDelta(TypedDict):
    session_date: str
    session_sequence: int
    before: int
    after: int
    delta: int
    net_advances_changed: bool


class LineDiff(TypedDict):
    shape: DiffShape
    first_divergent_session: str | None
    tail_offset: int | None
    changed_session_count: int
    session_deltas: list[SessionDelta]
    reasons: list[str]


class CorrectionImpact(TypedDict):
    before_cutoff: str
    after_cutoff: str
    before_status: str
    after_status: str
    before_final_value: int | None
    after_final_value: int | None
    diff: LineDiff
    recompute_from_session: str | None
    ignored_future_event_count: int


def _publishable(result: Mapping[str, Any], label: str) -> list[BreadthLinePoint]:
    """Return a result's points, or raise if the result was never publishable.

    ``core`` deliberately empties ``points`` for any non-resolved status and moves
    the contiguous prefix to ``causal_prefix_diagnostic``. Reading that prefix here
    would quietly convert a diagnostic into an answer.
    """

    if not isinstance(result, Mapping):
        raise BreadthLineValidationError(f"{label} must be a result mapping.")
    status = result.get("status")
    if status != "resolved":
        raise BreadthLineValidationError(
            f"{label} has status {status!r}; only a resolved line can be analysed."
        )
    points = result.get("points")
    if not isinstance(points, Sequence):
        raise BreadthLineValidationError(f"{label} is missing its points.")
    return list(points)


def line_deltas(result: Mapping[str, Any]) -> list[int]:
    """The per-session net advances — the seed-independent content of the line.

    Two lines built from different seeds have different levels but identical
    deltas. When comparing series, compare these.
    """

    return [point["net_advances"] for point in _publishable(result, "result")]


def rebase_line(result: Mapping[str, Any], new_seed: int) -> BreadthLineResult:
    """Re-express a resolved line as though it had started from ``new_seed``.

    Every level moves by ``new_seed - result["seed"]``; no delta changes. This is
    what makes the seed a bookkeeping choice rather than a measurement, and it is
    the only honest way to overlay two lines with different origins.
    """

    points = _publishable(result, "result")
    if isinstance(new_seed, bool) or not isinstance(new_seed, int):
        raise BreadthLineValidationError("new_seed must be an integer.")

    offset = new_seed - result["seed"]
    rebased: list[BreadthLinePoint] = [
        {**point, "cumulative_line": point["cumulative_line"] + offset} for point in points
    ]
    return {
        **result,  # type: ignore[misc]
        "seed": new_seed,
        "points": rebased,
        "final_value": rebased[-1]["cumulative_line"] if rebased else new_seed,
    }


def diff_lines(before: Mapping[str, Any], after: Mapping[str, Any]) -> LineDiff:
    """Compare two resolved evaluations of the same line, session by session.

    The classification is the point:

    ``identical``
        Nothing moved.
    ``constant_offset``
        Every session from the first divergence onward moved by the *same*
        amount, and only one session's net advances changed. That is a textbook
        restatement: the market did not move, the data did.
    ``reshaped``
        More than one session changed, or the offsets differ. The line's shape
        changed and the tail offset is not a sufficient summary.
    ``not_comparable``
        The two evaluations do not describe the same session calendar, so a
        session-by-session diff would be meaningless.

    Note that lines with different seeds are compared *after* an implicit rebase:
    a pure seed change is reported as a ``constant_offset`` starting at the first
    session, with no net-advances change — the level shifted, the market did not.
    """

    left = _publishable(before, "before")
    right = _publishable(after, "after")

    reasons: list[str] = []
    if [p["session_date"] for p in left] != [p["session_date"] for p in right]:
        reasons.append("session_calendars_differ")
        return {
            "shape": "not_comparable",
            "first_divergent_session": None,
            "tail_offset": None,
            "changed_session_count": 0,
            "session_deltas": [],
            "reasons": reasons,
        }

    deltas: list[SessionDelta] = []
    for old, new in zip(left, right):
        deltas.append(
            {
                "session_date": old["session_date"],
                "session_sequence": old["session_sequence"],
                "before": old["cumulative_line"],
                "after": new["cumulative_line"],
                "delta": new["cumulative_line"] - old["cumulative_line"],
                "net_advances_changed": old["net_advances"] != new["net_advances"],
            }
        )

    divergent = [d for d in deltas if d["delta"] != 0]
    if not divergent:
        shape: DiffShape = "identical"
        first: str | None = None
        tail: int | None = 0
    else:
        first = divergent[0]["session_date"]
        start = deltas.index(divergent[0])
        tail_deltas = {d["delta"] for d in deltas[start:]}
        restated = [d for d in deltas if d["net_advances_changed"]]
        if len(tail_deltas) == 1 and len(restated) <= 1:
            shape = "constant_offset"
            tail = divergent[0]["delta"]
            if restated:
                reasons.append(f"restated_session:{restated[0]['session_date']}")
            else:
                reasons.append("level_shift_only")
        else:
            shape = "reshaped"
            tail = None
            reasons.append(f"multiple_sessions_restated:{len(restated)}")

    return {
        "shape": shape,
        "first_divergent_session": first,
        "tail_offset": tail,
        "changed_session_count": len(divergent),
        "session_deltas": deltas,
        "reasons": reasons,
    }


def correction_impact(
    contract: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    before_cutoff: str,
    after_cutoff: str,
) -> CorrectionImpact:
    """Evaluate the same archive at two knowledge cutoffs and diff the results.

    This is the question an operator actually has after a correction lands: *how
    much of the archive between these two instants changed what I had published?*
    Both evaluations are causal, so the "before" line is exactly what was
    knowable then — not today's data truncated, which is the usual bug.

    Raises if either cutoff produced an unpublishable line; a diff against a
    diagnostic prefix would be a comparison of two different kinds of object.
    """

    before = calculate_cumulative_advance_decline_line(contract, events, before_cutoff)
    after = calculate_cumulative_advance_decline_line(contract, events, after_cutoff)
    return {
        "before_cutoff": before_cutoff,
        "after_cutoff": after_cutoff,
        "before_status": before["status"],
        "after_status": after["status"],
        "before_final_value": before["final_value"],
        "after_final_value": after["final_value"],
        "diff": diff_lines(before, after),
        "recompute_from_session": after["recompute_from_session"],
        "ignored_future_event_count": before["ignored_future_event_count"],
    }
