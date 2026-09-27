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
from sqlalchemy.exc import IntegrityError

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
)
from backend.routers.research.access import require_researcher
from database import crud
from research.budget.pricing import parse_usd_per_million

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


def _price_payload(row: Any) -> dict[str, Any]:
    return {
        "input_usd_per_million": str(row.input_usd_per_million),
        "output_usd_per_million": str(row.output_usd_per_million),
        "cached_input_usd_per_million": (
            None
            if row.cached_input_usd_per_million is None
            else str(row.cached_input_usd_per_million)
        ),
        "updated_at": (
            row.updated_at.isoformat() if getattr(row, "updated_at", None) else None
        ),
    }


def _safe_payload(
    connection: Any,
    *,
    admin: bool,
    profile_count: Optional[int] = None,
    prices: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Never include the secret value. Endpoint/ref name are admin-only.

    ``profile_count`` (admin only) is how many agent profiles, archived ones
    included, reference the connection; any reference blocks deletion.
    ``prices`` (``{model: price_row}``) drives the per-model budget prices
    every caller may see: researchers size participant budgets with them.
    """
    models = _parse_models(connection.models_json)
    prices = prices or {}
    payload: dict[str, Any] = {
        "connection_id": str(connection.connection_id),
        "label": connection.label,
        "models": models,
        "is_active": bool(connection.is_active),
        # Readiness is derived, never a stored credential.
        "ready": _secret_present(connection.secret_ref) and bool(connection.is_active),
        # USD per million tokens per model; a metered arm whose model has no
        # price fails closed (503 price_missing), so completeness is shown.
        "model_prices": {
            model: (_price_payload(prices[model]) if model in prices else None)
            for model in models
        },
        "pricing": {
            "complete": all(model in prices for model in models),
            "missing_models": [model for model in models if model not in prices],
        },
    }
    if admin:
        payload["base_url"] = connection.base_url
        # Name of the environment variable only — never its value.
        payload["secret_ref"] = connection.secret_ref
        payload["profile_count"] = int(profile_count or 0)
    return payload


def _label_exists_error() -> HTTPException:
    """The one typed duplicate-label conflict, shared by create and update."""
    return HTTPException(
        status_code=409,
        detail={
            "code": "CONNECTION_LABEL_EXISTS",
            "field": "label",
            "message": "A connection with that label already exists",
        },
    )


def _connection_in_use_error(profile_count: int) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "CONNECTION_IN_USE",
            "message": (
                f"The connection is still used by {profile_count} agent "
                "profile(s), archived profiles included; point them at another "
                "connection before deleting it"
            ),
            "profile_count": profile_count,
        },
    )


class ModelPricePayload(BaseModel):
    """USD per million tokens for one model (decimal strings, 6 dp max)."""

    model_config = ConfigDict(extra="forbid")

    input_usd_per_million: str
    output_usd_per_million: str
    cached_input_usd_per_million: Optional[str] = None

    @field_validator("input_usd_per_million", "output_usd_per_million")
    @classmethod
    def validate_price(cls, value: str) -> str:
        return str(parse_usd_per_million(value))

    @field_validator("cached_input_usd_per_million")
    @classmethod
    def validate_cached_price(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        return str(parse_usd_per_million(value))


class ProviderConnectionPayload(BaseModel):
    """Admin-maintained connection; ``extra=forbid`` blocks typo'd fields."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    # Name of the deployment env var holding the key, never the key itself.
    secret_ref: str = Field(..., min_length=1)
    models: list[str] = Field(default_factory=list)
    is_active: bool = True
    # Optional per-model budget prices. Omitted (None) leaves the stored
    # prices unchanged; ``{}`` clears them; a ``null`` entry deletes one.
    model_prices: Optional[dict[str, Optional[ModelPricePayload]]] = None

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


def _price_rows(payload: ProviderConnectionPayload) -> Optional[dict[str, Optional[dict[str, Any]]]]:
    """Validated ``{model: {...}|None}`` for the CRUD helper, or ``None`` (unchanged)."""
    if payload.model_prices is None:
        return None
    if payload.model_prices == {}:
        # An empty map clears every price of the connection.
        return {model: None for model in payload.models}
    unknown = sorted(model for model in payload.model_prices if model not in payload.models)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PRICE_MODEL_UNKNOWN",
                "field": "model_prices",
                "message": "prices may only be set for the connection's allowed models: "
                + ", ".join(unknown),
                "models": unknown,
            },
        )
    return {
        model: (
            None
            if price is None
            else {
                "input_usd_per_million": parse_usd_per_million(price.input_usd_per_million),
                "output_usd_per_million": parse_usd_per_million(price.output_usd_per_million),
                "cached_input_usd_per_million": (
                    None
                    if price.cached_input_usd_per_million is None
                    else parse_usd_per_million(price.cached_input_usd_per_million)
                ),
            }
        )
        for model, price in payload.model_prices.items()
    }


