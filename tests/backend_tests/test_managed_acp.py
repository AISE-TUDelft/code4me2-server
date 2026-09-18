import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.acp_authorization import (
    AcpAuthorizationService,
    AcpServerAuthorization,
    AcpSessionAuthorization,
)
from backend.routers.acp import (
    ManagedInferenceRequest,
    ManagedRunRequest,
    _managed_policy,
    create_managed_run,
    expected_schema_revision,
    get_acp_capabilities,
    get_participant_readiness,
    run_managed_inference,
)
from backend.routers.agent.ingest import get_agent_run
from backend.routers.agent.profiles import AgentProfilePayload
from backend.routers.agents import TaskCreateRequest, create_agent_task
from backend.routers.analytics.auth_utils import AuthenticatedUser


class MemoryTokenStore:
    def __init__(self):
        self.data = {}

    def get(self, token_type, token, reset_exp=False):
        return self.data.get((token_type, token))

    def set(self, token_type, token, info, force_reset_exp=False):
        self.data[(token_type, token)] = dict(info)

    def consume(self, token_type, token):
        return self.data.pop((token_type, token), None)

    def discard(self, token_type, token):
        self.data.pop((token_type, token), None)


def _authorized_store():
    store = MemoryTokenStore()
    store.set("auth_token", "auth", {"user_id": "user"})
    store.set("user_token", "user", {"session_token": "parent"})
    store.set("session_token", "parent", {"project_tokens": ["project"]})
    store.set("project_token", "project", {"session_tokens": ["parent"]})
    return store


def _scope():
    return AcpServerAuthorization(
        acp_token="acp",
        user_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        project_info={},
        workspace="/workspace",
    )


def test_managed_grants_are_isolated_per_launch_and_accept_windows_paths():
    store = _authorized_store()
    tokens = iter(["grant-one", "grant-two", "acp-token"])
    service = AcpAuthorizationService(store, token_factory=lambda: next(tokens))

    first = service.prepare_grant(
        auth_token="auth",
        project_id="project",
        workspace=r"C:\study\project",
        launch_id="launch-one",
        path_format="windows",
        managed_protocol_version="1",
    )
    second = service.prepare_grant(
        auth_token="auth",
        project_id="project",
        workspace=r"C:\study\project",
        launch_id="launch-two",
        path_format="windows",
        managed_protocol_version="1",
    )
    retried = service.prepare_grant(
        auth_token="auth",
        project_id="project",
        workspace=r"C:\study\project",
        launch_id="launch-one",
        path_format="windows",
        managed_protocol_version="1",
    )

    assert first.workspace == r"C:\study\project"
    assert retried.grant == first.grant
    assert store.get("acp_grant", first.grant) is not None
    assert store.get("acp_grant", second.grant) is not None
    assert store.get("acp_grant", retried.grant) is not None
    session = service.exchange_grant(second.grant)
    assert session.acp_token == "acp-token"


def test_capabilities_report_schema_readiness():
    expected = expected_schema_revision()
    assert expected is not None
    db = MagicMock()
    db.execute.return_value.scalar.return_value = expected
    app = MagicMock()
    app.get_db_session.return_value = db

    result = get_acp_capabilities(app)

    assert result["schema_ready"] is True
    assert result["expected_schema_revision"] == expected
    assert result["managed_protocol_versions"] == ["1"]
    assert result["supported_runtimes"] == ["code4me2-agent"]
    db.close.assert_called_once()


def test_capabilities_reject_a_stale_schema_revision():
    expected = expected_schema_revision()
    assert expected is not None
    db = MagicMock()
    db.execute.return_value.scalar.return_value = "000000000000"
    app = MagicMock()
    app.get_db_session.return_value = db

    with pytest.raises(HTTPException) as error:
        get_acp_capabilities(app)

    assert error.value.status_code == 503
    assert expected in error.value.detail
    db.close.assert_called_once()


def test_expected_schema_revision_honours_the_operator_override(monkeypatch):
    monkeypatch.setenv("MANAGED_SCHEMA_REQUIRED_REVISION", "  abc123  ")

    assert expected_schema_revision() == "abc123"


def test_capabilities_fail_closed_when_no_head_can_be_derived(monkeypatch):
    import backend.routers.acp as acp

    monkeypatch.delenv("MANAGED_SCHEMA_REQUIRED_REVISION", raising=False)
    monkeypatch.setattr(acp, "_expected_schema_revision_resolved", False)
    monkeypatch.setattr(acp, "_expected_schema_revision_cache", None)
    monkeypatch.setattr(acp, "_derive_expected_schema_revision", lambda: None)
    db = MagicMock()
    db.execute.return_value.scalar.return_value = "anything"
    app = MagicMock()
    app.get_db_session.return_value = db

    assert acp.expected_schema_revision() is None
    with pytest.raises(HTTPException) as error:
        get_acp_capabilities(app)

    assert error.value.status_code == 503


