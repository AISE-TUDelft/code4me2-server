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
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Body, Cookie, Depends, Header, HTTPException, Response

import Queries
from App import App
from agents import provider as provider_module
from backend.acp_authorization import (
    AcpAuthorizationDenied,
    AcpAuthorizationService,
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
from backend.routers.agent.consent import resolve_store_agent_content_for_acp
from backend.routers.agent.acp_auth import require_acp_scope
from agents import registry
from backend.routers.analytics.auth_utils import AuthenticatedUser, require_admin
from database import crud
from utils import create_uuid

router = APIRouter()

# Fallback runtime config, used only when no agent profile can be resolved for
# the user (e.g. every profile has been deactivated). Deliberately minimal and
# read-only-ish: a handful of harmless inspection commands and no write tools,
# so an unconfigured agent degrades to something safe rather than to something
# powerful.
FALLBACK_COMMANDS_ALLOWLIST = ["pwd", "ls", "cat", "grep", "rg"]
FALLBACK_TOOLS = ["read_file", "list_files", "search_files"]
FALLBACK_MODEL = "qwen2.5-coder:7b"
FALLBACK_MAX_ITERATIONS = 6

_BEARER_PREFIX = "Bearer "


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
        profile = registry.resolve_assignment(db, uuid.UUID(scope.user_id))
    finally:
        db.close()
    if profile is None:
        raise HTTPException(status_code=503, detail="No active agent profile is configured.")

    payload = dict(body)
    payload["model"] = profile.model
    if profile.temperature is not None:
        payload["temperature"] = profile.temperature
    upstream = provider_module.resolve_upstream(
        model=profile.model,
        base_url=profile.base_url,
        api_key_ref=profile.api_key_ref,
        framework_version=profile.framework_version,
    )
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


def _bearer_token(authorization: str) -> Optional[str]:
    if not authorization.startswith(_BEARER_PREFIX):
        return None
    token = authorization[len(_BEARER_PREFIX) :]
    return token or None


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
    """
    service = AcpAuthorizationService(app.get_redis_manager())
    try:
        prepared = service.prepare_grant(
            auth_token=auth_token,
            project_id=str(request.project_id),
            workspace=request.workspace,
        )
        return JsonResponseWithStatus(
            status_code=201,
            content=PrepareAcpGrantPostResponse(
                grant=prepared.grant,
                workspace=prepared.workspace,
                expires_in_seconds=prepared.expires_in_seconds,
            ),
        )
    except AcpAuthorizationDenied as error:
        return JsonResponseWithStatus(
            status_code=401,
            content=AcpAuthorizationError(message=str(error)),
        )


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
    commands_allowlist = list(FALLBACK_COMMANDS_ALLOWLIST)
    tools = list(FALLBACK_TOOLS)
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
                base_url = profile.base_url
                api_key_ref = profile.api_key_ref
                approval_policy = profile.approval_policy
                temperature = profile.temperature
                max_context_tokens = profile.max_context_tokens
                max_iterations = max(1, int(profile.max_steps or max_iterations))
                try:
                    parsed_tools = json.loads(profile.tools_json or "[]")
                    if isinstance(parsed_tools, list):
                        tools = [str(t) for t in parsed_tools]
                except (json.JSONDecodeError, TypeError):
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
        # Never fail the agent's startup on a config lookup problem — it would
        # leave the developer with a dead agent rather than a degraded one.
        logging.error(
            f"[ACP/agent-config] config resolution failed, using fallbacks — {error}",
            exc_info=True,
        )
    finally:
        db.close()

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
        ),
    )


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
