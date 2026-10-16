"""ACP host-evidence and compatibility gate (Issue 01).

Public surface:

* :mod:`research.compatibility.enums` - closed vocabularies for capabilities,
  states, fidelity, enforcement owners, decisions and reason codes.
* :mod:`research.compatibility.models` - Pydantic v2 contracts for the
  ``AcpCapabilityReceiptV1`` (and its versioned sub-objects).
* :mod:`research.compatibility.canonical` - deterministic canonical JSON and
  SHA-256 hashing.
* :mod:`research.compatibility.redaction` - recursive secret removal.
* :mod:`research.compatibility.receipt` - fixture-transcript ingestion and
  receipt validation.
* :mod:`research.compatibility.evaluate` - the deterministic compatibility
  evaluator.

The persistence adapters live in :mod:`research.compatibility.store` and take
a caller-supplied SQLAlchemy ``Session`` so this package stays free of any
application/App import.
"""
