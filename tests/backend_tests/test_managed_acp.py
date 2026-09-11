import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.acp_authorization import (
    AcpAuthorizationService,
    AcpServerAuthorization,
    AcpSessionAuthorization,
)
from backend.routers.acp import (
    EXPECTED_SCHEMA_REVISION,
    ManagedInferenceRequest,
    ManagedRunRequest,
    _managed_policy,
    create_managed_run,
    get_acp_capabilities,
    get_participant_readiness,
    run_managed_inference,
)
from backend.routers.agent.ingest import get_agent_run
from backend.routers.agent.profiles import AgentProfilePayload
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
    db = MagicMock()
    db.execute.return_value.scalar.return_value = EXPECTED_SCHEMA_REVISION
    app = MagicMock()
    app.get_db_session.return_value = db

    result = get_acp_capabilities(app)

    assert result["schema_ready"] is True
    assert result["managed_protocol_versions"] == ["1"]
    assert result["supported_runtimes"] == ["code4me2-agent"]
    db.close.assert_called_once()


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
    profile = SimpleNamespace(
        framework_version="code4me2-agent",
        name="managed-arm",
        model="managed-model",
        tools_json="[]",
        approval_policy="auto",
        max_steps=3,
        max_context_tokens=1000,
        temperature=0.2,
        api_key_ref="STUDY_PROVIDER_KEY",
    )
    db = MagicMock()
    app = MagicMock()
    app.get_db_session.return_value = db
    user = AuthenticatedUser(user_id=user_id, is_admin=False, email="p@example.com", name="P")
    monkeypatch.delenv("STUDY_PROVIDER_KEY", raising=False)

    with patch("agents.registry.resolve_assignment", return_value=profile), patch(
        "backend.routers.acp.crud.get_user_by_id", return_value=None
    ), patch("backend.routers.acp.resolve_store_agent_content_for_acp", return_value=False):
        with pytest.raises(Exception) as error:
            get_participant_readiness(user, app)

    assert error.value.status_code == 503
    assert "STUDY_PROVIDER_KEY" in error.value.detail
    db.close.assert_called_once()


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


def test_profile_validation_rejects_tools_from_another_runtime():
    with pytest.raises(ValueError, match="unsupported by code4me2-agent"):
        AgentProfilePayload(
            name="invalid-arm",
            model="model",
            framework_version="code4me2-agent",
            tools_json='["shell"]',
            approval_policy="auto",
            max_steps=3,
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


def test_profile_validation_rejects_provider_secrets_and_unsafe_urls():
    common = {
        "name": "managed-arm",
        "model": "model",
        "framework_version": "code4me2-agent",
        "tools_json": "[]",
        "approval_policy": "auto",
        "max_steps": 3,
    }
    with pytest.raises(ValueError, match="NAME of an environment variable"):
        AgentProfilePayload(**common, api_key_ref="gsk_actualSecretMaterial123456")
    with pytest.raises(ValueError, match="base_url"):
        AgentProfilePayload(**common, base_url="https://user:secret@provider.example/v1")

    valid = AgentProfilePayload(
        **common,
        api_key_ref="STUDY_PROVIDER_KEY",
        base_url="https://provider.example/v1/",
    )
    assert valid.api_key_ref == "STUDY_PROVIDER_KEY"
    assert valid.base_url == "https://provider.example/v1"


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
    )
    profile = SimpleNamespace(base_url="https://provider.example/v1", api_key_ref="KEY")
    db = MagicMock()
    app = MagicMock()
    app.get_db_session.return_value = db
    forwarded = MagicMock()

    with patch(
        "backend.routers.acp.crud.get_agent_task_by_external_run_id", return_value=task
    ), patch("backend.routers.acp.crud.get_agent_profile", return_value=profile), patch(
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
    )
    profile = SimpleNamespace(base_url="https://provider.example/v1", api_key_ref="KEY")
    db = MagicMock()
    app = MagicMock()
    app.get_db_session.return_value = db

    with patch(
        "backend.routers.acp.crud.get_agent_task_by_external_run_id", return_value=task
    ), patch("backend.routers.acp.crud.get_agent_profile", return_value=profile):
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
    )
    profile = SimpleNamespace(base_url="https://provider.example/v1", api_key_ref="KEY")
    db = MagicMock()
    app = MagicMock()
    app.get_db_session.return_value = db

    with patch(
        "backend.routers.acp.crud.get_agent_task_by_external_run_id", return_value=task
    ), patch("backend.routers.acp.crud.get_agent_profile", return_value=profile):
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
