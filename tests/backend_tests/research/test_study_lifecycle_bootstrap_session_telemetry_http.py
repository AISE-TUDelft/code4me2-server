"""Real HTTP contracts for the post-enrollment bootstrap/session/telemetry path.

Setup uses only the black-box study endpoints (``POST /studies`` then
``POST /join``). The suite drives a packaged (macos/arm64) release through
bootstrap (revision-free manifest), session create/heartbeat/close (including
``created:false`` reuse), and telemetry (accepted, duplicate, retryable,
terminal) plus receipt fetch, all against PostgreSQL over real HTTP.
"""

from __future__ import annotations

import json
import os
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
from backend.routers.research.bootstrap import BOOTSTRAP_SIGNING_SECRET
from database.migration.migration_manager import MigrationManager
from main import app
from research.runtime.bootstrap.service import BootstrapSigningContext
from research.study.agents.enums import DistributionMode, QualificationStatus
from research.study.agents.models import (
    AdapterRef,
    AgentReleaseV1,
    DistributionArtifact,
)

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


def _qualified_packaged_release(session) -> str:
    """Insert a QUALIFIED packaged codex release for macos/arm64.

    Modeled on the BYOA helper: the row carries one macos/arm64 artifact with
    a sha256 digest plus a PASS conformance receipt bound to that exact
    artifact/adapter/host, which is what derives the QUALIFIED status the
    bootstrap composition requires.
    """
    artifact_digest = "sha256:" + "c" * 64
    adapter_digest = "sha256:" + "d" * 64
    release = AgentReleaseV1(
        agent_id="codex-acp",
        release_id=f"codex-packaged-{uuid.uuid4()}",
        version="1.2.3",
        source_manifest_digest=artifact_digest,
        distribution_mode=DistributionMode.PACKAGED,
        artifacts=[
            DistributionArtifact(
                os="macos",
                arch="arm64",
                path="codex/1.2.3/macos-arm64.tar.gz",
                sha256=artifact_digest,
                size=1048576,
            ),
        ],
        adapter=AdapterRef(
            adapter_id="acp-adapter",
            version="0.4.0",
            digest=adapter_digest,
        ),
        qualification_status=QualificationStatus.QUALIFIED,
    )
    release_json = release.model_dump(mode="json")
    release_json["qualification_status"] = QualificationStatus.UNQUALIFIED.value
    release_json["conformance"] = [
        {
            "status": "PASS",
            "artifact_digest": artifact_digest,
            "adapter_digest": adapter_digest,
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


def _admin(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=True,
        email="http-admin@example.com",
        name="HTTP Admin",
        can_research=True,
    )


def _telemetry_event(
    *, study_id: str, enrollment_id: str, research_session_id: str, event_id: str
) -> dict:
    return {
        "event_id": event_id,
        "schema_version": "1",
        "event_type": "tool.completed",
        "source": "ide",
        "study_id": study_id,
        "enrollment_id": enrollment_id,
        "research_session_id": research_session_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "emitter_id": "lifecycle-emitter",
        "emitter_sequence": 1,
        "provenance": {
            "source": "ide",
            "normalizer_version": "1",
            "fidelity": "normalized",
        },
    }


def test_http_packaged_bootstrap_session_telemetry_lifecycle(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "lifecycle-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "lifecycle-participant@example.com")
        release_id = _qualified_packaged_release(session)
        profile_id, _connection_id = _release_profile(session, owner_id, release_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "Packaged lifecycle study",
            "session_policy": {
                "idle_timeout_seconds": 600,
                "resume_grace_seconds": 120,
                "heartbeat_seconds": 30,
            },
            "profile_ids": [str(profile_id)],
        },
    )
    assert created.status_code == 201, created.text
    study = created.json()["study"]
    study_id = study["study_id"]

    current_user["value"] = _participant(participant_id)
    joined = client.post(
        "/api/research/join",
        json={"join_code": study["join_code"], "accept_consent": True},
    )
    assert joined.status_code == 201, joined.text
    enrollment_id = joined.json()["enrollment_id"]

    # One secret must sign (bootstrap `_SIGNER`) and verify (the module-level
    # `BOOTSTRAP_SIGNING_SECRET` the session/telemetry routers read). The suite
    # env provides it through pytest-env, so reusing that value keeps the
    # bootstrap-issued capability valid for the whole downstream flow.
    signing_secret = BOOTSTRAP_SIGNING_SECRET
    assert signing_secret, "BOOTSTRAP_SIGNING_SECRET must be configured for this suite"
    with patch(
        "backend.routers.research.bootstrap._SIGNER",
        BootstrapSigningContext(secret=signing_secret),
    ):
        bootstrap = client.post(
            "/api/research/bootstrap/research-sessions",
            json={
                "enrollment_id": enrollment_id,
                "context_id": "lifecycle-packaged-context",
                "environment": {"os": "macos", "arch": "arm64"},
            },
        )
    assert bootstrap.status_code == 201, bootstrap.text
    manifest = bootstrap.json()["manifest"]
    assert manifest["study_id"] == study_id
    assert manifest["enrollment_id"] == enrollment_id
    assert "revision_id" not in manifest
    assert "revision_id" not in json.dumps(manifest)
    assert manifest["assignment"]["agent_profile_id"] == str(profile_id)
    assert manifest["assignment"]["profile_digest"]
    assert manifest["agent_release"]["agent_id"] == "codex-acp"
    assert manifest["agent_release"]["distribution_mode"] == "PACKAGED"
    assert manifest["agent_release"]["artifact_digest"] == "sha256:" + "c" * 64
    assert "TEST_KEY" not in json.dumps(manifest)
    capability = manifest["session_capability"]
    assert capability["enrollment_id"] == enrollment_id
    assert capability["study_id"] == study_id
    assert capability["research_session_id"] == manifest["research_session"]["research_session_id"]
    assert set(capability["scope"]) >= {"telemetry:write", "session:heartbeat", "session:close"}
    research_session_id = manifest["research_session"]["research_session_id"]

    opened = client.post(
        "/api/research/sessions/",
        json={
            "capability": capability,
            "enrollment_id": enrollment_id,
            "study_id": study_id,
            "manifest_digest": manifest["manifest_digest"],
            "context_id": "lifecycle-packaged-context",
        },
    )
    assert opened.status_code == 200, opened.text
    # compose_bootstrap already created this context's session row, so the
    # first session call reuses it idempotently rather than creating a new one.
    assert opened.json()["created"] is False
    assert opened.json()["session"]["research_session_id"] == research_session_id
    assert opened.json()["session"]["state"] == "not_started"
    assert opened.json()["heartbeat_seconds"]

    reused = client.post(
        "/api/research/sessions/",
        json={
            "capability": capability,
            "enrollment_id": enrollment_id,
            "study_id": study_id,
            "manifest_digest": manifest["manifest_digest"],
            "context_id": "lifecycle-packaged-context",
        },
    )
    assert reused.status_code == 200, reused.text
    assert reused.json()["created"] is False
    assert reused.json()["session"]["research_session_id"] == research_session_id

    heartbeat = client.post(
        "/api/research/sessions/heartbeat",
        json={
            "capability": capability,
            "research_session_id": research_session_id,
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["session"]["research_session_id"] == research_session_id
    assert heartbeat.json()["heartbeat_seconds"]

    event_id = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    batch = client.post(
        "/api/research/telemetry/batches",
        json={
            "batch_id": batch_id,
            "session_capability": capability,
            "client_instance_id": "lifecycle-client",
            "events": [
                _telemetry_event(
                    study_id=study_id,
                    enrollment_id=enrollment_id,
                    research_session_id=research_session_id,
                    event_id=event_id,
                )
            ],
        },
    )
    assert batch.status_code == 200, batch.text
    assert len(batch.json()["accepted"]) == 1
    assert batch.json()["accepted"][0]["event_id"] == event_id
    assert batch.json()["accepted"][0]["disposition"] == "ACCEPTED"
    receipt_id = batch.json()["receipt_id"]

    duplicate = client.post(
        "/api/research/telemetry/batches",
        json={
            "batch_id": batch_id,
            "session_capability": capability,
            "client_instance_id": "lifecycle-client",
            "events": [
                _telemetry_event(
                    study_id=study_id,
                    enrollment_id=enrollment_id,
                    research_session_id=research_session_id,
                    event_id=event_id,
                )
            ],
        },
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["receipt_id"] == receipt_id

    tampered = dict(capability)
    tampered["signature"] = "0" * 64
    transient = client.post(
        "/api/research/telemetry/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "session_capability": tampered,
            "client_instance_id": "lifecycle-client",
            "events": [
                _telemetry_event(
                    study_id=study_id,
                    enrollment_id=enrollment_id,
                    research_session_id=research_session_id,
                    event_id=str(uuid.uuid4()),
                )
            ],
        },
    )
    assert transient.status_code == 200, transient.text
    assert len(transient.json()["retryable"]) == 1
    assert transient.json()["retryable"][0]["reason"] == "CAPABILITY_INVALID"

    current_user["value"] = _admin(owner_id)
    receipt = client.get(f"/api/research/telemetry/receipts/{receipt_id}")
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["receipt"]["receipt_id"] == receipt_id
    assert receipt.json()["enrollment_id"] == enrollment_id
    assert receipt.json()["research_session_id"] == research_session_id

    current_user["value"] = _participant(participant_id)
    closed = client.post(
        "/api/research/sessions/close",
        json={
            "capability": capability,
            "research_session_id": research_session_id,
            "reason": "explicit_completion",
        },
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["session"]["state"] == "ended"

    current_user["value"] = _owner(owner_id)
    revoked = client.post(
        f"/api/research/studies/{study_id}/enrollments/{enrollment_id}/revoke",
        json={"actor": "lifecycle-owner"},
    )
    assert revoked.status_code == 200, revoked.text

    current_user["value"] = _participant(participant_id)
    terminal = client.post(
        "/api/research/telemetry/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "session_capability": capability,
            "client_instance_id": "lifecycle-client",
            "events": [
                _telemetry_event(
                    study_id=study_id,
                    enrollment_id=enrollment_id,
                    research_session_id=research_session_id,
                    event_id=str(uuid.uuid4()),
                )
            ],
        },
    )
    assert terminal.status_code == 200, terminal.text
    assert len(terminal.json()["rejected"]) == 1
    assert terminal.json()["rejected"][0]["reason"] == "REVOKED"
