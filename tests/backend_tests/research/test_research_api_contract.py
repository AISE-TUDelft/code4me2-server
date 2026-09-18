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
from research.study.agents.models import AgentReleaseV1

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
    artifact_digest = "sha256:" + "a" * 64
    adapter_digest = "sha256:" + "b" * 64
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
                "artifacts": [],
                "adapter": {
                    "adapter_id": "http-adapter",
                    "version": "1.0.0",
                    "digest": adapter_digest,
                },
                "conformance": [
                    {
                        "status": "PASS",
                        "artifact_digest": artifact_digest,
                        "adapter_digest": adapter_digest,
                        "case_results": [{"status": "PASS"}],
                    }
                ],
            }),
        },
    )
    session.execute(
        text(
            "INSERT INTO public.agent_profile "
            "(profile_id, owner_user_id, name, model, release_id, tools_json, approval_policy, max_steps) "
            "VALUES (:profile_id, :owner_id, 'http-profile', 'model', :release_id, '[]', 'auto', 1)"
        ),
        {"profile_id": profile_id, "owner_id": owner_id, "release_id": release_id},
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
            "(profile_id, owner_user_id, name, model, release_id, connection_id, tools_json, approval_policy, max_steps) "
            "VALUES (:profile_id, :owner_id, 'http-locked', 'model', :release_id, :connection_id, '[]', 'auto', 1)"
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
        qualification_status=QualificationStatus.QUALIFIED,
    )
    release_json = release.model_dump(mode="json")
    release_json["qualification_status"] = QualificationStatus.UNQUALIFIED.value
    release_json["conformance"] = [
        {
            "status": "PASS",
            "artifact_digest": release.source_manifest_digest,
            "host": {"os": "macos", "arch": "arm64"},
            "case_results": [{"status": "PASS"}],
        }
    ]
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


def _release_profile(session, owner_id: uuid.UUID, release_id: str) -> tuple[uuid.UUID, uuid.UUID]:
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
            "(profile_id, owner_user_id, name, model, release_id, connection_id, tools_json, approval_policy, max_steps) "
            "VALUES (:profile_id, :owner_id, 'Codex', 'model', :release_id, :connection_id, '[]', 'auto', 1)"
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


def test_http_lifecycle_join_stop_and_clone_contract(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "http-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "http-participant@example.com")
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

    current_user["value"] = _participant(participant_id)
    stopped_join = client.get(f"/api/research/join/{join_code}")
    assert stopped_join.status_code == 409
    assert stopped_join.json()["detail"]["code"] == "STUDY_STOPPED"

    current_user["value"] = _owner(owner_id)
    cloned = client.post(f"/api/research/studies/{study_id}/clone")
    assert cloned.status_code == 201, cloned.text
    clone = cloned.json()["study"]
    assert clone["study_id"] != study_id
    assert clone["research_status"] == "DRAFT"
    assert clone["join_code"] != join_code

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
        assert session.execute(
            text("SELECT count(*) FROM public.study_agent_profile WHERE study_id = :id"),
            {"id": clone["study_id"]},
        ).scalar_one() == 0
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
        json={"name": "Revoke study", "profile_ids": [str(profile_id)]},
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

    current_user["value"] = _owner(owner_id)
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
        json={"name": "HTTP lock study", "profile_ids": [str(profile_id)]},
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
        profile_id, _connection_id = _release_profile(session, owner_id, release_id)
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

