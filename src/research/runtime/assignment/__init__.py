"""Enrollment-scoped profile assignment (Issue 05).

Public surface:

* :mod:`research.runtime.assignment.enums` - allocation outcomes and typed
  reason codes.
* :mod:`research.runtime.assignment.models` - ``AssignmentV1`` and typed
  results.
* :mod:`research.runtime.assignment.service` - server-authoritative, sticky,
  equal-random allocation over ``enrollment_id``.
* :mod:`research.runtime.assignment.store` - persistence adapters taking a
  caller-supplied SQLAlchemy ``Session``.

The core package never imports ``App``, FastAPI, or a session factory.
"""

from .enums import (
    AllocationOutcome,
    AssignmentReasonCode,
)
from .models import (
    AssignmentIssue,
    AssignmentResult,
    AssignmentV1,
)
from .service import allocate

__all__ = [
    "AllocationOutcome",
    "AssignmentIssue",
    "AssignmentReasonCode",
    "AssignmentResult",
    "AssignmentV1",
    "allocate",
]
