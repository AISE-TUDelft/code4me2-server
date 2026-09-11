"""Admin CRUD for agent profiles, plus the plugin-facing registry.

Endpoints (mounted under ``/api/agent``):

  GET    /available-tools        tool names a profile may select
  GET    /profiles               list profiles (admin)
  POST   /profiles               create a profile (admin)
  PUT    /profiles/{id}          update a profile (admin)
  DELETE /profiles/{id}          delete a profile (admin)
  GET    /registry               profiles in the shape the plugin consumes
  GET    /assignments            list A/B assignments (admin)
  PUT    /assignments/{user_id}  pin a user to a profile (admin)
  DELETE /assignments/{user_id}  clear an assignment, re-rolling on next task

Profiles are the unit of experimental control: a profile fixes the runtime, the
provider, the model, the tool allowlist and the approval policy, and users are
assigned to one as an A/B arm.
"""

from __future__ import annotations

import json
import re
import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError

from agents.tools import KNOWN_AGENT_TOOLS, tools_for_framework
from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
)
from database import crud
from database.db_schemas import AgentProfile, AgentProfileAssignment  # noqa: TC001

router = APIRouter()

# Runtimes a profile may target. Anything else would mint tasks the plugin has
# no way to launch, so it's rejected at the API boundary rather than failing
# later at agent-startup time.
SUPPORTED_FRAMEWORKS = ("code4me2-agent", "goose", "codex")
SUPPORTED_APPROVAL_POLICIES = ("auto", "per_step", "suggestion_only")


class AgentProfilePayload(BaseModel):
    name: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    framework_version: str = Field(default="code4me2-agent")
    # Generic OpenAI-compatible endpoint. None = fall back to the server default.
    base_url: Optional[str] = Field(default=None)
    # Name of the env var holding the upstream key — never the key itself, so a
    # profile is always safe to read back over the API.
    api_key_ref: Optional[str] = Field(default=None)
    tools_json: str = Field(default="[]")
    approval_policy: str = Field(..., min_length=1)
    max_steps: int = Field(..., ge=1)
    is_active: bool = Field(default=True)
    # None = don't inject; let the provider use its own default.
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_context_tokens: Optional[int] = Field(default=None, ge=1)

    @field_validator("name", "model")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        candidate = value.strip().rstrip("/")
        parsed = urlparse(candidate)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "base_url must be an HTTP(S) endpoint without credentials, query, or fragment"
            )
        return candidate

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
        # Re-serialise so stored values are canonical regardless of input spacing.
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

    @field_validator("api_key_ref")
    @classmethod
    def validate_api_key_ref(cls, value: Optional[str]) -> Optional[str]:
        """Reject anything that looks like a key rather than a variable name.

        This is a guardrail against the obvious mistake: pasting the actual
        secret into the field. Env var names are short and uppercase, so a long
        value or one containing key-ish punctuation is almost certainly a
        credential that must not be persisted.
        """
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", candidate):
            raise ValueError(
                "api_key_ref must be the NAME of an environment variable "
                "(e.g. OPENAI_API_KEY), not an API key value"
            )
        return candidate


def _profile_to_dict(profile: AgentProfile) -> dict[str, Any]:
    return {
        "profile_id": str(profile.profile_id),
        "name": profile.name,
        "model": profile.model,
        "framework_version": profile.framework_version,
        "base_url": profile.base_url,
        "api_key_ref": profile.api_key_ref,
        "tools_json": profile.tools_json,
        "approval_policy": profile.approval_policy,
        "max_steps": profile.max_steps,
        "is_active": profile.is_active,
        "temperature": profile.temperature,
        "max_context_tokens": profile.max_context_tokens,
        "created_at": (
            profile.created_at.isoformat()
            if isinstance(profile.created_at, datetime)
            else profile.created_at
        ),
    }


def _profile_to_registry_dict(profile: AgentProfile) -> dict[str, Any]:
    """Shape a profile for the plugin, including the env the runtime expects.

    Note that ``api_key_ref`` is exposed but the key is not: the plugin only
    ever launches a *local* agent process, and the process reads the credential
    from the backend at runtime, so the secret never transits this endpoint.
    """
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
        "base_url": profile.base_url,
        "api_key_ref": profile.api_key_ref,
        "tools": tools,
        "approval_policy": profile.approval_policy,
        "max_steps": profile.max_steps,
        "max_context_tokens": profile.max_context_tokens,
        "is_active": profile.is_active,
        "temperature": profile.temperature,
        "env": env,
    }


@router.get("/available-tools", summary="List tool names a profile may select")
def list_available_tools(
    framework_version: Optional[str] = Query(
        default=None,
        description="Restrict to the tools relevant for one runtime "
        "(code4me2-agent | goose | codex). Omit for the full catalogue.",
    ),
    current_user: AuthenticatedUser = Depends(require_admin),
):
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
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        profiles = crud.list_agent_profiles(db)
        return JsonResponseWithStatus(
            status_code=200,
            content={"profiles": [_profile_to_dict(p) for p in profiles]},
        )
    finally:
        db.close()


