"""The relay entry points share one funded gate and one budget meter.

* ``access.require_funded_access`` honours an ENROLLMENT-scoped kill switch
  (real database) — the scope the old ``/api/agent/inference`` and
  ``/api/acp/grant`` checks never consulted.
* ``/api/agent/inference`` passes a meter for study-funded Chat Completions
  tasks and none for developer (non-study) tasks or Codex bodies.
* ``/api/acp/chat/completions`` reserves before posting and voids on an
  upstream error.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from backend.routers.acp import acp_chat_completions
from backend.routers.agents import InferenceRequest, run_agent_inference
from backend.routers.research import access
from research.analysis.operations import store as operations_store
from research.analysis.operations.models import KillSwitchRecord, KillSwitchScope, KillSwitchScopeKind

from ._ui_overhaul_seed import seed_account, seed_enrollment, seed_participant, seed_study
from .test_research_api_contract import http_runtime  # noqa: F401 - fixture


# --------------------------------------------------------------------------- scoped gate (DB)


def test_require_funded_access_honours_enrollment_scoped_kill_switch(http_runtime):
    _, session_factory, _ = http_runtime
    with session_factory() as db:
        owner_id = seed_account(db, "owner@example.com", can_research=True)
        account_id = seed_account(db, "participant@example.com")
        study_id = seed_study(db, owner_id=owner_id, name="gate study")
        participant_id = seed_participant(db, account_id)
        enrollment_id = seed_enrollment(db, participant_id=participant_id, study_id=study_id)

        live = access.require_funded_access(db, account_id=account_id, study_id=study_id)
        assert live.enrollment_id == enrollment_id

        record = operations_store.engage_kill_switch(
            db,
            KillSwitchRecord(
                switch_id=uuid.uuid4(),
                scope=KillSwitchScope(kind=KillSwitchScopeKind.ENROLLMENT, scope_id=enrollment_id),
                reason="test",
                actor="admin@example.com",
                engaged_at=datetime.now(timezone.utc),
            ),
        )
        # The old study-only predicate did not see this switch...
        assert operations_store.is_kill_switch_engaged(db, study_id=study_id) is False
        # ...the shared gate does, with or without a study id at hand.
        with pytest.raises(access.FundedAccessRefused) as refused:
            access.require_funded_access(db, account_id=account_id, study_id=study_id)
        assert refused.value.code == "KILL_SWITCH_ENGAGED"
        with pytest.raises(access.FundedAccessRefused):
            access.require_funded_access(db, account_id=account_id)

        operations_store.release_kill_switch(db, record.switch_id, released_at=datetime.now(timezone.utc))
        assert access.require_funded_access(db, account_id=account_id).enrollment_id == enrollment_id


# --------------------------------------------------------------------------- /api/agent/inference wiring


def _task(**overrides):
    base = dict(
        task_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        research_session_id=uuid.uuid4(),
        profile=None,
        profile_id=None,
        agent_profile="goose-arm",
        model="frozen-model",
        temperature=None,
        tools_json="[]",
        framework_version="goose",
        funding_owner_user_id=uuid.uuid4(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _connection():
    return SimpleNamespace(
        connection_id=uuid.uuid4(), label="study", base_url="https://provider.example/v1",
        secret_ref="EXAMPLE_PROVIDER_KEY", models_json='["frozen-model"]', is_active=True,
    )


def _run_relay(task, request_body, *, enrollment):
    app = MagicMock()
    app.get_db_session.return_value = MagicMock()
    forwarded = MagicMock()
    with patch("backend.routers.agents.crud.get_agent_task", return_value=task), patch(
        "backend.routers.agents.access.require_funded_access", return_value=enrollment
    ) as gate, patch(
        "backend.routers.agents.resolve_store_agent_content", return_value=False
    ), patch(
        "backend.routers.agents.provider_module.funding_owner_for_task", return_value=(task.funding_owner_user_id, False)
    ), patch(
        "backend.routers.agents.provider_module.resolve_task_connection", return_value=_connection()
    ), patch(
        "backend.routers.agents.inference.run_inference", new=AsyncMock(return_value=forwarded)
    ) as run_inference:
        result = asyncio.run(
            run_agent_inference(InferenceRequest(task_id=task.task_id, request=request_body), app, task.session_id)
        )
    assert result is forwarded
    return gate, run_inference.await_args.kwargs


def test_study_task_is_metered_and_gated_with_its_own_study():
    task = _task()
    enrollment = SimpleNamespace(enrollment_id=task.enrollment_id, study_id=task.study_id)
    gate, kwargs = _run_relay(task, {"model": "x", "messages": [{"role": "user", "content": "hi"}]}, enrollment=enrollment)
    gate.assert_called_once()
    assert gate.call_args.kwargs == {"account_id": task.owner_user_id, "study_id": task.study_id}
    meter = kwargs["meter"]
    assert meter is not None
    assert meter.enrollment_id == task.enrollment_id
    assert meter.study_id == task.study_id
    assert meter.entry_point == "agent_inference"
    assert meter.model == "frozen-model"
    assert meter.research_session_id == task.research_session_id


def test_developer_task_is_not_metered():
    task = _task(study_id=None, enrollment_id=None, research_session_id=None)
    _, kwargs = _run_relay(task, {"model": "x", "messages": []}, enrollment=None)
    assert kwargs["meter"] is None


@pytest.mark.parametrize(
    ("framework", "request_body"),
    [
        # A Responses-shaped body on a Goose study task would reach the provider unmetered.
        ("goose", {"model": "x", "input": [{"role": "user", "content": "hi"}]}),
        # A Codex study arm never relays through the study's key.
        ("codex", {"model": "x", "input": [{"role": "user", "content": "hi"}]}),
        ("codex", {"model": "x", "messages": [{"role": "user", "content": "hi"}]}),
        # Not a Chat Completions body at all.
        ("code4me2-agent", {"model": "x"}),
    ],
)
def test_study_tasks_that_cannot_be_metered_are_refused(framework, request_body):
    task = _task(framework_version=framework)
    enrollment = SimpleNamespace(enrollment_id=task.enrollment_id, study_id=task.study_id)
    app = MagicMock()
    app.get_db_session.return_value = MagicMock()
    with patch("backend.routers.agents.crud.get_agent_task", return_value=task), patch(
        "backend.routers.agents.access.require_funded_access", return_value=enrollment
    ), patch("backend.routers.agents.resolve_store_agent_content", return_value=False), patch(
        "backend.routers.agents.provider_module.funding_owner_for_task", return_value=(task.funding_owner_user_id, False)
    ), patch(
        "backend.routers.agents.provider_module.resolve_task_connection", return_value=_connection()
    ), patch("backend.routers.agents.inference.run_inference", new=AsyncMock()) as run_inference:
        with pytest.raises(HTTPException) as error:
            asyncio.run(run_agent_inference(InferenceRequest(task_id=task.task_id, request=request_body), app, task.session_id))
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "RELAY_NOT_METERED"
    run_inference.assert_not_awaited()


def test_refused_gate_is_a_403_before_any_provider_resolution():
    task = _task()
    app = MagicMock()
    app.get_db_session.return_value = MagicMock()
    with patch("backend.routers.agents.crud.get_agent_task", return_value=task), patch(
        "backend.routers.agents.access.require_funded_access",
        side_effect=access.FundedAccessRefused("KILL_SWITCH_ENGAGED", "engaged"),
    ), patch("backend.routers.agents.provider_module.resolve_task_connection") as resolve:
        with pytest.raises(HTTPException) as error:
            asyncio.run(run_agent_inference(InferenceRequest(task_id=task.task_id, request={"messages": []}), app, task.session_id))
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "KILL_SWITCH_ENGAGED"
    resolve.assert_not_called()


# --------------------------------------------------------------------------- /api/acp/chat/completions wiring


class RecordingMeter:
    instances: list["RecordingMeter"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls: list[tuple] = []
        self.refusal = None
        RecordingMeter.instances.append(self)

    async def reserve(self, body, *, request_id, upstream_base_url):
        self.calls.append(("reserve", dict(body), upstream_base_url))
        body["max_tokens"] = 123
        return self.refusal

    async def resolve_response(self, body, *, upstream_status):
        self.calls.append(("resolve_response", body, upstream_status))

    async def resolve_upstream_error(self, status):
        self.calls.append(("resolve_upstream_error", status))

    async def resolve_transport_error(self, error):
        self.calls.append(("resolve_transport_error", type(error).__name__))

    def response_headers(self):
        return {"X-Test-Meter": "1"}


def _chat_completions(upstream_handler, *, refusal=None):
    RecordingMeter.instances.clear()
    scope = SimpleNamespace(user_id=str(uuid.uuid4()))
    profile = SimpleNamespace(
        model="frozen-model", temperature=0.5, framework_version="code4me2-agent",
        connection_id=uuid.uuid4(), funding_owner_user_id=uuid.uuid4(),
    )
    assignment = SimpleNamespace(profile=profile, study_id=uuid.uuid4())
    enrollment = SimpleNamespace(enrollment_id=uuid.uuid4(), study_id=assignment.study_id)
    upstream = SimpleNamespace(
        base_url="https://provider.example/v1", api_key="k",
        endpoint=lambda responses_api=False: "https://provider.example/v1/chat/completions",
    )
    app = MagicMock()
    app.get_db_session.return_value = MagicMock()

    def make_meter(**kwargs):
        meter = RecordingMeter(**kwargs)
        meter.refusal = refusal
        return meter

    with patch("backend.routers.acp.registry.resolve_assignment_context", return_value=assignment), patch(
        "backend.routers.acp._require_funded_access", return_value=enrollment
    ), patch(
        "backend.routers.acp.provider_module.funding_owner_for_profile", return_value=(profile.funding_owner_user_id, False)
    ), patch(
        "backend.routers.acp.provider_module.resolve_task_connection", return_value=_connection()
    ), patch(
        "backend.routers.acp.provider_module.resolve_upstream", return_value=upstream
    ), patch(
        "backend.routers.acp.InferenceMeter", side_effect=make_meter
    ), patch(
        "backend.routers.acp.httpx.AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(upstream_handler))
    ):
        response = asyncio.run(
            acp_chat_completions({"model": "participant", "messages": [{"role": "user", "content": "hi"}]}, scope, app)
        )
    return response, RecordingMeter.instances[0], enrollment, assignment


def test_acp_chat_completions_reserves_then_settles():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}})

    response, meter, enrollment, assignment = _chat_completions(handler)
    assert response.status_code == 200
    assert response.headers["x-test-meter"] == "1"
    assert meter.kwargs["enrollment_id"] == enrollment.enrollment_id
    assert meter.kwargs["study_id"] == assignment.study_id
    assert meter.kwargs["entry_point"] == "acp_chat_completions"
    assert meter.kwargs["model"] == "frozen-model"
    assert meter.calls[0][0] == "reserve"
    assert seen["body"]["max_tokens"] == 123  # the cap injected by the meter reached the provider
    assert seen["body"]["model"] == "frozen-model"
    assert meter.calls[1] == ("resolve_response", {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}}, 200)


def test_acp_chat_completions_voids_on_upstream_error_and_returns_refusals():
    response, meter, _, _ = _chat_completions(lambda request: httpx.Response(429, json={"error": "slow"}))
    assert response.status_code == 429
    assert meter.calls[-1] == ("resolve_upstream_error", 429)

    from fastapi import Response

    refusal = Response(content=b'{"error": {"code": "quota_exhausted"}}', status_code=402, media_type="application/json")
    calls = []
    response, meter, _, _ = _chat_completions(lambda request: calls.append(1) or httpx.Response(200, json={}), refusal=refusal)
    assert response.status_code == 402
    assert calls == []  # refused before anything was sent
    assert [call[0] for call in meter.calls] == ["reserve"]


# --------------------------------------------------------------------------- /api/acp/grant wiring


def test_grant_uses_the_scoped_funded_gate():
    import Queries
    from backend.routers.acp import prepare_acp_grant

    user_id = uuid.uuid4()
    app = MagicMock()
    app.get_db_session.return_value = MagicMock()
    with patch("backend.routers.acp._resolve_cookie_account", return_value=user_id), patch(
        "backend.routers.acp.access.require_funded_access",
        side_effect=access.FundedAccessRefused("KILL_SWITCH_ENGAGED", "engaged for this enrollment"),
    ) as gate:
        with pytest.raises(HTTPException) as error:
            prepare_acp_grant(Queries.PrepareAcpGrant(project_id=uuid.uuid4(), workspace="/workspace"), app, "cookie")
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "KILL_SWITCH_ENGAGED"
    assert gate.call_args.kwargs["account_id"] == user_id
