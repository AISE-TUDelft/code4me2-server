"""Derived telemetry coverage and metric projections (Issue 09).

Projections are versioned, derived views over immutable canonical events. They
never replace source events, and missing capability data is represented as
``value: null`` with an explicit coverage state rather than a zero measurement.
"""

from .models import (
    COVERAGE_VERSION,
    DERIVATION_VERSION,
    DerivedMetricV1,
    FamilyCoverage,
    SessionRunSummaryV1,
    TelemetryCoverageV1,
)
from .service import (
    as_canonical_event,
    coverage_by_family,
    derive_event_count_metric,
    derive_usage_metric,
    event_family,
    session_summary,
)

__all__ = [
    "COVERAGE_VERSION",
    "DERIVATION_VERSION",
    "DerivedMetricV1",
    "FamilyCoverage",
    "SessionRunSummaryV1",
    "TelemetryCoverageV1",
    "as_canonical_event",
    "coverage_by_family",
    "derive_event_count_metric",
    "derive_usage_metric",
    "event_family",
    "session_summary",
]