def test_readiness_rejects_an_uncertified_assigned_runtime():
    user_id = uuid.uuid4()
    profile = SimpleNamespace(framework_version="goose", name="goose-arm")
    db = MagicMock()
    app = MagicMock()
    app.get_db_session.return_value = db
    user = AuthenticatedUser(user_id=user_id, is_admin=False, email="p@example.com", name="P")

    with patch("agents.registry.resolve_assignment", return_value=profile):
        with pytest.raises(Exception) as error:
            get_participant_readiness(user, app)

    assert error.value.status_code == 409
    assert "not managed" in error.value.detail
    db.close.assert_called_once()


def test_readiness_rejects_missing_server_side_provider_credential(monkeypatch):
    user_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    profile = SimpleNamespace(
        framework_version="code4me2-agent",
        name="managed-arm",
        model="managed-model",
        tools_json="[]",
        approval_policy="auto",
        max_steps=3,
        max_context_tokens=1000,
        temperature=0.2,
        connection_id=connection_id,
        funding_owner_user_id=uuid.uuid4(),
    )
    connection = SimpleNamespace(
        connection_id=connection_id,
        label="study-provider",
        base_url="https://provider.example/v1",
        secret_ref="STUDY_PROVIDER_KEY",
        models_json='["managed-model"]',
        is_active=True,
    )
    db = MagicMock()
    app = MagicMock()
    app.get_db_session.return_value = db
    user = AuthenticatedUser(user_id=user_id, is_admin=False, email="p@example.com", name="P")
    monkeypatch.delenv("STUDY_PROVIDER_KEY", raising=False)

    with patch("agents.registry.resolve_assignment", return_value=profile), patch(
        "backend.routers.acp.crud.get_provider_connection", return_value=connection
    ), patch(
        "backend.routers.acp.crud.provider_connection_is_available", return_value=True
    ), patch(
        "backend.routers.acp.crud.get_user_by_id", return_value=None
    ), patch("backend.routers.acp.resolve_store_agent_content_for_acp", return_value=False):
        with pytest.raises(Exception) as error:
            get_participant_readiness(user, app)

    assert error.value.status_code == 503
    assert error.value.detail["code"] == "SECRET_MISSING"
    assert "STUDY_PROVIDER_KEY" in error.value.detail["message"]
    db.close.assert_called_once()


def test_readiness_accepts_admin_managed_connection_without_a_per_user_grant(monkeypatch):
    """An authorized researcher may use an active admin-managed connection.

    The schema has no ``provider_connection_grant`` table (guarded by
    ``test_research_schema_consolidation``); profile/researcher authorization is
    role-based and readiness is connection state, so the positive path resolves
    the connection without any per-user grant row.
    """
    user_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    researcher_id = uuid.uuid4()
    profile = SimpleNamespace(
        framework_version="code4me2-agent",
        name="managed-arm",
        model="managed-model",
        tools_json="[]",
        approval_policy="auto",
        max_steps=3,
        max_context_tokens=1000,
        temperature=0.2,
        connection_id=connection_id,
        funding_owner_user_id=researcher_id,
    )
    connection = SimpleNamespace(
        connection_id=connection_id,
        label="study-provider",
        base_url="https://provider.example/v1",
        secret_ref="STUDY_PROVIDER_KEY",
        models_json='["managed-model"]',
        is_active=True,
    )
    db = MagicMock()
    app = MagicMock()
    app.get_db_session.return_value = db
    user = AuthenticatedUser(user_id=user_id, is_admin=False, email="p@example.com", name="P")
    monkeypatch.setenv("STUDY_PROVIDER_KEY", "test-provider-secret")

    with patch("agents.registry.resolve_assignment", return_value=profile), patch(
        "backend.routers.acp.crud.get_provider_connection", return_value=connection
    ), patch(
        "backend.routers.acp.crud.provider_connection_is_available", return_value=True
    ) as available, patch(
        "backend.routers.acp.crud.get_user_by_id", return_value=None
    ), patch("backend.routers.acp.resolve_store_agent_content_for_acp", return_value=False):
        result = get_participant_readiness(user, app)

    assert result["ready"] is True
    assert result["runtime"] == "code4me2-agent"
    # The researcher's authorization is checked against connection state, not a
    # per-user grant row.
    available.assert_called_once_with(db, connection_id, researcher_id)
    db.close.assert_called_once()


