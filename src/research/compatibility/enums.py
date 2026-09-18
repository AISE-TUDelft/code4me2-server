"""Closed vocabularies for the ACP compatibility gate.

Every value here is part of a persisted contract (receipt JSON, decision
responses, reason codes), so members are additive only: renaming or removing a
member is a breaking change to already-stored receipts.
"""

from __future__ import annotations

from enum import Enum


class CapabilityId(str, Enum):
    """Host/agent capability observed by a pinned fixture probe."""

    INITIALIZE = "INITIALIZE"
    TOOL_LIFECYCLE = "TOOL_LIFECYCLE"
    TOOL_UPDATES = "TOOL_UPDATES"
    PERMISSION_REQUEST = "PERMISSION_REQUEST"
    EDIT_PROPOSAL = "EDIT_PROPOSAL"
    DIFF = "DIFF"
    CANCELLATION = "CANCELLATION"
    SESSION_CLOSE = "SESSION_CLOSE"
    SESSION_LOAD = "SESSION_LOAD"
    PLANS = "PLANS"
    USAGE = "USAGE"


class CapabilityState(str, Enum):
    """Observed support state for one capability.

    ``UNKNOWN``, ``UNAVAILABLE``, ``PARTIAL`` and ``BROKEN`` are deliberately
    distinct from ``SUPPORTED``: an ambiguous or absent observation must never
    be promoted to a supported claim.
    """

    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"
    BROKEN = "BROKEN"


class Fidelity(str, Enum):
    """How faithfully the observation captures the underlying behavior."""

    EXACT = "EXACT"
    NORMALIZED = "NORMALIZED"
    INFERRED = "INFERRED"


class EnforcementOwner(str, Enum):
    """Which component actually enforces the observed capability."""

    HOST = "HOST"
    AGENT = "AGENT"
    PROXY = "PROXY"
    PLUGIN = "PLUGIN"
    UNKNOWN = "UNKNOWN"


class CompatibilityDecision(str, Enum):
    """Terminal outcome of evaluating required capabilities."""

    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE_ENVIRONMENT = "INCOMPATIBLE_ENVIRONMENT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    BROKEN = "BROKEN"


class CompatibilityReasonCode(str, Enum):
    """Machine-readable justification for a compatibility decision."""

    OK = "OK"

    # Receipt presence / integrity.
    MISSING_RECEIPT = "MISSING_RECEIPT"
    RECEIPT_UNVERIFIED = "RECEIPT_UNVERIFIED"
    RECEIPT_HASH_MISMATCH = "RECEIPT_HASH_MISMATCH"

    # Environment / identity mismatches.
    ENVIRONMENT_MISMATCH = "ENVIRONMENT_MISMATCH"
    IDE_BUILD_MISMATCH = "IDE_BUILD_MISMATCH"
    AI_ASSISTANT_BUILD_MISMATCH = "AI_ASSISTANT_BUILD_MISMATCH"
    PLUGIN_VERSION_MISMATCH = "PLUGIN_VERSION_MISMATCH"
    AGENT_ID_MISMATCH = "AGENT_ID_MISMATCH"
    AGENT_VERSION_MISMATCH = "AGENT_VERSION_MISMATCH"
    ADAPTER_VERSION_MISMATCH = "ADAPTER_VERSION_MISMATCH"
    PROTOCOL_VERSION_MISMATCH = "PROTOCOL_VERSION_MISMATCH"

    # Capability evidence gaps.
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    CAPABILITY_UNKNOWN = "CAPABILITY_UNKNOWN"
    CAPABILITY_PARTIAL = "CAPABILITY_PARTIAL"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    CAPABILITY_BROKEN = "CAPABILITY_BROKEN"

    # Fixture parsing.
    PARSE_FAILED = "PARSE_FAILED"
