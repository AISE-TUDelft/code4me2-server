"""Closed vocabularies for researcher read models (Issue 12).

Roles and reason codes are additive-only contracts: a new role or reason is a
new member, never a renamed one.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["ReadModelReasonCode", "ResearcherRole"]


class ResearcherRole(str, Enum):
    """Researcher access role granted per study.

    ``PRIVACY_OPERATOR`` is deliberately separate: it may read the private
    account-to-participant mapping but **never** telemetry content/read models.
    """

    OWNER = "OWNER"
    ANALYST = "ANALYST"
    VIEWER = "VIEWER"
    PRIVACY_OPERATOR = "PRIVACY_OPERATOR"


class ReadModelReasonCode(str, Enum):
    """Stable machine-readable reason a read-model request was rejected."""

    OK = "OK"
    FORBIDDEN_STUDY = "FORBIDDEN_STUDY"
    TELEMETRY_FORBIDDEN = "TELEMETRY_FORBIDDEN"
    NO_ROLE = "NO_ROLE"
    NO_REVISION = "NO_REVISION"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    INVALID_POPULATION = "INVALID_POPULATION"
    RETENTION_BLOCKED = "RETENTION_BLOCKED"
    WRITER_UNAVAILABLE = "WRITER_UNAVAILABLE"