def _prices_for(db: Any, connection_id: uuid.UUID) -> dict[str, Any]:
    return {row.model: row for row in crud.list_model_prices(db, connection_id)}


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
        profile_counts: dict[Any, int] = {}
        if current_user.is_admin:
            connections = crud.list_provider_connections(db)
            profile_counts = crud.count_profiles_by_connection(db)
        else:
            connections = crud.list_available_provider_connections(
                db, current_user.user_id
            )
        prices_by_connection = crud.list_model_prices_by_connection(db)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "connections": [
                    _safe_payload(
                        connection,
                        admin=current_user.is_admin,
                        profile_count=profile_counts.get(connection.connection_id, 0),
                        prices=prices_by_connection.get(connection.connection_id, {}),
                    )
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
            raise _label_exists_error()
        prices = _price_rows(payload)
        try:
            connection = crud.create_provider_connection(
                db,
                label=payload.label,
                base_url=payload.base_url,
                secret_ref=payload.secret_ref,
                models_json=_connection_payload_json(payload),
                is_active=payload.is_active,
            )
        except IntegrityError as exc:
            # A concurrent create won the unique label between the check above
            # and the insert.
            db.rollback()
            raise _label_exists_error() from exc
        if prices is not None:
            crud.replace_model_prices(
                db, connection.connection_id, prices,
                allowed_models=payload.models, updated_by=current_user.email,
            )
        return JsonResponseWithStatus(
            status_code=201,
            content={
                "connection": _safe_payload(
                    connection,
                    admin=True,
                    profile_count=crud.count_profiles_for_connection(
                        db, connection.connection_id
                    ),
                    prices=_prices_for(db, connection.connection_id),
                )
            },
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
        if crud.get_provider_connection(db, connection_id) is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        holder = crud.get_provider_connection_by_label(db, payload.label)
        if holder is not None and holder.connection_id != connection_id:
            raise _label_exists_error()
        prices = _price_rows(payload)
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
        except IntegrityError as exc:
            # Unique-label race with a concurrent create/rename.
            db.rollback()
            raise _label_exists_error() from exc
        if connection is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        if prices is not None:
            crud.replace_model_prices(
                db, connection_id, prices,
                allowed_models=payload.models, updated_by=current_user.email,
            )
        else:
            # Models removed from the allowlist drop their (now unusable) price.
            crud.replace_model_prices(
                db, connection_id, {}, allowed_models=payload.models,
                updated_by=current_user.email,
            )
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "connection": _safe_payload(
                    connection,
                    admin=True,
                    profile_count=crud.count_profiles_for_connection(
                        db, connection_id
                    ),
                    prices=_prices_for(db, connection_id),
                )
            },
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
    """Delete an unreferenced connection.

    A connection still referenced by any agent profile (archived included) is
    refused with a typed 409 ``CONNECTION_IN_USE``; the ``RESTRICT`` foreign key
    is the backstop for a concurrent reference, and its violation is rolled
    back so the session is never left in a failed state.
    """
    require_admin(current_user)
    db = app.get_db_session()
    try:
        if crud.get_provider_connection(db, connection_id) is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        in_use = crud.count_profiles_for_connection(db, connection_id)
        if in_use:
            raise _connection_in_use_error(in_use)
        try:
            deleted = crud.delete_provider_connection(db, connection_id)
        except IntegrityError as exc:
            db.rollback()
            raise _connection_in_use_error(
                max(1, crud.count_profiles_for_connection(db, connection_id))
            ) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Connection not found")
        return JsonResponseWithStatus(
            status_code=200,
            content={"deleted": True, "connection_id": str(connection_id)},
        )
    finally:
        db.close()


