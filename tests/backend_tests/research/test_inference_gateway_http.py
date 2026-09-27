"""HTTP contract of the research inference gateway (Goose's metered relay).

Real database, real router, mocked provider (``httpx.MockTransport``). Every
case checks two things: what the agent receives, and what the ledger recorded
(RESERVED → SETTLED / FORFEITED / VOIDED, or nothing at all on a refusal).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from functools import partial
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import text

from research.budget import encode_capability_bearer, issue_inference_capability
from research.budget import ledger
from research.runtime.bootstrap.capability import issue_capability

from ._ui_overhaul_seed import (
    seed_account,
    seed_assignment,
    seed_byoa_release,
    seed_connection,
    seed_enrollment,
    seed_participant,
    seed_profile,
    seed_study,
)
from .test_research_api_contract import http_runtime  # noqa: F401 - fixture

GATEWAY = "/api/research/inference/v1/chat/completions"
SECRET = os.environ.get("BOOTSTRAP_SIGNING_SECRET", "test-bootstrap-signing-secret")
MODEL = "goose-model"


def _sse(*chunks: dict, done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode()


USAGE_CHUNK = {
    "id": "x",
    "choices": [],
    "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
}
CONTENT_CHUNK = {"id": "x", "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]}
STOP_CHUNK = {"id": "x", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}


class _Fixture:
    def __init__(self, session_factory, monkeypatch, *, framework: str = "goose", limit_micro_usd: int = 5_000_000, price: bool = True):
        self.factory = session_factory
        monkeypatch.setenv("UI_OVERHAUL_KEY", "secret-provider-key")
        with session_factory() as db:
            self.owner_id = seed_account(db, "owner@example.com", can_research=True)
            self.account_id = seed_account(db, "participant@example.com")
            self.study_id = seed_study(db, owner_id=self.owner_id, name="gateway study")
            # seed_connection prices the model (1/4 USD per million) unless told not to.
            self.connection_id = seed_connection(db, models=(MODEL,), priced=price)
            release_id = seed_byoa_release(db, agent_id=framework, agent_package=framework, agent_command=framework)
            self.profile_id = seed_profile(
                db, owner_id=self.owner_id, framework_version=framework, release_id=release_id,
                connection_id=self.connection_id, name="arm", model=MODEL, temperature=0.2,
            )
            self.participant_id = seed_participant(db, self.account_id)
            self.enrollment_id = seed_enrollment(db, participant_id=self.participant_id, study_id=self.study_id)
            seed_assignment(
                db, enrollment_id=self.enrollment_id, study_id=self.study_id, profile_id=self.profile_id,
                snapshot={
                    "profile_id": str(self.profile_id), "name": "arm", "model": MODEL,
                    "framework_version": framework, "tools_json": "[]", "approval_policy": "auto",
                    "max_steps": 5, "temperature": 0.2, "connection_id": str(self.connection_id),
                    "release_id": release_id,
                },
            )
            ledger.create_balance(db, enrollment_id=self.enrollment_id, study_id=self.study_id, limit_micro_usd=limit_micro_usd, commit=True)
            self.session_id = uuid.uuid4()

    def bearer(self, **overrides) -> str:
        capability = issue_inference_capability(
            secret=overrides.pop("secret", SECRET), ttl_seconds=overrides.pop("ttl_seconds", 3600),
            revocation_epoch=overrides.pop("revocation_epoch", 0), enrollment_id=self.enrollment_id,
            research_session_id=self.session_id, study_id=self.study_id, now=overrides.pop("now", None),
        )
        if overrides:
            capability = capability.model_copy(update=overrides)
        return "Bearer " + encode_capability_bearer(capability)

    def reservations(self):
        with self.factory() as db:
            return [
                dict(row) for row in db.execute(
                    text("SELECT * FROM public.inference_reservation WHERE enrollment_id = :e ORDER BY reserved_at"),
                    {"e": self.enrollment_id},
                ).mappings().all()
            ]

    def balance(self):
        with self.factory() as db:
            return ledger.balance_view(db, self.enrollment_id)


def _request(stream: bool = True, **extra) -> dict:
    body = {
        "model": "participant-config-model",
        "stream": stream,
        "messages": [{"role": "user", "content": "hello from goose"}],
        "tools": [{"type": "function", "function": {"name": "developer__shell", "parameters": {"type": "object"}}}],
    }
    body.update(extra)
    return body


def _upstream(handler):
    return patch(
        "agents.inference.httpx.AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )


def test_streaming_call_settles_from_the_usage_chunk(http_runtime, monkeypatch):
    client, session_factory, _ = http_runtime
    fx = _Fixture(session_factory, monkeypatch)
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, content=_sse(CONTENT_CHUNK, STOP_CHUNK, USAGE_CHUNK), headers={"content-type": "text/event-stream"})

    with _upstream(handler):
        response = client.post(GATEWAY, json=_request(), headers={"Authorization": fx.bearer()})

    assert response.status_code == 200, response.text
    assert b'"content": "hi"' in response.content
    assert "X-Inference-Budget-Available-Micro-USD" in response.headers
    assert seen["auth"] == "Bearer secret-provider-key"
    assert seen["url"] == "https://provider.test/v1/chat/completions"
    forwarded = seen["body"]
    assert forwarded["model"] == MODEL  # frozen model, not the participant's config
    assert forwarded["temperature"] == 0.2
    assert forwarded["tools"][0]["function"]["name"] == "developer__shell"  # untouched
    assert forwarded["max_tokens"] > 0 and forwarded["n"] == 1
    assert forwarded["stream_options"] == {"include_usage": True}
    rows = fx.reservations()
    assert [row["state"] for row in rows] == ["SETTLED"]
    assert rows[0]["prompt_tokens"] == 40 and rows[0]["completion_tokens"] == 10
    assert rows[0]["charged_micro_usd"] == 40 * 1 + 10 * 4
    assert rows[0]["entry_point"] == "research_gateway"
    balance = fx.balance()
    assert balance.settled_micro_usd == 80 and balance.reserved_micro_usd == 0


def test_stream_without_usage_forfeits_the_hold(http_runtime, monkeypatch):
    client, session_factory, _ = http_runtime
    fx = _Fixture(session_factory, monkeypatch)
    with _upstream(lambda request: httpx.Response(200, content=_sse(CONTENT_CHUNK, STOP_CHUNK), headers={"content-type": "text/event-stream"})):
        response = client.post(GATEWAY, json=_request(), headers={"Authorization": fx.bearer()})
    assert response.status_code == 200
    rows = fx.reservations()
    assert [row["state"] for row in rows] == ["FORFEITED"]
    assert rows[0]["charged_micro_usd"] == rows[0]["hold_micro_usd"]
    assert rows[0]["resolution_reason"] == "usage_missing"
    assert fx.balance().settled_micro_usd == rows[0]["hold_micro_usd"]


def test_upstream_429_is_passed_through_and_voided(http_runtime, monkeypatch):
    client, session_factory, _ = http_runtime
    fx = _Fixture(session_factory, monkeypatch)
    with _upstream(lambda request: httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "7"})):
        response = client.post(GATEWAY, json=_request(), headers={"Authorization": fx.bearer()})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    rows = fx.reservations()
    assert [row["state"] for row in rows] == ["VOIDED"]
    assert rows[0]["upstream_status"] == 429 and rows[0]["charged_micro_usd"] == 0
    balance = fx.balance()
    assert balance.settled_micro_usd == 0 and balance.reserved_micro_usd == 0


def test_connect_error_is_a_502_and_voided(http_runtime, monkeypatch):
    client, session_factory, _ = http_runtime
    fx = _Fixture(session_factory, monkeypatch)

    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with _upstream(handler):
        response = client.post(GATEWAY, json=_request(), headers={"Authorization": fx.bearer()})
    assert response.status_code == 502
    rows = fx.reservations()
    assert [row["state"] for row in rows] == ["VOIDED"]
    assert rows[0]["resolution_reason"] == "transport:ConnectError"


def test_read_error_mid_stream_forfeits(http_runtime, monkeypatch):
    client, session_factory, _ = http_runtime
    fx = _Fixture(session_factory, monkeypatch)

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _sse(CONTENT_CHUNK, done=False)
            raise httpx.ReadError("connection reset")

    with _upstream(lambda request: httpx.Response(200, stream=Broken(), headers={"content-type": "text/event-stream"})):
        response = client.post(GATEWAY, json=_request(), headers={"Authorization": fx.bearer()})
    assert response.status_code == 200  # headers were already sent; the body is truncated
    rows = fx.reservations()
    assert [row["state"] for row in rows] == ["FORFEITED"]


def test_non_streaming_call_settles_from_the_body(http_runtime, monkeypatch):
    client, session_factory, _ = http_runtime
    fx = _Fixture(session_factory, monkeypatch)
    upstream_json = {
        "id": "x", "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 20},
    }
    with _upstream(lambda request: httpx.Response(200, json=upstream_json)):
        response = client.post(GATEWAY, json=_request(stream=False), headers={"Authorization": fx.bearer()})
    assert response.status_code == 200 and response.json()["choices"][0]["message"]["content"] == "hi"
    rows = fx.reservations()
    assert [row["state"] for row in rows] == ["SETTLED"]
    assert rows[0]["charged_micro_usd"] == 30 + 20 * 4


def test_exhausted_budget_is_a_402_without_a_reservation(http_runtime, monkeypatch):
    client, session_factory, _ = http_runtime
    fx = _Fixture(session_factory, monkeypatch, limit_micro_usd=50)
    calls = []
    with _upstream(lambda request: calls.append(1) or httpx.Response(200, content=_sse(USAGE_CHUNK))):
        response = client.post(GATEWAY, json=_request(), headers={"Authorization": fx.bearer()})
    assert response.status_code == 402, response.text
    error = response.json()["error"]
    assert error["type"] == "insufficient_quota" and error["code"] == "quota_exhausted"
    assert "budget is used up" in error["message"]
    assert calls == []  # nothing reached the provider
    assert fx.reservations() == []
    balance = fx.balance()
    assert balance.refused_count == 1 and balance.reserved_micro_usd == 0
    assert balance.exhausted_at is not None


def test_missing_price_is_a_503_and_nothing_is_reserved(http_runtime, monkeypatch):
    client, session_factory, _ = http_runtime
    fx = _Fixture(session_factory, monkeypatch, price=False)
    with _upstream(lambda request: httpx.Response(200, content=_sse(USAGE_CHUNK))):
        response = client.post(GATEWAY, json=_request(), headers={"Authorization": fx.bearer()})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "price_missing"
    assert fx.reservations() == []


@pytest.mark.parametrize(
    "case",
    ["missing", "garbage", "tampered", "expired", "stale_epoch", "wrong_audience", "wrong_secret"],
)
def test_invalid_capabilities_are_401(http_runtime, monkeypatch, case):
    client, session_factory, _ = http_runtime
    fx = _Fixture(session_factory, monkeypatch)
    headers = {}
    if case == "garbage":
        headers = {"Authorization": "Bearer sk-not-a-capability"}
    elif case == "tampered":
        headers = {"Authorization": fx.bearer(scope=["inference:relay", "telemetry:write"])}
    elif case == "expired":
        headers = {"Authorization": fx.bearer(ttl_seconds=1, now=datetime.now(timezone.utc) - timedelta(hours=1))}
    elif case == "stale_epoch":
        headers = {"Authorization": fx.bearer(revocation_epoch=5)}
    elif case == "wrong_audience":
        session_capability = issue_capability(
            "research-runtime", ["telemetry:write"], 3600, 0, SECRET,
            enrollment_id=fx.enrollment_id, research_session_id=fx.session_id, study_id=fx.study_id,
        )
        headers = {"Authorization": "Bearer " + encode_capability_bearer(session_capability)}
    elif case == "wrong_secret":
        headers = {"Authorization": fx.bearer(secret="another-secret")}
    with _upstream(lambda request: httpx.Response(200, content=_sse(USAGE_CHUNK))):
        response = client.post(GATEWAY, json=_request(), headers=headers)
    assert response.status_code == 401, (case, response.text)
    assert response.json()["detail"]["code"] == "CAPABILITY_INVALID"
    assert fx.reservations() == []


def test_revoked_enrollment_and_engaged_kill_switch_are_403(http_runtime, monkeypatch):
    client, session_factory, _ = http_runtime
    fx = _Fixture(session_factory, monkeypatch)
    from research.analysis.operations import store as operations_store
    from research.analysis.operations.models import (
        KillSwitchRecord,
        KillSwitchScope,
        KillSwitchScopeKind,
    )

    # An ENROLLMENT-scoped switch (the scope the old relay gate never consulted).
    with session_factory() as db:
        record = operations_store.engage_kill_switch(
            db,
            KillSwitchRecord(
                switch_id=uuid.uuid4(),
                scope=KillSwitchScope(kind=KillSwitchScopeKind.ENROLLMENT, scope_id=fx.enrollment_id),
                reason="test",
                actor="admin@example.com",
                engaged_at=datetime.now(timezone.utc),
            ),
        )
    with _upstream(lambda request: httpx.Response(200, content=_sse(USAGE_CHUNK))):
        response = client.post(GATEWAY, json=_request(), headers={"Authorization": fx.bearer()})
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "KILL_SWITCH_ENGAGED"
    assert fx.reservations() == []

    with session_factory() as db:
        operations_store.release_kill_switch(db, record.switch_id, released_at=datetime.now(timezone.utc))
    with _upstream(lambda request: httpx.Response(200, content=_sse(CONTENT_CHUNK, STOP_CHUNK, USAGE_CHUNK), headers={"content-type": "text/event-stream"})):
        response = client.post(GATEWAY, json=_request(), headers={"Authorization": fx.bearer()})
    assert response.status_code == 200, response.text

    with session_factory() as db:
        db.execute(
            text("UPDATE public.research_enrollment SET status = 'REVOKED', revocation_epoch = revocation_epoch + 1 WHERE enrollment_id = :e"),
            {"e": fx.enrollment_id},
        )
        db.commit()
    with _upstream(lambda request: httpx.Response(200, content=_sse(USAGE_CHUNK))):
        response = client.post(GATEWAY, json=_request(), headers={"Authorization": fx.bearer()})
    assert response.status_code == 401  # epoch bumped: the capability is stale
    assert [row["state"] for row in fx.reservations()] == ["SETTLED"]


def test_codex_arm_is_refused_and_responses_bodies_are_400(http_runtime, monkeypatch):
    client, session_factory, _ = http_runtime
    fx = _Fixture(session_factory, monkeypatch, framework="codex")
    with _upstream(lambda request: httpx.Response(200, content=_sse(USAGE_CHUNK))):
        response = client.post(GATEWAY, json=_request(), headers={"Authorization": fx.bearer()})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "RUNTIME_NOT_SUPPORTED"
    response = client.post(GATEWAY, json={"input": [], "model": "x"}, headers={"Authorization": fx.bearer()})
    assert response.status_code == 400
    assert fx.reservations() == []


def test_client_disconnect_after_the_first_chunk_forfeits(http_runtime, monkeypatch):
    """The generator is closed from the consumer's side (aclose): the hold is forfeited."""
    from agents import inference
    from agents import provider as provider_module
    from database import crud
    from research.budget import InferenceMeter

    from .test_research_api_contract import RuntimeApp

    _, session_factory, _ = http_runtime
    fx = _Fixture(session_factory, monkeypatch)
    runtime = RuntimeApp(session_factory)
    with session_factory() as db:
        connection = provider_module.connection_view(crud.get_provider_connection(db, fx.connection_id))
    meter = InferenceMeter(
        app=runtime, enrollment_id=fx.enrollment_id, study_id=fx.study_id,
        connection_id=fx.connection_id, model=MODEL, entry_point="research_gateway",
    )

    class TwoChunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _sse(CONTENT_CHUNK, done=False)
            yield _sse(STOP_CHUNK, USAGE_CHUNK)  # never consumed by the disconnected client

    async def scenario():
        with _upstream(lambda request: httpx.Response(200, stream=TwoChunks(), headers={"content-type": "text/event-stream"})):
            response = await inference.run_inference(
                task_uuid=uuid.uuid4(), session_uuid=uuid.uuid4(), openai_body=_request(), enrichment=None,
                agent_profile="arm", model=MODEL, connection=connection, temperature=None,
                framework_version="goose", profile_tools_json=None, content_included=False,
                record_observation_events=False, tool_filtering=False, meter=meter, app=runtime,
            )
            iterator = response.body_iterator
            first = await iterator.__anext__()
            assert b"hi" in first
            await iterator.aclose()  # the client went away after the first chunk

    asyncio.run(scenario())
    rows = fx.reservations()
    assert [row["state"] for row in rows] == ["FORFEITED"]
    assert rows[0]["resolution_reason"] == "usage_missing"
    assert rows[0]["charged_micro_usd"] == rows[0]["hold_micro_usd"]
    assert fx.balance().reserved_micro_usd == 0


def test_stream_wall_clock_cap_forfeits_a_hung_provider(http_runtime, monkeypatch):
    from research.budget.settings import BudgetSettings

    client, session_factory, _ = http_runtime
    fx = _Fixture(session_factory, monkeypatch)
    monkeypatch.setattr(
        BudgetSettings, "from_env", classmethod(lambda cls, environ=None: BudgetSettings(stream_total_timeout_seconds=1))
    )

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _sse(CONTENT_CHUNK, done=False)
            await asyncio.sleep(1.2)
            yield _sse(CONTENT_CHUNK, done=False)
            yield _sse(STOP_CHUNK, USAGE_CHUNK)

    with _upstream(lambda request: httpx.Response(200, stream=Slow(), headers={"content-type": "text/event-stream"})):
        response = client.post(GATEWAY, json=_request(), headers={"Authorization": fx.bearer()})
    assert response.status_code == 200
    rows = fx.reservations()
    assert [row["state"] for row in rows] == ["FORFEITED"]
    assert rows[0]["resolution_reason"] == "stream_timeout"
