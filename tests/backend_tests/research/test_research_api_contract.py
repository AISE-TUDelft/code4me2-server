"""Real HTTP contracts for lifecycle, join, revoke and clone flows."""

from __future__ import annotations

import os
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from App import App
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from database.migration.migration_manager import MigrationManager
from main import app
from research.runtime.bootstrap.capability import issue_capability
from research.runtime.bootstrap.service import BootstrapSigningContext
from research.study.agents.enums import DistributionMode, QualificationStatus
from research.study.agents.models import AgentConfigBinding, AgentReleaseV1
from research.telemetry.ingestion.models import IngestionContext
from research.telemetry.ingestion.service import _record_from_event, compute_event_digest
from research.telemetry.ingestion.store import SqlAlchemyIngestionStore
from research.telemetry.models import CanonicalEventV1, Coverage, Provenance

from ._byoa_contract import BYOA_CONFIG_BINDINGS

VALID_SESSION_POLICY = {
    "idle_timeout_seconds": 600,
    "resume_grace_seconds": 120,
    "heartbeat_seconds": 30,
}

load_dotenv()
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)


@dataclass
class RuntimeApp:
    session_factory: sessionmaker

    def get_db_session(self):
        return self.session_factory()


@pytest.fixture()
def http_runtime():
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        connection.commit()
    os.environ.setdefault("TEST_MODE", "true")
    manager = MigrationManager(use_test_db=True)
    manager.init_migrations()
    manager.migrate()
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    runtime = RuntimeApp(session_factory)
    current_user = {"value": None}

    def authenticated_user():
        return current_user["value"]

    app.dependency_overrides[App.get_instance] = lambda: runtime
    app.dependency_overrides[get_current_user] = authenticated_user
    try:
        with TestClient(app) as client:
            yield client, session_factory, current_user
    finally:
        app.dependency_overrides.pop(App.get_instance, None)
        app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


def _seed_user(session, email: str, *, can_research: bool = False) -> uuid.UUID:
    config_id = session.execute(
        text("INSERT INTO public.config (config_data) VALUES ('{}') RETURNING config_id")
    ).scalar_one()
    user_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.\"user\" "
            "(user_id, joined_at, email, name, password, config_id, verified, can_research) "
            "VALUES (:user_id, :joined_at, :email, :name, 'x', :config_id, true, :can_research)"
        ),
        {
            "user_id": user_id,
            "joined_at": datetime.now(timezone.utc),
            "email": email,
            "name": email.split("@", 1)[0],
            "config_id": config_id,
            "can_research": can_research,
        },
    )
    session.commit()
    return user_id


def _profile(session, owner_id: uuid.UUID) -> uuid.UUID:
    profile_id = uuid.uuid4()
    release_id = f"http-release-{uuid.uuid4()}"
    artifact_digest = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex
    adapter_digest = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex
    session.execute(
        text(
            "INSERT INTO public.agent_release "
            "(release_id, agent_id, source_manifest_digest, status, release_json, created_at) "
            "VALUES (:release_id, 'http-agent', :source_digest, 'QUALIFIED', CAST(:release_json AS JSONB), now())"
        ),
        {
            "release_id": release_id,
            "source_digest": artifact_digest,
            "release_json": json.dumps({
                "schema_version": "1",
                "agent_id": "http-agent",
                "release_id": release_id,
                "version": "1.0.0",
                "source_manifest_digest": artifact_digest,
                "distribution_mode": "BYOA_EXTERNAL",
                "agent_command": "http-agent",
                "agent_package": "http-agent",
                "byoa_config": list(BYOA_CONFIG_BINDINGS),
                "artifacts": [],
                "adapter": {
                    "adapter_id": "http-adapter",
                    "version": "1.0.0",
                    "digest": adapter_digest,
                },
                "tests": {
                    "status": "PASS",
                    "approval_options": ["auto", "per_step", "suggestion_only"],
                    "cases": [{"case_id": "acp.initialize", "status": "PASS"}],
                },
            }),
        },
    )
    session.execute(
        text(
            "INSERT INTO public.agent_profile "
            "(profile_id, owner_user_id, name, model, framework_version, release_id, tools_json, approval_policy, max_steps) "
            "VALUES (:profile_id, :owner_id, :name, 'model', :framework, :release_id, '[]', 'auto', 1)"
        ),
        {
            "profile_id": profile_id,
            "owner_id": owner_id,
            "release_id": release_id,
            # Profiles are unique per (owner, name); each fixture gets its own.
            "name": f"http-profile-{profile_id.hex[:8]}",
            # The fixture release is BYOA; the matching framework is a
            # participant-installed one (ISSUE-03 executable contract).
            "framework": "codex",
        },
    )
    session.commit()
    return profile_id


