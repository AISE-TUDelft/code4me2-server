"""ACP authorization and runtime configuration for locally launched agents.

Endpoints (mounted under ``/api/acp``):

  POST /grant                       plugin mints a one-time launch grant
  POST /session/exchange            agent trades the grant for a session token
  POST /session/validate-or-refresh agent revalidates and slides its expiry
  GET  /agent-config                agent fetches its assigned runtime config
  POST /persistent-auth-token       admin mints a non-expiring auth token

Why this exists: the built-in ``code4me2-agent`` runtime is a separate OS
process started by the IDE plugin. It has no session cookie, so it can't
authenticate the way the plugin does. The grant handoff gives it a credential of
its own, scoped to exactly one user, project and workspace directory.

``/agent-config`` is where merge decision 6 lands for the built-in runtime: the
agent no longer reads a hardcoded local default, it asks the backend which
provider endpoint, model, tools and step budget to use. That's what makes the
assigned A/B profile actually govern the runtime we control, the same way the
inference relay governs the ones we don't.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Body, Cookie, Depends, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

import Queries  # noqa: TC001 - FastAPI evaluates route annotations at runtime
from agents import provider as provider_module
from agents import registry
from agents.tools import CODE4ME2_AGENT_TOOLS
from App import App
from backend.acp_authorization import (
    AcpAuthorizationDenied,
    AcpAuthorizationService,
    AcpServerAuthorization,
)
from backend.Responses import (
    AcpAgentConfigGetResponse,
    AcpAuthorizationError,
    ExchangeAcpGrantPostResponse,
    JsonResponseWithStatus,
    PersistentAuthTokenPostResponse,
    PrepareAcpGrantPostResponse,
    ValidateAcpSessionPostResponse,
)
from backend.routers.agent.acp_auth import require_acp_scope, require_acp_server_scope
from backend.routers.agent.consent import resolve_store_agent_content_for_acp
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
)
from backend.routers.research import access
from database import crud
from research.analysis.operations import store as operations_store
from research.study.agents.enums import MANAGED_RUNTIME_FRAMEWORK
from utils import create_uuid

router = APIRouter()


def _active_enrollment_id(db, *, account_id, study_id):
    """Resolve the account's live enrollment id for kill-switch scoping.

    Best-effort: ``None`` (no participant, no active enrollment, or a store
    error) simply means the enrollment-scoped kill switch cannot be matched,
    and the study-scoped check plus ``require_live_enrollment`` stay decisive.
    """
    if account_id is None:
        return None
    try:
        from research.participants import identity as identity_store
        from research.participants.enums import EnrollmentStatus

        participant = identity_store.get_participant_by_account(db, account_id)
        if participant is None:
            return None
        for row in identity_store.list_enrollments(db, participant.participant_id):
            if row.status != EnrollmentStatus.ACTIVE.value:
                continue
            if study_id is not None and row.study_id != study_id:
                continue
            return row.enrollment_id
    except Exception:  # noqa: BLE001 - the funded gate below is decisive
        return None
    return None


def _require_funded_access(
    db, *, account_id, study_id, enrollment_id=None
) -> None:
    """Re-check live enrollment/window/kill switch for a study-funded operation.

    The single funded gate for ACP paths: the account must have an ACTIVE
    enrollment, the study must not be ``STUDY_STOPPED`` and must still be open,
    and no operator kill switch may be engaged — at study scope or for the
    account's own enrollment. A previously issued run/capability never bypasses
    current server state.
    """
    scoped_enrollment_id = enrollment_id
    if scoped_enrollment_id is None:
        scoped_enrollment_id = _active_enrollment_id(
            db, account_id=account_id, study_id=study_id
        )
    kill_switch_check = operations_store.db_kill_switch_check(
        db, study_id=study_id, enrollment_id=scoped_enrollment_id
    )
    try:
        access.require_live_enrollment(
            db,
            account_id=account_id,
            study_id=study_id,
            kill_switch_check=kill_switch_check,
        )
    except access.FundedAccessRefused as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


def _require_funded_task(db, task) -> None:
    """Re-check live enrollment/window/kill switch for a study-funded task.

    Applied to the managed run replay and inference paths so a previously issued
    run/capability never bypasses current server state.
    """
    if getattr(task, "study_id", None) is None:
        return
    _require_funded_access(
        db,
        account_id=getattr(task, "owner_user_id", None),
        study_id=task.study_id,
        enrollment_id=getattr(task, "enrollment_id", None),
    )

# Fallback runtime config, used only when no agent profile can be resolved for
# the user (e.g. every profile has been deactivated). Deliberately minimal and
# read-only-ish: a handful of harmless inspection commands and no write tools,
# so an unconfigured agent degrades to something safe rather than to something
# powerful.
FALLBACK_COMMANDS_ALLOWLIST = ["pwd", "ls", "cat", "grep", "rg"]
FALLBACK_TOOLS = ["read_file", "list_files", "search_files"]
FALLBACK_MODEL = "qwen2.5-coder:7b"
FALLBACK_MAX_ITERATIONS = 6
FALLBACK_MAX_CONTEXT_TOKENS = 16_000
MANAGED_PROTOCOL_VERSION = "1"
MANAGED_RUNTIME = MANAGED_RUNTIME_FRAMEWORK
SUPPORTED_APPROVAL_POLICIES = frozenset({"auto", "per_step", "suggestion_only"})

_BEARER_PREFIX = "Bearer "

# The managed-agent schema readiness check compares the applied Alembic revision
# against the deployment's code head. Derive that head from the migration graph
# instead of baking a literal into the release: a hardcoded value silently goes
# stale the moment a migration is added and would falsely report readiness.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_ALEMBIC_SCRIPT_LOCATION = _REPO_ROOT / "src" / "database" / "migration"

_expected_schema_revision_cache: Optional[str] = None
_expected_schema_revision_resolved = False


@router.post("/chat/completions")
async def acp_chat_completions(
    body: dict = Body(...),
    scope=Depends(require_acp_scope),
    app: App = Depends(App.get_instance),
) -> Response:
    """Proxy a built-in agent turn without exposing provider credentials locally."""
    if body.get("stream"):
        raise HTTPException(status_code=400, detail="Streaming is not supported for ACP inference.")

    db = app.get_db_session()
    try:
        user_uuid = uuid.UUID(scope.user_id)
        assignment = registry.resolve_assignment_context(db, user_uuid)
        if assignment is None:
            raise HTTPException(status_code=503, detail="No active agent profile is configured.")
        # Live enrollment/window/kill-switch gate BEFORE provider resolution: a
        # stopped or closed study never reaches a provider connection.
        _require_funded_access(
            db, account_id=user_uuid, study_id=assignment.study_id
        )
        profile = assignment.profile
        funding_owner_user_id, owner_is_admin = provider_module.funding_owner_for_profile(
            db, profile
        )
        try:
            connection = provider_module.resolve_task_connection(
                db,
                profile,
                funding_owner_user_id,
                owner_is_admin=owner_is_admin,
            )
            upstream = provider_module.resolve_upstream(
                model=profile.model,
                connection=connection,
                framework_version=profile.framework_version,
            )
        except provider_module.ProviderReadinessError as exc:
            raise HTTPException(
                status_code=503, detail={"code": exc.code, "message": exc.message}
            ) from exc
    finally:
        db.close()

    payload = dict(body)
    payload["model"] = profile.model
    if profile.temperature is not None:
        payload["temperature"] = profile.temperature
    async with httpx.AsyncClient(timeout=120) as client:
        upstream_response = await client.post(
            upstream.endpoint(responses_api=False),
            json=payload,
            headers={"Authorization": f"Bearer {upstream.api_key}"},
        )
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        media_type=upstream_response.headers.get("content-type", "application/json"),
    )


class ManagedRunRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=500)


class ManagedInferenceRequest(ManagedRunRequest):
    request: dict


def _bearer_token(authorization: str) -> Optional[str]:
    if not authorization.startswith(_BEARER_PREFIX):
        return None
    token = authorization[len(_BEARER_PREFIX) :]
    return token or None


def _schema_revision(app: App) -> Optional[str]:
    """Return the applied Alembic revision without relying on ORM metadata."""
    db = app.get_db_session()
    try:
        return db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception as error:
        logging.warning("[ACP/capabilities] schema revision unavailable: %s", error)
        return None
    finally:
        db.close()


def _derive_expected_schema_revision() -> Optional[str]:
    """Return the single Alembic head, or ``None`` when it is not unique.

    ``alembic.ini`` records ``script_location``/``version_locations`` relative to
    the package root, so resolve them explicitly: Alembic otherwise resolves
    relative locations against the process working directory, which differs
    between a dev shell and the deployed server.
    """
    try:
        cfg = Config(str(_ALEMBIC_INI))
        cfg.set_main_option("path_separator", "os")
        cfg.set_main_option("script_location", str(_ALEMBIC_SCRIPT_LOCATION))
        cfg.set_main_option(
            "version_locations", str(_ALEMBIC_SCRIPT_LOCATION / "versions")
        )
        heads = ScriptDirectory.from_config(cfg).get_heads()
    except Exception as error:
        logging.warning(
            "[ACP/capabilities] could not derive the Alembic head: %s", error
        )
        return None
    if len(heads) != 1:
        logging.warning(
            "[ACP/capabilities] expected exactly one Alembic head, found %d: %r",
            len(heads),
            heads,
        )
        return None
    return heads[0]


def expected_schema_revision() -> Optional[str]:
    """Alembic revision the managed-agent schema must be at to be ready.

    ``MANAGED_SCHEMA_REQUIRED_REVISION`` is an operator escape hatch that pins an
    explicit revision (for example in a staged rollout). Otherwise the expected
    revision is the migration graph's single head; zero or multiple heads yield
    ``None`` so the readiness probe fails closed instead of guessing. The derived
    value is memoized because the head cannot change while the process runs.
    """
    override = os.environ.get("MANAGED_SCHEMA_REQUIRED_REVISION", "").strip()
    if override:
        return override
    global _expected_schema_revision_cache, _expected_schema_revision_resolved
    if not _expected_schema_revision_resolved:
        _expected_schema_revision_cache = _derive_expected_schema_revision()
        _expected_schema_revision_resolved = True
    return _expected_schema_revision_cache


@router.get("/capabilities")
def get_acp_capabilities(app: App = Depends(App.get_instance)) -> dict:
    """Advertise the managed-agent contract and whether its schema is deployed.

    Deployment readiness is gated on the expected Alembic revision so a
    readiness probe can fail before participant traffic is enabled: when the
    schema is missing or behind, this returns HTTP 503 instead of a 200 with
    ``schema_ready=False``.
    """
    revision = _schema_revision(app)
    expected = expected_schema_revision()
    ready = revision is not None and revision == expected
    payload = {
        "managed_protocol_versions": [MANAGED_PROTOCOL_VERSION],
        "supported_runtimes": [MANAGED_RUNTIME],
        "schema_ready": ready,
        "schema_revision": revision,
        "expected_schema_revision": expected,
    }
    if not ready:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Managed agent schema not ready: have {revision!r}, "
                f"want {expected!r}"
            ),
        )
    return payload


@router.post(
    "/persistent-auth-token",
    response_model=PersistentAuthTokenPostResponse,
    status_code=201,
)
def create_persistent_auth_token(
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
) -> JsonResponseWithStatus:
    """Mint a non-expiring auth token for the calling admin.

    Needed for unattended runs (benchmark harnesses, long-lived dev setups)
    where nobody is around to re-login when the normal token expires. Admin-only
    and scoped to the caller's own user, since it deliberately sidesteps
    session expiry.
    """
    auth_token = create_uuid()
    app.get_redis_manager().set(
        "auth_token",
        auth_token,
        {"user_id": str(current_user.user_id)},
    )
    logging.info(
        f"[ACP] persistent auth token minted for admin "
        f"{str(current_user.user_id)[:8]}…"
    )
    return JsonResponseWithStatus(
        status_code=201,
        content=PersistentAuthTokenPostResponse(auth_token=auth_token),
    )


@router.post("/grant", response_model=PrepareAcpGrantPostResponse, status_code=201)
def prepare_acp_grant(
    request: Queries.PrepareAcpGrant,
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie(""),
) -> JsonResponseWithStatus:
    """Mint a one-time launch grant for the authenticated plugin.

    The plugin writes the returned grant where the agent process will find it.
    Grants are short-lived and single-use, so a stale handoff file is inert.

    The grant is the participant's funded launch credential, so it is gated by
    the same live-enrollment/window/kill-switch rule as every other funded path.
    """
    user_id = _resolve_cookie_account(app, auth_token)
    if user_id is not None:
        db = app.get_db_session()
        try:
            kill_switch_check = operations_store.db_kill_switch_check(db)
            access.require_live_enrollment(
                db, account_id=user_id, kill_switch_check=kill_switch_check
            )
        except access.FundedAccessRefused as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        finally:
            db.close()

    service = AcpAuthorizationService(app.get_redis_manager())
    try:
        prepared = service.prepare_grant(
            auth_token=auth_token,
            project_id=str(request.project_id),
            workspace=request.workspace,
            launch_id=request.launch_id,
            path_format=request.path_format,
            managed_protocol_version=request.managed_protocol_version,
        )
        return JsonResponseWithStatus(
            status_code=201,
            content=PrepareAcpGrantPostResponse(
                grant=prepared.grant,
                workspace=prepared.workspace,
                expires_in_seconds=prepared.expires_in_seconds,
                launch_id=prepared.launch_id,
                path_format=prepared.path_format,
                managed_protocol_version=prepared.managed_protocol_version,
            ),
        )
    except AcpAuthorizationDenied as error:
        return JsonResponseWithStatus(
            status_code=401,
            content=AcpAuthorizationError(message=str(error)),
        )


def _resolve_cookie_account(app: App, auth_token: str) -> Optional[uuid.UUID]:
    """Resolve the cookie's account id, or ``None`` when it is not authenticated."""
    if not auth_token:
        return None
    try:
        info = app.get_redis_manager().get("auth_token", auth_token)
        if not isinstance(info, dict) or not info.get("user_id"):
            return None
        return uuid.UUID(str(info["user_id"]))
    except Exception:
        return None


