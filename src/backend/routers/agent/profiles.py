"""Researcher-owned agent profiles plus the plugin-facing registry.

Endpoints (mounted under ``/api/agent``):

  GET    /available-tools        tool names a profile may select
  GET    /profiles               list the caller's profiles (admin: all)
  POST   /profiles               create a profile
  PUT    /profiles/{id}          update a profile
  DELETE /profiles/{id}          archive a profile
  GET    /registry               profiles in the shape the plugin consumes

A profile is a private, editable template owned by exactly one researcher
(``agent_profile.owner_user_id``). It selects an administrator-managed
``provider_connection`` and a ``model`` from that connection's allowed list; the
release it pins is the approved artifact. The provider endpoint and secret live
on the connection, never on the profile. Names are unique per owner.

There is no client-chosen arm: task/run assignment is server-authoritative (see
``agents.registry``).
"""

from __future__ import annotations

import json
import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError

from agents.tools import KNOWN_AGENT_TOOLS, tools_for_framework
from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
)
from backend.routers.research.access import (
    is_owner,
    require_owner,
    require_researcher,
)
from database import crud
from database.db_schemas import AgentProfile
from research.study.agents import store as registry_store
from research.study.agents.distributions import (
    distribution_supported_platforms,
    resolve_distribution_view,
)

router = APIRouter()

SUPPORTED_FRAMEWORKS = ("code4me2-agent", "goose", "codex")
SUPPORTED_APPROVAL_POLICIES = ("auto", "per_step", "suggestion_only")


