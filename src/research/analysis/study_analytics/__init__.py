"""Researcher study analytics: participant table, dashboard and arm comparison.

Issue 03 of run ``2026-09-24-research-ui-overhaul``. Read-only, study-scoped
read models over the canonical telemetry store:

* :mod:`research.analysis.study_analytics.models` - plain row types;
* :mod:`research.analysis.study_analytics.store` - read-only SQL (metadata JSONB
  paths only, retention tombstones excluded, no login identity);
* :mod:`research.analysis.study_analytics.metrics` - pure participant-level
  metrics and the three response builders.

The participant (enrollment) is the randomisation unit: arms are compared on
per-participant values, following the agent-trace analyses of SWE-chat and the
work built on it. The package never imports ``App``, FastAPI or a session
factory.
"""

from .metrics import (
    FALLBACK_MAX_CONTEXT_TOKENS,
    METRIC_KEYS,
    analyze_participant,
    build_participant_detail,
    build_participants,
    build_study_summary,
)
from .models import (
    ArmRow,
    AssignmentRow,
    DailyEventCount,
    DateWindow,
    EnrollmentRow,
    EventRow,
    SessionRow,
    StudyFrame,
)

__all__ = [
    "ArmRow",
    "AssignmentRow",
    "DailyEventCount",
    "DateWindow",
    "EnrollmentRow",
    "EventRow",
    "FALLBACK_MAX_CONTEXT_TOKENS",
    "METRIC_KEYS",
    "SessionRow",
    "StudyFrame",
    "analyze_participant",
    "build_participant_detail",
    "build_participants",
    "build_study_summary",
]
