"""Pure projection functions over canonical research events (Issue 09).

Every projection is deterministic, carries a derivation version and population,
and reads only committed event facts. Missing capability data stays visible as
``value: None`` with an explicit coverage state; projections never replace or
mutate the source events.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - used in annotations only
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

from research.telemetry.enums import CoverageState
from research.telemetry.models import CanonicalEventV1

from .models import (
    COVERAGE_VERSION,
    DERIVATION_VERSION,
    DerivedMetricV1,
    FamilyCoverage,
    SessionRunSummaryV1,
    TelemetryCoverageV1,
)

if TYPE_CHECKING:
    from uuid import UUID

__all__ = [
    "as_canonical_event",
    "coverage_by_family",
    "derive_event_count_metric",
    "derive_usage_metric",
    "event_family",
    "session_summary",
]


def as_canonical_event(record: object) -> CanonicalEventV1:
    """Return a canonical event for a stored record or a canonical event.

    The stored record carries the complete canonical envelope, so this is a
    direct re-validation of the authority rather than a lossy field-by-field
    reconstruction.
    """
    if isinstance(record, CanonicalEventV1):
        return record
    envelope = getattr(record, "envelope", None)
    if isinstance(envelope, dict) and envelope:
        return CanonicalEventV1.model_validate(envelope)
    if isinstance(record, dict):
        return CanonicalEventV1.model_validate(record)
    raise TypeError(f"cannot read a canonical event from {type(record)!r}")


def event_family(event_type: str) -> str:
    """Return the canonical event family of a dotted event type."""
    return event_type.split(".", 1)[0] if event_type else "unknown"


def coverage_by_family(
    events: Iterable[object],
    study_id: UUID,
    *,
    population: str = "all_events",
    computed_at: Optional[datetime] = None,
) -> TelemetryCoverageV1:
    """Build the explicit per-family coverage projection for a study."""
    counts: dict[str, dict[str, int]] = {}
    total = 0
    for record in events:
        event = as_canonical_event(record)
        total += 1
        family = event_family(event.event_type)
        bucket = counts.setdefault(
            family,
            dict.fromkeys(CoverageState, 0),
        )
        bucket[event.coverage.state.value] = bucket.get(event.coverage.state.value, 0) + 1

    families = [
        FamilyCoverage(
            family=family,
            total=sum(bucket.values()),
            available=bucket[CoverageState.AVAILABLE.value],
            unavailable=bucket[CoverageState.UNAVAILABLE.value],
            partial=bucket[CoverageState.PARTIAL.value],
            unknown=bucket[CoverageState.UNKNOWN.value],
            needs_review=bucket[CoverageState.NEEDS_REVIEW.value],
        )
        for family, bucket in sorted(counts.items())
    ]
    return TelemetryCoverageV1(
        study_id=study_id,
        population=population,
        coverage_version=COVERAGE_VERSION,
        families=families,
        denominators={"events": total, "families": len(families)},
        computed_at=computed_at,
    )


def session_summary(
    events: Iterable[object],
    research_session_id: Optional[UUID] = None,
) -> SessionRunSummaryV1:
    """Summarize the events of one session (or of all provided events)."""
    selected = [
        as_canonical_event(record)
        for record in events
        if research_session_id is None
        or record.research_session_id == research_session_id
    ]
    family_counts: dict[str, int] = {}
    run_ids: set[str] = set()
    first: Optional[datetime] = None
    last: Optional[datetime] = None
    for event in selected:
        family = event_family(event.event_type)
        family_counts[family] = family_counts.get(family, 0) + 1
        if event.agent_run_id:
            run_ids.add(event.agent_run_id)
        if first is None or event.occurred_at < first:
            first = event.occurred_at
        if last is None or event.occurred_at > last:
            last = event.occurred_at

    return SessionRunSummaryV1(
        research_session_id=research_session_id,
        event_count=len(selected),
        family_counts=family_counts,
        agent_run_ids=sorted(run_ids),
        first_occurred_at=first,
        last_occurred_at=last,
        derivation_version=DERIVATION_VERSION,
    )


def derive_usage_metric(
    events: Sequence[object],
    *,
    metric_id: str = "usage_tokens_per_event",
    population: str = "all_events",
) -> DerivedMetricV1:
    """Derive mean usage tokens per event with explicit coverage.

    Only events whose ``usage_capability`` is ``AVAILABLE`` and whose value is
    observed contribute; if none did, the value is ``None`` with ``UNAVAILABLE``
    coverage — never ``0``.
    """
    canonical = [as_canonical_event(record) for record in events]
    observed = [
        event
        for event in canonical
        if event.metrics.usage_capability.state == CoverageState.AVAILABLE
        and event.metrics.usage_tokens is not None
    ]
    numerator = sum(event.metrics.usage_tokens or 0 for event in observed)
    denominator = len(observed)
    value: Optional[float] = None
    state = CoverageState.UNAVAILABLE
    if denominator > 0:
        value = numerator / denominator
        state = CoverageState.AVAILABLE
    return DerivedMetricV1(
        metric_id=metric_id,
        numerator=numerator if denominator > 0 else None,
        denominator=denominator if denominator > 0 else None,
        coverage_predicate=(
            f"{population}: usage_capability.state == AVAILABLE "
            "and usage_tokens is not None"
        ),
        derivation_version=DERIVATION_VERSION,
        value=value,
        coverage_state=state,
        source_event_count=len(canonical),
    )


def derive_event_count_metric(
    events: Sequence[object],
    *,
    metric_id: str = "event_count",
    population: str = "all_events",
) -> DerivedMetricV1:
    """Derive the total event count (an observed count, not an inference)."""
    count = len(events)
    return DerivedMetricV1(
        metric_id=metric_id,
        numerator=count,
        denominator=count,
        coverage_predicate=f"{population}: all stored events",
        derivation_version=DERIVATION_VERSION,
        value=float(count),
        coverage_state=CoverageState.AVAILABLE,
        source_event_count=count,
    )
