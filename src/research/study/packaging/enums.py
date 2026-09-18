"""Closed vocabularies for runtime packaging and conformance (Issue 11).

Every member is part of a persisted or reported contract (package manifests,
verification results, conformance receipts). Members are additive only, and the
conformance statuses preserve ``UNSUPPORTED``/``UNKNOWN`` so a missing capability
can never be mistaken for a pass.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["ConformanceStatus", "PackageReasonCode", "PrerequisiteState"]


class ConformanceStatus(str, Enum):
    """Truthful outcome of one conformance case (or a whole receipt).

    ``PASS`` is the only status that supports a qualified advertised behavior.
    ``UNSUPPORTED``/``UNKNOWN`` are explicit non-passes, and ``BLOCKED`` means
    the case could not be exercised at all.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"


class PrerequisiteState(str, Enum):
    """Whether a case prerequisite is established, unsupported, or unknown."""

    ESTABLISHED = "ESTABLISHED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


class PackageReasonCode(str, Enum):
    """Stable machine-readable reason a package/component was blocked."""

    OK = "OK"

    # Manifest structure.
    INVALID_MANIFEST = "INVALID_MANIFEST"
    SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"
    EMPTY_COMPONENTS = "EMPTY_COMPONENTS"
    DUPLICATE_PLATFORM = "DUPLICATE_PLATFORM"

    # Path containment.
    PATH_ESCAPE = "PATH_ESCAPE"
    PATH_UNSAFE = "PATH_UNSAFE"

    # Integrity.
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    SIGNATURE_MISMATCH = "SIGNATURE_MISMATCH"
    ARTIFACT_MISSING = "ARTIFACT_MISSING"
    MISSING_COMPONENT = "MISSING_COMPONENT"
    NOT_EXECUTABLE = "NOT_EXECUTABLE"

    # Archive hygiene.
    UNDECLARED_EXECUTABLE = "UNDECLARED_EXECUTABLE"
    SECRET_FILE_PRESENT = "SECRET_FILE_PRESENT"

    # Selection.
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    BOOTSTRAP_DIGEST_MISMATCH = "BOOTSTRAP_DIGEST_MISMATCH"

    # Self-check / tooling.
    SELF_CHECK_FAILED = "SELF_CHECK_FAILED"
    WRITER_UNAVAILABLE = "WRITER_UNAVAILABLE"

    # Qualification.
    CONFORMANCE_NOT_PASSED = "CONFORMANCE_NOT_PASSED"
    RECEIPT_HOST_MISMATCH = "RECEIPT_HOST_MISMATCH"
    RELEASE_NOT_FOUND = "RELEASE_NOT_FOUND"
