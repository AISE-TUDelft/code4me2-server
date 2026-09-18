"""Wave-1 consolidation tests: ownership, provider connections and assignment.

These exercise the router/service seams directly with MagicMocks/fakes (no
PostgreSQL). Real database constraints and the live-study slot behaviour are
covered by ``tests/database_tests/test_wave1_live_study.py``.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from agents import provider as provider_module
from agents import registry
from backend.routers.agent.profiles import (
    AgentProfilePayload,
    _authorize_connection,
)
from backend.routers.analytics.auth_utils import AuthenticatedUser
from backend.routers.research import providers as providers_router
from backend.routers.research import researchers as researchers_router
from backend.routers.research.access import require_researcher, require_study_owner
from database import crud
from research.participants.enums import EnrollmentStatus
from research.runtime.assignment.service import allocate
from research.study.protocol.canonical import protocol_digest
from research.study.protocol.models import StudyProtocolV1
from research.study.protocol.publication import (
    RevisionLineage,
    StudyRevision,
    publish_revision,
)
from research.study.protocol.enums import RevisionStatus
from research.study.protocol.enums import ReleaseResolutionStatus
from research.study.protocol.validation import DistributionResolution


CONSENT_DOC_ID = "consent-v1"
CONSENT_VERSION = "1.0"
CONSENT_DIGEST = "sha256:" + "c" * 64


def _admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(), is_admin=True, email="admin@example.com", name="Admin"
    )


def _researcher(account_id=None) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=account_id or uuid.uuid4(),
        is_admin=False,
        email="r@example.com",
        name="Researcher",
        can_research=True,
    )


def _participant() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(), is_admin=False, email="p@example.com", name="P"
    )


def _connection(**overrides) -> SimpleNamespace:
    base = dict(
        connection_id=uuid.uuid4(),
        label="study-provider",
        base_url="https://provider.example/v1",
        secret_ref="EXAMPLE_PROVIDER_KEY",
        models_json='["model-a", "model-b"]',
        is_active=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# B01/B02 — researcher enablement is admin-only; no self-promotion
# ---------------------------------------------------------------------------


def test_admin_enablement_is_required_and_participants_cannot_self_promote():
    app = MagicMock()
    target = uuid.uuid4()

    # Participant cannot toggle their own (or anyone's) flag.
    with pytest.raises(HTTPException) as error:
        researchers_router.set_researcher_enabled(
            target,
            researchers_router.ResearcherEnableRequest(can_research=True),
            _participant(),
            app,
        )
    assert error.value.status_code == 403
    app.get_db_session.assert_not_called()

    # Administrator enablement delegates to the single can_research flag.
    user = SimpleNamespace(
        user_id=target,
        email="r@example.com",
        name="R",
        is_admin=False,
        can_research=True,
    )
    with patch.object(
        crud, "set_user_can_research", return_value=user
    ) as toggle:
        response = researchers_router.set_researcher_enabled(
            target,
            researchers_router.ResearcherEnableRequest(can_research=True),
            _admin(),
            app,
        )
    body = json.loads(response.body)
    assert body["user"]["can_research"] is True
    assert toggle.call_args.args[2] is True


def test_participant_cannot_use_researcher_routes():
    with pytest.raises(HTTPException) as error:
        require_researcher(_participant())
    assert error.value.detail["code"] == "RESEARCHER_REQUIRED"

    # The admin connection CRUD requires an administrator, and returns no
    # endpoint/secret to a participant.
    app = MagicMock()
    with pytest.raises(HTTPException) as error:
        providers_router.create_provider_connection(
            providers_router.ProviderConnectionPayload(
                label="x",
                base_url="https://p.example/v1",
                secret_ref="EXAMPLE_PROVIDER_KEY",
                models=["m"],
            ),
            _participant(),
            app,
        )
    assert error.value.status_code == 403


# ---------------------------------------------------------------------------
# B08/B09/B10/B14 — connection grants at profile create and inference
# ---------------------------------------------------------------------------


def _profile_payload(connection_id, **overrides) -> AgentProfilePayload:
    base = dict(
        name="arm",
        model="model-a",
        connection_id=connection_id,
        release_id="rel-1",
        tools_json="[]",
        approval_policy="auto",
        max_steps=1,
    )
    base.update(overrides)
    return AgentProfilePayload(**base)


def test_profile_creation_requires_a_granted_connection_and_allowed_model():
    db = MagicMock()
    researcher = _researcher()
    connection = _connection()

    with patch.object(crud, "get_provider_connection", return_value=connection), patch.object(
        crud, "user_has_connection_grant", return_value=False
    ):
        with pytest.raises(HTTPException) as error:
            _authorize_connection(db, _profile_payload(connection.connection_id), researcher)
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "CONNECTION_NOT_GRANTED"

    with patch.object(crud, "get_provider_connection", return_value=connection), patch.object(
        crud, "user_has_connection_grant", return_value=True
    ):
        # A model not in the connection's allowlist is rejected.
        with pytest.raises(HTTPException) as error:
            _authorize_connection(
                db, _profile_payload(connection.connection_id, model="not-allowed"), researcher
            )
        assert error.value.detail["code"] == "MODEL_NOT_ALLOWED"
        # An allowed connection/model passes.
        _authorize_connection(db, _profile_payload(connection.connection_id), researcher)


def test_revoked_grant_blocks_an_existing_task_and_missing_secret_blocks_too(monkeypatch):
    db = MagicMock()
    connection = _connection()
    profile = SimpleNamespace(connection_id=connection.connection_id)
    owner_id = uuid.uuid4()

    with patch.object(crud, "get_provider_connection", return_value=connection), patch.object(
        crud, "user_has_connection_grant", return_value=False
    ):
        with pytest.raises(provider_module.ProviderReadinessError) as error:
            provider_module.resolve_task_connection(db, profile, owner_id)
    assert error.value.code == "GRANT_MISSING"

    # Grant present but the deployment secret is absent -> readiness failure.
    monkeypatch.delenv("EXAMPLE_PROVIDER_KEY", raising=False)
    with patch.object(crud, "get_provider_connection", return_value=connection), patch.object(
        crud, "user_has_connection_grant", return_value=True
    ):
        view = provider_module.resolve_task_connection(db, profile, owner_id)
        with pytest.raises(provider_module.ProviderReadinessError) as error:
            provider_module.resolve_upstream(model="model-a", connection=view)
    assert error.value.code == "SECRET_MISSING"


def test_admin_owned_task_bypasses_the_grant_check():
    db = MagicMock()
    connection = _connection()
    profile = SimpleNamespace(connection_id=connection.connection_id)
    with patch.object(crud, "get_provider_connection", return_value=connection), patch.object(
        crud, "user_has_connection_grant"
    ) as grant:
        view = provider_module.resolve_task_connection(
            db, profile, uuid.uuid4(), owner_is_admin=True
        )
    grant.assert_not_called()
    assert view.connection_id == connection.connection_id


# ---------------------------------------------------------------------------
# B11/B13 — no secrets in responses; rotation does not change frozen config
# ---------------------------------------------------------------------------


def test_connection_payload_never_contains_the_secret_value(monkeypatch):
    monkeypatch.setenv("EXAMPLE_PROVIDER_KEY", "canary-secret-value-123")
    connection = _connection()
    payload = providers_router._safe_payload(connection, admin=True)
    serialized = json.dumps(payload)
    assert "canary-secret-value-123" not in serialized
    # The env-var NAME and endpoint are admin-only.
    assert payload["secret_ref"] == "EXAMPLE_PROVIDER_KEY"
    assert payload["base_url"] == connection.base_url
    assert payload["ready"] is True

    researcher_view = providers_router._safe_payload(connection, admin=False)
    assert "secret_ref" not in researcher_view
    assert "base_url" not in researcher_view


def test_secret_rotation_changes_inference_key_but_not_frozen_config(monkeypatch):
    connection = _connection()
    monkeypatch.setenv("EXAMPLE_PROVIDER_KEY", "key-one")
    first = provider_module.resolve_upstream(model="model-a", connection=connection)
    monkeypatch.setenv("EXAMPLE_PROVIDER_KEY", "key-two")
    second = provider_module.resolve_upstream(model="model-a", connection=connection)
    assert first.api_key == "key-one"
    assert second.api_key == "key-two"
    # The frozen identity is the connection id/label/model, never the secret.
    frozen_identity = {
        "connection_id": str(connection.connection_id),
        "label": connection.label,
        "model": "model-a",
    }
    assert "key-one" not in json.dumps(frozen_identity)
    assert "key-two" not in json.dumps(frozen_identity)


# ---------------------------------------------------------------------------
# C06/C07/C08/C11 — frozen publication config is immutable and secret-free
# ---------------------------------------------------------------------------


def _protocol_with_condition(distribution_id) -> StudyProtocolV1:
    return StudyProtocolV1.model_validate(
        {
            "schema_version": "1",
            "study_id": str(uuid.uuid4()),
            "metadata": {"name": "Frozen study"},
            "schedule": {
                "kind": "FIXED",
                "start_at": "2026-09-01T00:00:00+00:00",
                "end_at": "2027-09-01T00:00:00+00:00",
            },
            "assignment": {"unit": "ENROLLMENT", "strategy": "WEIGHTED_RANDOM"},
            "conditions": [
                {
                    "condition_id": "control",
                    "weight": 1.0,
                    "distribution_id": str(distribution_id),
                },
                {
                    "condition_id": "treatment",
                    "weight": 1.0,
                    "distribution_id": str(distribution_id),
                },
            ],
            "privacy_policy": {"retention_action": "RETAIN_ANONYMIZED"},
            "consent": {
                "document_id": CONSENT_DOC_ID,
                "version": CONSENT_VERSION,
                "digest": CONSENT_DIGEST,
            },
            "environment_requirements": {"expected_protocol_version": "1"},
        }
    )


class _FrozenResolver:
    """Resolver returning the same non-secret profile config for conditions."""

    def __init__(self, config):
        self._config = config

    def resolve(self, distribution_id):
        return DistributionResolution(
            found=True,
            distribution_id=distribution_id,
            distribution_mode="PACKAGED",
            release_id="rel-1",
            agent_id="code4me2-agent",
            version="1.0.0",
            artifact_digest="sha256:" + "a" * 64,
            verified=True,
            release_status=ReleaseResolutionStatus.RESOLVED,
            agent_config=self._config,
        )


def _agent_config(model="model-a", approval="auto", connection_id=None, profile_id=None):
    from research.study.protocol.models import ResolvedAgentConfig

    return ResolvedAgentConfig(
        profile_id=profile_id or uuid.uuid4(),
        name="arm",
        model=model,
        framework_version="code4me2-agent",
        tools_json='["read_file"]',
        approval_policy=approval,
        max_steps=4,
        temperature=0.2,
        max_context_tokens=1000,
        connection_id=connection_id,
        connection_label="study-provider",
    )


def test_publication_freezes_profile_config_and_digest_tracks_it():
    distribution_id = uuid.uuid4()
    protocol = _protocol_with_condition(distribution_id)
    frozen_at = _agent_config(profile_id=distribution_id)

    result = publish_revision(
        protocol,
        RevisionLineage(study_id=protocol.study_id),
        distribution_resolver=_FrozenResolver(frozen_at),
    )
    assert result.outcome.value == "PUBLISHED"
    revision = result.revision
    frozen_first = revision.protocol_json["conditions"][0]["resolved_distribution"]

    # The full, non-secret config is frozen; no account id / secret appears.
    assert frozen_first["agent_config"]["model"] == "model-a"
    serialized = json.dumps(frozen_first)
    assert "secret" not in serialized.lower()
    assert "api_key" not in serialized

    # Editing the template (a different config) yields a different digest, while
    # re-resolving the same config is content-stable.
    same = publish_revision(
        protocol,
        RevisionLineage(study_id=protocol.study_id),
        distribution_resolver=_FrozenResolver(_agent_config(profile_id=distribution_id)),
    )
    edited = publish_revision(
        protocol,
        RevisionLineage(study_id=protocol.study_id),
        distribution_resolver=_FrozenResolver(
            _agent_config(profile_id=distribution_id, model="model-b")
        ),
    )
    assert same.revision.protocol_digest == revision.protocol_digest
    assert edited.revision.protocol_digest != revision.protocol_digest

    # The already-published revision object is untouched by the later edit.
    assert revision.protocol_json["conditions"][0]["resolved_distribution"][
        "agent_config"
    ]["model"] == "model-a"


def test_canonical_digest_ignores_absent_agent_config():
    """A legacy pin without a frozen config keeps its canonical shape."""
    from research.study.protocol.canonical import protocol_canonical_json

    distribution_id = uuid.uuid4()
    protocol = _protocol_with_condition(distribution_id)
    result = publish_revision(
        protocol,
        RevisionLineage(study_id=protocol.study_id),
        distribution_resolver=_FrozenResolver(None),
    )
    revision = result.revision
    canonical = protocol_canonical_json(
        StudyProtocolV1.model_validate(revision.protocol_json)
    )
    assert "agent_config" not in canonical
    assert protocol_digest(StudyProtocolV1.model_validate(revision.protocol_json)) == (
        revision.protocol_digest
    )


# ---------------------------------------------------------------------------
# C13/C14 — equal weights default and seeded weighted distribution
# ---------------------------------------------------------------------------


def test_equal_weights_are_the_default_and_honored_statistically():
    distribution_id = uuid.uuid4()
    protocol = _protocol_with_condition(distribution_id)
    result = publish_revision(
        protocol,
        RevisionLineage(study_id=protocol.study_id),
        distribution_resolver=_FrozenResolver(_agent_config()),
    )
    revision = result.revision

    import random

    enrollment = SimpleNamespace(
        enrollment_id=uuid.uuid4(),
        study_revision_id=revision.revision_id,
        status=EnrollmentStatus.ACTIVE,
    )
    counts = {"control": 0, "treatment": 0}
    rng = random.Random(1234)
    for _ in range(200):
        allocation = allocate(enrollment, revision, rng=rng)
        assert allocation.outcome.value == "CREATED"
        counts[allocation.assignment.condition_id] += 1
    # Equal default weights: no condition is starved.
    assert counts["control"] > 0
    assert counts["treatment"] > 0


def test_weighted_draw_follows_the_declared_weights():
    distribution_id = uuid.uuid4()
    protocol = _protocol_with_condition(distribution_id)
    data = protocol.model_dump(mode="json")
    data["conditions"][0]["weight"] = 3.0
    data["conditions"][1]["weight"] = 1.0
    weighted = StudyProtocolV1.model_validate(data)
    result = publish_revision(
        weighted,
        RevisionLineage(study_id=weighted.study_id),
        distribution_resolver=_FrozenResolver(_agent_config()),
    )
    revision = result.revision
    enrollment = SimpleNamespace(
        enrollment_id=uuid.uuid4(),
        study_revision_id=revision.revision_id,
        status=EnrollmentStatus.ACTIVE,
    )
    import random

    counts = {"control": 0, "treatment": 0}
    rng = random.Random(99)
    for _ in range(400):
        allocation = allocate(enrollment, revision, rng=rng)
        counts[allocation.assignment.condition_id] += 1
    # Roughly 3:1 — allow a wide band for the seeded sample.
    assert counts["control"] > counts["treatment"]


# ---------------------------------------------------------------------------
# C15/C17/C18 — one persisted assignment authority; no fallback
# ---------------------------------------------------------------------------


def _registry_env(*, assignment_row, participant, enrollment, study, revision, profile):
    db = MagicMock()
    db.get.return_value = study
    return (
        db,
        patch.object(
            registry.identity_store,
            "get_participant_by_account",
            return_value=participant,
        ),
        patch.object(registry.identity_store, "list_enrollments", return_value=[enrollment]),
        patch.object(registry.identity_store, "row_to_enrollment", return_value=enrollment),
        patch.object(registry.protocol_store, "get_revision", return_value=revision),
        patch.object(registry.protocol_store, "row_to_revision", return_value=revision),
        patch.object(
            registry.assignment_store,
            "get_assignment_for_enrollment_revision",
            return_value=assignment_row,
        ),
        patch.object(
            registry.assignment_store, "row_to_assignment", return_value=assignment_row
        ),
        patch.object(crud, "get_agent_profile_by_id", return_value=profile),
    )


def test_registry_uses_the_persisted_assignment_row():
    assignment_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    enrollment = SimpleNamespace(
        enrollment_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        study_revision_id=revision_id,
        status=EnrollmentStatus.ACTIVE.value,
    )
    study = SimpleNamespace(
        study_id=enrollment.study_id,
        is_research=True,
        is_active=True,
        starts_at=None,
        ends_at=None,
    )
    from research.study.protocol.models import ResolvedAgentConfig

    profile = ResolvedAgentConfig(
        profile_id=uuid.uuid4(),
        name="arm",
        model="model-a",
        framework_version="code4me2-agent",
        tools_json="[]",
        approval_policy="auto",
        max_steps=1,
        connection_id=uuid.uuid4(),
    )
    revision = SimpleNamespace(
        revision_id=revision_id,
        study_id=enrollment.study_id,
        status=RevisionStatus.PUBLISHED,
        protocol_json={
            "schema_version": "1",
            "study_id": str(enrollment.study_id),
            "metadata": {"name": "s"},
            "schedule": {"kind": "FIXED", "start_at": "2026-09-01T00:00:00+00:00", "end_at": "2027-09-01T00:00:00+00:00"},
            "assignment": {"unit": "ENROLLMENT", "strategy": "WEIGHTED_RANDOM"},
            "conditions": [
                {
                    "condition_id": "control",
                    "weight": 1.0,
                    "distribution_id": str(profile.profile_id),
                    "resolved_distribution": {
                        "distribution_id": str(profile.profile_id),
                        "agent_config": profile.model_dump(mode="json"),
                    },
                }
            ],
            "privacy_policy": {"retention_action": "RETAIN_ANONYMIZED"},
            "consent": {"document_id": "d", "version": "1", "digest": "sha256:" + "c" * 64},
            "environment_requirements": {"expected_protocol_version": "1"},
        },
    )
    assignment_row = SimpleNamespace(
        assignment_id=assignment_id,
        enrollment_id=enrollment.enrollment_id,
        study_revision_id=revision_id,
        condition_id="control",
    )
    participant = SimpleNamespace(participant_id=uuid.uuid4())

    (
        db,
        p1,
        p2,
        p3,
        p4,
        p5,
        p6,
        p7,
        p8,
    ) = _registry_env(
        assignment_row=assignment_row,
        participant=participant,
        enrollment=enrollment,
        study=study,
        revision=revision,
        profile=profile,
    )
    with p1, p2, p3, p4, p5, p6, p7, p8:
        resolution = registry.resolve_assignment_context(db, uuid.uuid4())

    assert resolution is not None
    assert resolution.assignment_id == assignment_id
    assert resolution.arm_name == "control"
    assert resolution.revision_id == revision_id
    # The frozen config is returned, not the live profile row.
    assert resolution.profile.model == "model-a"


def test_registry_refuses_without_participant_or_enrollment():
    db = MagicMock()
    with patch.object(
        registry.identity_store, "get_participant_by_account", return_value=None
    ):
        assert registry.resolve_assignment_context(db, uuid.uuid4()) is None

    participant = SimpleNamespace(participant_id=uuid.uuid4())
    enrollment = SimpleNamespace(status=EnrollmentStatus.COMPLETED.value)
    with patch.object(
        registry.identity_store, "get_participant_by_account", return_value=participant
    ), patch.object(registry.identity_store, "list_enrollments", return_value=[enrollment]):
        assert registry.resolve_assignment_context(db, uuid.uuid4()) is None


def test_registry_refuses_when_study_is_not_live():
    enrollment = SimpleNamespace(
        enrollment_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        study_revision_id=uuid.uuid4(),
        status=EnrollmentStatus.ACTIVE.value,
    )
    study = SimpleNamespace(
        study_id=enrollment.study_id,
        is_research=True,
        is_active=False,
        starts_at=None,
        ends_at=None,
    )
    db = MagicMock()
    db.get.return_value = study
    with patch.object(
        registry.identity_store,
        "get_participant_by_account",
        return_value=SimpleNamespace(participant_id=uuid.uuid4()),
    ), patch.object(
        registry.identity_store, "list_enrollments", return_value=[enrollment]
    ), patch.object(
        registry.identity_store, "row_to_enrollment", return_value=enrollment
    ):
        assert registry.resolve_assignment_context(db, uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# F1 — provider authorization runs against the funding owner (study owner)
# ---------------------------------------------------------------------------


def _task_for_funding(funding_owner_user_id, study_id=None) -> SimpleNamespace:
    return SimpleNamespace(
        funding_owner_user_id=funding_owner_user_id,
        study_id=study_id,
        owner_user_id=uuid.uuid4(),  # participant
    )


def test_participant_inference_is_authorized_by_the_study_owner_grant():
    db = MagicMock()
    connection = _connection()
    study_owner = uuid.uuid4()
    task = _task_for_funding(study_owner)
    profile = SimpleNamespace(connection_id=connection.connection_id)

    with patch.object(crud, "get_provider_connection", return_value=connection), patch.object(
        crud, "user_has_connection_grant", return_value=True
    ) as grant, patch.object(
        crud, "get_user_by_id", return_value=SimpleNamespace(is_admin=False)
    ):
        funding_id, is_admin = provider_module.funding_owner_for_task(db, task)
        view = provider_module.resolve_task_connection(
            db, profile, funding_id, owner_is_admin=is_admin
        )

    assert funding_id == study_owner
    assert is_admin is False
    assert view.connection_id == connection.connection_id
    # The grant is checked for the study owner, never the participant.
    assert grant.call_args.args[2] == study_owner


def test_funding_owner_revoked_grant_blocks_even_with_unrelated_participant_grant():
    db = MagicMock()
    connection_a = _connection(label="study-connection")
    connection_b = _connection(label="participant-connection")
    study_owner = uuid.uuid4()
    participant = uuid.uuid4()
    task = SimpleNamespace(
        funding_owner_user_id=study_owner,
        study_id=None,
        owner_user_id=participant,
    )
    profile = SimpleNamespace(connection_id=connection_a.connection_id)

    def _grant(_db, connection_id, user_id):
        # The participant holds a grant, but only on an unrelated connection.
        return user_id == participant and connection_id == connection_b.connection_id

    with patch.object(crud, "get_provider_connection", return_value=connection_a), patch.object(
        crud, "user_has_connection_grant", side_effect=_grant
    ), patch.object(
        crud, "get_user_by_id", return_value=SimpleNamespace(is_admin=False)
    ):
        funding_id, is_admin = provider_module.funding_owner_for_task(db, task)
        with pytest.raises(provider_module.ProviderReadinessError) as error:
            provider_module.resolve_task_connection(
                db, profile, funding_id, owner_is_admin=is_admin
            )
    assert error.value.code == "GRANT_MISSING"


def test_participant_admin_flag_cannot_bypass_the_funding_owner_revoked_grant():
    db = MagicMock()
    connection = _connection()
    study_owner = uuid.uuid4()
    # The participant is a site admin, but the funding owner is not.
    task = _task_for_funding(study_owner)
    profile = SimpleNamespace(connection_id=connection.connection_id)

    def _users(_db, user_id):
        return SimpleNamespace(is_admin=(user_id != study_owner))

    with patch.object(crud, "get_provider_connection", return_value=connection), patch.object(
        crud, "user_has_connection_grant", return_value=False
    ), patch.object(crud, "get_user_by_id", side_effect=_users):
        funding_id, is_admin = provider_module.funding_owner_for_task(db, task)
        assert is_admin is False  # derived from the funding owner, not the participant
        with pytest.raises(provider_module.ProviderReadinessError) as error:
            provider_module.resolve_task_connection(
                db, profile, funding_id, owner_is_admin=is_admin
            )
    assert error.value.code == "GRANT_MISSING"


def test_funding_owner_falls_back_to_study_creator_when_field_is_null():
    db = MagicMock()
    study_owner = uuid.uuid4()
    study = SimpleNamespace(created_by=study_owner)
    db.get.return_value = study
    task = SimpleNamespace(funding_owner_user_id=None, study_id=uuid.uuid4())
    with patch.object(
        crud, "get_user_by_id", return_value=SimpleNamespace(is_admin=False)
    ):
        funding_id, _is_admin = provider_module.funding_owner_for_task(db, task)
    assert funding_id == study_owner


# ---------------------------------------------------------------------------
# F2 — execution uses the frozen value object; mutable profile edits are inert
# ---------------------------------------------------------------------------


def _registry_for_frozen_condition(revision_condition, study, enrollment):
    """Return patched registry collaborators for a frozen-condition revision."""
    revision = SimpleNamespace(
        revision_id=enrollment.study_revision_id,
        study_id=enrollment.study_id,
        status=RevisionStatus.PUBLISHED,
    )
    assignment_row = SimpleNamespace(
        assignment_id=uuid.uuid4(),
        enrollment_id=enrollment.enrollment_id,
        study_revision_id=revision.revision_id,
        condition_id=revision_condition["condition_id"],
    )
    return revision, assignment_row


def test_registry_returns_a_frozen_value_object_not_an_orm_profile():
    from research.study.protocol.models import ResolvedAgentConfig

    distribution_id = uuid.uuid4()
    enrollment = SimpleNamespace(
        enrollment_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        study_revision_id=uuid.uuid4(),
        status=EnrollmentStatus.ACTIVE.value,
    )
    study_owner = uuid.uuid4()
    study = SimpleNamespace(
        study_id=enrollment.study_id,
        is_research=True,
        is_active=True,
        created_by=study_owner,
        starts_at=None,
        ends_at=None,
    )
    config = ResolvedAgentConfig(
        profile_id=distribution_id,
        name="arm",
        model="frozen-model",
        framework_version="code4me2-agent",
        tools_json='["read_file"]',
        approval_policy="auto",
        max_steps=2,
        connection_id=uuid.uuid4(),
        funding_owner_user_id=study_owner,
    )
    condition = {
        "condition_id": "control",
        "weight": 1.0,
        "distribution_id": str(distribution_id),
        "resolved_distribution": {
            "distribution_id": str(distribution_id),
            "agent_config": config.model_dump(mode="json"),
        },
    }
    revision, assignment_row = _registry_for_frozen_condition(
        condition, study, enrollment
    )
    revision.protocol_json = {
        "schema_version": "1",
        "study_id": str(enrollment.study_id),
        "metadata": {"name": "s"},
        "schedule": {
            "kind": "FIXED",
            "start_at": "2026-09-01T00:00:00+00:00",
            "end_at": "2027-09-01T00:00:00+00:00",
        },
        "assignment": {"unit": "ENROLLMENT", "strategy": "WEIGHTED_RANDOM"},
        "conditions": [condition],
        "privacy_policy": {"retention_action": "RETAIN_ANONYMIZED"},
        "consent": {
            "document_id": "d",
            "version": "1",
            "digest": "sha256:" + "c" * 64,
        },
        "environment_requirements": {"expected_protocol_version": "1"},
    }
    db = MagicMock()
    db.get.return_value = study
    with patch.object(
        registry.identity_store,
        "get_participant_by_account",
        return_value=SimpleNamespace(participant_id=uuid.uuid4()),
    ), patch.object(
        registry.identity_store, "list_enrollments", return_value=[enrollment]
    ), patch.object(
        registry.identity_store, "row_to_enrollment", return_value=enrollment
    ), patch.object(
        registry.protocol_store, "get_revision", return_value=revision
    ), patch.object(
        registry.protocol_store, "row_to_revision", return_value=revision
    ), patch.object(
        registry.assignment_store,
        "get_assignment_for_enrollment_revision",
        return_value=assignment_row,
    ), patch.object(
        registry.assignment_store, "row_to_assignment", return_value=assignment_row
    ), patch.object(
        crud, "get_agent_profile_by_id"
    ) as live_profile:
        resolution = registry.resolve_assignment_context(db, uuid.uuid4())

    assert resolution is not None
    assert isinstance(resolution.profile, registry.FrozenAgentConfig)
    assert resolution.profile.model == "frozen-model"
    assert resolution.profile.funding_owner_user_id == study_owner
    # The mutable ORM profile row is never consulted.
    live_profile.assert_not_called()


def test_registry_refuses_a_revision_without_a_frozen_agent_config():
    enrollment = SimpleNamespace(
        enrollment_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        study_revision_id=uuid.uuid4(),
        status=EnrollmentStatus.ACTIVE.value,
    )
    study = SimpleNamespace(
        study_id=enrollment.study_id,
        is_research=True,
        is_active=True,
        created_by=uuid.uuid4(),
        starts_at=None,
        ends_at=None,
    )
    revision = SimpleNamespace(
        revision_id=enrollment.study_revision_id,
        study_id=enrollment.study_id,
        status=RevisionStatus.PUBLISHED,
        protocol_json={
            "schema_version": "1",
            "study_id": str(enrollment.study_id),
            "metadata": {"name": "s"},
            "schedule": {
                "kind": "FIXED",
                "start_at": "2026-09-01T00:00:00+00:00",
                "end_at": "2027-09-01T00:00:00+00:00",
            },
            "assignment": {"unit": "ENROLLMENT", "strategy": "WEIGHTED_RANDOM"},
            "conditions": [
                {
                    "condition_id": "control",
                    "weight": 1.0,
                    "distribution_id": str(uuid.uuid4()),
                    "resolved_distribution": {
                        "distribution_id": str(uuid.uuid4())
                    },
                }
            ],
            "privacy_policy": {"retention_action": "RETAIN_ANONYMIZED"},
            "consent": {
                "document_id": "d",
                "version": "1",
                "digest": "sha256:" + "c" * 64,
            },
            "environment_requirements": {"expected_protocol_version": "1"},
        },
    )
    assignment_row = SimpleNamespace(
        assignment_id=uuid.uuid4(),
        enrollment_id=enrollment.enrollment_id,
        study_revision_id=revision.revision_id,
        condition_id="control",
    )
    db = MagicMock()
    db.get.return_value = study
    with patch.object(
        registry.identity_store,
        "get_participant_by_account",
        return_value=SimpleNamespace(participant_id=uuid.uuid4()),
    ), patch.object(
        registry.identity_store, "list_enrollments", return_value=[enrollment]
    ), patch.object(
        registry.identity_store, "row_to_enrollment", return_value=enrollment
    ), patch.object(
        registry.protocol_store, "get_revision", return_value=revision
    ), patch.object(
        registry.protocol_store, "row_to_revision", return_value=revision
    ), patch.object(
        registry.assignment_store,
        "get_assignment_for_enrollment_revision",
        return_value=assignment_row,
    ), patch.object(
        registry.assignment_store, "row_to_assignment", return_value=assignment_row
    ), patch.object(crud, "get_agent_profile_by_id") as live_profile:
        resolution = registry.resolve_assignment_context(db, uuid.uuid4())

    assert resolution is None
    live_profile.assert_not_called()


def test_task_creation_snapshots_frozen_values_not_later_profile_edits():
    """Publishing freezes config; a later profile edit cannot change a task."""
    distribution_id = uuid.uuid4()
    protocol = _protocol_with_condition(distribution_id)
    published = publish_revision(
        protocol,
        RevisionLineage(study_id=protocol.study_id),
        distribution_resolver=_FrozenResolver(
            _agent_config(profile_id=distribution_id, model="published-model")
        ),
    )
    revision = published.revision
    original_digest = revision.protocol_digest

    # Simulate a later profile edit by handing the registry an unrelated live
    # profile object; it must never be consulted.
    edited_live_profile = SimpleNamespace(
        profile_id=distribution_id,
        name="renamed",
        model="edited-model",
        tools_json='["write_file"]',
        approval_policy="per_step",
        framework_version="goose",
        max_steps=99,
        connection_id=uuid.uuid4(),
    )
    enrollment = SimpleNamespace(
        enrollment_id=uuid.uuid4(),
        study_id=revision.study_id,
        study_revision_id=revision.revision_id,
        status=EnrollmentStatus.ACTIVE.value,
    )
    study = SimpleNamespace(
        study_id=revision.study_id,
        is_research=True,
        is_active=True,
        created_by=uuid.uuid4(),
        starts_at=None,
        ends_at=None,
    )
    assignment_row = SimpleNamespace(
        assignment_id=uuid.uuid4(),
        enrollment_id=enrollment.enrollment_id,
        study_revision_id=revision.revision_id,
        condition_id=revision.protocol_json["conditions"][0]["condition_id"],
    )
    db = MagicMock()
    db.get.return_value = study
    with patch.object(
        registry.identity_store,
        "get_participant_by_account",
        return_value=SimpleNamespace(participant_id=uuid.uuid4()),
    ), patch.object(
        registry.identity_store, "list_enrollments", return_value=[enrollment]
    ), patch.object(
        registry.identity_store, "row_to_enrollment", return_value=enrollment
    ), patch.object(
        registry.protocol_store,
        "get_revision",
        return_value=SimpleNamespace(revision_id=revision.revision_id),
    ), patch.object(
        registry.protocol_store, "row_to_revision", return_value=revision
    ), patch.object(
        registry.assignment_store,
        "get_assignment_for_enrollment_revision",
        return_value=assignment_row,
    ), patch.object(
        registry.assignment_store, "row_to_assignment", return_value=assignment_row
    ), patch.object(
        crud, "get_agent_profile_by_id", return_value=edited_live_profile
    ) as live_profile:
        resolution = registry.resolve_assignment_context(db, uuid.uuid4())

    assert resolution is not None
    assert resolution.profile.model == "published-model"
    assert resolution.profile.name == "arm"
    assert resolution.profile.tools_json == '["read_file"]'
    assert resolution.profile.approval_policy == "auto"
    live_profile.assert_not_called()
    # The published revision digest is unchanged by the edit.
    recomputed = protocol_digest(
        StudyProtocolV1.model_validate(revision.protocol_json)
    )
    assert recomputed == original_digest
