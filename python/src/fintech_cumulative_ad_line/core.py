"""Causal, revision-aware Cumulative Advance/Decline Line.

The A/D Line is a running sum: ``line[n] = line[n-1] + (advances - declines)``.
Two consequences follow from that single recurrence, and everything difficult
about this module is one of them.

**1. State is history.** Every published value depends on every prior session,
so one restated session silently offsets *every* later value. Correcting the
past is therefore not an edit — it is a **recomputation** of the whole suffix.
This module treats the line as a *projection over an append-only event archive*
rather than a mutable series: corrections arrive as new revision events, and the
line is rebuilt from the seed. ``recompute_from_session`` names the earliest
session a caller must recompute from.

**2. The level is arbitrary; only the changes are real.** The line starts at a
``seed`` whose value is a bookkeeping choice, so absolute levels from two series
are never comparable — a seed of 0 and a seed of 100 describe the same market.
The contract makes the seed and its ``seed_lineage_id`` explicit so that two
lines can be checked for a common origin before anyone compares them.

Correctness rests on three refusals:

* **Never look ahead.** Events with ``available_at`` past ``knowledge_cutoff``
  are skipped before validation — a malformed *future* event cannot break an
  earlier query, and a *future* correction cannot leak into it.
* **Never splice a suffix.** If a session is missing, cancelled, or not yet
  final, the cumulative chain is broken. Sessions after the break are dropped
  entirely rather than summed across the hole, because a sum across a gap looks
  like a real level and is not one. The contiguous prefix survives, but only as
  ``causal_prefix_diagnostic`` — never as publishable ``points``.
* **Never guess a lineage.** Each session's revision chain must be a unique,
  correctly-linked sequence (``revision_number`` 1..n, each event naming its
  parent). Two events competing for the same revision are ``ambiguous``.

The function consumes an explicit continuity contract and an append-only event
archive. It never sorts events, invents a missing session, or applies an event
that was unavailable at the requested knowledge cutoff.

Companion article (canonical): https://thefintechbuilder.com/market-breadth-and-internals/advance-decline-breadth/cumulative-advance-decline-line/
Catalog topic id: D04-F01-A03  (Domain D04 — Market Breadth and Internals / Family D04-F01 — Advance/Decline Breadth)
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Literal, Mapping, Sequence, TypedDict


MAX_SAFE_INTEGER = 9_007_199_254_740_991
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ResultStatus = Literal["resolved", "incomplete", "ambiguous", "unsupported"]
Action = Literal["upsert", "cancel"]


class BreadthLineValidationError(ValueError):
    """Raised only for malformed values, not for modeled data-quality states."""


class BreadthLinePoint(TypedDict):
    session_sequence: int
    session_date: str
    event_id: str
    revision_number: int
    effective_at: str
    available_at: str
    advances: int
    declines: int
    unchanged: int
    excluded: int
    unclassified: int
    universe_size: int
    net_advances: int
    cumulative_line: int
    source_is_provisional: bool
    line_is_provisional: bool
    universe_snapshot_id: str
    source_evidence_state: Literal["ready"]


class BreadthLineResult(TypedDict):
    status: ResultStatus
    reasons: list[str]
    knowledge_cutoff: str
    seed: int
    points: list[BreadthLinePoint]
    final_value: int | None
    causal_prefix_diagnostic: list[BreadthLinePoint]
    missing_sessions: list[str]
    cancelled_sessions: list[str]
    contains_provisional: bool
    ignored_future_event_count: int
    recompute_from_session: str | None
    blocked_from_session: str | None
    metric: Literal["cumulative_issue_count_advance_decline_line"]


IDENTITY_FIELDS = (
    "series_id",
    "continuity_id",
    "venue_id",
    "universe_id",
    "calendar_id",
    "session_type",
    "comparison_basis",
)
COUNT_FIELDS = ("advances", "declines", "unchanged", "excluded", "unclassified", "universe_size")
SOURCE_EVIDENCE_STATES = ("ready", "incomplete", "ambiguous", "unsupported")


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise BreadthLineValidationError(f"{field} must be an RFC 3339 timestamp.")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise BreadthLineValidationError(
            f"{field} must be an RFC 3339 timestamp."
        ) from exc
    if parsed.tzinfo is None:
        raise BreadthLineValidationError(f"{field} must include a UTC offset.")
    return parsed.astimezone(timezone.utc)


def _date(value: Any, field: str) -> str:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise BreadthLineValidationError(f"{field} must be a YYYY-MM-DD date.")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise BreadthLineValidationError(f"{field} must be a real date.") from exc
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BreadthLineValidationError(f"{field} must be a non-empty string.")
    return value


def _safe_integer(value: Any, field: str, *, nonnegative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BreadthLineValidationError(f"{field} must be a safe integer.")
    if abs(value) > MAX_SAFE_INTEGER or (nonnegative and value < 0):
        qualifier = "non-negative " if nonnegative else ""
        raise BreadthLineValidationError(f"{field} must be a {qualifier}safe integer.")
    return value


def _base_result(
    status: ResultStatus,
    reasons: list[str],
    cutoff: str,
    seed: int,
    *,
    ignored: int = 0,
) -> BreadthLineResult:
    return {
        "status": status,
        "reasons": reasons,
        "knowledge_cutoff": cutoff,
        "seed": seed,
        "points": [],
        "final_value": None,
        "causal_prefix_diagnostic": [],
        "missing_sessions": [],
        "cancelled_sessions": [],
        "contains_provisional": False,
        "ignored_future_event_count": ignored,
        "recompute_from_session": None,
        "blocked_from_session": None,
        "metric": "cumulative_issue_count_advance_decline_line",
    }


def calculate_cumulative_advance_decline_line(
    contract: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    knowledge_cutoff: str,
) -> BreadthLineResult:
    """Evaluate one causally known line under an explicit continuity contract.

    `resolved` means all declared sessions have one final, reconciled current
    revision at the cutoff. `incomplete` preserves a causal prefix when a
    session is missing, cancelled, or provisional. `ambiguous` means a unique
    revision chain cannot be selected. `unsupported` means the archive cannot
    belong to this continuity contract and must not be spliced into the line.
    """

    if not isinstance(contract, Mapping):
        raise BreadthLineValidationError("contract must be a mapping.")
    if isinstance(events, (str, bytes)) or not isinstance(events, Sequence):
        raise BreadthLineValidationError("events must be an ordered sequence.")

    cutoff_dt = _timestamp(knowledge_cutoff, "knowledge_cutoff")
    seed = _safe_integer(contract.get("seed"), "seed")
    _text(contract.get("seed_lineage_id"), "seed_lineage_id")
    seed_effective = _timestamp(contract.get("seed_effective_at"), "seed_effective_at")
    seed_available = _timestamp(contract.get("seed_available_at"), "seed_available_at")
    if seed_available < seed_effective:
        raise BreadthLineValidationError("seed_available_at cannot precede seed_effective_at.")
    for field in IDENTITY_FIELDS:
        _text(contract.get(field), field)
    if contract.get("metric") != "cumulative_issue_count_advance_decline_line":
        return _base_result("unsupported", ["unsupported_metric"], knowledge_cutoff, seed)

    expected_raw = contract.get("expected_sessions")
    if isinstance(expected_raw, (str, bytes)) or not isinstance(expected_raw, Sequence):
        raise BreadthLineValidationError("expected_sessions must be an ordered sequence.")
    expected: list[tuple[int, str]] = []
    previous_date: str | None = None
    for position, raw in enumerate(expected_raw, start=1):
        if not isinstance(raw, Mapping):
            raise BreadthLineValidationError("each expected session must be a mapping.")
        sequence = _safe_integer(raw.get("session_sequence"), "session_sequence", nonnegative=True)
        session_date = _date(raw.get("session_date"), "session_date")
        if sequence != position or (previous_date is not None and session_date <= previous_date):
            return _base_result(
                "unsupported", ["expected_session_sequence_not_contiguous"], knowledge_cutoff, seed
            )
        expected.append((sequence, session_date))
        previous_date = session_date
    expected_by_date = {date: sequence for sequence, date in expected}

    if cutoff_dt < seed_available:
        result = _base_result("incomplete", ["seed_not_yet_available"], knowledge_cutoff, seed)
        result["blocked_from_session"] = expected[0][1] if expected else None
        return result

    validated: list[dict[str, Any]] = []
    previous_available: datetime | None = None
    seen_event_ids: set[str] = set()
    duplicate_event_id = False
    unsupported_reasons: set[str] = set()
    ignored = 0

    for raw in events:
        if not isinstance(raw, Mapping):
            raise BreadthLineValidationError("each event must be a mapping.")
        event = dict(raw)
        available_dt = _timestamp(event.get("available_at"), "available_at")
        if available_dt > cutoff_dt:
            ignored += 1
            continue
        event_id = _text(event.get("event_id"), "event_id")
        if event_id in seen_event_ids:
            duplicate_event_id = True
        seen_event_ids.add(event_id)
        ingest_sequence = _safe_integer(event.get("ingest_sequence"), "ingest_sequence", nonnegative=True)
        if ingest_sequence != len(validated) + 1:
            unsupported_reasons.add("event_sequence_not_contiguous")
        effective_dt = _timestamp(event.get("effective_at"), "effective_at")
        if available_dt < effective_dt:
            raise BreadthLineValidationError("available_at cannot precede effective_at.")
        if previous_available is not None and available_dt < previous_available:
            unsupported_reasons.add("events_not_in_ingest_order")
        previous_available = available_dt
        event["_available_dt"] = available_dt
        event["_effective_dt"] = effective_dt
        event["session_date"] = _date(event.get("session_date"), "session_date")
        event["session_sequence"] = _safe_integer(
            event.get("session_sequence"), "session_sequence", nonnegative=True
        )
        event["revision_number"] = _safe_integer(
            event.get("revision_number"), "revision_number", nonnegative=True
        )
        if event["revision_number"] < 1:
            raise BreadthLineValidationError("revision_number must be at least 1.")
        action = event.get("action")
        if action not in ("upsert", "cancel"):
            raise BreadthLineValidationError("action must be upsert or cancel.")
        if event.get("supersedes_event_id") is not None:
            _text(event.get("supersedes_event_id"), "supersedes_event_id")
        for field in IDENTITY_FIELDS:
            _text(event.get(field), field)
            if event[field] != contract[field]:
                unsupported_reasons.add("series_break_required")
        expected_sequence = expected_by_date.get(event["session_date"])
        if expected_sequence is None or expected_sequence != event["session_sequence"]:
            unsupported_reasons.add("event_outside_expected_calendar")
        _text(event.get("universe_snapshot_id"), "universe_snapshot_id")
        if not isinstance(event.get("is_final"), bool):
            raise BreadthLineValidationError("is_final must be a boolean.")
        if event.get("source_evidence_state") not in SOURCE_EVIDENCE_STATES:
            raise BreadthLineValidationError(
                "source_evidence_state must be ready, incomplete, ambiguous, or unsupported."
            )
        if action == "upsert":
            counts = {
                field: _safe_integer(event.get(field), field, nonnegative=True)
                for field in COUNT_FIELDS
            }
            if counts["advances"] + counts["declines"] + counts["unchanged"] + counts["excluded"] + counts["unclassified"] != counts["universe_size"]:
                unsupported_reasons.add("unreconciled_issue_partition")
            if event["source_evidence_state"] == "ready" and counts["unclassified"] != 0:
                unsupported_reasons.add("ready_source_contains_unclassified_issues")
            event.update(counts)
        validated.append(event)

    if unsupported_reasons:
        return _base_result("unsupported", sorted(unsupported_reasons), knowledge_cutoff, seed)

    visible = validated
    if duplicate_event_id:
        return _base_result(
            "ambiguous", ["duplicate_event_id"], knowledge_cutoff, seed, ignored=ignored
        )

    grouped: dict[str, list[dict[str, Any]]] = {date: [] for _, date in expected}
    for event in visible:
        grouped[event["session_date"]].append(event)

    selected: dict[str, dict[str, Any]] = {}
    corrected_sessions: list[str] = []
    for _, session_date in expected:
        chain = grouped[session_date]
        if not chain:
            continue
        for index, event in enumerate(chain, start=1):
            required_parent = None if index == 1 else chain[index - 2]["event_id"]
            if event["revision_number"] != index or event.get("supersedes_event_id") != required_parent:
                return _base_result(
                    "ambiguous",
                    [f"non_unique_revision_lineage:{session_date}"],
                    knowledge_cutoff,
                    seed,
                    ignored=ignored,
                )
        selected[session_date] = chain[-1]
        if len(chain) > 1:
            corrected_sessions.append(session_date)

    running = seed
    provisional_seen = False
    reasons: list[str] = []
    missing: list[str] = []
    cancelled: list[str] = []
    points: list[BreadthLinePoint] = []
    blocked: str | None = None
    source_block_status: ResultStatus | None = None

    for sequence, session_date in expected:
        event = selected.get(session_date)
        if event is None:
            missing.append(session_date)
            blocked = blocked or session_date
            continue
        if event["action"] == "cancel":
            cancelled.append(session_date)
            blocked = blocked or session_date
            continue
        source_state = event["source_evidence_state"]
        if source_state != "ready":
            blocked = blocked or session_date
            reasons.append(f"source_evidence_{source_state}:{session_date}")
            if source_state == "unsupported":
                source_block_status = "unsupported"
            elif source_state == "ambiguous" and source_block_status != "unsupported":
                source_block_status = "ambiguous"
            elif source_block_status is None:
                source_block_status = "incomplete"
            continue
        if blocked is not None:
            continue  # preserve only the contiguous causal prefix; never splice a suffix
        net = event["advances"] - event["declines"]
        running = _safe_integer(running + net, "cumulative_line")
        source_provisional = not event["is_final"]
        provisional_seen = provisional_seen or source_provisional
        points.append(
            {
                "session_sequence": sequence,
                "session_date": session_date,
                "event_id": event["event_id"],
                "revision_number": event["revision_number"],
                "effective_at": event["effective_at"],
                "available_at": event["available_at"],
                "advances": event["advances"],
                "declines": event["declines"],
                "unchanged": event["unchanged"],
                "excluded": event["excluded"],
                "unclassified": event["unclassified"],
                "universe_size": event["universe_size"],
                "net_advances": net,
                "cumulative_line": running,
                "source_is_provisional": source_provisional,
                "line_is_provisional": provisional_seen,
                "universe_snapshot_id": event["universe_snapshot_id"],
                "source_evidence_state": "ready",
            }
        )

    if missing:
        reasons.append("missing_expected_session")
    if cancelled:
        reasons.append("cancelled_session")
    if provisional_seen:
        reasons.append("provisional_source_in_suffix")
    status: ResultStatus = source_block_status or ("incomplete" if reasons else "resolved")
    publishable = status == "resolved"
    return {
        "status": status,
        "reasons": reasons,
        "knowledge_cutoff": knowledge_cutoff,
        "seed": seed,
        "points": points if publishable else [],
        "final_value": points[-1]["cumulative_line"] if publishable and points else (seed if publishable else None),
        "causal_prefix_diagnostic": [] if publishable else points,
        "missing_sessions": missing,
        "cancelled_sessions": cancelled,
        "contains_provisional": provisional_seen,
        "ignored_future_event_count": ignored,
        "recompute_from_session": min(corrected_sessions) if corrected_sessions else None,
        "blocked_from_session": blocked,
        "metric": "cumulative_issue_count_advance_decline_line",
    }