def test_resolve_task_connection_returns_the_active_connection_without_a_grant():
    from agents import provider as provider_module

    connection = SimpleNamespace(
        connection_id=uuid.uuid4(),
        label="study-provider",
        base_url="https://provider.example/v1",
        secret_ref="STUDY_PROVIDER_KEY",
        models_json='["managed-model"]',
        is_active=True,
    )
    profile = SimpleNamespace(connection_id=connection.connection_id)
    owner_user_id = uuid.uuid4()
    db = MagicMock()

    with patch(
        "database.crud.get_provider_connection", return_value=connection
    ), patch(
        "database.crud.provider_connection_is_available", return_value=True
    ) as available:
        resolved = provider_module.resolve_task_connection(db, profile, owner_user_id)

    assert resolved.connection_id == connection.connection_id
    assert resolved.is_active is True
    available.assert_called_once_with(db, connection.connection_id, owner_user_id)



def test_managed_policy_applies_the_assigned_command_allowlist():
    user_id = uuid.uuid4()
    profile = SimpleNamespace(
        framework_version="code4me2-agent",
        name="managed-arm",
        model="managed-model",
        tools_json='["run_command"]',
        approval_policy="per_step",
        max_steps=3,
        max_context_tokens=1000,
        temperature=0.2,
    )
    user = SimpleNamespace(config_id=uuid.uuid4())
    config = SimpleNamespace(
        config_data='{"agent":{"commands_allowlist":["git", "rg"]}}'
    )

    with patch("backend.routers.acp.crud.get_user_by_id", return_value=user), patch(
        "backend.routers.acp.crud.get_config_by_id", return_value=config
    ), patch(
        "backend.routers.acp.resolve_store_agent_content_for_acp", return_value=False
    ):
        policy = _managed_policy(MagicMock(), user_id, profile)

    assert policy["commands_allowlist"] == ["git", "rg"]


def test_plugin_task_response_includes_the_assigned_approval_policy():
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    profile = SimpleNamespace(
        name="goose-per-step",
        model="study-model",
        approval_policy="per_step",
        tools_json="[]",
        temperature=None,
        framework_version="goose",
        profile_id=uuid.uuid4(),
    )
    assignment = SimpleNamespace(
        profile=profile,
        study_id=uuid.uuid4(),
        assignment_id=uuid.uuid4(),
        arm_name="treatment",
        is_baseline=False,
    )
    task = SimpleNamespace(
        task_id=uuid.uuid4(),
        model="study-model",
        approval_policy="per_step",
    )
    db = MagicMock()
    app = MagicMock()
    app.get_db_session.return_value = db

    with patch("backend.routers.agents.crud.get_agent_task", return_value=None), patch(
        "backend.routers.agents.crud.get_session_by_id",
        return_value=SimpleNamespace(user_id=user_id),
    ), patch("agents.registry.resolve_assignment_context", return_value=assignment), patch(
        "backend.routers.agents.resolve_store_agent_content", return_value=False
    ), patch("backend.routers.agents.crud.create_agent_task", return_value=task):
        response = create_agent_task(TaskCreateRequest(task_id=task.task_id), app, session_id)

    assert json.loads(response.body)["agent_launch"] == {"approval_policy": "per_step"}


def test_profile_validation_rejects_tools_from_another_runtime():
    with pytest.raises(ValueError, match="unsupported by codex"):
        AgentProfilePayload(
            name="invalid-arm",
            model="model",
            framework_version="codex",
            tools_json='["read_file"]',
            approval_policy="auto",
            max_steps=3,
            connection_id=uuid.uuid4(),
        )


def test_profile_validation_rejects_unknown_approval_policy():
    with pytest.raises(ValueError, match="approval_policy"):
        AgentProfilePayload(
            name="invalid-arm",
            model="model",
            framework_version="code4me2-agent",
            tools_json='["read_file"]',
            approval_policy="sometimes",
            max_steps=3,
        )


def test_profile_validation_rejects_retired_provider_fields():
    common = {
        "name": "managed-arm",
        "model": "model",
        "framework_version": "code4me2-agent",
        "tools_json": "[]",
        "approval_policy": "auto",
        "max_steps": 3,
        "connection_id": uuid.uuid4(),
    }
    # Researchers cannot name an arbitrary secret env var or backend URL: the
    # retired fields are rejected outright (extra="forbid").
    with pytest.raises(ValueError):
        AgentProfilePayload(**common, api_key_ref="STUDY_PROVIDER_KEY")
    with pytest.raises(ValueError):
        AgentProfilePayload(**common, base_url="https://provider.example/v1")
    with pytest.raises(ValueError):
        AgentProfilePayload(**common, distribution_mode="PACKAGED")

    valid = AgentProfilePayload(**common, release_id="rel-1")
    assert valid.connection_id is not None
    assert valid.release_id == "rel-1"


