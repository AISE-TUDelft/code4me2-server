"""Closed vocabularies for runtime packaging (Issue 11).

Every member is part of a persisted or reported contract (package manifests,
verification results). Members are additive only.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["PackageReasonCode"]


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
    RECIPE_TESTS_FAILED = "RECIPE_TESTS_FAILED"
    RELEASE_DISABLED = "RELEASE_DISABLED"
    RELEASE_NOT_FOUND = "RELEASE_NOT_FOUND"