def _connected_profile(session, owner_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    connection_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    base_profile_id = _profile(session, owner_id)
    release_id = session.execute(
        text("SELECT release_id FROM public.agent_profile WHERE profile_id = :profile_id"),
        {"profile_id": base_profile_id},
    ).scalar_one()
    session.execute(
        text("DELETE FROM public.agent_profile WHERE profile_id = :profile_id"),
        {"profile_id": base_profile_id},
    )
    session.execute(
        text(
            "INSERT INTO public.provider_connection "
            "(connection_id, label, base_url, secret_ref, models_json, is_active, created_at) "
            "VALUES (:connection_id, :label, 'https://provider.test', 'TEST_KEY', '[\"model\"]', true, now())"
        ),
        {"connection_id": connection_id, "label": f"http-{connection_id}"},
    )
    session.execute(
        text(
            "INSERT INTO public.agent_profile "
            "(profile_id, owner_user_id, name, model, framework_version, release_id, connection_id, tools_json, approval_policy, max_steps) "
            "VALUES (:profile_id, :owner_id, 'http-locked', 'model', 'codex', :release_id, :connection_id, '[]', 'auto', 1)"
        ),
        {
            "profile_id": profile_id,
            "owner_id": owner_id,
            "release_id": release_id,
            "connection_id": connection_id,
        },
    )
    session.commit()
    return profile_id, connection_id


def _qualified_byoa_release(session) -> str:
    release = AgentReleaseV1(
        agent_id="codex",
        release_id="codex-http-v1",
        version="1.0.0",
        source_manifest_digest="sha256:" + "a" * 64,
        distribution_mode=DistributionMode.BYOA_EXTERNAL,
        agent_package="codex",
        byoa_config=[
            AgentConfigBinding(**item) for item in BYOA_CONFIG_BINDINGS
        ],
        qualification_status=QualificationStatus.QUALIFIED,
    )
    release_json = release.model_dump(mode="json")
    release_json["qualification_status"] = QualificationStatus.UNQUALIFIED.value
    release_json["tests"] = {
        "status": "PASS",
        "approval_options": ["auto", "per_step", "suggestion_only"],
        "cases": [{"case_id": "acp.initialize", "status": "PASS"}],
    }
    session.execute(
        text(
            "INSERT INTO public.agent_release "
            "(release_id, agent_id, source_manifest_digest, status, release_json, created_at) "
            "VALUES (:release_id, :agent_id, :manifest, :status, CAST(:release_json AS jsonb), now())"
        ),
        {
            "release_id": release.release_id,
            "agent_id": release.agent_id,
            "manifest": release.source_manifest_digest,
            "status": release.qualification_status.value,
            "release_json": json.dumps(release_json),
        },
    )
    session.commit()
    return release.release_id


def _release_profile(
    session,
    owner_id: uuid.UUID,
    release_id: str,
    *,
    framework_version: str = "code4me2-agent",
) -> tuple[uuid.UUID, uuid.UUID]:
    connection_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.provider_connection "
            "(connection_id, label, base_url, secret_ref, models_json, is_active, created_at) "
            "VALUES (:connection_id, :label, 'https://provider.test', 'TEST_KEY', '[\"model\"]', true, now())"
        ),
        {"connection_id": connection_id, "label": f"codex-{connection_id}"},
    )
    session.execute(
        text(
            "INSERT INTO public.agent_profile "
            "(profile_id, owner_user_id, name, model, framework_version, release_id, connection_id, tools_json, approval_policy, max_steps) "
            "VALUES (:profile_id, :owner_id, 'Codex', 'model', :framework, :release_id, :connection_id, '[]', 'auto', 1)"
        ),
        {
            "profile_id": profile_id,
            "owner_id": owner_id,
            "release_id": release_id,
            "connection_id": connection_id,
            "framework": framework_version,
        },
    )
    session.commit()
    return profile_id, connection_id


def _owner(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=False,
        email="http-owner@example.com",
        name="HTTP Owner",
        can_research=True,
    )


def _participant(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=False,
        email="http-participant@example.com",
        name="HTTP Participant",
    )


def _admin(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=True,
        email="http-admin@example.com",
        name="HTTP Admin",
        can_research=True,
    )


