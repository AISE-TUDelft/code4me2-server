"""Pure study/profile-scoped read-model builders."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Optional, Sequence

from research.telemetry.enums import CoverageState
from research.telemetry.projections.models import COVERAGE_VERSION
from research.telemetry.projections.service import coverage_by_family, derive_event_count_metric, derive_usage_metric

from .models import (
    DerivedMetricV1,
    EnrollmentCoverageV1,
    ParticipantAssignmentV1,
    ParticipantCoverageRowV1,
    ParticipantEventCoverageV1,
    ParticipantSessionCoverageV1,
    StudyParticipantCoverageV1,
    StudySummaryV1,
    TelemetryCoverageV1,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

__all__ = [
    "build_derived_metrics",
    "build_enrollment_coverage",
    "build_participant_coverage",
    "build_study_summary",
    "build_telemetry_coverage",
    "population_definition",
]

_POPULATIONS = {
    "study": "all records bound to this study",
    "all_events": "all stored canonical events for the study",
    "enrolled": "all enrolled participants for the study",
}


def population_definition(population: str) -> str:
    return _POPULATIONS.get(population, f"custom population {population!r}")


def _status_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def build_study_summary(study: Any, profiles: Iterable[Any] = ()) -> StudySummaryV1:
    profile_list = list(profiles)
    return StudySummaryV1(
        study_id=study.study_id,
        name=study.name,
        research_status=getattr(study, "research_status", None),
        profile_count=len(profile_list),
        profile_ids=[profile.profile_id for profile in profile_list],
        config_digest=getattr(study, "research_config_digest", None),
    )


def build_enrollment_coverage(
    enrollments: Iterable[Any],
    *,
    study_id: UUID,
    population: str = "enrolled",
) -> EnrollmentCoverageV1:
    status_counts: dict[str, int] = {}
    total = 0
    for enrollment in enrollments:
        total += 1
        status = _status_value(enrollment.status)
        status_counts[status] = status_counts.get(status, 0) + 1
    return EnrollmentCoverageV1(
        study_id=study_id,
        population=population,
        total_enrollments=total,
        status_counts=status_counts,
        coverage=CoverageState.AVAILABLE if total else CoverageState.UNAVAILABLE,
        coverage_reason=None if total else "no enrollments in population",
    )


def build_telemetry_coverage(
    records: Iterable[Any],
    *,
    study_id: UUID,
    population: str = "all_events",
) -> TelemetryCoverageV1:
    projection = coverage_by_family(list(records), study_id, population=population)
    return TelemetryCoverageV1(
        study_id=study_id,
        coverage_version=projection.coverage_version,
        population=projection.population,
        families=projection.families,
        denominators=projection.denominators,
        computed_at=projection.computed_at,
    )


def build_derived_metrics(
    records: Sequence[Any],
    *,
    study_id: UUID,
    population: str = "all_events",
) -> list[DerivedMetricV1]:
    metrics = [
        derive_event_count_metric(list(records), population=population),
        derive_usage_metric(list(records), population=population),
    ]
    return [
        DerivedMetricV1(
            study_id=study_id,
            derivation_version=metric.derivation_version,
            population=population,
            metric_id=metric.metric_id,
            numerator=metric.numerator,
            denominator=metric.denominator,
            value=metric.value,
            coverage_state=metric.coverage_state,
            coverage_predicate=metric.coverage_predicate,
            source_event_count=metric.source_event_count,
        )
        for metric in metrics
    ]


def _later(
    current: Optional[datetime], candidate: Optional[datetime]
) -> Optional[datetime]:
    """Return the later of two optional timestamps (``None``-safe)."""
    if candidate is None:
        return current
    if current is None or candidate > current:
        return candidate
    return current


def build_participant_coverage(
    enrollments: Iterable[Any],
    assignments: Iterable[Any],
    sessions: Iterable[Any],
    events: Iterable[Any],
    *,
    study_id: UUID,
    population: str = "enrolled",
) -> StudyParticipantCoverageV1:
    """Build the study-owner-scoped per-participant coverage read model.

    Every input is already study-scoped by the caller, so rows can never mix
    studies. Rows are keyed by the study-local ``enrollment_id`` /
    ``participant_code`` only; login identity is never joined. An event whose
    ``enrollment_id`` is missing (e.g. the enrollment row was removed) is not
    attributed to a participant row rather than being merged into another.
    """
    assignment_by_enrollment: dict[Any, Any] = {}
    for assignment in assignments:
        assignment_by_enrollment.setdefault(assignment.enrollment_id, assignment)

    session_stats: dict[Any, dict[str, Any]] = {}
    for research_session in sessions:
        stats = session_stats.setdefault(
            research_session.enrollment_id,
            {
                "total": 0,
                "active": 0,
                "terminal": 0,
                "last_activity_at": None,
                "last_heartbeat_at": None,
            },
        )
        stats["total"] += 1
        if research_session.state.is_terminal:
            stats["terminal"] += 1
        else:
            stats["active"] += 1
        stats["last_activity_at"] = _later(
            stats["last_activity_at"], research_session.last_activity_at
        )
        stats["last_heartbeat_at"] = _later(
            stats["last_heartbeat_at"], research_session.last_heartbeat_at
        )

    event_stats: dict[Any, dict[str, Any]] = {}
    for record in events:
        enrollment_id = record.enrollment_id
        if enrollment_id is None:
            continue
        stats = event_stats.setdefault(
            enrollment_id,
            {
                "total": 0,
                "by_event_type": {},
                "by_source": {},
                "last_occurred_at": None,
            },
        )
        stats["total"] += 1
        stats["by_event_type"][record.event_type] = (
            stats["by_event_type"].get(record.event_type, 0) + 1
        )
        stats["by_source"][record.source] = stats["by_source"].get(record.source, 0) + 1
        stats["last_occurred_at"] = _later(
            stats["last_occurred_at"], record.occurred_at
        )

    participants: list[ParticipantCoverageRowV1] = []
    for enrollment in sorted(
        enrollments, key=lambda item: (item.enrolled_at, item.participant_code)
    ):
        assignment = assignment_by_enrollment.get(enrollment.enrollment_id)
        session_summary = session_stats.get(enrollment.enrollment_id) or {}
        event_summary = event_stats.get(enrollment.enrollment_id) or {}
        participants.append(
            ParticipantCoverageRowV1(
                enrollment_id=enrollment.enrollment_id,
                participant_code=enrollment.participant_code,
                status=_status_value(enrollment.status),
                enrolled_at=enrollment.enrolled_at,
                updated_at=enrollment.updated_at,
                assignment=(
                    ParticipantAssignmentV1(
                        assignment_id=assignment.assignment_id,
                        agent_profile_id=assignment.agent_profile_id,
                        strategy=assignment.strategy,
                        randomization_epoch=assignment.randomization_epoch,
                        profile_digest=assignment.profile_digest,
                        status=assignment.status,
                        assigned_at=assignment.assigned_at,
                    )
                    if assignment is not None
                    else None
                ),
                sessions=ParticipantSessionCoverageV1(
                    total=session_summary.get("total", 0),
                    active=session_summary.get("active", 0),
                    terminal=session_summary.get("terminal", 0),
                    last_activity_at=session_summary.get("last_activity_at"),
                    last_heartbeat_at=session_summary.get("last_heartbeat_at"),
                ),
                events=ParticipantEventCoverageV1(
                    total=event_summary.get("total", 0),
                    by_event_type=dict(
                        sorted(event_summary.get("by_event_type", {}).items())
                    ),
                    by_source=dict(
                        sorted(event_summary.get("by_source", {}).items())
                    ),
                    last_occurred_at=event_summary.get("last_occurred_at"),
                ),
            )
        )

    total = len(participants)
    return StudyParticipantCoverageV1(
        study_id=study_id,
        population=population,
        participant_count=total,
        participants=participants,
        coverage=CoverageState.AVAILABLE if total else CoverageState.UNAVAILABLE,
        coverage_reason=None if total else "no enrollments in population",
    )
