"""Researcher read models and scoped RBAC (Issue 12).

Read models carry study/profile identity, coverage/derivation version, and the
explicit population definition. Unavailable data is ``null`` + a coverage state,
never zero.

Public surface:

* :mod:`research.analysis.read_models.enums` - researcher roles and typed reason codes.
* :mod:`research.analysis.read_models.models` - ``StudySummaryV1``,
    ``EnrollmentCoverageV1``, ``TelemetryCoverageV1``,
  ``DerivedMetricV1``.
* :mod:`research.analysis.read_models.rbac` - per-study authorization (privacy operator
  access is separate from telemetry read).
* :mod:`research.analysis.read_models.service` - pure builders.
* :mod:`research.analysis.read_models.store` - Session-supplied queries.

The core package never imports ``App``, FastAPI, or a session factory.
"""

from .enums import ReadModelReasonCode, ResearcherRole
from .models import (
    COVERAGE_VERSION,
    DERIVATION_VERSION,
    DerivedMetricV1,
    EnrollmentCoverageV1,
    FamilyCoverage,
    ReadModelIssue,
    StudySummaryV1,
    TelemetryCoverageV1,
)
from .rbac import (
    ReadModelAuthorizationError,
    ResearcherGrant,
    can_read_private_mapping,
    can_read_study,
    can_read_telemetry,
    require_study_access,
    require_telemetry_access,
    role_for_study,
)
from .service import (
    build_derived_metrics,
    build_enrollment_coverage,
    build_study_summary,
    build_telemetry_coverage,
    population_definition,
)

__all__ = [
    "COVERAGE_VERSION",
    "DERIVATION_VERSION",
    "DerivedMetricV1",
    "EnrollmentCoverageV1",
    "FamilyCoverage",
    "ReadModelAuthorizationError",
    "ReadModelIssue",
    "ReadModelReasonCode",
    "ResearcherGrant",
    "ResearcherRole",
    "StudySummaryV1",
    "TelemetryCoverageV1",
    "build_derived_metrics",
    "build_enrollment_coverage",
    "build_revision_summary",
    "build_telemetry_coverage",
    "can_read_private_mapping",
    "can_read_study",
    "can_read_telemetry",
    "population_definition",
    "require_study_access",
    "require_telemetry_access",
    "role_for_study",
]
