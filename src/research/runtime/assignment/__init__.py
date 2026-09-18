"""Enrollment-scoped assignment and exposure (Issue 05).

Public surface:

* :mod:`research.runtime.assignment.enums` - allocation/exposure outcomes and typed
  reason codes.
* :mod:`research.runtime.assignment.models` - ``AssignmentV1`` and ``ExposureV1`` (two
  separate immutable facts) plus typed results.
* :mod:`research.runtime.assignment.service` - server-authoritative, sticky, weighted
  allocation over ``enrollment_id``.
* :mod:`research.runtime.assignment.exposure` - idempotent exposure receipts.
* :mod:`research.runtime.assignment.store` - persistence adapters taking a caller-
  supplied SQLAlchemy ``Session``.

The core package never imports ``App``, FastAPI, or a session factory.
"""

from .enums import (
    AllocationOutcome,
    AssignmentReasonCode,
    ExposureOutcome,
    ExposureReasonCode,
)
from .exposure import record_exposure
from .models import (
    AssignmentIssue,
    AssignmentResult,
    AssignmentV1,
    ExposureEnvironment,
    ExposureIssue,
    ExposureResult,
    ExposureV1,
)
from .service import allocate

__all__ = [
    "AllocationOutcome",
    "AssignmentIssue",
    "AssignmentReasonCode",
    "AssignmentResult",
    "AssignmentV1",
    "ExposureEnvironment",
    "ExposureIssue",
    "ExposureOutcome",
    "ExposureReasonCode",
    "ExposureResult",
    "ExposureV1",
    "allocate",
    "record_exposure",
]
