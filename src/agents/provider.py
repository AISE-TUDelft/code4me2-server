"""Upstream LLM provider resolution for the agent inference relay.

Provider endpoints and secrets are administrator-managed facts: a
``provider_connection`` row owns the OpenAI-compatible ``base_url`` and the
*name* of the deployment secret (``secret_ref``) that holds the key. A profile
selects a connection and a model. Secrets are resolved from the environment only
at actual request time and are never stored, logged, snapshotted or returned.

There is deliberately no fallback: a missing/inactive connection, a missing
secret, or a model not allowed by the connection is a readiness failure, never
an accidental use of a server default or another owner's connection.

Codex's proprietary Responses API is not chat-completions-shaped, so it stays a
special-cased normalization path keyed on ``framework_version == "codex"``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "ProviderReadinessError",
    "ResolvedConnection",
    "Upstream",
    "connection_model_allowed",
    "connection_view",
    "resolve_api_key",
    "resolve_task_connection",
    "resolve_upstream",
]


@dataclass(frozen=True)
class ResolvedConnection:
    """A session-independent snapshot of a provider connection.

    Taking primitives out of the ORM row means the resolved connection stays
    usable after the request session closes, without holding a lazy relationship.
    """

    connection_id: object
    label: str
    base_url: str
    secret_ref: str
    models_json: str
    is_active: bool


def connection_view(connection) -> ResolvedConnection:
    return ResolvedConnection(
        connection_id=connection.connection_id,
        label=connection.label,
        base_url=connection.base_url,
        secret_ref=connection.secret_ref,
        models_json=connection.models_json,
        is_active=bool(connection.is_active),
    )


def resolve_task_connection(
    db,
    profile,
    owner_user_id,
    *,
    owner_is_admin: bool = False,
) -> ResolvedConnection:
    """Resolve and authorize the connection a task's profile selects.

    Re-checked at every inference request: the connection must exist, be active,
    and (unless the task owner is an administrator) be explicitly granted to the
    owner. A revoked grant therefore blocks a previously created task.
    """
    from database import crud  # local import keeps provider.py dependency-light

    connection_id = getattr(profile, "connection_id", None)
    if connection_id is None:
        raise ProviderReadinessError(
            "CONNECTION_MISSING",
            "the task's profile does not reference a provider connection",
        )
    connection = crud.get_provider_connection(db, connection_id)
    if connection is None:
        raise ProviderReadinessError(
            "CONNECTION_MISSING",
            "the task's provider connection no longer exists",
        )
    if not owner_is_admin:
        if owner_user_id is None or not crud.provider_connection_is_available(
            db, connection.connection_id, owner_user_id
        ):
            raise ProviderReadinessError(
                "CONNECTION_NOT_READY",
                f"provider connection {connection.label!r} is not ready",
            )
    return connection_view(connection)


def funding_owner_for_task(db, task):
    """Return ``(funding_owner_user_id, owner_is_admin)`` for connection auth.

    The grant belongs to the researcher who owns the study that funds a task, not
    the participant who ran it. The frozen ``task.funding_owner_user_id`` is
    authoritative; ``Study.created_by`` is a fallback for tasks created before
    the field was populated. A participant's own admin flag never applies.
    """
    from database import crud
    from database.db_schemas import Study as StudyRow

    funding_owner_user_id = getattr(task, "funding_owner_user_id", None)
    if funding_owner_user_id is None and getattr(task, "study_id", None) is not None:
        study = db.get(StudyRow, task.study_id)
        if study is not None:
            funding_owner_user_id = getattr(study, "created_by", None)
    return _funding_owner_admin(db, funding_owner_user_id)


def funding_owner_for_profile(db, profile):
    """Return ``(funding_owner_user_id, owner_is_admin)`` for a frozen profile."""
    return _funding_owner_admin(
        db, getattr(profile, "funding_owner_user_id", None)
    )


def _funding_owner_admin(db, funding_owner_user_id):
    from database import crud

    owner_is_admin = False
    if funding_owner_user_id is not None:
        owner = crud.get_user_by_id(db, funding_owner_user_id)
        owner_is_admin = bool(owner is not None and owner.is_admin)
    return funding_owner_user_id, owner_is_admin


class ProviderReadinessError(RuntimeError):
    """A typed, actionable failure to resolve a usable provider connection.

    The message is safe to return to a caller: it names the reason and the
    connection label, never a secret value or the raw URL secret.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Upstream:
    """A resolved OpenAI-compatible upstream target."""

    base_url: str
    api_key: str
    model: str
    # Which agent runtime this task belongs to: code4me2-agent | goose | codex.
    framework_version: Optional[str] = None

    @property
    def is_responses_api(self) -> bool:
        """Whether this runtime speaks OpenAI's Responses API rather than
        chat-completions. Only Codex does."""
        return (self.framework_version or "").strip().lower() == "codex"

    def endpoint(self, *, responses_api: bool) -> str:
        """Full URL for the request, given the wire shape actually observed."""
        suffix = "responses" if responses_api else "chat/completions"
        return f"{self.base_url.rstrip('/')}/{suffix}"


def connection_model_allowed(connection, model: str) -> bool:
    """Whether ``model`` is in the connection's allowed model list."""
    try:
        models = json.loads(getattr(connection, "models_json", None) or "[]")
    except (json.JSONDecodeError, TypeError):
        models = []
    return str(model) in {str(item) for item in models}


def resolve_api_key(secret_ref: Optional[str]) -> str:
    """Read a provider secret from the environment by its variable *name*.

    Raises :class:`ProviderReadinessError` when the reference is unset or the
    environment variable is empty: there is no unauthenticated fallback, so a
    missing secret is a clear readiness failure rather than a silent 401.
    """
    env_name = (secret_ref or "").strip()
    if not env_name:
        raise ProviderReadinessError(
            "SECRET_REF_MISSING",
            "the provider connection has no secret reference configured",
        )
    key = os.getenv(env_name, "").strip()
    if not key:
        raise ProviderReadinessError(
            "SECRET_MISSING",
            f"provider secret ${env_name} is not present in the environment",
        )
    return key


def resolve_upstream(
    *,
    model: str,
    connection,
    framework_version: Optional[str] = None,
) -> Upstream:
    """Build the upstream target from the task's frozen connection identity.

    The connection is re-validated here, at request time, so a revoked/disabled
    connection blocks both new and previously created tasks. The secret value is
    read from the environment and never logged.
    """
    if connection is None:
        raise ProviderReadinessError(
            "CONNECTION_MISSING",
            "the task's profile does not reference a provider connection",
        )
    if not getattr(connection, "is_active", False):
        raise ProviderReadinessError(
            "CONNECTION_INACTIVE",
            f"provider connection {getattr(connection, 'label', '?')!r} is not active",
        )
    if not connection_model_allowed(connection, model):
        raise ProviderReadinessError(
            "MODEL_NOT_ALLOWED",
            f"model {model!r} is not allowed by connection "
            f"{getattr(connection, 'label', '?')!r}",
        )

    resolved = Upstream(
        base_url=str(connection.base_url).strip(),
        api_key=resolve_api_key(getattr(connection, "secret_ref", None)),
        model=model,
        framework_version=framework_version,
    )
    logging.info(
        "[Agent/provider] connection=%r model=%r runtime=%s authenticated=%s",
        getattr(connection, "label", None),
        resolved.model,
        resolved.framework_version or "unknown",
        bool(resolved.api_key),
    )
    return resolved