@router.post("/session/exchange", response_model=ExchangeAcpGrantPostResponse)
def exchange_acp_grant(
    request: Queries.ExchangeAcpGrant,
    app: App = Depends(App.get_instance),
) -> JsonResponseWithStatus:
    """Trade a launch grant for the agent process's session token.

    Unauthenticated by design — possession of the grant *is* the credential.
    That's safe because the grant is single-use, short-lived, and bound to a
    scope the redeemer cannot influence.
    """
    service = AcpAuthorizationService(app.get_redis_manager())
    try:
        session = service.exchange_grant(request.grant)
        return JsonResponseWithStatus(
            status_code=200,
            content=ExchangeAcpGrantPostResponse(
                acp_token=session.acp_token,
                project_id=session.project_id,
                workspace=session.workspace,
                expires_in_seconds=session.expires_in_seconds,
            ),
        )
    except AcpAuthorizationDenied as error:
        return JsonResponseWithStatus(
            status_code=401,
            content=AcpAuthorizationError(message=str(error)),
        )


@router.post(
    "/session/validate-or-refresh",
    response_model=ValidateAcpSessionPostResponse,
)
def validate_or_refresh_acp_session(
    app: App = Depends(App.get_instance),
    authorization: str = Header(default=""),
) -> JsonResponseWithStatus:
    """Revalidate an agent session and slide its expiry.

    Also re-checks that the parent plugin login is still alive, so logging out
    in the IDE invalidates the agent process rather than leaving it running with
    a credential valid for another hour.
    """
    token = _bearer_token(authorization)
    if token is None:
        return JsonResponseWithStatus(
            status_code=401, content=AcpAuthorizationError()
        )
    service = AcpAuthorizationService(app.get_redis_manager())
    try:
        session = service.validate_or_refresh(token)
        return JsonResponseWithStatus(
            status_code=200,
            content=ValidateAcpSessionPostResponse(
                project_id=session.project_id,
                workspace=session.workspace,
                expires_in_seconds=session.expires_in_seconds,
            ),
        )
    except AcpAuthorizationDenied as error:
        return JsonResponseWithStatus(
            status_code=401,
            content=AcpAuthorizationError(message=str(error)),
        )


