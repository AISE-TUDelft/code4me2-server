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


class TelemetryCoverageV1(BaseModel):
    model_config = _BASE

    study_id: UUID
    coverage_version: str = COVERAGE_VERSION
    population: str
    families: list[FamilyCoverage] = Field(default_factory=list)
    denominators: dict[str, int] = Field(default_factory=dict)
    computed_at: Optional[datetime] = None


class ParticipantAssignmentV1(BaseModel):
    """The frozen assignment facts a study owner may inspect (no snapshot)."""

    model_config = _BASE

    assignment_id: UUID
    agent_profile_id: UUID
    strategy: str
    randomization_epoch: int
    profile_digest: str
    status: str
    assigned_at: datetime


class ParticipantSessionCoverageV1(BaseModel):
    """Session counts and liveness for one enrolled participant."""

    model_config = _BASE

    total: int = 0
    active: int = 0
    terminal: int = 0
    last_activity_at: Optional[datetime] = None
    last_heartbeat_at: Optional[datetime] = None


class ParticipantEventCoverageV1(BaseModel):
    """Canonical event counts for one enrolled participant."""

    model_config = _BASE

    total: int = 0
    by_event_type: dict[str, int] = Field(default_factory=dict)
    by_source: dict[str, int] = Field(default_factory=dict)
    last_occurred_at: Optional[datetime] = None


class ParticipantCoverageRowV1(BaseModel):
    """One study-local participant with assignment and coverage facts.

    Keyed by ``enrollment_id``/``participant_code`` only: account, participant
    and login identity are never part of this projection.
    """

    model_config = _BASE

    enrollment_id: UUID
    participant_code: str
    status: str
    enrolled_at: datetime
    updated_at: datetime
    assignment: Optional[ParticipantAssignmentV1] = None
    sessions: ParticipantSessionCoverageV1 = Field(
        default_factory=ParticipantSessionCoverageV1
    )
    events: ParticipantEventCoverageV1 = Field(
        default_factory=ParticipantEventCoverageV1
    )


class StudyParticipantCoverageV1(BaseModel):
    """Study-owner-scoped per-participant read model (no cross-study rows)."""

    model_config = _BASE

    study_id: UUID
    coverage_version: str = COVERAGE_VERSION
    population: str = "enrolled"
    participant_count: int = 0
    participants: list[ParticipantCoverageRowV1] = Field(default_factory=list)
    coverage: CoverageState = CoverageState.UNKNOWN
    coverage_reason: Optional[str] = None


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
