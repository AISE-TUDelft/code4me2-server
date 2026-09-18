"""Study/profile-scoped researcher read models."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from research.telemetry.enums import CoverageState
from research.telemetry.projections.models import COVERAGE_VERSION, DERIVATION_VERSION, FamilyCoverage

_BASE = ConfigDict(extra="forbid")


class StudySummaryV1(BaseModel):
    model_config = _BASE

    study_id: UUID
    name: str
    research_status: Optional[str] = None
    profile_count: int = 0
    profile_ids: list[UUID] = Field(default_factory=list)
    config_digest: Optional[str] = None
    population: str = "study"


class EnrollmentCoverageV1(BaseModel):
    model_config = _BASE

    study_id: UUID
    coverage_version: str = COVERAGE_VERSION
    population: str
    total_enrollments: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    coverage: CoverageState = CoverageState.UNKNOWN
    coverage_reason: Optional[str] = None


class ProfileExposureV1(BaseModel):
    model_config = _BASE

    study_id: UUID
    agent_profile_id: UUID
    profile_digest: str
    coverage_version: str = COVERAGE_VERSION
    population: str
    assigned_count: int = 0
    exposed_count: int = 0
    non_exposure_count: int = 0
    exposure_rate: Optional[float] = None
    coverage: CoverageState = CoverageState.UNKNOWN
    coverage_reason: Optional[str] = None


class TelemetryCoverageV1(BaseModel):
    model_config = _BASE

    study_id: UUID
    coverage_version: str = COVERAGE_VERSION
    population: str
    families: list[FamilyCoverage] = Field(default_factory=list)
    denominators: dict[str, int] = Field(default_factory=dict)
    computed_at: Optional[datetime] = None


class DerivedMetricV1(BaseModel):
    model_config = _BASE

    study_id: UUID
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
    model_config = _BASE

    code: str
    message: str
    field: str = ""