def test_http_lifecycle_join_stop_and_clone_contract(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "http-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "http-participant@example.com")
        admin_id = _seed_user(session, "http-admin@example.com", can_research=True)
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "HTTP lifecycle study",
            "description": "contract",
            "telemetry_policy": {"metadata_only": True},
            "session_policy": {
                "idle_timeout_seconds": 60,
                "resume_grace_seconds": 60,
                "heartbeat_seconds": 10,
            },
            "profile_ids": [str(profile_id)],
        },
    )
    assert created.status_code == 201, created.text
    study = created.json()["study"]
    study_id = study["study_id"]
    join_code = study["join_code"]
    assert study["research_status"] == "DRAFT"
    assert study["research_config_digest"]
    assert study["profile_selections"][0]["profile_id"] == str(profile_id)
    assert study["enrollment_count"] == 0
    assert study["active_enrollment_count"] == 0
    assert study["assignment_count"] == 0
    assert study["active_assignment_count"] == 0
    assert study["active_session_count"] == 0
    assert study["lifecycle_capabilities"] == {
        "metadata_editable": True,
        "stoppable": True,
        "cloneable": False,
        "joinable": True,
    }
    assert study["kill_switch"] is None

    listed = client.get("/api/research/studies")
    assert listed.status_code == 200
    assert listed.json()["studies"][0]["study_id"] == study_id
    fetched = client.get(f"/api/research/studies/{study_id}")
    assert fetched.status_code == 200

    current_user["value"] = _participant(participant_id)
    resolved = client.get(f"/api/research/join/{join_code}")
    assert resolved.status_code == 200
    assert resolved.json()["study"]["study_id"] == study_id
    joined = client.post(
        "/api/research/join",
        json={"join_code": join_code, "accept_consent": True},
    )
    assert joined.status_code == 201, joined.text
    enrollment_id = joined.json()["enrollment_id"]
    assert joined.json()["assignment_id"]
    assert joined.json()["agent_profile_id"]

    current_user["value"] = _owner(owner_id)
    populated = client.get(f"/api/research/studies/{study_id}")
    assert populated.status_code == 200
    populated_study = populated.json()["study"]
    assert populated_study["enrollment_count"] == 1
    assert populated_study["active_enrollment_count"] == 1
    assert populated_study["assignment_count"] == 1
    assert populated_study["active_assignment_count"] == 1
    assert populated_study["lifecycle_capabilities"]["metadata_editable"] is False
    assert "participant_code" not in json.dumps(populated_study)
    assert "http-participant@example.com" not in json.dumps(populated_study)
    assert "TEST_KEY" not in json.dumps(populated_study)

    current_user["value"] = _admin(admin_id)
    blank_reason = client.post(
        "/api/research/operations/kill-switch",
        json={"scope_kind": "STUDY", "scope_id": study_id, "reason": "   "},
    )
    assert blank_reason.status_code == 422

    current_user["value"] = _participant(participant_id)
    unauthorized_switch = client.post(
        "/api/research/operations/kill-switch",
        json={"scope_kind": "STUDY", "scope_id": study_id, "reason": "maintenance"},
    )
    assert unauthorized_switch.status_code == 403

    current_user["value"] = _admin(admin_id)
    engaged = client.post(
        "/api/research/operations/kill-switch",
        json={"scope_kind": "STUDY", "scope_id": study_id, "reason": "maintenance"},
    )
    assert engaged.status_code == 201, engaged.text
    switch_id = engaged.json()["switch_id"]

    signing_secret = "http-contract-secret"
    bootstrap_capability = issue_capability(
        audience="research-runtime",
        scope=["telemetry:write", "session:heartbeat", "session:close"],
        ttl_seconds=900,
        revocation_epoch=0,
        secret=signing_secret,
        enrollment_id=uuid.UUID(enrollment_id),
        research_session_id=uuid.uuid4(),
        study_id=uuid.UUID(study_id),
    )
    with patch("backend.routers.research.bootstrap.BOOTSTRAP_SIGNING_SECRET", signing_secret), patch(
        "backend.routers.research.sessions.BOOTSTRAP_SIGNING_SECRET", signing_secret
    ), patch("backend.routers.research.telemetry.BOOTSTRAP_SIGNING_SECRET", signing_secret):
        session_response = client.post(
            "/api/research/sessions/",
            json={
                "capability": bootstrap_capability.model_dump(mode="json"),
                "enrollment_id": enrollment_id,
                "study_id": study_id,
                "manifest_digest": "http-manifest",
                "context_id": "http-contract-context",
            },
        )
        assert session_response.status_code == 403, session_response.text
        assert session_response.json()["detail"]["code"] == "KILL_SWITCH_ENGAGED"

        current_user["value"] = _admin(admin_id)
        released = client.post(
            f"/api/research/operations/kill-switch/{switch_id}/release"
        )
        assert released.status_code == 200, released.text

        current_user["value"] = _participant(participant_id)
        session_response = client.post(
            "/api/research/sessions/",
            json={
                "capability": bootstrap_capability.model_dump(mode="json"),
                "enrollment_id": enrollment_id,
                "study_id": study_id,
                "manifest_digest": "http-manifest",
                "context_id": "http-contract-context",
            },
        )
        assert session_response.status_code == 201, session_response.text
        research_session_id = session_response.json()["session"]["research_session_id"]
        session_capability = issue_capability(
            audience="research-runtime",
            scope=["telemetry:write", "session:heartbeat", "session:close"],
            ttl_seconds=900,
            revocation_epoch=0,
            secret=signing_secret,
            enrollment_id=uuid.UUID(enrollment_id),
            research_session_id=uuid.UUID(research_session_id),
            study_id=uuid.UUID(study_id),
        )
        heartbeat = client.post(
            "/api/research/sessions/heartbeat",
            json={
                "capability": session_capability.model_dump(mode="json"),
                "research_session_id": research_session_id,
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text
        activity = client.post(
            "/api/research/sessions/activity",
            json={
                "capability": session_capability.model_dump(mode="json"),
                "research_session_id": research_session_id,
            },
        )
        assert activity.status_code == 200, activity.text
        assert activity.json()["session"]["state"] == "running"
        event_id = str(uuid.uuid4())
        batch = client.post(
            "/api/research/telemetry/batches",
            json={
                "batch_id": str(uuid.uuid4()),
                "session_capability": session_capability.model_dump(mode="json"),
                "client_instance_id": "http-contract-client",
                "events": [
                    {
                        "event_id": event_id,
                        "schema_version": "1",
                        "event_type": "tool.completed",
                        "source": "ide",
                        "study_id": study_id,
                        "enrollment_id": enrollment_id,
                        "research_session_id": research_session_id,
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "emitter_id": "http-contract-emitter",
                        "emitter_sequence": 1,
                        "provenance": {
                            "source": "ide",
                            "normalizer_version": "1",
                            "fidelity": "normalized",
                        },
                    }
                ],
            },
        )
        assert batch.status_code == 200, batch.text
        assert len(batch.json()["accepted"]) == 1
        closed = client.post(
            "/api/research/sessions/close",
            json={
                "capability": session_capability.model_dump(mode="json"),
                "research_session_id": research_session_id,
                "reason": "explicit_completion",
            },
        )
        assert closed.status_code == 200, closed.text

    current_user["value"] = _owner(owner_id)
    locked = client.patch(
        f"/api/research/studies/{study_id}/metadata",
        json={"name": "too late"},
    )
    assert locked.status_code == 409
    assert locked.json()["detail"]["code"] == "STUDY_METADATA_LOCKED"

    stopped = client.post(
        f"/api/research/studies/{study_id}/stop", json={"actor": "http-owner"}
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["study"]["research_status"] == "STUDY_STOPPED"
    assert stopped.json()["study"]["kill_switch"]["status"] == "ENGAGED"

    current_user["value"] = _admin(admin_id)
    stopped_switch = client.post(
        "/api/research/operations/kill-switch",
        json={"scope_kind": "STUDY", "scope_id": study_id, "reason": "second switch"},
    )
    assert stopped_switch.status_code == 409
    assert stopped_switch.json()["detail"]["code"] == "STUDY_STOPPED"

    current_user["value"] = _participant(participant_id)
    stopped_join = client.get(f"/api/research/join/{join_code}")
    assert stopped_join.status_code == 409
    assert stopped_join.json()["detail"]["code"] == "STUDY_STOPPED"

    current_user["value"] = _owner(owner_id)
    cloned = client.post(
        f"/api/research/studies/{study_id}/clone",
        json={"profile_ids": [str(profile_id)]},
    )
    assert cloned.status_code == 201, cloned.text
    clone = cloned.json()["study"]
    assert clone["study_id"] != study_id
    assert clone["research_status"] == "DRAFT"
    assert clone["join_code"] != join_code
    assert [item["profile_id"] for item in clone["profile_selections"]] == [
        str(profile_id)
    ]

    session = session_factory()
    try:
        assert session.execute(
            text("SELECT count(*) FROM public.research_enrollment WHERE enrollment_id = :id"),
            {"id": enrollment_id},
        ).scalar_one() == 1
        assert session.execute(
            text("SELECT count(*) FROM public.study_assignment WHERE enrollment_id = :id"),
            {"id": enrollment_id},
        ).scalar_one() == 1
        clone_selections = session.execute(
            text(
                "SELECT profile_id FROM public.study_agent_profile WHERE study_id = :id"
            ),
            {"id": clone["study_id"]},
        ).scalars().all()
        assert [str(row) for row in clone_selections] == [str(profile_id)]
    finally:
        session.close()


def test_http_revoke_marks_enrollment_and_assignment_terminal(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "revoke-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "revoke-participant@example.com")
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "Revoke terminal study",
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [str(profile_id)],
        },
    )
    assert created.status_code == 201, created.text
    study = created.json()["study"]
    study_id = study["study_id"]

    current_user["value"] = _participant(participant_id)
    resolved = client.get(f"/api/research/join/{study['join_code']}")
    assert resolved.status_code == 200, resolved.text
    joined = client.post(
        "/api/research/join",
        json={"join_code": study["join_code"], "accept_consent": True},
    )
    assert joined.status_code == 201, joined.text
    enrollment_id = joined.json()["enrollment_id"]
    assert joined.json()["assignment_id"]

    signing_secret = "revoke-terminal-secret"
    bootstrap_capability = issue_capability(
        audience="research-runtime",
        scope=["telemetry:write", "session:heartbeat", "session:close"],
        ttl_seconds=900,
        revocation_epoch=0,
        secret=signing_secret,
        enrollment_id=uuid.UUID(enrollment_id),
        research_session_id=uuid.uuid4(),
        study_id=uuid.UUID(study_id),
    )
    with patch("backend.routers.research.bootstrap.BOOTSTRAP_SIGNING_SECRET", signing_secret), patch(
        "backend.routers.research.sessions.BOOTSTRAP_SIGNING_SECRET", signing_secret
    ):
        session_response = client.post(
            "/api/research/sessions/",
            json={
                "capability": bootstrap_capability.model_dump(mode="json"),
                "enrollment_id": enrollment_id,
                "study_id": study_id,
                "manifest_digest": "revoke-manifest",
                "context_id": "revoke-context",
            },
        )
        assert session_response.status_code == 201, session_response.text
        research_session_id = session_response.json()["session"]["research_session_id"]
        session_capability = issue_capability(
            audience="research-runtime",
            scope=["telemetry:write", "session:heartbeat", "session:close"],
            ttl_seconds=900,
            revocation_epoch=0,
            secret=signing_secret,
            enrollment_id=uuid.UUID(enrollment_id),
            research_session_id=uuid.UUID(research_session_id),
            study_id=uuid.UUID(study_id),
        )
        heartbeat = client.post(
            "/api/research/sessions/heartbeat",
            json={
                "capability": session_capability.model_dump(mode="json"),
                "research_session_id": research_session_id,
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text

    current_user["value"] = _owner(owner_id)
    revoked = client.post(
        f"/api/research/studies/{study_id}/enrollments/{enrollment_id}/revoke",
        json={"actor": "revoke-owner"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] is True
    assert revoked.json()["enrollment_id"] == enrollment_id
    assert revoked.json()["session_count"] == 1

    # Terminal enrollment asserted over HTTP: the participant's own status
    # projection now reports REVOKED.
    current_user["value"] = _participant(participant_id)
    mine = client.get("/api/research/participants/me")
    assert mine.status_code == 200, mine.text
    statuses = [
        entry["status"]
        for entry in mine.json()["enrollments"]
        if entry["enrollment_id"] == enrollment_id
    ]
    assert statuses == ["REVOKED"]

    # Session revocation asserted over HTTP: the pre-revoke capability no
    # longer operates the revoked session.
    with patch(
        "backend.routers.research.sessions.BOOTSTRAP_SIGNING_SECRET", signing_secret
    ):
        dead = client.post(
            "/api/research/sessions/heartbeat",
            json={
                "capability": session_capability.model_dump(mode="json"),
                "research_session_id": research_session_id,
            },
        )
    assert dead.status_code == 403, dead.text
    assert dead.json()["detail"]["code"] == "CAPABILITY_INVALID"

    # The revoke response carries no assignment state, so the terminal
    # assignment is asserted via an in-test database read.
    session = session_factory()
    try:
        enrollment_status = session.execute(
            text("SELECT status FROM public.research_enrollment WHERE enrollment_id = :id"),
            {"id": enrollment_id},
        ).scalar_one()
        assignment_status = session.execute(
            text("SELECT status FROM public.study_assignment WHERE enrollment_id = :id"),
            {"id": enrollment_id},
        ).scalar_one()
        revoked_session_state = session.execute(
            text(
                "SELECT state FROM public.research_session "
                "WHERE session_id = :id"
            ),
            {"id": research_session_id},
        ).scalar_one()
    finally:
        session.close()
    assert enrollment_status == "REVOKED"
    assert assignment_status == "REVOKED"
    assert revoked_session_state == "revoked"


def test_http_web_join_contract_matrix(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "matrix-owner@example.com", can_research=True)
        cross_owner_id = _seed_user(session, "matrix-cross-owner@example.com", can_research=True)
        stopped_owner_id = _seed_user(session, "matrix-stopped-owner@example.com", can_research=True)
        participant_one_id = _seed_user(session, "matrix-one@example.com")
        participant_two_id = _seed_user(session, "matrix-two@example.com")
        participant_three_id = _seed_user(session, "matrix-three@example.com")
        profile_id = _profile(session, owner_id)
        cross_profile_id = _profile(session, cross_owner_id)
        stopped_profile_id = _profile(session, stopped_owner_id)
    finally:
        session.close()

    studies = []
    for study_owner_id, name, selected_profile_id in (
        (owner_id, "Matrix active", profile_id),
        (cross_owner_id, "Matrix cross-study", cross_profile_id),
        (stopped_owner_id, "Matrix stopped", stopped_profile_id),
    ):
        current_user["value"] = _owner(study_owner_id)
        response = client.post(
            "/api/research/studies",
            json={
                "name": name,
                "session_policy": VALID_SESSION_POLICY,
                "profile_ids": [str(selected_profile_id)],
            },
        )
        assert response.status_code == 201, response.text
        studies.append(response.json()["study"])
    active, cross_study, stopped = studies

    def counts():
        session = session_factory()
        try:
            return tuple(
                session.execute(text(f"SELECT count(*) FROM public.{table}")).scalar_one()
                for table in (
                    "research_participant",
                    "research_enrollment",
                    "study_assignment",
                )
            )
        finally:
            session.close()

    current_user["value"] = None
    assert client.get(f"/api/research/join/{active['join_code']}").status_code == 401
    assert client.post(
        "/api/research/join", json={"join_code": active["join_code"], "accept_consent": True}
    ).status_code == 401

    current_user["value"] = _participant(participant_one_id)
    resolved = client.get(f"/api/research/join/{active['join_code']}")
    assert resolved.status_code == 200, resolved.text
    resolved_body = resolved.json()
    assert resolved_body["study"] == {
        "study_id": active["study_id"],
        "name": active["name"],
        "description": active["description"],
        "research_status": "DRAFT",
    }
    assert "email" not in json.dumps(resolved_body)
    assert "participant" not in json.dumps(resolved_body).lower()
    before = counts()
    for body in (
        {"join_code": active["join_code"]},
        {"join_code": active["join_code"], "accept_consent": False},
    ):
        rejected = client.post("/api/research/join", json=body)
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == "CONSENT_REQUIRED"
        assert counts() == before
    assert client.post(
        "/api/research/join",
        json={"join_code": active["join_code"], "accept_consent": True, "extra": True},
    ).status_code == 422
    assert counts() == before

    joined = client.post(
        "/api/research/join",
        json={"join_code": active["join_code"], "accept_consent": True},
    )
    assert joined.status_code == 201, joined.text
    first = joined.json()
    assert {"enrollment_id", "study_id", "assignment_id", "agent_profile_id"} <= first.keys()
    assert "email" not in json.dumps(first)
    assert counts() == (1, 1, 1)
    repeated = client.post(
        "/api/research/join",
        json={"join_code": active["join_code"], "accept_consent": True},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["enrollment_id"] == first["enrollment_id"]
    assert repeated.json()["assignment_id"] == first["assignment_id"]
    assert counts() == (1, 1, 1)

    cross_before = counts()
    cross = client.post(
        "/api/research/join",
        json={"join_code": cross_study["join_code"], "accept_consent": True},
    )
    assert cross.status_code == 409, cross.text
    assert cross.json()["detail"]["code"] == "ACTIVE_ENROLLMENT_EXISTS"
    assert counts() == cross_before

    current_user["value"] = _participant(participant_two_id)
    second = client.post(
        "/api/research/join",
        json={"join_code": cross_study["join_code"], "accept_consent": True},
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["enrollment_id"]
    current_user["value"] = _owner(cross_owner_id)
    revoked = client.post(
        f"/api/research/studies/{cross_study['study_id']}/enrollments/{second_id}/revoke",
        json={"actor": "matrix-owner"},
    )
    assert revoked.status_code == 200, revoked.text
    current_user["value"] = _participant(participant_two_id)
    terminal = client.post(
        "/api/research/join",
        json={"join_code": cross_study["join_code"], "accept_consent": True},
    )
    assert terminal.status_code == 409, terminal.text
    assert terminal.json()["detail"]["code"] == "ALREADY_ENROLLED"
    assert counts() == (2, 2, 2)

    current_user["value"] = _owner(stopped_owner_id)
    stopped_response = client.post(
        f"/api/research/studies/{stopped['study_id']}/stop", json={"actor": "matrix-owner"}
    )
    assert stopped_response.status_code == 200, stopped_response.text
    current_user["value"] = _participant(participant_three_id)
    stopped_before = counts()
    stopped_join = client.post(
        "/api/research/join",
        json={"join_code": stopped["join_code"], "accept_consent": True},
    )
    assert stopped_join.status_code == 409, stopped_join.text
    assert stopped_join.json()["detail"]["code"] == "STUDY_STOPPED"
    assert counts() == stopped_before

    current_user["value"] = _owner(stopped_owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "Revoke study",
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [str(stopped_profile_id)],
        },
    )
    assert created.status_code == 201, created.text
    study = created.json()["study"]

    current_user["value"] = _participant(participant_three_id)
    joined = client.post(
        "/api/research/join",
        json={"join_code": study["join_code"], "accept_consent": True},
    )
    assert joined.status_code == 201, joined.text
    enrollment_id = joined.json()["enrollment_id"]

    current_user["value"] = _owner(stopped_owner_id)
    revoked = client.post(
        f"/api/research/studies/{study['study_id']}/enrollments/{enrollment_id}/revoke",
        json={"actor": "http-owner"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] is True

    session = session_factory()
    try:
        assert session.execute(
            text("SELECT status FROM public.research_enrollment WHERE enrollment_id = :id"),
            {"id": enrollment_id},
        ).scalar_one() == "REVOKED"
        assert session.execute(
            text("SELECT status FROM public.study_assignment WHERE enrollment_id = :id"),
            {"id": enrollment_id},
        ).scalar_one() == "REVOKED"
    finally:
        session.close()


def test_http_profile_update_returns_typed_lock_after_study_consent(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "locked-http-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "locked-http-participant@example.com")
        profile_id, connection_id = _connected_profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "HTTP lock study",
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [str(profile_id)],
        },
    )
    assert created.status_code == 201, created.text
    study = created.json()["study"]

    current_user["value"] = _participant(participant_id)
    joined = client.post(
        "/api/research/join",
        json={"join_code": study["join_code"], "accept_consent": True},
    )
    assert joined.status_code == 201, joined.text

    current_user["value"] = _owner(owner_id)
    updated = client.put(
        f"/api/agent/profiles/{profile_id}",
        json={
            "name": "http-locked",
            "model": "model",
            "framework_version": "code4me2-agent",
            "connection_id": str(connection_id),
            "release_id": None,
            "tools_json": "[]",
            "approval_policy": "auto",
            "max_steps": 1,
            "is_active": True,
        },
    )
    assert updated.status_code == 409, updated.text
    assert updated.json()["detail"]["code"] == "PROFILE_LOCKED"


def test_http_bootstrap_qualified_codex_manifest_is_revision_free(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "codex-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "codex-participant@example.com")
        release_id = _qualified_byoa_release(session)
        profile_id, _connection_id = _release_profile(
            session, owner_id, release_id, framework_version="codex"
        )
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "Codex HTTP study",
            "session_policy": {
                "idle_timeout_seconds": 60,
                "resume_grace_seconds": 60,
                "heartbeat_seconds": 10,
            },
            "profile_ids": [str(profile_id)],
        },
    )
    assert created.status_code == 201, created.text
    study = created.json()["study"]

    current_user["value"] = _participant(participant_id)
    joined = client.post(
        "/api/research/join",
        json={"join_code": study["join_code"], "accept_consent": True},
    )
    assert joined.status_code == 201, joined.text
    enrollment_id = joined.json()["enrollment_id"]

    signing_secret = "codex-http-secret"
    with patch(
        "backend.routers.research.bootstrap._SIGNER",
        BootstrapSigningContext(secret=signing_secret),
    ):
        bootstrap = client.post(
            "/api/research/bootstrap/research-sessions",
            json={
                "enrollment_id": enrollment_id,
                "context_id": "codex-http-context",
                "environment": {"os": "macos", "arch": "arm64"},
            },
        )
    assert bootstrap.status_code == 201, bootstrap.text
    manifest = bootstrap.json()["manifest"]
    assert manifest["study_id"] == study["study_id"]
    assert "revision_id" not in manifest
    assert manifest["assignment"]["agent_profile_id"] == str(profile_id)
    assert manifest["assignment"]["profile_digest"]
    assert manifest["agent_release"]["agent_id"] == "codex"
    assert manifest["agent_release"]["distribution_mode"] == "BYOA_EXTERNAL"
    assert "TEST_KEY" not in str(manifest)

    with patch(
        "backend.routers.research.bootstrap._SIGNER",
        BootstrapSigningContext(secret=signing_secret),
    ):
        repeated = client.post(
            "/api/research/bootstrap/research-sessions",
            json={
                "enrollment_id": enrollment_id,
                "context_id": "codex-http-context",
                "environment": {"os": "macos", "arch": "arm64"},
            },
        )
    assert repeated.status_code == 201, repeated.text
    repeated_manifest = repeated.json()["manifest"]
    assert repeated_manifest["assignment"] == manifest["assignment"]
    assert repeated_manifest["research_session"] == manifest["research_session"]


def test_http_create_study_orders_multiple_profile_selections(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "multi-owner@example.com", can_research=True)
        first_profile = _profile(session, owner_id)
        second_profile = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "Multi-profile study",
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [str(second_profile), str(first_profile)],
        },
    )
    assert created.status_code == 201, created.text
    study = created.json()["study"]
    assert [item["profile_id"] for item in study["profile_selections"]] == [
        str(second_profile),
        str(first_profile),
    ]

    session = session_factory()
    try:
        rows = session.execute(
            text(
                "SELECT profile_id, selection_order FROM public.study_agent_profile "
                "WHERE study_id = :study_id ORDER BY selection_order"
            ),
            {"study_id": study["study_id"]},
        ).all()
    finally:
        session.close()
    assert [(str(row[0]), row[1]) for row in rows] == [
        (str(second_profile), 0),
        (str(first_profile), 1),
    ]


def test_http_create_study_rejects_empty_profile_selection(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "empty-owner@example.com", can_research=True)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    for payload in (
        {"name": "No profiles", "profile_ids": []},
        {"name": "Missing profiles"},
    ):
        response = client.post("/api/research/studies", json=payload)
        assert response.status_code == 422, response.text

    session = session_factory()
    try:
        assert session.execute(
            text("SELECT count(*) FROM public.study WHERE created_by = :owner"),
            {"owner": owner_id},
        ).scalar_one() == 0
        assert session.execute(
            text("SELECT count(*) FROM public.study_agent_profile")
        ).scalar_one() == 0
    finally:
        session.close()


def test_http_configuration_is_frozen_after_create_and_only_metadata_is_writable(
    http_runtime,
):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "frozen-owner@example.com", can_research=True)
        selected_profile = _profile(session, owner_id)
        other_profile = _profile(session, owner_id)
    finally:
        session.close()

    telemetry_policy = {"metadata_only": True}
    session_policy = {
        "heartbeat_seconds": 10,
        "idle_timeout_seconds": 60,
        "resume_grace_seconds": 30,
    }
    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "Frozen study",
            "telemetry_policy": telemetry_policy,
            "session_policy": session_policy,
            "profile_ids": [str(selected_profile)],
        },
    )
    assert created.status_code == 201, created.text
    study_id = created.json()["study"]["study_id"]

    # The configuration write surface is create-once: the only study write route
    # that accepts a body is /metadata, and no bare-path write route exists.
    from backend.routers.research import studies as studies_router

    writes = {
        (route.path, method)
        for route in studies_router.router.routes
        for method in getattr(route, "methods", set())
        if method in {"PATCH", "PUT", "POST"} and route.path in {"", "/{study_id}"}
    }
    assert writes == {("", "POST")}, writes
    assert "/{study_id}/metadata" in {route.path for route in studies_router.router.routes}
    assert client.patch(
        f"/api/research/studies/{study_id}", json={"name": "forced"}
    ).status_code == 405

    # A forced configuration update after create cannot change profile ids,
    # telemetry policy or the session schedule.
    forced = client.patch(
        f"/api/research/studies/{study_id}/metadata",
        json={
            "name": "Renamed",
            "profile_ids": [str(other_profile)],
            "telemetry_policy": {},
            "session_policy": {"heartbeat_seconds": 999},
        },
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["study"]["name"] == "Renamed"

    session = session_factory()
    try:
        stored_config = session.execute(
            text("SELECT research_config_json FROM public.study WHERE study_id = :id"),
            {"id": study_id},
        ).scalar_one()
        selections = session.execute(
            text(
                "SELECT profile_id FROM public.study_agent_profile "
                "WHERE study_id = :id ORDER BY selection_order"
            ),
            {"id": study_id},
        ).scalars().all()
    finally:
        session.close()
    assert stored_config["telemetry_policy"] == telemetry_policy
    assert stored_config["session_policy"] == session_policy
    assert stored_config["profile_ids"] == [str(selected_profile)]
    assert [str(row) for row in selections] == [str(selected_profile)]


def test_http_stopped_study_cannot_be_reactivated_or_reconfigured(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "reactivate-owner@example.com", can_research=True)
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "Terminal study",
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [str(profile_id)],
        },
    )
    assert created.status_code == 201, created.text
    study_id = created.json()["study"]["study_id"]

    stopped = client.post(
        f"/api/research/studies/{study_id}/stop", json={"actor": "owner"}
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["study"]["research_status"] == "STUDY_STOPPED"

    # There is no resume/reactivate/publish surface.
    from backend.routers.research import studies as studies_router

    paths = {route.path for route in studies_router.router.routes}
    assert not [
        path
        for path in paths
        if any(token in path for token in ("resume", "reactivate", "publish"))
    ]
    assert client.post(f"/api/research/studies/{study_id}/resume").status_code == 404

    # The closest available write route is metadata, and it refuses terminally.
    resumed = client.patch(
        f"/api/research/studies/{study_id}/metadata", json={"name": "reopened"}
    )
    assert resumed.status_code == 409, resumed.text
    assert resumed.json()["detail"]["code"] == "STUDY_METADATA_LOCKED"

    fetched = client.get(f"/api/research/studies/{study_id}")
    assert fetched.status_code == 200
    assert fetched.json()["study"]["research_status"] == "STUDY_STOPPED"


def test_http_participant_cannot_revoke_and_no_leave_route_exists(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "noleave-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "noleave-participant@example.com")
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "No-leave study",
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [str(profile_id)],
        },
    )
    assert created.status_code == 201, created.text
    study = created.json()["study"]

    current_user["value"] = _participant(participant_id)
    joined = client.post(
        "/api/research/join",
        json={"join_code": study["join_code"], "accept_consent": True},
    )
    assert joined.status_code == 201, joined.text
    enrollment_id = joined.json()["enrollment_id"]

    revoked = client.post(
        f"/api/research/studies/{study['study_id']}/enrollments/{enrollment_id}/revoke",
        json={"actor": "participant"},
    )
    assert revoked.status_code == 403, revoked.text
    assert revoked.json()["detail"]["code"] == "RESEARCHER_REQUIRED"

    # The participant control plane exposes no leave/withdraw surface.
    from backend.routers.research import router as research_router

    paths = {route.path for route in research_router.routes}
    assert not [
        path
        for path in paths
        if "leave" in path.lower() or "withdraw" in path.lower()
    ]

    session = session_factory()
    try:
        assert session.execute(
            text("SELECT status FROM public.research_enrollment WHERE enrollment_id = :id"),
            {"id": enrollment_id},
        ).scalar_one() == "ACTIVE"
    finally:
        session.close()