@router.get("/agent-config", response_model=AcpAgentConfigGetResponse)
def get_acp_agent_config(
    app: App = Depends(App.get_instance),
    authorization: str = Header(default=""),
    managed_protocol_version: Optional[str] = Query(default=None),
) -> JsonResponseWithStatus:
    """Return the runtime config for the calling agent process.

    Resolution order:

    1. the user's assigned agent profile (the sticky A/B draw) — this is the
       normal path, and it's what makes the assignment govern the runtime;
    2. the user's ``config`` row, whose optional ``agent`` section can override
       the command allowlist (an operational safety setting rather than an
       experimental condition, so it stays outside the profile);
    3. the conservative fallback constants above.

    No API key is ever returned — only ``api_key_ref``, the environment
    variable name the agent reads it from locally.
    """
    token = _bearer_token(authorization)
    if token is None:
        return JsonResponseWithStatus(
            status_code=401, content=AcpAuthorizationError()
        )

    service = AcpAuthorizationService(app.get_redis_manager())
    try:
        session = service.validate_or_refresh(token)
    except AcpAuthorizationDenied:
        return JsonResponseWithStatus(
            status_code=401, content=AcpAuthorizationError()
        )

    # Fallback defaults, overridden below where a profile/config supplies values.
    # Managed callers never use these fallbacks (see managed branch below);
    # they fail closed instead so a malformed study profile cannot start work.
    commands_allowlist = list(FALLBACK_COMMANDS_ALLOWLIST)
    tools = list(FALLBACK_TOOLS)
    tools_parse_failed = False
    model = FALLBACK_MODEL
    max_iterations = FALLBACK_MAX_ITERATIONS
    agent_profile: Optional[str] = None
    framework_version: Optional[str] = None
    base_url: Optional[str] = None
    api_key_ref: Optional[str] = None
    approval_policy: Optional[str] = None
    temperature: Optional[float] = None
    max_context_tokens: Optional[int] = None
    store_agent_content = False

    db = app.get_db_session()
    try:
        try:
            user_uuid = uuid.UUID(str(session.user_id))
        except (ValueError, TypeError):
            logging.warning(
                f"[ACP/agent-config] scope user_id is not a UUID: {session.user_id!r}"
            )
            user_uuid = None

        if user_uuid is not None:
            # Import locally to avoid a circular import at module load time
            # (agents.registry → database.crud → … → this router package).
            from agents import registry

            profile = registry.resolve_assignment(db, user_uuid)
            if profile is not None:
                agent_profile = profile.name
                framework_version = profile.framework_version
                model = profile.model
                approval_policy = profile.approval_policy
                temperature = profile.temperature
                max_context_tokens = profile.max_context_tokens
                max_iterations = max(1, int(profile.max_steps or max_iterations))
                try:
                    parsed_tools = json.loads(profile.tools_json or "[]")
                    if isinstance(parsed_tools, list):
                        tools = [str(t) for t in parsed_tools]
                    else:
                        tools_parse_failed = True
                except (json.JSONDecodeError, TypeError):
                    tools_parse_failed = True
                    logging.warning(
                        f"[ACP/agent-config] profile {profile.name!r} has invalid "
                        f"tools_json — using fallback tool set"
                    )
                logging.info(
                    f"[ACP/agent-config] profile={profile.name!r} "
                    f"runtime={framework_version} model={model} "
                    f"tools={len(tools)} max_iterations={max_iterations}"
                )
            else:
                logging.warning(
                    "[ACP/agent-config] no active profile for user — "
                    "serving conservative fallback config"
                )

            store_agent_content = resolve_store_agent_content_for_acp(
                db, session.user_id
            )

            # The command allowlist is an operational guardrail, so it can be
            # narrowed per-user via the config row independently of the profile.
            user = crud.get_user_by_id(db, user_uuid) if user_uuid else None
            if user is not None:
                config_row = crud.get_config_by_id(db, user.config_id)
                if config_row is not None and config_row.config_data:
                    overrides = _parse_agent_config_section(config_row.config_data)
                    raw_allowlist = overrides.get("commands_allowlist")
                    if isinstance(raw_allowlist, list):
                        commands_allowlist = [
                            str(c).strip() for c in raw_allowlist if c
                        ]
    except Exception as error:
        if managed_protocol_version is not None:
            raise HTTPException(
                status_code=503,
                detail="Assigned managed-agent policy could not be resolved",
            ) from error
        # Legacy/developer runtimes retain their conservative fallback behavior.
        logging.error(
            f"[ACP/agent-config] config resolution failed, using fallbacks — {error}",
            exc_info=True,
        )
    finally:
        db.close()

    transport: Optional[str] = None
    if managed_protocol_version is not None:
        if managed_protocol_version != MANAGED_PROTOCOL_VERSION:
            raise HTTPException(status_code=426, detail="Unsupported managed protocol")
        if agent_profile is None:
            raise HTTPException(
                status_code=503,
                detail="No active study agent profile is assigned to this user",
            )
        if tools_parse_failed:
            raise HTTPException(
                status_code=503, detail="Assigned profile has invalid tools"
            )
        if (
            not model
            or approval_policy not in SUPPORTED_APPROVAL_POLICIES
            or max_iterations < 1
            or not set(tools).issubset(CODE4ME2_AGENT_TOOLS)
        ):
            raise HTTPException(
                status_code=503, detail="Assigned profile policy is incomplete"
            )
        if framework_version != MANAGED_RUNTIME:
            raise HTTPException(
                status_code=409,
                detail=f"Assigned runtime {framework_version!r} is not managed in this release",
            )
        # Managed runtimes always use this server as transport. Provider location
        # and credential references stay server-side.
        base_url = None
        api_key_ref = None
        if max_context_tokens is None:
            max_context_tokens = FALLBACK_MAX_CONTEXT_TOKENS
        transport = "managed_backend"

    return JsonResponseWithStatus(
        status_code=200,
        content=AcpAgentConfigGetResponse(
            agent_profile=agent_profile,
            framework_version=framework_version,
            model=model,
            base_url=base_url,
            api_key_ref=api_key_ref,
            commands_allowlist=commands_allowlist,
            tools=tools,
            max_iterations=max_iterations,
            max_context_tokens=max_context_tokens,
            approval_policy=approval_policy,
            temperature=temperature,
            store_agent_content=store_agent_content,
            transport=transport,
            managed_protocol_version=(
                MANAGED_PROTOCOL_VERSION if transport is not None else None
            ),
        ),
    )


