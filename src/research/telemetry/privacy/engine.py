"""Shared privacy policy engine: classification, redaction and hashing.

Redaction happens here, before any spool, log, retry queue, analytics, or
export. The engine is defensive in depth: even if a hand-crafted policy allows
everything, ``SECRET`` fields are dropped and a final recursive scan blocks any
residual secret shape from surviving serialization.

Policy actions:

* ``ALLOW`` - keep the value (``SYSTEM`` / ``BEHAVIORAL`` by default).
* ``REDACT`` - replace the value with a marker (``CONTENT`` without allowance).
* ``HASH`` - replace code metadata with a SHA-256 of its canonical JSON.
* ``DROP`` - remove the field entirely (``SECRET``).
* ``BLOCK`` - reject the whole event (explicitly blocked class).
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from research.canonical import canonical_hash
from research.study.protocol.enums import TelemetryFieldClass

from ..enums import FieldClass, PolicyAction
from ..models import CanonicalEventV1, PrivacySummary
from .classify import classify_field, contains_secret_value, looks_secret_value

REDACTED_MARKER = "[REDACTED]"

_BASE_CONFIG = ConfigDict(extra="forbid")


def _declared_field_classes(raw_classes: Any) -> set[FieldClass]:
    """Map a study's ``allowed_field_classes`` onto runtime field classes.

    Studies are authored in the study vocabulary (STRUCTURAL / METRICS /
    DIAGNOSTICS / CONTENT) and the study validator also accepts the runtime
    names (SYSTEM / BEHAVIORAL / CODE_METADATA / CONTENT). Both are honoured,
    with the same mapping ``from_revision_policy`` uses; unknown names are
    ignored. Without this, a policy such as ``[STRUCTURAL, …, CONTENT]`` would
    resolve to ``{CONTENT}`` alone and drop every structural field.
    """
    classes: set[FieldClass] = set()
    for raw in raw_classes if isinstance(raw_classes, (list, tuple, set)) else []:
        value = raw.value if isinstance(raw, TelemetryFieldClass) else str(raw).upper()
        if value == TelemetryFieldClass.STRUCTURAL.value:
            classes.update({FieldClass.SYSTEM, FieldClass.BEHAVIORAL})
        elif value == TelemetryFieldClass.METRICS.value:
            classes.add(FieldClass.SYSTEM)
        elif value == TelemetryFieldClass.DIAGNOSTICS.value:
            classes.add(FieldClass.BEHAVIORAL)
        else:
            try:
                classes.add(FieldClass(value))
            except ValueError:
                continue
    return classes


def runtime_field_classes(raw_classes: Any) -> list[str]:
    """The runtime class names a study's ``allowed_field_classes`` resolves to.

    Sorted and without ``SECRET``. An empty list means nothing usable was
    declared, which every consumer reads as the metadata default (SYSTEM,
    BEHAVIORAL, CODE_METADATA): ingestion here, the IDE plugin and the ACP
    proxy it configures. The bootstrap manifest carries this list because the
    plugin only understands the runtime vocabulary; given study names it
    would keep only the runtime names it recognises and drop the rest.
    """
    declared = _declared_field_classes(raw_classes) - {FieldClass.SECRET}
    return sorted(field_class.value for field_class in declared)


class PrivacyPolicy(BaseModel):
    """Resolved field policy for one revision + consent state.

    Derived from a revision ``TelemetryPolicy`` plus the participant's active
    consent; ``SECRET`` is never in the allowed set and is enforced separately.
    """

    model_config = _BASE_CONFIG

    allowed_field_classes: list[FieldClass] = Field(
        default_factory=lambda: [
            FieldClass.SYSTEM,
            FieldClass.BEHAVIORAL,
            FieldClass.CODE_METADATA,
        ]
    )
    # CONTENT is persisted only when explicitly allowed AND consent is active.
    content_allowed: bool = False
    consent_active: bool = False
    policy_digest: Optional[str] = None
    blocked_field_classes: list[FieldClass] = Field(default_factory=list)
    # "hash" (default) or "allow" for CODE_METADATA.
    code_metadata_mode: str = "hash"

    @classmethod
    def default(cls) -> PrivacyPolicy:
        """The deny-by-default policy: metadata only, no content capture."""
        return cls()

    @classmethod
    def from_study_policy(cls, telemetry_policy: Mapping[str, Any], *, consent_active: bool) -> PrivacyPolicy:
        """Resolve the current manifest policy using the IDE's field defaults.

        Accepts both the study-facing authoring vocabulary (STRUCTURAL /
        METRICS / DIAGNOSTICS / CONTENT, the same names study creation
        validates) and the runtime vocabulary (SYSTEM / BEHAVIORAL /
        CODE_METADATA / CONTENT) — mirroring ``from_revision_policy``. An
        authored allowlist the resolver doesn't understand must not silently
        degrade to the default: without this, a researcher writing the
        documented study vocabulary gets broader capture than they declared.
        Content always needs the explicit content_capture flag and consent.
        Code metadata is allowed when declared (or using the legacy/default
        metadata policy); otherwise clients must upload SHA-256 tokens.
        """
        # The same resolution the bootstrap manifest sends the IDE plugin
        # (``runtime_field_classes``), so client and server agree.
        declared = _declared_field_classes(telemetry_policy.get("allowed_field_classes") or []) - {FieldClass.SECRET}
        allowed = declared or {FieldClass.SYSTEM, FieldClass.BEHAVIORAL, FieldClass.CODE_METADATA}
        return cls(
            allowed_field_classes=sorted(allowed, key=lambda item: item.value),
            content_allowed=telemetry_policy.get("content_capture") is True,
            consent_active=consent_active,
            code_metadata_mode="allow" if FieldClass.CODE_METADATA in allowed else "hash",
            policy_digest=canonical_hash(dict(telemetry_policy)),
        )

    @classmethod
    def from_revision_policy(
        cls,
        telemetry_policy: Any,
        *,
        consent_active: bool,
        code_metadata_mode: str = "hash",
    ) -> PrivacyPolicy:
        """Resolve a revision ``TelemetryPolicy`` + consent into a policy."""
        classes: set[FieldClass] = set()
        content_allowed = False
        raw_classes = getattr(telemetry_policy, "allowed_field_classes", []) or []
        for raw in raw_classes:
            value = raw.value if isinstance(raw, TelemetryFieldClass) else str(raw).upper()
            if value == TelemetryFieldClass.STRUCTURAL.value:
                classes.update({FieldClass.SYSTEM, FieldClass.BEHAVIORAL})
            elif value == TelemetryFieldClass.METRICS.value:
                classes.add(FieldClass.SYSTEM)
            elif value == TelemetryFieldClass.DIAGNOSTICS.value:
                classes.add(FieldClass.BEHAVIORAL)
            elif value == TelemetryFieldClass.CONTENT.value:
                content_allowed = True
                classes.add(FieldClass.CONTENT)
            else:
                # Runtime vocabulary name (SYSTEM / BEHAVIORAL / CODE_METADATA /
                # CONTENT): honour it exactly rather than silently dropping it.
                try:
                    runtime_class = FieldClass(value)
                except ValueError:
                    continue
                if runtime_class is FieldClass.CONTENT:
                    content_allowed = True
                classes.add(runtime_class)

        if not classes:
            classes.update(
                {FieldClass.SYSTEM, FieldClass.BEHAVIORAL, FieldClass.CODE_METADATA}
            )

        digest = canonical_hash(
            {
                "allowed": sorted(field_class.value for field_class in classes),
                "content_allowed": content_allowed,
            }
        )
        return cls(
            allowed_field_classes=sorted(classes, key=lambda item: item.value),
            content_allowed=content_allowed,
            consent_active=consent_active,
            policy_digest=digest,
            code_metadata_mode=code_metadata_mode,
        )

    def action_for(self, field_class: FieldClass) -> PolicyAction:
        """Resolve the policy action for one field class."""
        if field_class in self.blocked_field_classes:
            return PolicyAction.BLOCK
        if field_class == FieldClass.SECRET:
            return PolicyAction.DROP
        if field_class == FieldClass.CONTENT:
            if self.content_allowed and self.consent_active:
                return PolicyAction.ALLOW
            return PolicyAction.REDACT
        if field_class == FieldClass.CODE_METADATA:
            return (
                PolicyAction.HASH
                if self.code_metadata_mode == "hash"
                else PolicyAction.ALLOW
            )
        if field_class in self.allowed_field_classes:
            return PolicyAction.ALLOW
        return PolicyAction.DROP


class FilterResult(BaseModel):
    """A filtered event plus the summary of what the filter did."""

    model_config = _BASE_CONFIG

    event: CanonicalEventV1
    summary: PrivacySummary


class _Blocked(Exception):
    def __init__(self, path: str, field_class: FieldClass) -> None:
        super().__init__(path)
        self.path = path
        self.field_class = field_class


_DROPPED = object()


def _hash_value(value: Any) -> str:
    # Filtering at ingestion must preserve values already hashed by a producer.
    if isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        return value
    return "sha256:" + canonical_hash({"value": value})


def _record(
    actions: dict[str, int], classes: dict[str, int], action: PolicyAction, field_class: FieldClass
) -> None:
    actions[action.value] = actions.get(action.value, 0) + 1
    classes[field_class.value] = classes.get(field_class.value, 0) + 1


def _apply(
    value: Any,
    policy: PrivacyPolicy,
    path: str,
    actions: dict[str, int],
    classes: dict[str, int],
    redacted: list[str],
) -> Any:
    if isinstance(value, Mapping):
        filtered: dict[Any, Any] = {}
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            field_class = classify_field(key, item)
            action = policy.action_for(field_class)
            _record(actions, classes, action, field_class)
            if action == PolicyAction.BLOCK:
                raise _Blocked(child, field_class)
            if action == PolicyAction.DROP:
                redacted.append(child)
                continue
            if action == PolicyAction.REDACT:
                filtered[key] = REDACTED_MARKER
                redacted.append(child)
                continue
            if action == PolicyAction.HASH:
                filtered[key] = _hash_value(item)
                redacted.append(child)
                continue
            filtered[key] = _apply(item, policy, child, actions, classes, redacted)
        return filtered

    if isinstance(value, (list, tuple)):
        items = []
        for index, item in enumerate(value):
            child = f"{path}[{index}]"
            result = _apply(item, policy, child, actions, classes, redacted)
            if result is not _DROPPED:
                items.append(result)
        return items

    if looks_secret_value(value):
        # Defense in depth for scalar list items with no classifying key.
        _record(actions, classes, PolicyAction.DROP, FieldClass.SECRET)
        redacted.append(path)
        return _DROPPED
    return value


def _auxiliary_secret_field(event: CanonicalEventV1) -> Optional[str]:
    """Name of the first non-payload envelope field carrying a secret, or None.

    The privacy policy applies to the whole envelope: provenance, correlations,
    lifecycle/unknown values and run identity are all scanned so a secret can
    never ride along outside ``payload``.
    """
    candidates: list[tuple[str, Any]] = [
        ("provenance", event.provenance.model_dump(mode="json")),
        ("correlations", event.correlations.model_dump(mode="json")),
        ("lifecycle_state", event.lifecycle_state),
        ("unknown_event_type", event.unknown_event_type),
        ("unknown_source", event.unknown_source),
        ("unknown_lifecycle_state", event.unknown_lifecycle_state),
        ("agent_run_id", event.agent_run_id),
    ]
    for name, value in candidates:
        if value and contains_secret_value(value):
            return name
    return None


def filter_event(event: CanonicalEventV1, policy: PrivacyPolicy) -> FilterResult:
    """Filter ``event`` under ``policy`` and return the event plus its summary."""
    actions: dict[str, int] = {}
    classes: dict[str, int] = {}
    redacted: list[str] = []
    blocked = False
    block_reason: Optional[str] = None

    try:
        filtered_payload: Any = _apply(
            event.payload, policy, "", actions, classes, redacted
        )
    except _Blocked as blocked_error:
        blocked = True
        block_reason = (
            f"field {blocked_error.path} classified "
            f"{blocked_error.field_class.value} is blocked by policy"
        )
        filtered_payload = {}

    if not blocked and contains_secret_value(filtered_payload):
        blocked = True
        block_reason = "residual secret detected after filtering"
        filtered_payload = {}

    # Secrets are excluded from every envelope field, not only the payload:
    # provenance, correlation ids, lifecycle/unknown values and run identity are
    # scanned too and fail closed. A residual secret anywhere blocks the event.
    if not blocked:
        auxiliary = _auxiliary_secret_field(event)
        if auxiliary is not None:
            blocked = True
            block_reason = f"residual secret detected in {auxiliary} after filtering"
            filtered_payload = {}

    summary = PrivacySummary(
        policy_digest=policy.policy_digest,
        actions=actions,
        redacted_fields=sorted(redacted),
        blocked=blocked,
        block_reason=block_reason,
        field_classes=classes,
    )
    filtered_event = event.model_copy(
        update={"payload": dict(filtered_payload), "privacy": summary}
    )
    return FilterResult(event=filtered_event, summary=summary)


def filter_payload(
    payload: Mapping[str, Any], policy: PrivacyPolicy
) -> tuple[dict[str, Any], PrivacySummary]:
    """Filter a bare payload mapping (convenience for tests and extractors)."""
    actions: dict[str, int] = {}
    classes: dict[str, int] = {}
    redacted: list[str] = []
    blocked = False
    block_reason: Optional[str] = None
    try:
        filtered: Any = _apply(payload, policy, "", actions, classes, redacted)
    except _Blocked as blocked_error:
        blocked = True
        block_reason = (
            f"field {blocked_error.path} classified "
            f"{blocked_error.field_class.value} is blocked by policy"
        )
        filtered = {}
    if not blocked and contains_secret_value(filtered):
        blocked = True
        block_reason = "residual secret detected after filtering"
        filtered = {}
    summary = PrivacySummary(
        policy_digest=policy.policy_digest,
        actions=actions,
        redacted_fields=sorted(redacted),
        blocked=blocked,
        block_reason=block_reason,
        field_classes=classes,
    )
    return dict(filtered), summary