def test_managed_run_is_idempotent_only_for_the_same_scope():
    scope = _scope()
    task = SimpleNamespace(
        task_id=uuid.uuid4(),
        external_run_id="run-1",
        owner_user_id=uuid.UUID(scope.user_id),
        owner_project_id=uuid.UUID(scope.project_id),
        agent_session_id="acp-session-1",
        source="code4me2_agent",
        policy_snapshot={"version": "1"},
    )
    db = MagicMock()
    app = MagicMock()
    app.get_db_session.return_value = db

    with patch("backend.routers.acp.crud.get_agent_task_by_external_run_id", return_value=task):
        response = create_managed_run(
            ManagedRunRequest(run_id="run-1", session_id="acp-session-1"), app, scope
        )

    assert response.status_code == 200
    assert b'"run_id":"run-1"' in response.body

    task.owner_project_id = uuid.uuid4()
    with patch("backend.routers.acp.crud.get_agent_task_by_external_run_id", return_value=task):
        with pytest.raises(Exception) as error:
            create_managed_run(
                ManagedRunRequest(run_id="run-1", session_id="acp-session-1"), app, scope
            )
    assert error.value.status_code == 409


def test_managed_inference_disables_proxy_observation_events():
    scope = _scope()
    task = SimpleNamespace(
        task_id=uuid.uuid4(),
        owner_user_id=uuid.UUID(scope.user_id),
        owner_project_id=uuid.UUID(scope.project_id),
        agent_session_id="acp-session-1",
        source="code4me2_agent",
        policy_snapshot={
            "version": "1",
            "model": "managed-model",
            "tools": ["read_file"],
            "approval_policy": "auto",
            "max_iterations": 3,
            "max_context_tokens": 1000,
            "temperature": None,
            "store_agent_content": False,
        },
        agent_profile="managed-profile",
        framework_version="code4me2-agent",
        profile_id=uuid.uuid4(),
    )
    profile = SimpleNamespace(connection_id=uuid.uuid4())
    connection = SimpleNamespace(
        connection_id=profile.connection_id,
        label="study-provider",
        base_url="https://provider.example/v1",
        secret_ref="EXAMPLE_PROVIDER_KEY",
        models_json='["managed-model"]',
        is_active=True,
    )
    db = MagicMock()
    app = MagicMock()
    app.get_db_session.return_value = db
    forwarded = MagicMock()

    with patch(
        "backend.routers.acp.crud.get_agent_task_by_external_run_id", return_value=task
    ), patch(
        "backend.routers.acp.crud.get_agent_profile_by_id", return_value=profile
    ), patch(
        "backend.routers.acp.provider_module.resolve_task_connection",
        return_value=connection,
    ), patch(
        "backend.routers.acp.crud.get_user_by_id", return_value=None
    ), patch(
        "agents.inference.run_inference", new=AsyncMock(return_value=forwarded)
    ) as run_inference:
        result = asyncio.run(
            run_managed_inference(
                ManagedInferenceRequest(
                    run_id="run-1",
                    session_id="acp-session-1",
                    request={
                        "model": "participant-override",
                        "temperature": 1.9,
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                ),
                app,
                scope,
            )
        )

    assert result is forwarded
    assert run_inference.await_args.kwargs["record_observation_events"] is False
    assert run_inference.await_args.kwargs["model"] == "managed-model"
    assert run_inference.await_args.kwargs["openai_body"]["model"] == "managed-model"
    assert "temperature" not in run_inference.await_args.kwargs["openai_body"]


def test_managed_inference_rejects_tools_outside_snapshot():
    scope = _scope()
    task = SimpleNamespace(
        task_id=uuid.uuid4(),
        owner_user_id=uuid.UUID(scope.user_id),
        owner_project_id=uuid.UUID(scope.project_id),
        agent_session_id="acp-session-1",
        source="code4me2_agent",
        policy_snapshot={
            "version": "1",
            "model": "managed-model",
            "tools": ["read_file"],
            "approval_policy": "auto",
            "max_iterations": 3,
            "max_context_tokens": 1000,
            "temperature": 0.2,
        },
        agent_profile="managed-profile",
        framework_version="code4me2-agent",
        profile_id=uuid.uuid4(),
    )
    profile = SimpleNamespace(connection_id=uuid.uuid4())
    db = MagicMock()
    app = MagicMock()
    app.get_db_session.return_value = db

    with patch(
        "backend.routers.acp.crud.get_agent_task_by_external_run_id", return_value=task
    ), patch(
        "backend.routers.acp.crud.get_agent_profile_by_id", return_value=profile
    ), patch(
        "backend.routers.acp.provider_module.resolve_task_connection",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.acp.crud.get_user_by_id", return_value=None
    ):
        with pytest.raises(Exception) as error:
            asyncio.run(
                run_managed_inference(
                    ManagedInferenceRequest(
                        run_id="run-1",
                        session_id="acp-session-1",
                        request={
                            "messages": [{"role": "user", "content": "hello"}],
                            "tools": [
                                {
                                    "type": "function",
                                    "function": {"name": "write_file", "parameters": {}},
                                }
                            ],
                        },
                    ),
                    app,
                    scope,
                )
            )
    assert error.value.status_code == 403


def test_managed_inference_rejects_tool_choice_not_in_request():
    scope = _scope()
    task = SimpleNamespace(
        task_id=uuid.uuid4(),
        owner_user_id=uuid.UUID(scope.user_id),
        owner_project_id=uuid.UUID(scope.project_id),
        agent_session_id="acp-session-1",
        source="code4me2_agent",
        policy_snapshot={
            "version": "1",
            "model": "managed-model",
            "tools": ["read_file", "write_file"],
            "approval_policy": "auto",
            "max_iterations": 3,
            "max_context_tokens": 1000,
            "temperature": 0.2,
        },
        agent_profile="managed-profile",
        framework_version="code4me2-agent",
        profile_id=uuid.uuid4(),
    )
    profile = SimpleNamespace(connection_id=uuid.uuid4())
    db = MagicMock()
    app = MagicMock()
    app.get_db_session.return_value = db

    with patch(
        "backend.routers.acp.crud.get_agent_task_by_external_run_id", return_value=task
    ), patch(
        "backend.routers.acp.crud.get_agent_profile_by_id", return_value=profile
    ), patch(
        "backend.routers.acp.provider_module.resolve_task_connection",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.acp.crud.get_user_by_id", return_value=None
    ):
        with pytest.raises(Exception) as error:
            asyncio.run(
                run_managed_inference(
                    ManagedInferenceRequest(
                        run_id="run-1",
                        session_id="acp-session-1",
                        request={
                            "messages": [{"role": "user", "content": "hello"}],
                            "tools": [
                                {
                                    "type": "function",
                                    "function": {"name": "read_file", "parameters": {}},
                                }
                            ],
                            "tool_choice": {
                                "type": "function",
                                "function": {"name": "write_file"},
                            },
                        },
                    ),
                    app,
                    scope,
                )
            )
    assert error.value.status_code == 403


def test_managed_inference_rejects_non_chat_completions_payload():
    with pytest.raises(Exception) as error:
        asyncio.run(
            run_managed_inference(
                ManagedInferenceRequest(
                    run_id="run-1",
                    session_id="acp-session-1",
                    request={"input": [{"role": "user", "content": "hello"}]},
                ),
                MagicMock(),
                _scope(),
            )
        )
    assert error.value.status_code == 400


def test_run_readback_is_scoped_to_project_and_acp_session():
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    task = SimpleNamespace(
        owner_user_id=user_id,
        owner_project_id=project_id,
        agent_session_id="acp-session-1",
    )
    db = MagicMock()
    app = MagicMock()
    app.get_db_session.return_value = db
    wrong_project_scope = AcpSessionAuthorization(
        acp_token="token",
        user_id=str(user_id),
        project_id=str(uuid.uuid4()),
        workspace="/workspace",
    )

    with patch(
        "backend.routers.agent.ingest.crud.get_agent_task_by_external_run_id",
        return_value=task,
    ):
        with pytest.raises(Exception) as error:
            get_agent_run("run-1", "acp-session-1", app, wrong_project_scope)
    assert error.value.status_code == 403

    right_project_scope = AcpSessionAuthorization(
        acp_token="token",
        user_id=str(user_id),
        project_id=str(project_id),
        workspace="/workspace",
    )
    with patch(
        "backend.routers.agent.ingest.crud.get_agent_task_by_external_run_id",
        return_value=task,
    ):
        with pytest.raises(Exception) as error:
            get_agent_run("run-1", "another-session", app, right_project_scope)
    assert error.value.status_code == 409