def _managed_policy(db, user_id: uuid.UUID, profile) -> dict:
    """Build the executable v1 policy; malformed study profiles fail closed."""
    if profile.framework_version != MANAGED_RUNTIME:
        raise HTTPException(
            status_code=409,
            detail=f"Assigned runtime {profile.framework_version!r} is not managed in this release",
        )
    try:
        tools = json.loads(profile.tools_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=503, detail="Assigned profile has invalid tools") from error
    if (
        not isinstance(tools, list)
        or not all(isinstance(item, str) and item.strip() for item in tools)
        or not set(tools).issubset(CODE4ME2_AGENT_TOOLS)
    ):
        raise HTTPException(status_code=503, detail="Assigned profile has invalid tools")
    try:
        max_iterations = int(profile.max_steps)
    except (TypeError, ValueError):
        max_iterations = 0
    temperature = profile.temperature
    max_context_tokens = profile.max_context_tokens
    if isinstance(temperature, bool) or (
        temperature is not None
        and (
            not isinstance(temperature, (int, float))
            or not 0.0 <= float(temperature) <= 2.0
        )
    ):
        raise HTTPException(status_code=503, detail="Assigned profile temperature is invalid")
    if max_context_tokens is None:
        max_context_tokens = FALLBACK_MAX_CONTEXT_TOKENS
    if (
        isinstance(max_context_tokens, bool)
        or not isinstance(max_context_tokens, int)
        or max_context_tokens < 1
    ):
        raise HTTPException(status_code=503, detail="Assigned profile context limit is invalid")
    if (
        not isinstance(profile.model, str)
        or not profile.model.strip()
        or profile.approval_policy not in SUPPORTED_APPROVAL_POLICIES
        or max_iterations < 1
    ):
        raise HTTPException(status_code=503, detail="Assigned profile policy is incomplete")

    commands_allowlist = list(FALLBACK_COMMANDS_ALLOWLIST)
    user = crud.get_user_by_id(db, user_id)
    if user is not None:
        config_row = crud.get_config_by_id(db, user.config_id)
        if config_row is not None and config_row.config_data:
            raw_allowlist = _parse_agent_config_section(config_row.config_data).get(
                "commands_allowlist"
            )
            if isinstance(raw_allowlist, list):
                if not all(isinstance(command, str) and command.strip() for command in raw_allowlist):
                    raise HTTPException(status_code=503, detail="Assigned command policy is invalid")
                commands_allowlist = [command.strip() for command in raw_allowlist]

    return {
        "version": MANAGED_PROTOCOL_VERSION,
        "transport": "managed_backend",
        "agent_profile": profile.name,
        "framework_version": profile.framework_version,
        "model": profile.model.strip(),
        "tools": tools,
        "approval_policy": profile.approval_policy,
        "temperature": float(temperature) if temperature is not None else None,
        "max_iterations": max_iterations,
        "max_context_tokens": max_context_tokens,
        "commands_allowlist": commands_allowlist,
        "store_agent_content": resolve_store_agent_content_for_acp(db, str(user_id)),
    }


