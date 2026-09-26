"""Closed vocabularies for the canonical telemetry schema (Issue 06).

Every member must have a real producer (the IDE collector, the shared ACP/proxy
normalizer, or the server); a member with no producer is removed rather than
kept as an unreachable reserved token, because the vocabulary is the persisted/
exported contract. Otherwise members are additive only: renaming a produced
member is a breaking change to already-stored events.

Fidelity note: :class:`CanonicalFidelity` uses the **lowercase** telemetry-spec
values (``exact`` / ``normalized`` / ``inferred``). It is deliberately distinct
from the **uppercase** receipt fidelity in
:class:`research.compatibility.enums.Fidelity` (``EXACT`` / ``NORMALIZED`` /
``INFERRED``); the two must not be conflated.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "CANONICAL_SCHEMA_VERSION",
    "CanonicalEventType",
    "CanonicalFidelity",
    "CoverageState",
    "EventSource",
    "FieldClass",
    "LifecycleState",
    "PolicyAction",
]

#: The canonical telemetry envelope schema version. It is required on every
#: event and is never defaulted on ingestion.
CANONICAL_SCHEMA_VERSION = "1"


class CanonicalEventType(str, Enum):
    """Canonical, vendor-independent event concepts.

    These are canonical concepts, not ACP method names: an ACP notification is
    *normalized into* one of these, and an unknown construct becomes
    :attr:`UNKNOWN_SOURCE_EVENT` rather than a new vendor-named type.

    Members are additive only. A member with no producer anywhere in the
    platform (plugin IDE collector, shared ACP/proxy normalizer, server) is not
    part of the vocabulary: it was removed rather than left as an unreachable
    reserved token.
    """

    INTERACTION_STARTED = "interaction.started"
    INTERACTION_COMPLETED = "interaction.completed"

    AGENT_RUN_STARTED = "agent.run.started"
    AGENT_RUN_COMPLETED = "agent.run.completed"

    AGENT_MESSAGE_STARTED = "agent.message.started"
    AGENT_MESSAGE_COMPLETED = "agent.message.completed"

    TOOL_CREATED = "tool.created"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"

    PERMISSION_REQUESTED = "permission.requested"
    PERMISSION_DECIDED = "permission.decided"

    PLAN_UPDATED = "plan.updated"
    USAGE_UPDATED = "usage.updated"

    IDE_DOCUMENT_CHANGED = "ide.document.changed"
    IDE_FILE_OPENED = "ide.file.opened"
    IDE_FILE_SAVED = "ide.file.saved"
    IDE_FILE_CLOSED = "ide.file.closed"
    IDE_RUN_EXECUTED = "ide.run.executed"

    SYSTEM_AGENT_CRASHED = "system.agent.crashed"
    SYSTEM_PROXY_ERROR = "system.proxy.error"

    AGENT_ERROR = "agent.error"

    UNKNOWN_SOURCE_EVENT = "unknown_source_event"


class EventSource(str, Enum):
    """Which observation channel emitted the source event (lowercase values)."""

    ACP = "acp"
    IDE = "ide"
    LEGACY = "legacy"
    # The backend inference relay observes provider model calls directly; it is
    # a distinct observation channel from the ACP proxy and the IDE collector,
    # so source ownership can prevent double counting the same action.
    RELAY = "relay"


class FieldClass(str, Enum):
    """Data classification used to decide whether a field may persist."""

    SYSTEM = "SYSTEM"
    BEHAVIORAL = "BEHAVIORAL"
    CODE_METADATA = "CODE_METADATA"
    CONTENT = "CONTENT"
    SECRET = "SECRET"


class PolicyAction(str, Enum):
    """Terminal privacy decision for one field (or the whole event)."""

    ALLOW = "ALLOW"
    REDACT = "REDACT"
    HASH = "HASH"
    DROP = "DROP"
    BLOCK = "BLOCK"


class CoverageState(str, Enum):
    """Whether a measurement/observation is present, absent, or disputed.

    ``UNAVAILABLE`` means "the source does not expose it" and ``UNKNOWN`` means
    "not observed"; neither is zero or false.
    """

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    PARTIAL = "PARTIAL"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class CanonicalFidelity(str, Enum):
    """How faithfully a canonical field represents its source observation.

    Lowercase by canonical-spec convention; distinct from the uppercase receipt
    fidelity vocabulary in Issue 01.
    """

    EXACT = "exact"
    NORMALIZED = "normalized"
    INFERRED = "inferred"


class LifecycleState(str, Enum):
    """Lifecycle phase of a lifecycled canonical event."""

    PENDING = "pending"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
