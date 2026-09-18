"""Pydantic v2 contracts for derived telemetry projections (Issue 09).

Projections are derived, versioned views over immutable canonical events. A
missing measurement is represented as ``value: null`` plus an explicit coverage
state, never as a zero-valued measurement, and a projection never replaces the
source events.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves model annotations at runtime
from typing import Optional
from uuid import UUID  # noqa: TC003 - pydantic resolves model annotations at runtime

from pydantic import BaseModel, ConfigDict, Field

from research.telemetry.enums import CoverageState

_FROZEN = ConfigDict(extra="forbid", frozen=True)
_BASE = ConfigDict(extra="forbid")

COVERAGE_VERSION = "coverage-v1"
DERIVATION_VERSION = "derived-v1"


class FamilyCoverage(BaseModel):
    """Explicit per-event-family coverage counts."""

    model_config = _FROZEN

    family: str
    total: int
    available: int = 0
    unavailable: int = 0
    partial: int = 0
    unknown: int = 0
    needs_review: int = 0


class TelemetryCoverageV1(BaseModel):
    """Versioned coverage projection for one revision and population."""

    model_config = _FROZEN

    revision_id: UUID
    population: str
    coverage_version: str = COVERAGE_VERSION
    families: list[FamilyCoverage] = Field(default_factory=list)
    denominators: dict[str, int] = Field(default_factory=dict)
    computed_at: Optional[datetime] = None


class DerivedMetricV1(BaseModel):
    """A versioned derived metric that preserves coverage semantics.

    ``value`` is ``None`` when the metric cannot be computed (no observed
    denominator); that is a coverage statement, never a zero measurement.
    """

    model_config = _FROZEN

    metric_id: str
    numerator: Optional[int] = None
    denominator: Optional[int] = None
    coverage_predicate: str
    derivation_version: str = DERIVATION_VERSION
    value: Optional[float] = None
    coverage_state: CoverageState = CoverageState.UNKNOWN
    source_event_count: int = 0


class SessionRunSummaryV1(BaseModel):
    """A per-session summary that retains source provenance."""

    model_config = _FROZEN

    research_session_id: Optional[UUID] = None
    event_count: int = 0
    family_counts: dict[str, int] = Field(default_factory=dict)
    agent_run_ids: list[str] = Field(default_factory=list)
    first_occurred_at: Optional[datetime] = None
    last_occurred_at: Optional[datetime] = None
    derivation_version: str = DERIVATION_VERSION