def _seed_retained_event(session_factory, *, study_id: str, enrollment_id: str) -> None:
    """Persist one canonical event so the retained read model has content."""
    session = session_factory()
    try:
        research_session_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO public.research_session "
                "(session_id, enrollment_id, study_id, context_id, state, manifest_digest, "
                "environment_json, transitions_json, created_at) "
                "VALUES (:session_id, :enrollment_id, :study_id, 'retained-ctx', 'running', "
                "'manifest', '{}', '[]', now())"
            ),
            {
                "session_id": research_session_id,
                "enrollment_id": enrollment_id,
                "study_id": study_id,
            },
        )
        session.commit()
        now = datetime.now(timezone.utc)
        event = CanonicalEventV1(
            event_id=uuid.uuid4(),
            schema_version="1",
            event_type="tool.completed",
            source="ide",
            study_id=uuid.UUID(study_id),
            enrollment_id=uuid.UUID(enrollment_id),
            research_session_id=research_session_id,
            occurred_at=now,
            emitter_id="retained-emitter",
            emitter_sequence=1,
            payload={"tool_name": "read"},
            provenance=Provenance(source="ide", normalizer_version="1"),
            coverage=Coverage(state="AVAILABLE", capability="tool_lifecycle"),
        )
        context = IngestionContext(
            study_id=uuid.UUID(study_id),
            enrollment_id=uuid.UUID(enrollment_id),
            research_session_id=research_session_id,
            revocation_epoch=0,
        )
        record = _record_from_event(
            event, context, compute_event_digest(event), accepted_at=now
        )
        store = SqlAlchemyIngestionStore(session)
        store.insert_events([record])
        store.commit()
    finally:
        session.close()


