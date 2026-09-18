"""Typed validation for canonical telemetry envelopes.

Validation never raises for data: it returns a list of typed
:class:`TelemetryValidationError`. Unknown-but-preserved values produce a
``NEEDS_REVIEW`` finding rather than a hard rejection, while structural
problems (missing id, missing provenance, bad sequence) are errors.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Union

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from .enums import CanonicalEventType, EventSource
from .models import CanonicalEventV1
from .privacy.classify import contains_secret_value

__all__ = [
    "TelemetryValidationCode",
    "TelemetryValidationError",
    "validate_canonical_event",
]

_SCHEMA_VERSION = "1"
_KNOWN_EVENT_TYPES = frozenset(event_type.value for event_type in CanonicalEventType)
_KNOWN_SOURCES = frozenset(source.value for source in EventSource)


class TelemetryValidationCode(str, Enum):
    """Stable, machine-readable canonical-event validation reason codes."""

    SCHEMA_INVALID = "SCHEMA_INVALID"
    SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"
    SCHEMA_VERSION_MISSING = "SCHEMA_VERSION_MISSING"
    MISSING_EVENT_ID = "MISSING_EVENT_ID"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    MISSING_EMITTER_ID = "MISSING_EMITTER_ID"
    INVALID_EMITTER_SEQUENCE = "INVALID_EMITTER_SEQUENCE"
    MISSING_OCCURRED_AT = "MISSING_OCCURRED_AT"
    UNKNOWN_EVENT_TYPE = "UNKNOWN_EVENT_TYPE"
    UNKNOWN_SOURCE = "UNKNOWN_SOURCE"
    SECRET_PRESENT = "SECRET_PRESENT"
    PRIVACY_BLOCKED = "PRIVACY_BLOCKED"


class TelemetryValidationError(BaseModel):
    """One typed validation finding."""

    model_config = ConfigDict(extra="forbid")

    code: TelemetryValidationCode
    field: str
    message: str
    needs_review: bool = False


def _issue(
    code: TelemetryValidationCode, field: str, message: str, *, needs_review: bool = False
) -> TelemetryValidationError:
    return TelemetryValidationError(
        code=code, field=field, message=message, needs_review=needs_review
    )


def _validate_model(event: CanonicalEventV1) -> list[TelemetryValidationError]:
    errors: list[TelemetryValidationError] = []

    if event.schema_version != _SCHEMA_VERSION:
        errors.append(
            _issue(
                TelemetryValidationCode.SCHEMA_VERSION_UNSUPPORTED,
                "schema_version",
                f"unsupported schema_version {event.schema_version!r}",
            )
        )

    if event.event_id is None:
        errors.append(
            _issue(
                TelemetryValidationCode.MISSING_EVENT_ID, "event_id", "event_id is required"
            )
        )

    if event.provenance is None:
        errors.append(
            _issue(
                TelemetryValidationCode.MISSING_PROVENANCE,
                "provenance",
                "provenance is required",
            )
        )

    if not event.emitter_id or not str(event.emitter_id).strip():
        errors.append(
            _issue(
                TelemetryValidationCode.MISSING_EMITTER_ID,
                "emitter_id",
                "emitter_id is required for per-emitter ordering",
            )
        )

    if event.emitter_sequence is None or event.emitter_sequence < 1:
        errors.append(
            _issue(
                TelemetryValidationCode.INVALID_EMITTER_SEQUENCE,
                "emitter_sequence",
                "emitter_sequence must be a strictly positive integer",
            )
        )

    if event.occurred_at is None:
        errors.append(
            _issue(
                TelemetryValidationCode.MISSING_OCCURRED_AT,
                "occurred_at",
                "occurred_at is required",
            )
        )

    if event.privacy.blocked:
        errors.append(
            _issue(
                TelemetryValidationCode.PRIVACY_BLOCKED,
                "privacy.blocked",
                "the privacy filter blocked this event; a blocked event is never persistable",
            )
        )

    if event.event_type not in _KNOWN_EVENT_TYPES:
        errors.append(
            _issue(
                TelemetryValidationCode.UNKNOWN_EVENT_TYPE,
                "event_type",
                f"unrecognized event_type {event.event_type!r}",
                needs_review=True,
            )
        )
    elif event.unknown_event_type is not None:
        errors.append(
            _issue(
                TelemetryValidationCode.UNKNOWN_EVENT_TYPE,
                "unknown_event_type",
                f"source event type {event.unknown_event_type!r} was preserved",
                needs_review=True,
            )
        )

    if event.source not in _KNOWN_SOURCES:
        errors.append(
            _issue(
                TelemetryValidationCode.UNKNOWN_SOURCE,
                "source",
                f"unrecognized source {event.source!r}",
                needs_review=True,
            )
        )
    elif event.unknown_source is not None:
        errors.append(
            _issue(
                TelemetryValidationCode.UNKNOWN_SOURCE,
                "unknown_source",
                f"source {event.unknown_source!r} was preserved",
                needs_review=True,
            )
        )

    # Defense in depth: a filtered event must not still carry secret-shaped
    # material anywhere it can be persisted. Provenance and correlation handles
    # are attacker-influenced wire fields, so they are scanned too (not only the
    # payload/metrics).
    for field_name, value in (
        ("payload", event.payload),
        ("metrics", event.metrics.model_dump(mode="json")),
        ("provenance", event.provenance.model_dump(mode="json")),
        ("correlations", event.correlations.model_dump(mode="json")),
        ("lifecycle_state", event.lifecycle_state),
        ("unknown_event_type", event.unknown_event_type),
        ("unknown_source", event.unknown_source),
        ("unknown_lifecycle_state", event.unknown_lifecycle_state),
    ):
        if contains_secret_value(value):
            errors.append(
                _issue(
                    TelemetryValidationCode.SECRET_PRESENT,
                    field_name,
                    f"{field_name} contains secret material and must be filtered first",
                )
            )

    return errors


def validate_canonical_event(
    event_or_dict: Union[CanonicalEventV1, Mapping[str, Any]],
) -> list[TelemetryValidationError]:
    """Return every typed validation finding for a canonical event."""
    if isinstance(event_or_dict, CanonicalEventV1):
        return _validate_model(event_or_dict)

    if not isinstance(event_or_dict, Mapping):
        return [
            _issue(
                TelemetryValidationCode.SCHEMA_INVALID,
                "",
                "canonical event must be a CanonicalEventV1 or a mapping",
            )
        ]

    try:
        event = CanonicalEventV1.model_validate(dict(event_or_dict))
    except PydanticValidationError as error:
        details = error.errors()
        # A missing schema_version is its own typed reason: the envelope version
        # is never defaulted, so "absent" must be distinguishable from
        # "present but unsupported".
        for detail in details:
            location = tuple(detail.get("loc", ()))
            if detail.get("type") == "missing" and location == ("schema_version",):
                return [
                    _issue(
                        TelemetryValidationCode.SCHEMA_VERSION_MISSING,
                        "schema_version",
                        "schema_version is required; it is never defaulted on ingestion",
                    )
                ]
        first = details[0] if details else {}
        field = ".".join(str(part) for part in first.get("loc", ()))
        return [
            _issue(
                TelemetryValidationCode.SCHEMA_INVALID,
                field,
                str(first.get("msg", "canonical event failed schema validation")),
            )
        ]

    return _validate_model(event)
