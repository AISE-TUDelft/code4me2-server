"""Per-event validation for ingestion batches.

Validation never repairs or reclassifies an event. It returns typed
:class:`~research.telemetry.ingestion.models.IngestionIssue` findings: schema/version/
provenance/context problems and a defense-in-depth sensitive-field scan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from research.telemetry.validation import (
    TelemetryValidationCode,
    validate_canonical_event,
)

from .enums import IngestionReasonCode
from .models import IngestionContext, IngestionIssue

if TYPE_CHECKING:
    from research.telemetry.models import CanonicalEventV1

__all__ = ["validate_batch_event"]

_SCHEMA_VERSION = "1"

_CODE_MAP: dict[TelemetryValidationCode, IngestionReasonCode] = {
    TelemetryValidationCode.SCHEMA_INVALID: IngestionReasonCode.INVALID_SCHEMA,
    TelemetryValidationCode.SCHEMA_VERSION_UNSUPPORTED: (
        IngestionReasonCode.UNKNOWN_SCHEMA_VERSION
    ),
    TelemetryValidationCode.SCHEMA_VERSION_MISSING: (
        IngestionReasonCode.UNKNOWN_SCHEMA_VERSION
    ),
    TelemetryValidationCode.MISSING_EVENT_ID: IngestionReasonCode.MISSING_EVENT_ID,
    TelemetryValidationCode.MISSING_PROVENANCE: IngestionReasonCode.MISSING_PROVENANCE,
    TelemetryValidationCode.MISSING_EMITTER_ID: IngestionReasonCode.INVALID_SCHEMA,
    TelemetryValidationCode.INVALID_EMITTER_SEQUENCE: IngestionReasonCode.INVALID_SCHEMA,
    TelemetryValidationCode.MISSING_OCCURRED_AT: IngestionReasonCode.INVALID_SCHEMA,
    TelemetryValidationCode.SECRET_PRESENT: IngestionReasonCode.SENSITIVE_PAYLOAD,
    TelemetryValidationCode.PRIVACY_BLOCKED: IngestionReasonCode.PRIVACY_BLOCKED,
}


def _issue(
    code: IngestionReasonCode, field: str, message: str
) -> IngestionIssue:
    return IngestionIssue(code=code, field=field, message=message, permanent=True)


def validate_batch_event(
    event: CanonicalEventV1,
    context: Optional[IngestionContext],
    *,
    telemetry_schema_version: str = _SCHEMA_VERSION,
) -> list[IngestionIssue]:
    """Return every permanent reason ``event`` cannot be stored."""
    issues: list[IngestionIssue] = []

    if event.schema_version != telemetry_schema_version:
        issues.append(
            _issue(
                IngestionReasonCode.UNKNOWN_SCHEMA_VERSION,
                "schema_version",
                (
                    f"event schema_version {event.schema_version!r} does not match "
                    f"the batch telemetry_schema_version {telemetry_schema_version!r}"
                ),
            )
        )
    elif event.schema_version != _SCHEMA_VERSION:
        issues.append(
            _issue(
                IngestionReasonCode.UNKNOWN_SCHEMA_VERSION,
                "schema_version",
                f"unsupported telemetry schema version {event.schema_version!r}",
            )
        )

    for finding in validate_canonical_event(event):
        mapped = _CODE_MAP.get(finding.code)
        if mapped is None:
            # ``needs_review`` findings (unknown type/source) are preserved, not
            # rejected; the stored record retains the unknown token and coverage.
            continue
        issues.append(_issue(mapped, finding.field, finding.message))

    if not event.provenance:
        issues.append(
            _issue(
                IngestionReasonCode.MISSING_PROVENANCE,
                "provenance",
                "provenance is required to persist a canonical event",
            )
        )

    if context is not None:
        if event.study_id is not None and event.study_id != context.study_id:
            issues.append(
                _issue(
                    IngestionReasonCode.CONTEXT_MISMATCH,
                    "study_id",
                    "event study does not match the authorized study",
                )
            )
        if (
            event.enrollment_id is not None
            and event.enrollment_id != context.enrollment_id
        ):
            issues.append(
                _issue(
                    IngestionReasonCode.CONTEXT_MISMATCH,
                    "enrollment_id",
                    "event enrollment does not match the authorized enrollment",
                )
            )
        if (
            event.research_session_id is not None
            and event.research_session_id != context.research_session_id
        ):
            issues.append(
                _issue(
                    IngestionReasonCode.SESSION_OUT_OF_SCOPE,
                    "research_session_id",
                    "event belongs to a different research session",
                )
            )

    return issues