@router.get("/readiness")
def get_participant_readiness(
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
) -> dict:
    """Resolve and validate the signed-in participant's sticky study arm."""
    db = app.get_db_session()
    try:
        from agents import registry

        profile = registry.resolve_assignment(db, current_user.user_id)
        if profile is None:
            raise HTTPException(
                status_code=503,
                detail="No active study agent profile is assigned to this user",
            )
        policy = _managed_policy(db, current_user.user_id, profile)
        funding_owner_user_id, owner_is_admin = provider_module.funding_owner_for_profile(
            db, profile
        )
        try:
            connection = provider_module.resolve_task_connection(
                db,
                profile,
                funding_owner_user_id,
                owner_is_admin=owner_is_admin,
            )
            provider_module.resolve_upstream(
                model=profile.model,
                connection=connection,
                framework_version=profile.framework_version,
            )
        except provider_module.ProviderReadinessError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        return {
            "ready": True,
            "managed_protocol_version": MANAGED_PROTOCOL_VERSION,
            "runtime": profile.framework_version,
            "agent_profile": profile.name,
            "policy_version": policy["version"],
        }
    finally:
        db.close()


def _scope_ids(scope: AcpServerAuthorization) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    try:
        return (
            uuid.UUID(str(scope.user_id)),
            uuid.UUID(str(scope.project_id)),
            uuid.UUID(str(scope.session_id)),
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=401, detail="ACP authorization scope is malformed") from error


