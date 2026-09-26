"""SQL seeding helpers shared by the ``test_ui_overhaul_*`` HTTP contracts.

Rows are inserted directly (like the other research HTTP contracts) so each
test can build exactly the account / study / enrollment / telemetry shape it
asserts on, including legacy rows the current validators would refuse.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import text

from backend.routers.analytics.auth_utils import AuthenticatedUser

from ._byoa_contract import BYOA_CONFIG_BINDINGS, INFERENCE_GATEWAY_BINDINGS

#: One passing producer platform test: enough for ``derive_qualification_status``.
PASSING_TESTS = [
    {
        "os": "macos",
        "arch": "arm64",
        "self_check": "PASS",
        "acp_initialize": "PASS",
        "ran_at": "2026-09-21T00:00:00Z",
    }
]

VALID_SESSION_POLICY = {
    "idle_timeout_seconds": 600,
    "resume_grace_seconds": 120,
    "heartbeat_seconds": 30,
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value is not None else None


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def seed_account(
    session,
    email: str,
    *,
    name: Optional[str] = None,
    is_admin: bool = False,
    can_research: bool = False,
    joined_at: Optional[datetime] = None,
) -> uuid.UUID:
    config_id = session.execute(
        text("INSERT INTO public.config (config_data) VALUES ('{}') RETURNING config_id")
    ).scalar_one()
    user_id = uuid.uuid4()
    session.execute(
        text(
            'INSERT INTO public."user" '
            "(user_id, joined_at, email, name, password, config_id, verified, "
            "is_admin, can_research) "
            "VALUES (:user_id, :joined_at, :email, :name, 'x', :config_id, true, "
            ":is_admin, :can_research)"
        ),
        {
            "user_id": user_id,
            "joined_at": joined_at or now(),
            "email": email,
            "name": name or email.split("@", 1)[0],
            "config_id": config_id,
            "is_admin": is_admin,
            "can_research": can_research,
        },
    )
    session.commit()
    return user_id


def admin_user(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=True,
        can_research=True,
        email="ui-admin@example.com",
        name="UI Admin",
    )


def researcher_user(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=False,
        can_research=True,
        email="ui-researcher@example.com",
        name="UI Researcher",
    )


def participant_user(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=False,
        can_research=False,
        email="ui-participant@example.com",
        name="UI Participant",
    )


# ---------------------------------------------------------------------------
# Studies, participants and enrollments
# ---------------------------------------------------------------------------


def seed_study(
    session,
    *,
    owner_id: uuid.UUID,
    name: str,
    research_status: str = "ACTIVE",
    description: Optional[str] = None,
    telemetry_policy: Optional[dict[str, Any]] = None,
    starts_at: Optional[datetime] = None,
    ends_at: Optional[datetime] = None,
    join_code: Optional[str] = None,
) -> uuid.UUID:
    study_id = uuid.uuid4()
    config = {
        "telemetry_policy": (
            telemetry_policy if telemetry_policy is not None else {"metadata_only": True}
        ),
        "session_policy": dict(VALID_SESSION_POLICY),
    }
    session.execute(
        text(
            "INSERT INTO public.study "
            "(study_id, name, description, created_by, starts_at, ends_at, is_active, "
            "is_research, research_status, research_config_json, research_config_digest, "
            "join_code, created_at) "
            "VALUES (:study_id, :name, :description, :owner_id, :starts_at, :ends_at, "
            ":is_active, true, :status, CAST(:config AS jsonb), 'ui-digest', :join_code, now())"
        ),
        {
            "study_id": study_id,
            "name": name,
            "description": description,
            "owner_id": owner_id,
            "starts_at": starts_at or now(),
            "ends_at": ends_at,
            "is_active": research_status == "ACTIVE",
            "status": research_status,
            "config": json.dumps(config),
            "join_code": join_code,
        },
    )
    session.commit()
    return study_id


def seed_participant(session, account_id: uuid.UUID) -> uuid.UUID:
    participant_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.research_participant (participant_id, account_id, created_at) "
            "VALUES (:participant_id, :account_id, now())"
        ),
        {"participant_id": participant_id, "account_id": account_id},
    )
    session.commit()
    return participant_id


def seed_enrollment(
    session,
    *,
    participant_id: uuid.UUID,
    study_id: uuid.UUID,
    status: str = "ACTIVE",
    enrolled_at: Optional[datetime] = None,
    consent_accepted_at: Optional[datetime] = None,
    participant_code: Optional[str] = None,
) -> uuid.UUID:
    enrollment_id = uuid.uuid4()
    enrolled = enrolled_at or now()
    session.execute(
        text(
            "INSERT INTO public.research_enrollment "
            "(enrollment_id, participant_id, study_id, participant_code, status, "
            "revocation_epoch, eligibility_json, enrolled_at, updated_at, "
            "consent_accepted_at, retention_action) "
            "VALUES (:enrollment_id, :participant_id, :study_id, :code, :status, 0, "
            "CAST(:eligibility AS jsonb), :enrolled_at, :enrolled_at, :consent, "
            "'RETAIN_ANONYMIZED')"
        ),
        {
            "enrollment_id": enrollment_id,
            "participant_id": participant_id,
            "study_id": study_id,
            "code": participant_code or f"p_{uuid.uuid4().hex[:24]}",
            "status": status,
            "eligibility": json.dumps({"eligible": True, "reasons": ["ELIGIBLE"]}),
            "enrolled_at": enrolled,
            "consent": consent_accepted_at,
        },
    )
    session.commit()
    return enrollment_id


# ---------------------------------------------------------------------------
# Releases, connections and profiles
# ---------------------------------------------------------------------------


def _insert_release(session, release_json: dict[str, Any]) -> str:
    session.execute(
        text(
            "INSERT INTO public.agent_release "
            "(release_id, agent_id, source_manifest_digest, status, release_json, created_at) "
            "VALUES (:release_id, :agent_id, :digest, 'QUALIFIED', "
            "CAST(:release_json AS jsonb), now())"
        ),
        {
            "release_id": release_json["release_id"],
            "agent_id": release_json["agent_id"],
            "digest": release_json["source_manifest_digest"],
            "release_json": json.dumps(release_json),
        },
    )
    session.commit()
    return release_json["release_id"]


def seed_packaged_release(session, *, release_id: Optional[str] = None) -> str:
    """A QUALIFIED digest-pinned release of the managed runtime."""
    release_id = release_id or f"ui-packaged-{uuid.uuid4().hex[:12]}"
    return _insert_release(
        session,
        {
            "schema_version": "1",
            "agent_id": "code4me2-agent",
            "release_id": release_id,
            "version": "1.0.0",
            "source_manifest_digest": "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex,
            "distribution_mode": "PACKAGED",
            "artifacts": [
                {
                    "os": "macos",
                    "arch": "arm64",
                    "path": "runtime-macos-arm64.zip",
                    "sha256": "sha256:" + "a" * 64,
                    "size": 1,
                }
            ],
            "tests": PASSING_TESTS,
        },
    )


def seed_byoa_release(
    session,
    *,
    agent_id: str,
    agent_package: Optional[str] = None,
    agent_command: Optional[str] = None,
    bindings: Sequence[dict[str, Any]] = tuple(BYOA_CONFIG_BINDINGS),
    release_id: Optional[str] = None,
) -> str:
    """A QUALIFIED participant-installed (BYOA) release.

    A Goose release is gateway-bound, so the runtime bindings are added unless
    the caller already declared them (or deliberately passed none).
    """
    release_id = release_id or f"ui-byoa-{uuid.uuid4().hex[:12]}"
    bindings = [dict(binding) for binding in bindings]
    if agent_id == "goose" and bindings and not any(
        binding.get("field") == "inference_gateway_credential" for binding in bindings
    ):
        bindings.extend(dict(binding) for binding in INFERENCE_GATEWAY_BINDINGS)
    return _insert_release(
        session,
        {
            "schema_version": "1",
            "agent_id": agent_id,
            "release_id": release_id,
            "version": "1.0.0",
            "source_manifest_digest": "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex,
            "distribution_mode": "BYOA_EXTERNAL",
            "agent_package": agent_package,
            "agent_command": agent_command,
            "agent_command_args": [],
            "byoa_config": [dict(binding) for binding in bindings],
            "artifacts": [],
            "tests": PASSING_TESTS,
        },
    )


def seed_connection(
    session,
    *,
    label: Optional[str] = None,
    models: Sequence[str] = ("model",),
    is_active: bool = True,
    priced: bool = True,
) -> uuid.UUID:
    """A connection whose models are priced (1/4 USD per million) unless ``priced=False``.

    Metered study arms fail closed without a price, so the default keeps the
    existing study-creation flows working; pass ``priced=False`` to exercise
    the fail-closed paths.
    """
    connection_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.provider_connection "
            "(connection_id, label, base_url, secret_ref, models_json, is_active, created_at) "
            "VALUES (:connection_id, :label, 'https://provider.test/v1', 'UI_OVERHAUL_KEY', "
            ":models, :is_active, now())"
        ),
        {
            "connection_id": connection_id,
            "label": label or f"ui-{connection_id}",
            "models": json.dumps(list(models)),
            "is_active": is_active,
        },
    )
    if priced:
        for model in models:
            session.execute(
                text(
                    "INSERT INTO public.provider_model_price "
                    "(connection_id, model, input_usd_per_million, output_usd_per_million, updated_at) "
                    "VALUES (:connection_id, :model, 1.0, 4.0, now()) "
                    "ON CONFLICT (connection_id, model) DO NOTHING"
                ),
                {"connection_id": connection_id, "model": model},
            )
    session.commit()
    return connection_id


def seed_profile(
    session,
    *,
    owner_id: uuid.UUID,
    framework_version: str = "code4me2-agent",
    release_id: Optional[str] = None,
    connection_id: Optional[uuid.UUID] = None,
    name: Optional[str] = None,
    model: str = "model",
    temperature: Optional[float] = None,
    max_context_tokens: Optional[int] = None,
    system_prompt: Optional[str] = None,
    is_active: bool = True,
) -> uuid.UUID:
    """Insert a profile row directly, bypassing the create-time validators."""
    profile_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.agent_profile "
            "(profile_id, owner_user_id, name, model, framework_version, release_id, "
            "connection_id, tools_json, approval_policy, max_steps, temperature, "
            "max_context_tokens, system_prompt, is_active) "
            "VALUES (:profile_id, :owner_id, :name, :model, :framework, :release_id, "
            ":connection_id, '[]', 'auto', 1, :temperature, :max_context_tokens, "
            ":system_prompt, :is_active)"
        ),
        {
            "profile_id": profile_id,
            "owner_id": owner_id,
            "name": name or f"ui-profile-{profile_id.hex[:8]}",
            "model": model,
            "framework": framework_version,
            "release_id": release_id,
            "connection_id": connection_id,
            "temperature": temperature,
            "max_context_tokens": max_context_tokens,
            "system_prompt": system_prompt,
            "is_active": is_active,
        },
    )
    session.commit()
    return profile_id


# ---------------------------------------------------------------------------
# Assignments, sessions and telemetry
# ---------------------------------------------------------------------------


def seed_assignment(
    session,
    *,
    enrollment_id: uuid.UUID,
    study_id: uuid.UUID,
    profile_id: uuid.UUID,
    snapshot: dict[str, Any],
    profile_digest: str = "ui-profile-digest",
) -> uuid.UUID:
    assignment_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.study_assignment "
            "(assignment_id, enrollment_id, study_id, agent_profile_id, strategy, "
            "randomization_epoch, profile_digest, profile_snapshot_json, status, assigned_at) "
            "VALUES (:assignment_id, :enrollment_id, :study_id, :profile_id, 'RANDOM_EQUAL', "
            "0, :digest, CAST(:snapshot AS jsonb), 'ACTIVE', now())"
        ),
        {
            "assignment_id": assignment_id,
            "enrollment_id": enrollment_id,
            "study_id": study_id,
            "profile_id": profile_id,
            "digest": profile_digest,
            "snapshot": json.dumps(snapshot),
        },
    )
    session.commit()
    return assignment_id


def seed_research_session(
    session,
    *,
    enrollment_id: uuid.UUID,
    study_id: uuid.UUID,
    state: str,
    last_activity_at: Optional[datetime] = None,
) -> uuid.UUID:
    session_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.research_session "
            "(session_id, enrollment_id, study_id, context_id, state, opened_at, "
            "last_activity_at, manifest_digest, environment_json, transitions_json, "
            "created_at) "
            "VALUES (:session_id, :enrollment_id, :study_id, :context_id, :state, now(), "
            ":last_activity_at, 'ui-manifest', '{}', '[]', now())"
        ),
        {
            "session_id": session_id,
            "enrollment_id": enrollment_id,
            "study_id": study_id,
            "context_id": f"ctx-{session_id.hex[:8]}",
            "state": state,
            "last_activity_at": last_activity_at,
        },
    )
    session.commit()
    return session_id


def seed_event(
    session,
    *,
    enrollment_id: uuid.UUID,
    study_id: uuid.UUID,
    research_session_id: Optional[uuid.UUID],
    event_type: str,
    sequence: int,
    occurred_at: datetime,
    tool_call_id: Optional[str] = None,
    retention_state: str = "RETAINED",
    emitter_id: str = "ui-emitter",
    source: str = "acp",
    payload: Optional[dict[str, Any]] = None,
    lifecycle_state: Optional[str] = None,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    correlations = {"tool_call_id": tool_call_id} if tool_call_id else {}
    envelope = {
        "event_id": str(event_id),
        "schema_version": "1",
        "event_type": event_type,
        "source": source,
        "correlations": correlations,
        "payload": dict(payload or {}),
    }
    if lifecycle_state is not None:
        envelope["lifecycle_state"] = lifecycle_state
    session.execute(
        text(
            "INSERT INTO public.research_event "
            "(event_id, schema_version, event_type, source, study_id, enrollment_id, "
            "research_session_id, emitter_id, emitter_sequence, occurred_at, "
            "envelope_json, digest, accepted_at, retention_state) "
            "VALUES (:event_id, '1', :event_type, :source, :study_id, :enrollment_id, "
            ":session_id, :emitter_id, :sequence, :occurred_at, CAST(:envelope AS jsonb), "
            ":digest, now(), :retention_state)"
        ),
        {
            "event_id": event_id,
            "event_type": event_type,
            "source": source,
            "study_id": study_id,
            "enrollment_id": enrollment_id,
            "session_id": research_session_id,
            "emitter_id": emitter_id,
            "sequence": sequence,
            "occurred_at": occurred_at,
            "envelope": json.dumps(envelope),
            "digest": f"ui-digest-{event_id.hex}",
            "retention_state": retention_state,
        },
    )
    session.commit()
    return event_id
