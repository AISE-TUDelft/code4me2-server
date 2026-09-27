"""The research inference gateway: Goose's metered path to the study's provider key.

``POST /api/research/inference/v1/chat/completions`` is an OpenAI-compatible
Chat Completions endpoint. Goose is pointed at it through the release's
runtime bindings (``OPENAI_HOST``/``OPENAI_BASE_PATH``) and presents the
inference capability minted at bootstrap as its bearer token. Every call:

1. decodes and verifies the capability (signature, audience ``inference``,
   scope ``inference:relay``, expiry, revocation epoch, enrollment/study);
2. re-checks the funded gate (ACTIVE enrollment, study open, kill switch at
   study and enrollment scope);
3. resolves the sticky assignment's frozen profile (Goose only) and the
   funding owner's provider connection;
4. runs ``run_inference`` with a budget meter: worst-case hold reserved before
   the request leaves, ``max_tokens`` capped, actual usage settled afterwards.

Goose's tool definitions pass through untouched (``tool_filtering=False``) and
the model is forced to the frozen profile's. No ``agent_task`` is materialised
and no observation telemetry is written here: Goose's telemetry keeps coming
from the ACP proxy, and the ledger row is the cost record. The research
session is deliberately not required to be live (budgets are per enrollment;
an idle-ended session must not break a running chat); the reservation is
attributed to the enrollment's active session when there is one.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response
from fastapi.concurrency import run_in_threadpool

from agents import inference, registry
from agents import provider as provider_module
from App import App
from backend.routers.research import access
from research.budget import (
    InferenceMeter,
    decode_capability_bearer,
    verify_inference_capability,
)
from research.participants import identity as identity_store
from research.runtime.sessions import store as session_store
from research.study.agents.enums import INFERENCE_GATEWAY_FRAMEWORKS

router = APIRouter()

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"


def _signing_secret() -> str:
    return os.environ.get("BOOTSTRAP_SIGNING_SECRET") or ""


@dataclass(frozen=True)
class GatewayContext:
    enrollment_id: uuid.UUID
    study_id: uuid.UUID
    research_session_id: Optional[uuid.UUID]
    profile: object
    connection: provider_module.ResolvedConnection


def _authorize(app: App, capability) -> GatewayContext:
    """Verify the capability and the funded gate; resolve profile + connection."""
    db = app.get_db_session()
    try:
        enrollment_row = identity_store.get_enrollment(db, capability.enrollment_id)
        if enrollment_row is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "CAPABILITY_INVALID", "message": "unknown enrollment"},
            )
        verification = verify_inference_capability(
            capability,
            secret=_signing_secret(),
            current_revocation_epoch=int(enrollment_row.revocation_epoch or 0),
            expected_enrollment_id=enrollment_row.enrollment_id,
            expected_study_id=enrollment_row.study_id,
        )
        if not verification.ok:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "CAPABILITY_INVALID",
                    "reason": verification.reason.value,
                    "message": verification.message,
                },
            )
        participant = identity_store.get_participant(db, enrollment_row.participant_id)
        if participant is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "CAPABILITY_INVALID", "message": "unknown participant"},
            )
        try:
            active = access.require_funded_access(
                db, account_id=participant.account_id, study_id=enrollment_row.study_id
            )
        except access.FundedAccessRefused as exc:
            raise HTTPException(
                status_code=403, detail={"code": exc.code, "message": exc.message}
            ) from exc
        if active.enrollment_id != enrollment_row.enrollment_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "ENROLLMENT_NOT_ACTIVE",
                    "message": "the capability's enrollment is no longer the live one",
                },
            )
        assignment = registry.resolve_assignment_context(db, participant.account_id)
        if assignment is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ASSIGNMENT_UNAVAILABLE",
                    "message": "no assigned agent profile could be resolved",
                },
            )
        profile = assignment.profile
        if profile.framework_version not in INFERENCE_GATEWAY_FRAMEWORKS:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "RUNTIME_NOT_SUPPORTED",
                    "message": (
                        f"runtime {profile.framework_version!r} does not use the "
                        "research inference gateway"
                    ),
                },
            )
        funding_owner_user_id, owner_is_admin = provider_module.funding_owner_for_profile(
            db, profile
        )
        try:
            connection = provider_module.resolve_task_connection(
                db, profile, funding_owner_user_id, owner_is_admin=owner_is_admin
            )
        except provider_module.ProviderReadinessError as exc:
            raise HTTPException(
                status_code=503, detail={"code": exc.code, "message": exc.message}
            ) from exc
        session_row = session_store.get_active_session_for_enrollment(
            db, enrollment_row.enrollment_id
        )
        research_session_id = (
            getattr(session_row, "session_id", None) if session_row is not None else None
        )
        return GatewayContext(
            enrollment_id=enrollment_row.enrollment_id,
            study_id=enrollment_row.study_id,
            research_session_id=research_session_id or capability.research_session_id,
            profile=profile,
            connection=connection,
        )
    finally:
        db.close()


@router.post(CHAT_COMPLETIONS_PATH)
async def research_gateway_chat_completions(
    body: dict = Body(...),
    authorization: str = Header(default=""),
    app: App = Depends(App.get_instance),
) -> Response:
    """Metered, server-keyed Chat Completions for a Goose study arm."""
    capability = decode_capability_bearer(authorization)
    if capability is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "CAPABILITY_INVALID",
                "message": "the Authorization bearer is not a research inference capability",
            },
        )
    if "input" in body or not isinstance(body.get("messages"), list):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CHAT_COMPLETIONS_REQUIRED",
                "message": "the research inference gateway accepts Chat Completions bodies only",
            },
        )
    context = await run_in_threadpool(_authorize, app, capability)
    meter = InferenceMeter(
        app=app,
        enrollment_id=context.enrollment_id,
        study_id=context.study_id,
        connection_id=context.connection.connection_id,
        model=context.profile.model,
        entry_point="research_gateway",
        research_session_id=context.research_session_id,
    )
    logging.info(
        "[Research/inference] enrollment=%s model=%s stream=%s messages=%d",
        str(context.enrollment_id)[:8],
        context.profile.model,
        bool(body.get("stream")),
        len(body.get("messages") or []),
    )
    return await inference.run_inference(
        task_uuid=uuid.uuid4(),
        session_uuid=context.research_session_id or uuid.uuid4(),
        openai_body=body,
        enrichment=None,
        agent_profile=getattr(context.profile, "name", None),
        model=context.profile.model,
        connection=context.connection,
        temperature=getattr(context.profile, "temperature", None),
        framework_version=context.profile.framework_version,
        profile_tools_json=None,
        content_included=False,
        record_observation_events=False,
        tool_filtering=False,
        meter=meter,
        app=app,
    )
