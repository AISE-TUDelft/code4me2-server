"""Participant identity, consent, withdrawal and retention.

Flat home for the participant identity/consent state machine and the
idempotent retention execution service:

* :mod:`identity` - the pure enrollment/consent/re-consent/withdrawal state
  machine, participant-code generation and retention, the researcher-safe
  projection, and the SQLAlchemy persistence adapters.
* :mod:`retention` - pure retention planning/execution, the persistence
  helpers plus :class:`SqlAlchemyRetentionStore`, and the bounded retrying
  worker entry points.

The core packages never import ``App``, FastAPI, or a session factory.
"""
