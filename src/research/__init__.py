"""Research platform core packages.

This package is intentionally backend-agnostic: it must never import ``App``,
FastAPI, or a SQLAlchemy session factory. The API layer
(``backend.routers.research``) supplies sockets and persistence; the core
modules here only hold domain models, deterministic serialization, redaction,
receipt building/validation and the compatibility evaluator.
"""
