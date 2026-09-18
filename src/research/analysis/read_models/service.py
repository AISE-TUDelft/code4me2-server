"""Pure read-model builders (Issue 12).

Each builder is deterministic over injected canonical records / assignments /
exposures / enrollments and carries the revision id+digest, the coverage or
derivation version, and the explicit population definition. Missing data stays
visible: ``value``/``exposure_rate`` are ``None`` with an explicit coverage
state, never ``0``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Sequence

from research.telemetry.enums import CoverageState
from research.telemetry.projections.models import COVERAGE_VERSION
from research.telemetry.projections.service import (
    coverage_by_family,
    derive_event_count_metric,
    derive_usage_metric,
)

if TYPE_CHECKING:
    from uuid import UUID

from .models import (
    ConditionExposureV1,
    DerivedMetricV1,
    EnrollmentCoverageV1,
    StudyRevisionSummaryV1,
    TelemetryCoverageV1,
)

if TYPE_CHECKING:
    from research.participants.models import Enrollment
    from research.runtime.assignment.models import AssignmentV1, ExposureV1
    from research.study.protocol.publication import StudyRevision

__all__ = [
    "build_condition_exposures",
    "build_derived_metrics",
    "build_enrollment_coverage",
    "build_revision_summary",
    "build_telemetry_coverage",
    "population_definition",
]

_POPULATIONS = {
    "revision": "all records bound to this revision",
    "all_events": "all stored canonical events for the revision",
    "enrolled": "all enrolled participants for the revision",
    "exposed": "participants with an actual condition exposure",
}


def population_definition(population: str) -> str:
    """Return the human-readable definition of a population filter."""
    return _POPULATIONS.get(population, f"custom population {population!r}")


def _status_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def build_revision_summary(revision: StudyRevision) -> StudyRevisionSummaryV1:
    """Build the immutable revision summary read model."""
    protocol_json = revision.protocol_json or {}
    conditions = protocol_json.get("conditions") or []
    condition_ids = [str(condition.get("condition_id", "")) for condition in conditions]
    return StudyRevisionSummaryV1(
        study_id=revision.study_id,
        revision_id=revision.revision_id,
        revision_number=revision.revision_number,
        revision_digest=revision.protocol_digest,
        status=_status_value(revision.status),
        protocol_schema_version=str(protocol_json.get("schema_version", "1")),
        condition_count=len(conditions),
        condition_ids=condition_ids,
        published_at=revision.published_at,
        supersedes_revision_id=revision.supersedes_revision_id,
        created_at=revision.created_at,
        population="revision",
    )


def build_enrollment_coverage(
    enrollments: Iterable[Enrollment],
    *,
    study_id: UUID,
    revision_id: UUID,
    revision_digest: str,
    population: str = "enrolled",
) -> EnrollmentCoverageV1:
    """Summarize enrollment states with explicit coverage."""
    status_counts: dict[str, int] = {}
    total = 0
    for enrollment in enrollments:
        total += 1
        status = _status_value(enrollment.status)
        status_counts[status] = status_counts.get(status, 0) + 1
    if total == 0:
        coverage = CoverageState.UNAVAILABLE
        reason = "no enrollments in population"
    else:
        coverage = CoverageState.AVAILABLE
        reason = None
    return EnrollmentCoverageV1(
        study_id=study_id,
        revision_id=revision_id,
        revision_digest=revision_digest,
        coverage_version=COVERAGE_VERSION,
        population=population,
        total_enrollments=total,
        status_counts=status_counts,
        withdrawn_count=status_counts.get("WITHDRAWN", 0),
        coverage=coverage,
        coverage_reason=reason,
    )


def build_condition_exposures(
    assignments: Iterable[AssignmentV1],
    exposures: Iterable[ExposureV1],
    *,
    study_id: UUID,
    revision_id: UUID,
    revision_digest: str,
    population: str = "revision",
) -> list[ConditionExposureV1]:
    """Build per-condition assignment vs exposure read models."""
    assignment_list = list(assignments)
    exposures_by_assignment: dict[UUID, list[ExposureV1]] = {}
    for exposure in exposures:
        exposures_by_assignment.setdefault(exposure.assignment_id, []).append(exposure)

    condition_ids = sorted({assignment.condition_id for assignment in assignment_list})
    results: list[ConditionExposureV1] = []
    for condition_id in condition_ids:
        condition_assignments = [
            assignment
            for assignment in assignment_list
            if assignment.condition_id == condition_id
        ]
        exposed = 0
        non_exposed = 0
        for assignment in condition_assignments:
            outcomes = exposures_by_assignment.get(assignment.assignment_id, [])
            if any(exposure.is_exposure for exposure in outcomes):
                exposed += 1
            elif outcomes:
                non_exposed += 1
        assigned = len(condition_assignments)
        results.append(
            ConditionExposureV1(
                study_id=study_id,
                revision_id=revision_id,
                revision_digest=revision_digest,
                coverage_version=COVERAGE_VERSION,
                population=population,
                condition_id=condition_id,
                assigned_count=assigned,
                exposed_count=exposed,
                non_exposure_count=non_exposed,
                exposure_rate=(exposed / assigned) if assigned else None,
                coverage=(
                    CoverageState.AVAILABLE if assigned else CoverageState.UNAVAILABLE
                ),
                coverage_reason=None if assigned else "no assignments for condition",
            )
        )
    return results


def build_telemetry_coverage(
    records: Iterable[Any],
    *,
    study_id: UUID,
    revision_id: UUID,
    revision_digest: str,
    population: str = "all_events",
) -> TelemetryCoverageV1:
    """Wrap the shared coverage projection with revision context."""
    projection = coverage_by_family(
        list(records), revision_id, population=population
    )
    return TelemetryCoverageV1(
        study_id=study_id,
        revision_id=revision_id,
        revision_digest=revision_digest,
        coverage_version=projection.coverage_version,
        population=projection.population,
        families=projection.families,
        denominators=projection.denominators,
        computed_at=projection.computed_at,
    )


def _wrap_metric(
    metric: Any,
    *,
    study_id: UUID,
    revision_id: UUID,
    revision_digest: str,
    population: str,
) -> DerivedMetricV1:
    return DerivedMetricV1(
        study_id=study_id,
        revision_id=revision_id,
        revision_digest=revision_digest,
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


def build_derived_metrics(
    records: Sequence[Any],
    *,
    study_id: UUID,
    revision_id: UUID,
    revision_digest: str,
    population: str = "all_events",
) -> list[DerivedMetricV1]:
    """Build the versioned derived metrics with explicit coverage."""
    records = list(records)
    metrics = [
        derive_event_count_metric(records, population=population),
        derive_usage_metric(records, population=population),
    ]
    return [
        _wrap_metric(
            metric,
            study_id=study_id,
            revision_id=revision_id,
            revision_digest=revision_digest,
            population=population,
        )
        for metric in metrics
    ]