def _require_managed_task(db, body: ManagedRunRequest, scope: AcpServerAuthorization):
    user_id, project_id, _ = _scope_ids(scope)
    task = crud.get_agent_task_by_external_run_id(db, body.run_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Managed run does not exist")
    if (
        task.owner_user_id != user_id
        or task.owner_project_id != project_id
        or task.agent_session_id != body.session_id
        or task.source != "code4me2_agent"
    ):
        raise HTTPException(status_code=409, detail="Managed run belongs to another scope")
    return task


@router.post("/runs")
def create_managed_run(
    body: ManagedRunRequest,
    app: App = Depends(App.get_instance),
    scope: AcpServerAuthorization = Depends(require_acp_server_scope),
) -> JSONResponse:
    """Idempotently create a self-reporting managed runtime task."""
    user_id, project_id, parent_session_id = _scope_ids(scope)
    db = app.get_db_session()
    try:
        existing = crud.get_agent_task_by_external_run_id(db, body.run_id)
        if existing is not None:
            task = _require_managed_task(db, body, scope)
            # A replayed run must still satisfy current funded state.
            _require_funded_task(db, task)
            if not isinstance(task.policy_snapshot, dict):
                raise HTTPException(status_code=503, detail="Managed run policy is unavailable")
            return JSONResponse(
                {"task_id": str(task.task_id), "run_id": body.run_id, "policy": task.policy_snapshot},
                status_code=200,
            )

        from agents import registry

        assignment = registry.resolve_assignment_context(db, user_id)
        if assignment is None:
            raise HTTPException(
                status_code=503, detail="No active study agent profile is assigned to this user"
            )
        profile = assignment.profile
        policy = _managed_policy(db, user_id, profile)
        task = crud.create_agent_task(
            db,
            agent_profile=profile.name,
            model=profile.model,
            approval_policy=profile.approval_policy,
            tools_json=profile.tools_json,
            temperature=profile.temperature,
            framework_version=profile.framework_version,
            session_id=parent_session_id,
            owner_user_id=user_id,
            funding_owner_user_id=getattr(profile, "funding_owner_user_id", None),
            owner_project_id=project_id,
            external_run_id=body.run_id,
            agent_session_id=body.session_id,
            source="code4me2_agent",
            status="running",
            started_at=datetime.now(timezone.utc),
            policy_snapshot=policy,
            study_id=assignment.study_id,
            study_assignment_id=assignment.assignment_id,
            profile_id=profile.profile_id,
            consent_content_storage=policy["store_agent_content"],
        )
        return JSONResponse(
            {"task_id": str(task.task_id), "run_id": body.run_id, "policy": policy},
            status_code=201,
        )
    except HTTPException:
        raise
    except Exception as error:
        # A concurrent identical request can race the unique external_run_id.
        # Roll back before attempting an ownership-safe idempotent read.
        db.rollback()
        existing = crud.get_agent_task_by_external_run_id(db, body.run_id)
        if existing is not None:
            task = _require_managed_task(db, body, scope)
            # A replayed run must still satisfy current funded state.
            _require_funded_task(db, task)
            if not isinstance(task.policy_snapshot, dict):
                raise HTTPException(status_code=503, detail="Managed run policy is unavailable")
            return JSONResponse(
                {"task_id": str(task.task_id), "run_id": body.run_id, "policy": task.policy_snapshot},
                status_code=200,
            )
        logging.error("[ACP/runs] failed to create managed run: %s", error, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create managed run") from error
    finally:
        db.close()


@router.post("/inference")
async def run_managed_inference(
    body: ManagedInferenceRequest,
    app: App = Depends(App.get_instance),
    scope: AcpServerAuthorization = Depends(require_acp_server_scope),
):
    """Relay a managed runtime model call without proxy-generated telemetry."""
    if body.request.get("stream"):
        raise HTTPException(status_code=400, detail="Managed protocol v1 requires non-streaming inference")
    if "input" in body.request or not isinstance(body.request.get("messages"), list):
        raise HTTPException(status_code=400, detail="Managed protocol v1 requires a Chat Completions request")

    _, _, parent_session_id = _scope_ids(scope)
    db = app.get_db_session()
    try:
        task = _require_managed_task(db, body, scope)
        # Live enrollment/window/kill-switch gate for funded managed inference.
        _require_funded_task(db, task)
        policy = task.policy_snapshot
        if not _valid_managed_policy_snapshot(policy):
            raise HTTPException(status_code=503, detail="Managed run policy is unavailable")
        profile = None
        if task.profile_id is not None:
            profile = crud.get_agent_profile_by_id(db, task.profile_id)
        if profile is None:
            raise HTTPException(status_code=503, detail="Managed run provider configuration is unavailable")
        funding_owner_user_id, owner_is_admin = provider_module.funding_owner_for_task(
            db, task
        )
        try:
            connection = provider_module.resolve_task_connection(
                db, profile, funding_owner_user_id, owner_is_admin=owner_is_admin
            )
        except provider_module.ProviderReadinessError as exc:
            raise HTTPException(
                status_code=503, detail={"code": exc.code, "message": exc.message}
            ) from exc
        snapshot = {
            "task_id": task.task_id,
            "agent_profile": task.agent_profile,
            "model": policy.get("model"),
            "temperature": policy.get("temperature"),
            "tools_json": json.dumps(policy.get("tools", [])),
            "framework_version": task.framework_version,
            "connection": connection,
            "content_included": bool(policy.get("store_agent_content", False)),
        }
    finally:
        db.close()

    from agents import inference

    model_request = dict(body.request)
    model_request["model"] = snapshot["model"]
    _enforce_inference_tool_policy(model_request, policy_tools=policy["tools"])
    if snapshot["temperature"] is None:
        # A participant-supplied request may not broaden or change the assigned
        # experiment policy.  Absence in the snapshot means provider default,
        # not permission to provide a client-side override.
        model_request.pop("temperature", None)
    else:
        model_request["temperature"] = snapshot["temperature"]

    return await inference.run_inference(
        task_uuid=snapshot["task_id"],
        session_uuid=parent_session_id,
        openai_body=model_request,
        enrichment=None,
        agent_profile=snapshot["agent_profile"],
        model=snapshot["model"],
        connection=snapshot["connection"],
        temperature=snapshot["temperature"],
        framework_version=snapshot["framework_version"],
        profile_tools_json=snapshot["tools_json"],
        content_included=snapshot["content_included"],
        record_observation_events=False,
        app=app,
    )


def _valid_managed_policy_snapshot(policy: object) -> bool:
    if not isinstance(policy, dict):
        return False
    temperature = policy.get("temperature")
    context_limit = policy.get("max_context_tokens")
    iterations = policy.get("max_iterations")
    tools = policy.get("tools")
    return bool(
        policy.get("version") == MANAGED_PROTOCOL_VERSION
        and isinstance(policy.get("model"), str)
        and policy["model"].strip()
        and policy.get("approval_policy") in SUPPORTED_APPROVAL_POLICIES
        and isinstance(iterations, int)
        and not isinstance(iterations, bool)
        and iterations >= 1
        and isinstance(context_limit, int)
        and not isinstance(context_limit, bool)
        and context_limit >= 1
        and (
            temperature is None
            or (
                isinstance(temperature, (int, float))
                and not isinstance(temperature, bool)
                and 0.0 <= float(temperature) <= 2.0
            )
        )
        and isinstance(tools, list)
        and all(isinstance(tool, str) and tool.strip() for tool in tools)
        and set(tools).issubset(CODE4ME2_AGENT_TOOLS)
    )


def _enforce_inference_tool_policy(
    model_request: dict,
    *,
    policy_tools: list[str],
) -> None:
    """Prevent a managed client from broadening its snapshotted tool set."""
    allowed = set(policy_tools)
    wildcard_mcp = "mcp__*" in allowed
    requested = model_request.get("tools")
    if requested is None:
        model_request.pop("tool_choice", None)
        return
    if not isinstance(requested, list):
        raise HTTPException(status_code=400, detail="Managed inference tools must be a list")
    for definition in requested:
        if not isinstance(definition, dict):
            raise HTTPException(status_code=400, detail="Managed inference tool definition is invalid")
        function = definition.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(status_code=400, detail="Managed inference tool definition is invalid")
        if name not in allowed and not (wildcard_mcp and name.startswith("mcp__")):
            raise HTTPException(
                status_code=403,
                detail=f"Tool {name!r} is not enabled by the managed run policy",
            )
    tool_choice = model_request.get("tool_choice")
    if isinstance(tool_choice, dict):
        function = tool_choice.get("function")
        selected_name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(selected_name, str) or not selected_name.strip():
            raise HTTPException(status_code=400, detail="Managed inference tool choice is invalid")
        requested_names = {
            definition["function"]["name"]
            for definition in requested
        }
        if selected_name not in requested_names:
            raise HTTPException(
                status_code=403,
                detail=f"Tool choice {selected_name!r} is not present in the managed request",
            )
    elif tool_choice not in (None, "auto", "none", "required"):
        raise HTTPException(status_code=400, detail="Managed inference tool choice is invalid")
    if not requested:
        model_request.pop("tools", None)
        model_request.pop("tool_choice", None)


def _parse_agent_config_section(config_data: str) -> dict:
    """Extract the optional ``agent`` section from a user's config blob.

    Configs are stored as JSON in this platform. Group-5's original version
    also regex-parsed a HOCON ``agent { … }`` block; that is dropped here
    because regex-parsing a config language is fragile and the JSON path is the
    only shape this repo actually writes. A non-JSON config simply yields no
    overrides rather than being half-parsed.
    """
    try:
        parsed = json.loads(config_data)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    agent_section = parsed.get("agent")
    return agent_section if isinstance(agent_section, dict) else {}
