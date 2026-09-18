"""Pure study/profile-scoped read-model builders."""

from __future__ import annotations

from typing import Any, Iterable, Sequence, TYPE_CHECKING

from research.telemetry.enums import CoverageState
from research.telemetry.projections.models import COVERAGE_VERSION
from research.telemetry.projections.service import coverage_by_family, derive_event_count_metric, derive_usage_metric

from .models import (
    DerivedMetricV1,
    EnrollmentCoverageV1,
    ProfileExposureV1,
    StudySummaryV1,
    TelemetryCoverageV1,
)

if TYPE_CHECKING:
    from uuid import UUID

__all__ = [
    "build_derived_metrics",
    "build_enrollment_coverage",
    "build_profile_exposures",
    "build_study_summary",
    "build_telemetry_coverage",
    "population_definition",
]

_POPULATIONS = {
    "study": "all records bound to this study",
    "all_events": "all stored canonical events for the study",
    "enrolled": "all enrolled participants for the study",
    "exposed": "participants with an actual assigned-profile exposure",
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


def build_profile_exposures(
    assignments: Iterable[Any],
    exposures: Iterable[Any],
    *,
    study_id: UUID,
    population: str = "study",
) -> list[ProfileExposureV1]:
    assignment_list = list(assignments)
    exposures_by_assignment: dict[UUID, list[Any]] = {}
    for exposure in exposures:
        exposures_by_assignment.setdefault(exposure.assignment_id, []).append(exposure)
    profile_ids = sorted({assignment.agent_profile_id for assignment in assignment_list}, key=str)
    results: list[ProfileExposureV1] = []
    for profile_id in profile_ids:
        selected = [assignment for assignment in assignment_list if assignment.agent_profile_id == profile_id]
        exposed = 0
        non_exposed = 0
        digest = selected[0].profile_digest
        for assignment in selected:
            receipts = exposures_by_assignment.get(assignment.assignment_id, [])
            if any(receipt.is_exposure for receipt in receipts):
                exposed += 1
            elif receipts:
                non_exposed += 1
        assigned = len(selected)
        results.append(
            ProfileExposureV1(
                study_id=study_id,
                agent_profile_id=profile_id,
                profile_digest=digest,
                population=population,
                assigned_count=assigned,
                exposed_count=exposed,
                non_exposure_count=non_exposed,
                exposure_rate=exposed / assigned if assigned else None,
                coverage=CoverageState.AVAILABLE if assigned else CoverageState.UNAVAILABLE,
                coverage_reason=None if assigned else "no assignments for profile",
            )
        )
    return results


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
