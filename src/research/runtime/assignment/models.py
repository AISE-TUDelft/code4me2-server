"""Pydantic v2 contracts for enrollment-scoped profile assignment and exposure.

Assignment and exposure are **separate immutable facts**:

* :class:`AssignmentV1` is the server-authoritative, sticky agent profile
    allocated for one enrollment. It is never re-randomized.
* :class:`ExposureV1` records what the runtime actually started, with its own
  id and timestamp; it never rewrites the assignment.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves model annotations at runtime
from typing import Any, Optional
from uuid import UUID  # noqa: TC003 - pydantic resolves model annotations at runtime

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    AllocationOutcome,
    AssignmentReasonCode,
    ExposureOutcome,
    ExposureReasonCode,
)

_FROZEN = ConfigDict(extra="forbid", frozen=True)
_BASE = ConfigDict(extra="forbid")


class AssignmentV1(BaseModel):
    """The immutable agent profile assigned to one enrollment."""

    model_config = _FROZEN

    assignment_id: UUID
    enrollment_id: UUID
    study_id: UUID
    agent_profile_id: UUID
    strategy: str
    randomization_epoch: int = 0
    profile_digest: str
    profile_snapshot_json: dict[str, Any]
    status: str = "ACTIVE"
    assigned_at: datetime


class StudyProfileSelection(BaseModel):
    """A study-owned, digest-pinned profile available for random assignment."""

    model_config = _FROZEN

    study_id: UUID
    agent_profile_id: UUID
    profile_digest: str
    profile_snapshot_json: dict[str, Any]
    selection_order: int = 0


class ExposureEnvironment(BaseModel):
    """Host environment an exposure was attempted in."""

    model_config = _BASE

    os: str
    arch: str
    ide_build: Optional[str] = None
    plugin_version: Optional[str] = None
    host_kind: Optional[str] = None


class ExposureV1(BaseModel):
    """The durable record of one attempted runtime exposure."""

    model_config = _FROZEN

    exposure_id: UUID
    assignment_id: UUID
    study_id: UUID
    environment: ExposureEnvironment
    agent_release_id: str
    artifact_digest: Optional[str] = None
    adapter_version: Optional[str] = None
    observed_configuration: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    outcome: ExposureOutcome
    evidence_digest: Optional[str] = None
    idempotency_key: str
    created_at: datetime

    @property
    def is_exposure(self) -> bool:
        """Whether this receipt counts as an actual condition exposure."""
        return self.outcome.is_exposure


class AssignmentIssue(BaseModel):
    """One typed assignment/exposure rejection reason."""

    model_config = _BASE

    code: AssignmentReasonCode
    message: str
    field: str = ""


class AssignmentResult(BaseModel):
    """Typed outcome of an allocation attempt."""

    model_config = _BASE

    outcome: AllocationOutcome
    assignment: Optional[AssignmentV1] = None
    created: bool = False
    reason: AssignmentReasonCode = AssignmentReasonCode.OK
    issue: Optional[AssignmentIssue] = None


class ExposureIssue(BaseModel):
    """Typed exposure rejection reason."""

    model_config = _BASE

    code: ExposureReasonCode
    message: str
    field: str = ""


class ExposureResult(BaseModel):
    """Typed outcome of recording an exposure receipt."""

    model_config = _BASE

    accepted: bool
    exposure: Optional[ExposureV1] = None
    reused: bool = False
    is_exposure: bool = False
    reason: ExposureReasonCode = ExposureReasonCode.OK
    audit: list[str] = Field(default_factory=list)
    issue: Optional[ExposureIssue] = None
