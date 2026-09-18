"""Administrator-managed provider connections and researcher grants.

Mounted under ``/api/research``. A ``provider_connection`` owns the upstream
``base_url`` and the *name* of the deployment secret (``secret_ref``). Responses
never include the secret value; the env-var name and endpoint URL are exposed to
administrators only. Researchers see only the connections explicitly granted to
them, identified by id/label with their allowed models and a readiness flag.
"""

from __future__ import annotations

import json
import os
import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
)
from backend.routers.research.access import require_researcher
from database import crud

router = APIRouter()


def _parse_models(models_json: Any) -> list[str]:
    try:
        parsed = json.loads(models_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _secret_present(secret_ref: Optional[str]) -> bool:
    """Whether the deployment environment currently holds the secret value."""
    name = (secret_ref or "").strip()
    return bool(name) and bool(os.getenv(name, "").strip())


def _safe_payload(connection: Any, *, admin: bool) -> dict[str, Any]:
    """Never include the secret value. Endpoint/ref name are admin-only."""
    payload: dict[str, Any] = {
        "connection_id": str(connection.connection_id),
        "label": connection.label,
        "models": _parse_models(connection.models_json),
        "is_active": bool(connection.is_active),
        # Readiness is derived, never a stored credential.
        "ready": _secret_present(connection.secret_ref) and bool(connection.is_active),
    }
    if admin:
        payload["base_url"] = connection.base_url
        # Name of the environment variable only — never its value.
        payload["secret_ref"] = connection.secret_ref
    return payload


class ProviderConnectionPayload(BaseModel):
    """Admin-maintained connection; ``extra=forbid`` blocks typo'd fields."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    # Name of the deployment env var holding the key, never the key itself.
    secret_ref: str = Field(..., min_length=1)
    models: list[str] = Field(default_factory=list)
    is_active: bool = True

    @field_validator("label", "base_url", "secret_ref")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("base_url must be an http(s) endpoint")
        if any(ch in value for ch in ("#",)) or "@" in value.split("//", 1)[-1]:
            raise ValueError("base_url must not contain credentials or a fragment")
        return value.rstrip("/")

    @field_validator("secret_ref")
    @classmethod
    def validate_secret_ref(cls, value: str) -> str:
        """The field is an env-var *name*, never a pasted credential."""
        import re

        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value):
            raise ValueError(
                "secret_ref must be the NAME of an environment variable "
                "(e.g. EXAMPLE_PROVIDER_KEY), not a key value"
            )
        return value

    @field_validator("models")
    @classmethod
    def validate_models(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if not cleaned:
            raise ValueError("at least one allowed model is required")
        return sorted(set(cleaned))


def _connection_payload_json(payload: ProviderConnectionPayload) -> str:
    return json.dumps(payload.models)


@router.get(
    "/provider-connections",
    summary="List provider connections available to the caller",
)
def list_provider_connections(
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Administrators see all connections; researchers see only granted ones."""
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        if current_user.is_admin:
            connections = crud.list_provider_connections(db)
        else:
            connections = crud.list_available_provider_connections(
                db, current_user.user_id
            )
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "connections": [
                    _safe_payload(connection, admin=current_user.is_admin)
                    for connection in connections
                ]
            },
        )
    finally:
        db.close()


@router.post(
    "/provider-connections",
    summary="Create a provider connection (admin)",
)
def create_provider_connection(
    payload: ProviderConnectionPayload,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    require_admin(current_user)
    db = app.get_db_session()
    try:
        if crud.get_provider_connection_by_label(db, payload.label) is not None:
            raise HTTPException(
                status_code=409, detail="A connection with that label already exists"
            )
        connection = crud.create_provider_connection(
            db,
            label=payload.label,
            base_url=payload.base_url,
            secret_ref=payload.secret_ref,
            models_json=_connection_payload_json(payload),
            is_active=payload.is_active,
        )
        return JsonResponseWithStatus(
            status_code=201,
            content={"connection": _safe_payload(connection, admin=True)},
        )
    finally:
        db.close()


@router.put(
    "/provider-connections/{connection_id}",
    summary="Update a provider connection (admin)",
)
def update_provider_connection(
    connection_id: uuid.UUID,
    payload: ProviderConnectionPayload,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    require_admin(current_user)
    db = app.get_db_session()
    try:
        connection = crud.update_provider_connection(
            db,
            connection_id,
            label=payload.label,
            base_url=payload.base_url,
            secret_ref=payload.secret_ref,
            models_json=_connection_payload_json(payload),
            is_active=payload.is_active,
        )
        if connection is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        return JsonResponseWithStatus(
            status_code=200,
            content={"connection": _safe_payload(connection, admin=True)},
        )
    finally:
        db.close()


@router.delete(
    "/provider-connections/{connection_id}",
    summary="Delete a provider connection (admin)",
)
def delete_provider_connection(
    connection_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    require_admin(current_user)
    db = app.get_db_session()
    try:
        deleted = crud.delete_provider_connection(db, connection_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Connection not found")
        return JsonResponseWithStatus(
            status_code=200,
            content={"deleted": True, "connection_id": str(connection_id)},
        )
    finally:
        db.close()


