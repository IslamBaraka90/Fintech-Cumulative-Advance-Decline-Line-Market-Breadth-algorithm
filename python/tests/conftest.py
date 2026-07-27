"""Shared fixture access for both test modules.

The same JSON file backs the TypeScript suite, which is what makes the
cross-language parity claim checkable rather than aspirational.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ad_line_fixtures.json").read_text(encoding="utf-8"))
WORKED = json.loads((Path(__file__).parent / "fixtures" / "worked_example.json").read_text(encoding="utf-8"))

#: The archive is fully known by this instant, correction included.
LATE_CUTOFF = "2026-01-11T00:00:00Z"
#: One hour before the correction becomes available (it lands 2026-01-10T14:00:00Z).
EARLY_CUTOFF = "2026-01-09T23:00:00Z"


def inputs() -> tuple[dict, list[dict]]:
    """A fresh, deep-copied (contract, events) pair — never share mutable fixtures."""

    return copy.deepcopy(FIXTURE["contract"]), copy.deepcopy(FIXTURE["base_events"])


def correction(**patch) -> dict:
    """The fixture's revision-2 event for 2026-01-06, optionally patched."""

    event = copy.deepcopy(FIXTURE["synthetic_correction"])
    event.update(patch)
    return event


def resequence(events: list[dict]) -> list[dict]:
    """Renumber ingest_sequence after removing an event, so the archive stays contiguous."""

    for index, event in enumerate(events, start=1):
        event["ingest_sequence"] = index
    return events
