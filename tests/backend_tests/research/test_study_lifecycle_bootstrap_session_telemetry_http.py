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
    AgentConfigBinding,
    AgentReleaseV1,
    DistributionArtifact,
)
from research.telemetry.builder import EventBuilder, SequenceAllocator
from research.telemetry.normalization import (
    AdapterSpec,
    materialize_candidate,
    normalize_acp_observation,
    register_adapter,
    unregister_adapter,
)
from research.telemetry.normalization.generic_acp import GENERIC_ACP_NORMALIZER_VERSION

from ._byoa_contract import BYOA_CONFIG_BINDINGS

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


def _qualified_codex_byoa_release(session) -> str:
    """Insert a QUALIFIED BYOA codex release carrying an adapter identity.

    The PASS conformance receipt binds to the release's own
    ``source_manifest_digest`` (a BYOA release has no artifact digest) and to
    the adapter digest, which is exactly what ``derive_qualification_status``
    requires for the fixture to be bootstrap-selectable.
    """
    adapter_digest = "sha256:" + "d" * 64
    release = AgentReleaseV1(
        agent_id="codex",
        release_id=f"codex-byoa-{uuid.uuid4()}",
        version="1.2.3",
        source_manifest_digest="sha256:" + "2" * 64,
        distribution_mode=DistributionMode.BYOA_EXTERNAL,
        agent_package="codex",
        byoa_config=[
            AgentConfigBinding(**item) for item in BYOA_CONFIG_BINDINGS
        ],
        adapter=AdapterRef(
            adapter_id="acp-adapter",
            version="0.4.0",
            digest=adapter_digest,
            supported_release_ranges=[">=1.2.0,<1.3.0"],
        ),
        qualification_status=QualificationStatus.QUALIFIED,
    )
    release_json = release.model_dump(mode="json")
    release_json["qualification_status"] = QualificationStatus.UNQUALIFIED.value
    release_json["conformance"] = [
        {
            "status": "PASS",
            "artifact_digest": release.source_manifest_digest,
            "adapter_digest": adapter_digest,
            "host": {"os": "macos", "arch": "arm64"},
            "case_results": [
                {"case_id": "acp.initialize.session", "status": "PASS"}
            ],
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


def _partially_qualified_packaged_release(session) -> str:
    """Insert a packaged release whose PASS receipt covers only macOS.

    The release publishes macOS *and* Linux artifacts, but the receipt binds
    the macOS digest+platform only, so a Linux bootstrap must be refused even
    though the release-level status is QUALIFIED (ISSUE-10).
    """
    macos_digest = "sha256:" + "c" * 64
    linux_digest = "sha256:" + "e" * 64
    adapter_digest = "sha256:" + "d" * 64
    release = AgentReleaseV1(
        agent_id="codex-acp",
        release_id=f"codex-partial-{uuid.uuid4()}",
        version="1.2.3",
        source_manifest_digest=macos_digest,
        distribution_mode=DistributionMode.PACKAGED,
        artifacts=[
            DistributionArtifact(
                os="macos",
                arch="arm64",
                path="codex/1.2.3/macos-arm64.tar.gz",
                sha256=macos_digest,
                size=1048576,
            ),
            DistributionArtifact(
                os="linux",
                arch="x64",
                path="codex/1.2.3/linux-x64.tar.gz",
                sha256=linux_digest,
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
            "artifact_digest": macos_digest,
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


class _CodexRecordingAdapter:
    """Allowlisted test adapter: additive label, never rewrites generic fields."""

    adapter_version = "0.4.0"
    mapping_rule_version = "codex-recording-v1"
    supported_release_ranges = [">=1.2.0,<1.3.0"]

    def enrich(self, candidate):
        payload = dict(candidate.payload)
        payload["adapter_label"] = "codex"
        payload["session_id"] = "hijacked"
        return candidate.model_copy(update={"payload": payload})


_CODEX_ACP_OBSERVATION = {
    "jsonrpc": "2.0",
    "id": 11,
    "method": "session/prompt",
    "params": {"sessionId": "acp-session-1"},
}


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


def _telemetry_event(
    *,
    study_id: str,
    enrollment_id: str,
    research_session_id: str,
    event_id: str,
    payload: Optional[dict] = None,
) -> dict:
    event = {
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
    if payload is not None:
        event["payload"] = payload
    return event


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

    # The manifest is authenticated by the server before the plugin may trust it
    # (the signing secret never leaves the server).
    verified = client.post(
        "/api/research/bootstrap/verify",
        json={
            "manifest": manifest,
            "enrollment_id": enrollment_id,
            "context_id": "lifecycle-packaged-context",
        },
    )
    assert verified.status_code == 200, verified.text
    proof = verified.json()
    assert proof["verified"] is True
    assert proof["manifest_digest"] == manifest["manifest_digest"]
    assert proof["enrollment_id"] == enrollment_id
    assert proof["context_id"] == "lifecycle-packaged-context"

    # A tampered manifest fails the HMAC check.
    tampered_manifest = json.loads(json.dumps(manifest))
    tampered_manifest["agent_release"]["agent_id"] = "attacker-agent"
    tampered = client.post(
        "/api/research/bootstrap/verify",
        json={
            "manifest": tampered_manifest,
            "enrollment_id": enrollment_id,
            "context_id": "lifecycle-packaged-context",
        },
    )
    assert tampered.status_code == 409, tampered.text

    # A verification for another execution context is refused.
    wrong_context = client.post(
        "/api/research/bootstrap/verify",
        json={
            "manifest": manifest,
            "enrollment_id": enrollment_id,
            "context_id": "another-context",
        },
    )
    assert wrong_context.status_code == 409, wrong_context.text

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

    activity = client.post(
        "/api/research/sessions/activity",
        json={
            "capability": capability,
            "research_session_id": research_session_id,
        },
    )
    assert activity.status_code == 200, activity.text
    assert activity.json()["session"]["state"] == "running"
    activity_at = activity.json()["session"]["last_activity_at"]
    assert activity_at

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
    # Liveness must never manufacture qualifying activity (ISSUE-06).
    assert heartbeat.json()["session"]["last_heartbeat_at"]
    assert heartbeat.json()["session"]["last_activity_at"] == activity_at

    event_id = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    empty_batch = client.post(
        "/api/research/telemetry/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "session_capability": capability,
            "client_instance_id": "lifecycle-client",
            "events": [],
        },
    )
    assert empty_batch.status_code == 422, empty_batch.text
    assert empty_batch.json()["detail"]["code"] == "EMPTY_BATCH"
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

    tampered_receipt_attempt = dict(capability)
    tampered_receipt_attempt["signature"] = "0" * 64
    refused_receipt = client.post(
        "/api/research/telemetry/batches",
        json={
            "batch_id": batch_id,
            "session_capability": tampered_receipt_attempt,
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
    assert refused_receipt.status_code == 200, refused_receipt.text
    # Knowing a batch id is not enough: an invalid capability leaks no ACK.
    assert refused_receipt.json()["accepted"] == []
    assert refused_receipt.json()["duplicate"] == []
    assert refused_receipt.json()["rejected"] == []
    assert [entry["reason"] for entry in refused_receipt.json()["retryable"]] == [
        "CAPABILITY_INVALID"
    ]

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

    # Server-side privacy enforcement: content the study policy does not allow is
    # rejected, never silently stored or rewritten.
    content_event_id = str(uuid.uuid4())
    content_batch = client.post(
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
                    event_id=content_event_id,
                    payload={"prompt": "summarise the private source file"},
                )
            ],
        },
    )
    assert content_batch.status_code == 200, content_batch.text
    assert [entry["reason"] for entry in content_batch.json()["rejected"]] == ["PRIVACY_BLOCKED"]
    assert content_batch.json()["accepted"] == []
    session = session_factory()
    try:
        stored = session.execute(
            text("SELECT count(*) FROM public.research_event WHERE event_id = :event_id"),
            {"event_id": content_event_id},
        ).scalar_one()
    finally:
        session.close()
    assert stored == 0, "policy-blocked content must not be stored"

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


def test_http_codex_byoa_adapter_normalization_and_terminal_closure(http_runtime):
    """Codex BYOA release: adapter identity, generic normalization and closure.

    The release is participant-installed (``BYOA_EXTERNAL``) and carries an
    allowlisted adapter identity. The bootstrap manifest projects that identity
    (and no secret/revision field); a raw ACP observation normalizes through the
    generic path with an allowlisted adapter and with the generic fallback; the
    enriched canonical event persists with study/enrollment/session provenance;
    and terminal stop/revoke closes collection without deleting retained data.
    """
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "codex-byoa-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "codex-byoa-participant@example.com")
        release_id = _qualified_codex_byoa_release(session)
        profile_id, _connection_id = _release_profile(
            session, owner_id, release_id, framework_version="codex"
        )
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "Codex BYOA lifecycle study",
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
                "context_id": "lifecycle-codex-byoa-context",
                "environment": {"os": "macos", "arch": "arm64"},
            },
        )
    assert bootstrap.status_code == 201, bootstrap.text
    manifest = bootstrap.json()["manifest"]
    agent_release = manifest["agent_release"]
    assert agent_release["agent_id"] == "codex"
    assert agent_release["distribution_mode"] == "BYOA_EXTERNAL"
    assert agent_release["agent_package"] == "codex"
    assert agent_release["artifact_digest"] == ""
    assert agent_release["adapter_id"] == "acp-adapter"
    assert agent_release["adapter_version"] == "0.4.0"
    serialized = json.dumps(manifest)
    for forbidden in (
        "revision_id",
        "study_revision_id",
        "condition_id",
        "condition_exposure",
    ):
        assert forbidden not in serialized
    assert "TEST_KEY" not in serialized

    capability = manifest["session_capability"]
    research_session_id = manifest["research_session"]["research_session_id"]

    adapter_ref = AdapterRef(
        adapter_id=agent_release["adapter_id"],
        version=agent_release["adapter_version"],
        supported_release_ranges=[">=1.2.0,<1.3.0"],
    )
    register_adapter(
        AdapterSpec(
            adapter_id="acp-adapter",
            adapter_version="0.4.0",
            supported_release_ranges=(">=1.2.0,<1.3.0",),
        ),
        _CodexRecordingAdapter,
    )
    try:
        # No adapter -> the untouched generic result (no adapter provenance).
        generic = normalize_acp_observation(_CODEX_ACP_OBSERVATION)
        assert generic.adapter_version is None
        assert generic.candidates[0].adapter_version is None

        # Allowlisted adapter + in-range release -> additive enrichment only.
        enriched = normalize_acp_observation(
            _CODEX_ACP_OBSERVATION,
            adapter_ref=adapter_ref,
            release_version="1.2.3",
        )
        assert enriched.adapter_version == "0.4.0"
        candidate = enriched.candidates[0]
        assert candidate.payload["session_id"] == "acp-session-1"
        # Adapter-injected fields are re-filtered by the deny-by-default policy:
        # the vendor label is redacted rather than passing through untouched.
        assert candidate.payload["adapter_label"] == "[REDACTED]"
    finally:
        unregister_adapter("acp-adapter")

    opened = client.post(
        "/api/research/sessions/",
        json={
            "capability": capability,
            "enrollment_id": enrollment_id,
            "study_id": study_id,
            "manifest_digest": manifest["manifest_digest"],
            "context_id": "lifecycle-codex-byoa-context",
        },
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["session"]["research_session_id"] == research_session_id

    # The server opens the session in NOT_STARTED; an explicit qualifying
    # activity report is the documented transition to RUNNING (and makes the
    # later terminal close legal). Heartbeats are liveness only (ISSUE-06).
    activity = client.post(
        "/api/research/sessions/activity",
        json={
            "capability": capability,
            "research_session_id": research_session_id,
        },
    )
    assert activity.status_code == 200, activity.text
    assert activity.json()["session"]["state"] == "running"

    heartbeat = client.post(
        "/api/research/sessions/heartbeat",
        json={
            "capability": capability,
            "research_session_id": research_session_id,
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["session"]["last_heartbeat_at"]

    event = materialize_candidate(
        EventBuilder(SequenceAllocator()),
        enriched.candidates[0],
        enriched,
        emitter_id="codex-byoa-emitter",
        occurred_at=datetime.now(timezone.utc),
        study_id=uuid.UUID(study_id),
        enrollment_id=uuid.UUID(enrollment_id),
        research_session_id=uuid.UUID(research_session_id),
    )
    batch = client.post(
        "/api/research/telemetry/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "session_capability": capability,
            "client_instance_id": "codex-byoa-client",
            "events": [event.model_dump(mode="json")],
        },
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["accepted"][0]["event_id"] == str(event.event_id)

    session = session_factory()
    try:
        row = (
            session.execute(
                text(
                    "SELECT study_id, enrollment_id, research_session_id, envelope_json "
                    "FROM public.research_event WHERE event_id = :event_id"
                ),
                {"event_id": event.event_id},
            )
            .mappings()
            .one()
        )
    finally:
        session.close()
    assert str(row["study_id"]) == study_id
    assert str(row["enrollment_id"]) == enrollment_id
    assert str(row["research_session_id"]) == research_session_id
    envelope = row["envelope_json"]
    if isinstance(envelope, str):
        envelope = json.loads(envelope)
    assert envelope["provenance"]["adapter_version"] == "0.4.0"
    assert (
        envelope["provenance"]["normalizer_version"] == GENERIC_ACP_NORMALIZER_VERSION
    )
    assert envelope["payload"]["adapter_label"] == "[REDACTED]"
    assert envelope["payload"]["session_id"] == "acp-session-1"

    # Terminal stop + revoke: collection closes and no later event is stored,
    # while the already-persisted canonical event is retained.
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
        json={"actor": "codex-byoa-owner"},
    )
    assert revoked.status_code == 200, revoked.text

    current_user["value"] = _participant(participant_id)
    terminal = client.post(
        "/api/research/telemetry/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "session_capability": capability,
            "client_instance_id": "codex-byoa-client",
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


def test_http_bootstrap_refuses_an_unqualified_platform_artifact(http_runtime):
    """ISSUE-10: a release-qualified study still cannot bootstrap an artifact
    whose exact digest+platform+adapter was never covered by conformance."""
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "partial-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "partial-participant@example.com")
        release_id = _partially_qualified_packaged_release(session)
        profile_id, _connection_id = _release_profile(session, owner_id, release_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "Partially qualified packaged study",
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

    current_user["value"] = _participant(participant_id)
    joined = client.post(
        "/api/research/join",
        json={"join_code": study["join_code"], "accept_consent": True},
    )
    assert joined.status_code == 201, joined.text
    enrollment_id = joined.json()["enrollment_id"]

    signing_secret = BOOTSTRAP_SIGNING_SECRET
    assert signing_secret, "BOOTSTRAP_SIGNING_SECRET must be configured for this suite"
    with patch(
        "backend.routers.research.bootstrap._SIGNER",
        BootstrapSigningContext(secret=signing_secret),
    ):
        refused = client.post(
            "/api/research/bootstrap/research-sessions",
            json={
                "enrollment_id": enrollment_id,
                "context_id": "partial-context",
                "environment": {"os": "linux", "arch": "x64"},
            },
        )
    assert refused.status_code == 409, refused.text
    assert "ARTIFACT_NOT_QUALIFIED" in json.dumps(refused.json())
    assert "manifest" not in refused.json()

    session = session_factory()
    try:
        sessions = session.execute(
            text(
                "SELECT count(*) FROM public.research_session "
                "WHERE enrollment_id = :enrollment_id"
            ),
            {"enrollment_id": enrollment_id},
        ).scalar_one()
    finally:
        session.close()
    assert sessions == 0, "a refused bootstrap must not leave a session row"
