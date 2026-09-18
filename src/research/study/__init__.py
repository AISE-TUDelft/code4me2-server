"""Study definition, agent registry and runtime packaging.

Merged home for the versioned study protocol/publication service, the agent
release registry and capability snapshots, and the runtime packaging and
conformance suite. The individual sub-packages keep their original public
surfaces:

* :mod:`research.study.protocol`
* :mod:`research.study.agents`
* :mod:`research.study.packaging`

The core packages never import ``App``, FastAPI, or a session factory.
"""