def _models_for(connection: Any) -> list[str]:
    try:
        parsed = json.loads(getattr(connection, "models_json", None) or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


class AgentProfilePayload(BaseModel):
    """A profile template payload.

    ``extra="forbid"`` deliberately rejects the retired fields
    (``base_url``/``api_key_ref``/``distribution_mode``/``agent_package``/
    ``agent_command``): researchers select an authorized ``connection_id`` and a
    model, never an endpoint or a secret reference.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    framework_version: str = Field(default="code4me2-agent")
    # Administrator-managed provider connection this profile uses.
    connection_id: uuid.UUID
    # Exact approved artifact pin (agent_release.release_id).
    release_id: Optional[str] = Field(default=None)
    tools_json: str = Field(default="[]")
    approval_policy: str = Field(..., min_length=1)
    max_steps: int = Field(..., ge=1)
    is_active: bool = Field(default=True)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_context_tokens: Optional[int] = Field(default=None, ge=1)

    @field_validator("release_id")
    @classmethod
    def normalize_release_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.lower() == "latest":
            raise ValueError("release_id must be an immutable pin, not 'latest'")
        return candidate

    @field_validator("name", "model")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("tools_json")
    @classmethod
    def validate_tools_json(cls, value: str) -> str:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("tools_json must be a valid JSON array") from exc
        if not isinstance(parsed, list) or not all(
            isinstance(item, str) for item in parsed
        ):
            raise ValueError("tools_json must be a JSON array of strings")
        return json.dumps(parsed)

    @field_validator("framework_version")
    @classmethod
    def validate_framework(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in SUPPORTED_FRAMEWORKS:
            raise ValueError(
                f"framework_version must be one of {', '.join(SUPPORTED_FRAMEWORKS)}"
            )
        return normalized

    @field_validator("approval_policy")
    @classmethod
    def validate_approval_policy(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in SUPPORTED_APPROVAL_POLICIES:
            raise ValueError(
                "approval_policy must be one of "
                + ", ".join(SUPPORTED_APPROVAL_POLICIES)
            )
        return normalized

    @model_validator(mode="after")
    def validate_tools_for_framework(self):
        selected = set(json.loads(self.tools_json))
        unknown = selected - set(tools_for_framework(self.framework_version))
        if unknown:
            raise ValueError(
                f"tools_json contains tools unsupported by {self.framework_version}: "
                + ", ".join(sorted(unknown))
            )
        return self


def _authorize_connection(
    db: Any, payload: AgentProfilePayload, current_user: AuthenticatedUser
) -> Any:
    """Resolve the selected connection and enforce grant + model allowlist."""
    connection = crud.get_provider_connection(db, payload.connection_id)
    if connection is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CONNECTION_UNRESOLVED",
                "field": "connection_id",
                "message": "the selected provider connection does not exist",
            },
        )
    if not current_user.is_admin and not crud.provider_connection_is_available(
        db, connection.connection_id, current_user.user_id
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CONNECTION_NOT_GRANTED",
                "message": (
                    "the caller is not authorized to use this provider connection"
                ),
            },
        )
    if payload.model not in _models_for(connection):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MODEL_NOT_ALLOWED",
                "field": "model",
                "message": (
                    f"model {payload.model!r} is not allowed by connection "
                    f"{connection.label!r}"
                ),
            },
        )
    return connection


def _release_for(db: Any, profile: AgentProfile):
    """Rehydrate the registry release a profile pins, if any."""
    release_id = getattr(profile, "release_id", None)
    if not release_id:
        return None
    row = registry_store.get_release(db, release_id)
    if row is None:
        return None
    return registry_store.row_to_release(row)


def _connection_summary(db: Any, profile: AgentProfile) -> Optional[dict[str, Any]]:
    if getattr(profile, "connection_id", None) is None:
        return None
    connection = crud.get_provider_connection(db, profile.connection_id)
    if connection is None:
        return {"connection_id": str(profile.connection_id), "label": None}
    return {
        "connection_id": str(connection.connection_id),
        "label": connection.label,
        "models": _models_for(connection),
        "is_active": bool(connection.is_active),
    }


def _profile_to_dict(db: Any, profile: AgentProfile) -> dict[str, Any]:
    release = _release_for(db, profile)
    view = resolve_distribution_view(profile, release)
    return {
        "profile_id": str(profile.profile_id),
        "owner_user_id": str(profile.owner_user_id),
        "name": profile.name,
        "model": profile.model,
        "framework_version": profile.framework_version,
        "tools_json": profile.tools_json,
        "approval_policy": profile.approval_policy,
        "max_steps": profile.max_steps,
        "is_active": profile.is_active,
        "temperature": profile.temperature,
        "max_context_tokens": profile.max_context_tokens,
        "connection": _connection_summary(db, profile),
        "release_id": view.release_id,
        "release_version": view.version,
        "verified": view.verified,
        "supported_platforms": distribution_supported_platforms(release),
        "created_at": (
            profile.created_at.isoformat()
            if isinstance(profile.created_at, datetime)
            else profile.created_at
        ),
    }


def _profile_to_registry_dict(profile: AgentProfile) -> dict[str, Any]:
    """Shape a profile for the plugin: non-secret identity only."""
    try:
        tools = json.loads(profile.tools_json or "[]")
    except json.JSONDecodeError:
        tools = []

    env = {
        "SECURITY_CONFIRMATION_MODE": profile.approval_policy,
        "MAX_ITERATIONS": str(profile.max_steps),
        "CODE4ME_AGENT_PROFILE": profile.name,
    }
    if not tools:
        env["DISABLE_TOOLS"] = "true"

    return {
        "name": profile.name,
        "model": profile.model,
        "framework_version": profile.framework_version,
        "tools": tools,
        "approval_policy": profile.approval_policy,
        "max_steps": profile.max_steps,
        "max_context_tokens": profile.max_context_tokens,
        "is_active": profile.is_active,
        "temperature": profile.temperature,
        "release_id": getattr(profile, "release_id", None),
        "env": env,
    }


@router.get("/available-tools", summary="List tool names a profile may select")
def list_available_tools(
    framework_version: Optional[str] = Query(
        default=None,
        description="Restrict to the tools relevant for one runtime "
        "(code4me2-agent | goose | codex). Omit for the full catalogue.",
    ),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    require_researcher(current_user)
    tools = (
        tools_for_framework(framework_version)
        if framework_version
        else KNOWN_AGENT_TOOLS
    )
    return JsonResponseWithStatus(
        status_code=200,
        content={
            "tools": sorted(tools),
            "framework_version": framework_version,
            "frameworks": list(SUPPORTED_FRAMEWORKS),
        },
    )


@router.get("/profiles", summary="List agent profiles")
def list_agent_profiles(
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        owner_scope = None if current_user.is_admin else current_user.user_id
        profiles = crud.list_agent_profiles(db, owner_scope)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "profiles": [_profile_to_dict(db, profile) for profile in profiles]
            },
        )
    finally:
        db.close()


@router.post("/profiles", summary="Create an agent profile")
def create_agent_profile(
    payload: AgentProfilePayload,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        _authorize_connection(db, payload, current_user)
        profile = crud.create_agent_profile(
            db,
            owner_user_id=current_user.user_id,
            name=payload.name,
            model=payload.model,
            framework_version=payload.framework_version,
            connection_id=payload.connection_id,
            release_id=payload.release_id,
            tools_json=payload.tools_json,
            approval_policy=payload.approval_policy,
            max_steps=payload.max_steps,
            is_active=payload.is_active,
            temperature=payload.temperature,
            max_context_tokens=payload.max_context_tokens,
        )
        return JsonResponseWithStatus(
            status_code=201, content={"profile": _profile_to_dict(db, profile)}
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="You already have a profile with that name",
        ) from exc
    finally:
        db.close()


@router.get("/profiles/{profile_id}", summary="Fetch one agent profile")
def get_agent_profile(
    profile_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        profile = crud.get_agent_profile_by_id(db, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Agent profile not found")
        require_owner(current_user, profile.owner_user_id, subject="profile")
        return JsonResponseWithStatus(
            status_code=200, content={"profile": _profile_to_dict(db, profile)}
        )
    finally:
        db.close()


@router.put("/profiles/{profile_id}", summary="Update an agent profile")
def update_agent_profile(
    profile_id: uuid.UUID,
    payload: AgentProfilePayload,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Update a profile template.

    Editing a template never rewrites a published study snapshot: publication
    frozen the profile's configuration into the revision.
    """
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        existing = crud.get_agent_profile_by_id(db, profile_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Agent profile not found")
        require_owner(current_user, existing.owner_user_id, subject="profile")
        _authorize_connection(db, payload, current_user)
        profile = crud.update_agent_profile(
            db,
            profile_id=profile_id,
            name=payload.name,
            model=payload.model,
            framework_version=payload.framework_version,
            tools_json=payload.tools_json,
            approval_policy=payload.approval_policy,
            max_steps=payload.max_steps,
            is_active=payload.is_active,
            temperature=payload.temperature,
            max_context_tokens=payload.max_context_tokens,
            connection_id=payload.connection_id,
            release_id=payload.release_id,
            update_connection_id=True,
            update_release_id=True,
        )
        return JsonResponseWithStatus(
            status_code=200, content={"profile": _profile_to_dict(db, profile)}
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="You already have a profile with that name",
        ) from exc
    finally:
        db.close()


@router.delete("/profiles/{profile_id}", summary="Retire an agent profile")
def delete_agent_profile(
    profile_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Archive a profile; frozen study snapshots are never erased."""
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        existing = crud.get_agent_profile_by_id(db, profile_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Agent profile not found")
        require_owner(current_user, existing.owner_user_id, subject="profile")
        crud.delete_agent_profile(db, profile_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={"retired": True, "profile_id": str(profile_id)},
        )
    finally:
        db.close()


@router.get("/registry", summary="List profiles available to the plugin")
def list_agent_registry(
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        owner_scope = None if current_user.is_admin else current_user.user_id
        profiles = crud.list_agent_profiles(db, owner_scope)
        return JsonResponseWithStatus(
            status_code=200,
            content={"profiles": [_profile_to_registry_dict(p) for p in profiles]},
        )
    finally:
        db.close()