@router.post("/profiles", summary="Create an agent profile")
def create_agent_profile(
    payload: AgentProfilePayload,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        profile = crud.create_agent_profile(
            db,
            name=payload.name,
            model=payload.model,
            framework_version=payload.framework_version,
            base_url=payload.base_url,
            api_key_ref=payload.api_key_ref,
            tools_json=payload.tools_json,
            approval_policy=payload.approval_policy,
            max_steps=payload.max_steps,
            is_active=payload.is_active,
            temperature=payload.temperature,
            max_context_tokens=payload.max_context_tokens,
        )
        return JsonResponseWithStatus(
            status_code=201, content={"profile": _profile_to_dict(profile)}
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Agent profile name already exists"
        ) from exc
    finally:
        db.close()


@router.put("/profiles/{profile_id}", summary="Update an agent profile")
def update_agent_profile(
    profile_id: uuid.UUID,
    payload: AgentProfilePayload,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    """Update a profile.

    Note this does *not* retroactively change tasks already running: the
    experimental conditions (model, temperature, policy, tools) were snapshotted
    onto each ``agent_task`` row at creation. The provider triple is the
    exception and is read live, so rotating a credential or endpoint takes
    effect on the next call.
    """
    db = app.get_db_session()
    try:
        profile = crud.update_agent_profile(
            db,
            profile_id=profile_id,
            name=payload.name,
            model=payload.model,
            framework_version=payload.framework_version,
            base_url=payload.base_url,
            api_key_ref=payload.api_key_ref,
            tools_json=payload.tools_json,
            approval_policy=payload.approval_policy,
            max_steps=payload.max_steps,
            is_active=payload.is_active,
            temperature=payload.temperature,
            max_context_tokens=payload.max_context_tokens,
        )
        if profile is None:
            raise HTTPException(status_code=404, detail="Agent profile not found")
        return JsonResponseWithStatus(
            status_code=200, content={"profile": _profile_to_dict(profile)}
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Agent profile name already exists"
        ) from exc
    finally:
        db.close()


@router.delete("/profiles/{profile_id}", summary="Delete an agent profile")
def delete_agent_profile(
    profile_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    """Delete a profile.

    Deleting cascades to its assignments but leaves historical ``agent_task``
    rows intact (they reference the profile by name, not id), so past telemetry
    stays attributable. To retire an arm mid-study prefer setting
    ``is_active=false``, which stops new draws without disturbing assigned users.
    """
    db = app.get_db_session()
    try:
        deleted = crud.delete_agent_profile(db, profile_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Agent profile not found")
        return JsonResponseWithStatus(
            status_code=200,
            content={"deleted": True, "profile_id": str(profile_id)},
        )
    finally:
        db.close()


@router.get("/registry", summary="List profiles available to the plugin")
def list_agent_registry(
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        profiles = crud.list_agent_profiles(db)
        return JsonResponseWithStatus(
            status_code=200,
            content={"profiles": [_profile_to_registry_dict(p) for p in profiles]},
        )
    finally:
        db.close()


# ── A/B assignments ─────────────────────────────────────────────────────────
#
# Assignments are normally created automatically (sticky random draw) on a
# user's first agent task. These admin endpoints let you inspect them and pin a
# specific user to a specific profile (source="manual"), which overrides the
# random draw and is excluded from A/B analysis.


class AssignmentPayload(BaseModel):
    profile_id: uuid.UUID


def _assignment_to_dict(assignment: AgentProfileAssignment) -> dict[str, Any]:
    return {
        "user_id": str(assignment.user_id),
        "profile_id": str(assignment.profile_id),
        "profile_name": assignment.profile.name if assignment.profile else None,
        "source": assignment.source,
        "assigned_at": (
            assignment.assigned_at.isoformat()
            if isinstance(assignment.assigned_at, datetime)
            else assignment.assigned_at
        ),
    }


@router.get("/assignments", summary="List all agent profile assignments")
def list_agent_assignments(
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        assignments = crud.list_agent_profile_assignments(db)
        return JsonResponseWithStatus(
            status_code=200,
            content={"assignments": [_assignment_to_dict(a) for a in assignments]},
        )
    finally:
        db.close()


@router.put(
    "/assignments/{user_id}", summary="Manually pin a user to an agent profile"
)
def set_agent_assignment(
    user_id: uuid.UUID,
    payload: AssignmentPayload,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        if crud.get_agent_profile_by_id(db, payload.profile_id) is None:
            raise HTTPException(status_code=404, detail="Agent profile not found")
        assignment = crud.set_agent_profile_assignment(
            db, user_id=user_id, profile_id=payload.profile_id, source="manual"
        )
        return JsonResponseWithStatus(
            status_code=200, content={"assignment": _assignment_to_dict(assignment)}
        )
    finally:
        db.close()


@router.delete(
    "/assignments/{user_id}",
    summary="Clear a user's assignment (re-rolls on next task)",
)
def delete_agent_assignment(
    user_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        deleted = crud.delete_agent_profile_assignment(db, user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Assignment not found")
        return JsonResponseWithStatus(
            status_code=200,
            content={"deleted": True, "user_id": str(user_id)},
        )
    finally:
        db.close()
