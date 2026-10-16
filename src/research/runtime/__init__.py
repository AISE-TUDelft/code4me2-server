"""Research runtime: assignment, bootstrap and session lifecycle.

Merged home for the enrollment-scoped assignment service, the
secret-free bootstrap manifest and capability issuance, and the research
session/agent-run state machine. The individual sub-packages keep their
original public surfaces:

* :mod:`research.runtime.assignment`
* :mod:`research.runtime.bootstrap`
* :mod:`research.runtime.sessions`

The core packages never import ``App``, FastAPI, or a session factory.
"""