def test_http_researcher_read_models_keep_retained_rows_after_stop(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "retained-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "retained-participant@example.com")
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "Retained study",
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [str(profile_id)],
        },
    )
    assert created.status_code == 201, created.text
    study = created.json()["study"]

    current_user["value"] = _participant(participant_id)
    joined = client.post(
        "/api/research/join",
        json={"join_code": study["join_code"], "accept_consent": True},
    )
    assert joined.status_code == 201, joined.text
    enrollment_id = joined.json()["enrollment_id"]

    _seed_retained_event(
        session_factory,
        study_id=study["study_id"],
        enrollment_id=enrollment_id,
    )

    current_user["value"] = _owner(owner_id)
    stopped = client.post(
        f"/api/research/studies/{study['study_id']}/stop", json={"actor": "owner"}
    )
    assert stopped.status_code == 200, stopped.text

    coverage = client.get(
        "/api/research/operations/enrollments/coverage",
        params={"study_id": study["study_id"]},
    )
    assert coverage.status_code == 200, coverage.text
    coverage_body = coverage.json()
    assert coverage_body["total_enrollments"] == 1
    assert coverage_body["status_counts"] == {"STUDY_STOPPED": 1}
    assert coverage_body["coverage"] == "AVAILABLE"

    telemetry = client.get(
        "/api/research/operations/telemetry-coverage",
        params={"study_id": study["study_id"]},
    )
    assert telemetry.status_code == 200, telemetry.text
    telemetry_body = telemetry.json()
    assert telemetry_body["denominators"]["events"] == 1
    assert [family["family"] for family in telemetry_body["families"]] == ["tool"]

    # The study read still reports the retained enrollment.
    fetched = client.get(f"/api/research/studies/{study['study_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["study"]["enrollment_count"] == 1


