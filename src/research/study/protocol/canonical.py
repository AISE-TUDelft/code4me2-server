"""Deterministic canonical serialization and SHA-256 digest for study protocols.

This module reuses the Issue 01 canonical helpers
(:mod:`research.compatibility.canonical`) rather than reimplementing JSON
sorting and hashing. It adds one protocol-specific concern: normalizing
*set-like* arrays so that an equivalent document hashes identically no matter
how its lists were authored.

Canonicalization rules
----------------------

* Map keys are sorted (delegated to ``canonical_json``).
* ``conditions`` are sorted by ``condition_id``; condition order is not
  meaningful for execution because assignment selects by id and weight.
* ``environment_requirements.required_capabilities`` are sorted by
  ``(capability, require_state)``, ``consent.locale_refs`` and
  ``assignment.stratification`` are sorted lexically, and ``survey_hooks`` are
  sorted by ``(survey_id, trigger)``.
* A condition's unset ``resolved_distribution`` is omitted: it is absent on a
  draft and populated by the server at publication, so omitting it keeps a
  draft's digest independent of an optional pin that is not part of the draft.
* Everything else is preserved as-is, including ``None`` (inherited/default)
  and :class:`~research.study.protocol.models.ExplicitUnknown` values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from research.compatibility.canonical import (  # re-exported for callers
    canonical_bytes,
    canonical_hash,
    canonical_json,
)

if TYPE_CHECKING:
    from .models import StudyProtocolV1

__all__ = [
    "canonical_bytes",
    "canonical_hash",
    "canonical_json",
    "canonicalize_protocol",
    "protocol_canonical_bytes",
    "protocol_canonical_json",
    "protocol_digest",
]


def _sorted_list(values: Any, key) -> list[Any]:
    if not isinstance(values, list):
        return []
    return sorted(values, key=key)


def canonicalize_protocol(protocol: StudyProtocolV1) -> dict[str, Any]:
    """Return the canonical, order-normalized mapping for ``protocol``."""
    data = protocol.model_dump(mode="json")

    conditions = data.get("conditions")
    if isinstance(conditions, list):
        data["conditions"] = sorted(
            conditions,
            key=lambda item: (
                str(item.get("condition_id", "")),
                str(item.get("name") or ""),
            ),
        )
        # An unset frozen pin is semantically absent on a draft, so it is
        # omitted from the canonical bytes rather than serialized as ``null``.
        for item in data["conditions"]:
            if not isinstance(item, dict):
                continue
            if item.get("resolved_distribution") is None:
                item.pop("resolved_distribution", None)

    environment = data.get("environment_requirements")
    if isinstance(environment, dict):
        environment["required_capabilities"] = _sorted_list(
            environment.get("required_capabilities"),
            key=lambda item: (
                str(item.get("capability", "")),
                str(item.get("require_state", "")),
            ),
        )

    telemetry = data.get("telemetry_policy")
    if isinstance(telemetry, dict):
        telemetry["allowed_field_classes"] = _sorted_list(
            telemetry.get("allowed_field_classes"), key=str
        )

    assignment = data.get("assignment")
    if isinstance(assignment, dict) and isinstance(
        assignment.get("stratification"), list
    ):
        assignment["stratification"] = _sorted_list(
            assignment["stratification"], key=str
        )

    survey_hooks = data.get("survey_hooks")
    if isinstance(survey_hooks, list):
        data["survey_hooks"] = sorted(
            survey_hooks,
            key=lambda item: (
                str(item.get("survey_id", "")),
                str(item.get("trigger", "")),
            ),
        )

    governance = data.get("schema_governance")
    if isinstance(governance, dict):
        governance["compatible_from_schema_versions"] = _sorted_list(
            governance.get("compatible_from_schema_versions"), key=str
        )

    return data


def _digest_mapping(protocol: StudyProtocolV1) -> dict[str, Any]:
    """Canonical mapping with resolution metadata excluded from the digest.

    ``resolved_at`` records *when* a pin was resolved, not the pin's semantic
    identity, so it is dropped from the digest preimage: re-publishing identical
    content stays content-addressed regardless of wall-clock resolution time.
    The timestamp is still persisted in the stored ``protocol_json``.
    """
    data = canonicalize_protocol(protocol)
    for item in data.get("conditions", []) or []:
        if not isinstance(item, dict):
            continue
        frozen = item.get("resolved_distribution")
        if isinstance(frozen, dict):
            frozen.pop("resolved_at", None)
            # An absent frozen profile config is semantically absent (legacy
            # revisions), so it must not change a document's digest.
            if frozen.get("agent_config") is None:
                frozen.pop("agent_config", None)
    return data


def protocol_canonical_json(protocol: StudyProtocolV1) -> str:
    """Canonical JSON string for a protocol (sorted keys, compact separators)."""
    return canonical_json(_digest_mapping(protocol))


def protocol_canonical_bytes(protocol: StudyProtocolV1) -> bytes:
    """UTF-8 encoded :func:`protocol_canonical_json`."""
    return protocol_canonical_json(protocol).encode("utf-8")


def protocol_digest(protocol: StudyProtocolV1) -> str:
    """SHA-256 hex digest of the canonical protocol document."""
    return canonical_hash(_digest_mapping(protocol))


def digest_matches(protocol: StudyProtocolV1, expected_digest: str) -> bool:
    """Whether ``expected_digest`` equals the canonical digest of ``protocol``."""
    return bool(expected_digest) and protocol_digest(protocol) == expected_digest
