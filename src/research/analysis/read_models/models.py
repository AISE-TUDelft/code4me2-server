"""Pydantic v2 contracts for researcher read models (Issue 12).

Every read model carries the revision id/digest, the coverage/derivation
version, and the explicit population/filter definition, so a number can never be
read without its provenance. Missing measurement data is represented as
``value: null`` plus an explicit :class:`~research.telemetry.enums.CoverageState`,
never as ``0`` or "complete".
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves model annotations at runtime
from typing import Optional
from uuid import UUID  # noqa: TC003 - pydantic resolves model annotations at runtime

from pydantic import BaseModel, ConfigDict, Field

from research.telemetry.enums import CoverageState
from research.telemetry.projections.models import (
    COVERAGE_VERSION,
    DERIVATION_VERSION,
    FamilyCoverage,
)

from .enums import (
    ReadModelReasonCode,  # noqa: TC001 - pydantic resolves model field annotations at runtime
)

_BASE = ConfigDict(extra="forbid")

__all__ = [
    "COVERAGE_VERSION",
    "DERIVATION_VERSION",
    "ConditionExposureV1",
    "DerivedMetricV1",
    "EnrollmentCoverageV1",
    "FamilyCoverage",
    "ReadModelIssue",
    "StudyRevisionSummaryV1",
    "TelemetryCoverageV1",
]


class StudyRevisionSummaryV1(BaseModel):
    """An immutable published revision summary for the control plane."""

    model_config = _BASE

    study_id: UUID
    revision_id: UUID
    revision_number: int
    revision_digest: str
    status: str
    protocol_schema_version: str
    condition_count: int
    condition_ids: list[str] = Field(default_factory=list)
    published_at: Optional[datetime] = None
    supersedes_revision_id: Optional[UUID] = None
    created_at: Optional[datetime] = None
    population: str = "revision"


class EnrollmentCoverageV1(BaseModel):
    """Enrollment-state coverage for one revision/population."""

    model_config = _BASE

    study_id: UUID
    revision_id: UUID
    revision_digest: str
    coverage_version: str = COVERAGE_VERSION
    population: str
    total_enrollments: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    withdrawn_count: int = 0
    coverage: CoverageState = CoverageState.UNKNOWN
    coverage_reason: Optional[str] = None


class ConditionExposureV1(BaseModel):
    """Assignment vs exposure for one condition (separate facts)."""

    model_config = _BASE

    study_id: UUID
    revision_id: UUID
    revision_digest: str
    coverage_version: str = COVERAGE_VERSION
    population: str
    condition_id: str
    assigned_count: int = 0
    exposed_count: int = 0
    non_exposure_count: int = 0
    # None until at least one assignment exists; never a fabricated 0 rate.
    exposure_rate: Optional[float] = None
    coverage: CoverageState = CoverageState.UNKNOWN
    coverage_reason: Optional[str] = None


class TelemetryCoverageV1(BaseModel):
    """Per-event-family telemetry coverage for one revision/population."""

    model_config = _BASE

    study_id: UUID
    revision_id: UUID
    revision_digest: str
    coverage_version: str = COVERAGE_VERSION
    population: str
    families: list[FamilyCoverage] = Field(default_factory=list)
    denominators: dict[str, int] = Field(default_factory=dict)
    computed_at: Optional[datetime] = None


class DerivedMetricV1(BaseModel):
    """A versioned derived metric that preserves null/coverage semantics."""

    model_config = _BASE

    study_id: UUID
    revision_id: UUID
    revision_digest: str
    derivation_version: str = DERIVATION_VERSION
    population: str
    metric_id: str
    numerator: Optional[int] = None
    denominator: Optional[int] = None
    value: Optional[float] = None
    coverage_state: CoverageState = CoverageState.UNKNOWN
    coverage_predicate: str
    source_event_count: int = 0


class ReadModelIssue(BaseModel):
    """One typed read-model rejection reason."""

    model_config = _BASE

    code: ReadModelReasonCode
    message: str
    field: str = ""
