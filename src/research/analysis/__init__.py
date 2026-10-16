"""Researcher analysis: read models and pilot operations.

Merged home for the scoped researcher read models/RBAC and pilot operations
(health, release gate, kill switch and retention). The individual sub-packages
keep their original public surfaces:

* :mod:`research.analysis.read_models`
* :mod:`research.analysis.operations`

The core packages never import ``App``, FastAPI, or a session factory.
"""
