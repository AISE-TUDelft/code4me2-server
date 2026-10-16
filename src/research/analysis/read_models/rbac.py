"""Scoped researcher RBAC (Issue 12).

Authorization is per study. A researcher with a role on one study is rejected for
another study: scope is never partially widened by role alone. The privacy
operator capability is separate from telemetry read access.
"""

from __future__ import annotations

from typing import Iterable, Optional
from uuid import UUID  # noqa: TC003 - pydantic resolves ResearcherGrant.study_id at runtime

from pydantic import BaseModel, ConfigDict

from .enums import ReadModelReasonCode, ResearcherRole

__all__ = [
    "ReadModelAuthorizationError",
    "ResearcherGrant",
    "can_read_private_mapping",
    "can_read_study",
    "can_read_telemetry",
    "require_study_access",
    "require_telemetry_access",
    "role_for_study",
]

_BASE = ConfigDict(extra="forbid")

_TELEMETRY_ROLES = frozenset(
    {ResearcherRole.OWNER, ResearcherRole.ANALYST, ResearcherRole.VIEWER}
)
_PRIVATE_MAPPING_ROLES = frozenset({ResearcherRole.PRIVACY_OPERATOR})


class ResearcherGrant(BaseModel):
    """One researcher's role grant on one study."""

    model_config = _BASE

    researcher_id: str
    study_id: UUID
    role: ResearcherRole


class ReadModelAuthorizationError(PermissionError):
    """Typed authorization failure for a read-model/export request."""

    def __init__(self, code: ReadModelReasonCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def role_for_study(
    grants: Iterable[ResearcherGrant],
    researcher_id: str,
    study_id: UUID,
) -> Optional[ResearcherRole]:
    """Return the researcher's role on ``study_id``, or ``None``."""
    for grant in grants:
        if grant.researcher_id == researcher_id and grant.study_id == study_id:
            return grant.role
    return None


def can_read_study(
    role: Optional[ResearcherRole],
    study_id: UUID,
    *,
    permitted_study_ids: Iterable[UUID],
) -> bool:
    """Whether ``role`` may read ``study_id``.

    Requires BOTH an assigned role and an explicit study permission. A role on a
    different study never grants access here.
    """
    if role is None:
        return False
    permitted = set(permitted_study_ids)
    return study_id in permitted


def can_read_telemetry(role: Optional[ResearcherRole]) -> bool:
    """Whether ``role`` may read telemetry read models/exports.

    Privacy operators are excluded: they work with the private mapping, never
    telemetry content.
    """
    return role in _TELEMETRY_ROLES


def can_read_private_mapping(role: Optional[ResearcherRole]) -> bool:
    """Whether ``role`` may read the private account-to-participant mapping."""
    return role in _PRIVATE_MAPPING_ROLES


def require_study_access(
    grants: Iterable[ResearcherGrant],
    researcher_id: str,
    study_id: UUID,
) -> ResearcherRole:
    """Return the role or raise a typed authorization error."""
    grants = list(grants)
    role = role_for_study(grants, researcher_id, study_id)
    permitted = [grant.study_id for grant in grants if grant.researcher_id == researcher_id]
    if not can_read_study(role, study_id, permitted_study_ids=permitted):
        # Distinguish "this researcher has no grants at all" from "this
        # researcher has grants elsewhere but not on this study".
        code = (
            ReadModelReasonCode.NO_ROLE
            if not permitted
            else ReadModelReasonCode.FORBIDDEN_STUDY
        )
        raise ReadModelAuthorizationError(
            code, f"researcher is not authorized for study {study_id}"
        )
    assert role is not None
    return role


def require_telemetry_access(role: ResearcherRole) -> None:
    """Raise when ``role`` may not read telemetry read models."""
    if not can_read_telemetry(role):
        raise ReadModelAuthorizationError(
            ReadModelReasonCode.TELEMETRY_FORBIDDEN,
            f"role {role.value} may not read telemetry read models",
        )
