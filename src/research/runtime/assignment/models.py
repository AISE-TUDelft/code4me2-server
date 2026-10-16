"""Pydantic v2 contracts for enrollment-scoped profile assignment.

:class:`AssignmentV1` is the server-authoritative, sticky agent profile
allocated for one enrollment. It is never re-randomized; launch outcomes are
recorded against ``agent_run_id`` in the canonical run/event model.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves model annotations at runtime
from typing import Any, Optional
from uuid import UUID  # noqa: TC003 - pydantic resolves model annotations at runtime

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    AllocationOutcome,
    AssignmentReasonCode,
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


class AssignmentIssue(BaseModel):
    """One typed assignment rejection reason."""

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

