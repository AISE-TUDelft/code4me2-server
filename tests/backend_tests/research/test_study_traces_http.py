"""HTTP contract of the owner-only chat trace."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from research.telemetry.ingestion.models import IngestionContext
from research.telemetry.ingestion.service import _record_from_event, compute_event_digest
from research.telemetry.ingestion.store import SqlAlchemyIngestionStore
from research.telemetry.models import (
    CanonicalEventV1,
    Correlations,
    Coverage,
    EventMetrics,
    Provenance,
)

from .test_research_api_contract import (  # noqa: F401 - the fixture is used by name
    VALID_SESSION_POLICY,
    _owner,
    _participant,
    _profile,
    _seed_user,
    http_runtime,
)

T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
CANARY = "TRACE-CANARY prompt text"


def _setup(client, session_factory, current_user, *, content_capture: bool):
    session = session_factory()
    try:
        owner_id = _seed_user(session, "trace-owner@example.com", can_research=True)
        other_id = _seed_user(session, "trace-other@example.com", can_research=True)
        participants = [_seed_user(session, f"trace-p{index}@example.com") for index in range(2)]
        profile_id = _profile(session, owner_id)
    finally:
        session.close()
    policy = (
        {"allowed_field_classes": ["STRUCTURAL", "BEHAVIORAL", "CONTENT"], "content_capture": True}
        if content_capture
        else {}
    )
    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "Trace study",
            "default_budget_usd": "10",
            "session_policy": VALID_SESSION_POLICY,
            "telemetry_policy": policy,
            "profile_ids": [str(profile_id)],
        },
    )
    assert created.status_code == 201, created.text
    study = created.json()["study"]
    enrollments = []
    for user_id in participants:
        current_user["value"] = _participant(user_id)
        joined = client.post(
            "/api/research/join", json={"join_code": study["join_code"], "accept_consent": True}
        )
        assert joined.status_code == 201, joined.text
        enrollments.append(joined.json()["enrollment_id"])
    current_user["value"] = _owner(owner_id)
    return owner_id, other_id, study["study_id"], enrollments


def _seed(session_factory, study_id: str, enrollment_id: str, events: list[dict]) -> list[str]:
    session = session_factory()
    try:
        research_session_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO public.research_session "
                "(session_id, enrollment_id, study_id, context_id, state, manifest_digest, "
                "environment_json, transitions_json, created_at, opened_at, last_activity_at) "
                "VALUES (:session_id, :enrollment_id, :study_id, :context_id, 'running', "
                "'manifest', '{}', '[]', now(), :opened_at, :last_activity_at)"
            ),
            {
                "session_id": research_session_id,
                "enrollment_id": enrollment_id,
                "study_id": study_id,
                "context_id": f"trace-ctx-{research_session_id.hex[:8]}",
                # One hour of session time, so per-hour metrics have a denominator.
                "opened_at": T0,
                "last_activity_at": T0 + timedelta(hours=1),
            },
        )
        session.commit()
        records = []
        ids = []
        for sequence, spec in enumerate(events, start=1):
            event = CanonicalEventV1(
                event_id=uuid.uuid4(),
                schema_version="1",
                event_type=spec["type"],
                source=spec.get("source", "acp"),
                study_id=uuid.UUID(study_id),
                enrollment_id=uuid.UUID(enrollment_id),
                research_session_id=research_session_id,
                occurred_at=T0 + timedelta(seconds=spec["at"]),
                emitter_id="trace-proxy",
                emitter_sequence=sequence,
                correlations=Correlations(**spec.get("corr", {})),
                lifecycle_state=spec.get("lifecycle"),
                # ``chat: None`` seeds an event without a chat id (the agent's own reports).
                payload={
                    **({"session_id": spec.get("chat", "chat-1")} if spec.get("chat", "chat-1") is not None else {}),
                    **spec.get("payload", {}),
                },
                metrics=EventMetrics(**spec.get("metrics", {})),
                provenance=Provenance(source=spec.get("source", "acp"), normalizer_version="generic-acp-v2"),
                coverage=Coverage(state="AVAILABLE", capability="acp"),
            )
            context = IngestionContext(
                study_id=uuid.UUID(study_id),
                enrollment_id=uuid.UUID(enrollment_id),
                research_session_id=research_session_id,
                revocation_epoch=0,
            )
            records.append(_record_from_event(event, context, compute_event_digest(event), accepted_at=T0))
            ids.append(str(event.event_id))
        store = SqlAlchemyIngestionStore(session)
        store.insert_events(records)
        store.commit()
        return ids
    finally:
        session.close()


def _chat(prompt_text) -> list[dict]:
    return [
        {"type": "interaction.started", "at": 0, "payload": {"acp_method": "session/new"}},
        {"type": "agent.message.started", "at": 1, "corr": {"turn_id": "1"}},
        {
            "type": "agent.message.started",
            "at": 1,
            "lifecycle": "started",
            "corr": {"turn_id": "1"},
            "payload": {"message_kind": "user", "prompt": prompt_text},
        },
        {
            "type": "agent.message.started",
            "at": 2,
            "lifecycle": "started",
            "payload": {"message_kind": "thought", "message_id": "m1", "reasoning": {"type": "text", "text": "thinking"}},
        },
        {"type": "agent.message.completed", "at": 3, "payload": {"stop_reason": "end_turn"}},
        {"type": "agent.message.started", "at": 10, "corr": {"turn_id": "2"}},
        {"type": "agent.message.completed", "at": 11, "payload": {"stop_reason": "end_turn"}},
        {"type": "agent.message.started", "at": 20, "corr": {"turn_id": "3"}},
        {"type": "agent.message.completed", "at": 21, "payload": {"stop_reason": "cancelled"}},
    ]


def _url(study_id, enrollment_id):
    return f"/api/research/studies/{study_id}/enrollments/{enrollment_id}/trace"


def test_owner_reads_a_paged_chat_trace_and_no_one_else_does(http_runtime):
    client, session_factory, current_user = http_runtime
    owner_id, other_id, study_id, (first, second) = _setup(
        client, session_factory, current_user, content_capture=True
    )
    ids = _seed(session_factory, study_id, first, _chat([{"type": "text", "text": CANARY}]))
    # Another chat and another enrollment never leak into this trace.
    _seed(session_factory, study_id, first, [{"type": "agent.message.started", "at": 5, "chat": "chat-2", "corr": {"turn_id": "9"}}])
    _seed(session_factory, study_id, second, _chat([{"type": "text", "text": "other participant"}]))
    session = session_factory()
    try:
        # A retention tombstone is never shown.
        session.execute(
            text("UPDATE public.research_event SET retention_state = 'DELETED' WHERE event_id = :id"),
            {"id": ids[3]},
        )
        session.commit()
    finally:
        session.close()

    response = client.get(_url(study_id, first), params={"chat_id": "chat-1", "limit": 2})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["content_capture_enabled"] is True
    assert body["participant_code"].startswith("p_")
    assert [turn["kind"] for turn in body["turns"]] == ["preamble", "turn", "turn"]
    assert [turn["index"] for turn in body["turns"]] == [0, 1, 2]
    first_turn = body["turns"][1]
    assert first_turn["blocks"][0]["text"] == CANARY
    # The tombstoned thought chunk is gone.
    assert [block["type"] for block in first_turn["blocks"]] == ["prompt"]
    serialized = json.dumps(body)
    assert "other participant" not in serialized and "@example.com" not in serialized
    assert "account_id" not in serialized and "participant_id" not in serialized
    assert body["has_more"] is True

    page_two = client.get(
        _url(study_id, first), params={"chat_id": "chat-1", "limit": 2, "cursor": body["next_cursor"]}
    ).json()
    assert [turn["index"] for turn in page_two["turns"]] == [3]
    assert page_two["turns"][0]["stop_reason"] == "cancelled"
    assert page_two["has_more"] is False and page_two["next_cursor"] is None

    assert client.get(_url(study_id, first), params={"chat_id": "nope"}).json()["detail"]["code"] == "CHAT_NOT_FOUND"
    bad = client.get(_url(study_id, first), params={"chat_id": "chat-1", "cursor": "not-a-cursor"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "INVALID_CURSOR"
    assert client.get(_url(study_id, str(uuid.uuid4())), params={"chat_id": "chat-1"}).status_code == 404

    current_user["value"] = _owner(other_id)
    assert client.get(_url(study_id, first), params={"chat_id": "chat-1"}).status_code == 403


def test_a_metadata_only_study_shows_structure_without_text(http_runtime):
    client, session_factory, current_user = http_runtime
    _, _, study_id, (first, _) = _setup(client, session_factory, current_user, content_capture=False)
    _seed(session_factory, study_id, first, _chat("[REDACTED]"))

    body = client.get(_url(study_id, first), params={"chat_id": "chat-1"}).json()
    assert body["content_capture_enabled"] is False
    prompt_block, thought = body["turns"][1]["blocks"]
    assert prompt_block["text"] is None and prompt_block["redacted"] is True
    assert thought["type"] == "thought"


def test_the_built_in_agents_revise_report_reaches_the_trace(http_runtime):
    client, session_factory, current_user = http_runtime
    _, _, study_id, (first, _) = _setup(client, session_factory, current_user, content_capture=True)
    blob = json.dumps({"payload": {"tool_call_id": "t1", "text": CANARY}})
    _seed(
        session_factory,
        study_id,
        first,
        [
            {"type": "agent.message.started", "at": 1, "corr": {"turn_id": "1"}},
            {
                "type": "tool.created",
                "at": 2,
                "corr": {"tool_call_id": "t1"},
                "payload": {"tool_call_id": "t1", "tool_name": "Write a.py", "tool_kind": "edit"},
            },
            {
                "type": "permission.decided",
                "at": 3,
                "corr": {"tool_call_id": "t1"},
                "payload": {"decision": "reject", "selected_option_id": "revise"},
            },
            # The agent's own report has no chat id: it joins through its tool call.
            {
                "type": "permission.decided",
                "at": 4,
                "source": "relay",
                "chat": None,
                "corr": {"tool_call_id": "t1"},
                "payload": {"decision": "revised", "elicitation_action": "accept", "revise_status": "applied", "payload": blob},
                "metrics": {"counts": {"hunk_count": 3, "kept_hunk_count": 2}},
            },
            {"type": "tool.failed", "at": 5, "corr": {"tool_call_id": "t1"}, "payload": {"tool_call_id": "t1", "status": "failed"}},
            # A report about another chat's tool call never joins this trace.
            {
                "type": "permission.decided",
                "at": 6,
                "source": "relay",
                "chat": None,
                "corr": {"tool_call_id": "elsewhere"},
                "payload": {"decision": "revised", "elicitation_action": "decline"},
            },
        ],
    )
    body = client.get(_url(study_id, first), params={"chat_id": "chat-1"}).json()
    tools = [block for turn in body["turns"] for block in turn["blocks"] if block["type"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["permission"]["decision"] == "revise"
    assert tools[0]["permission"]["revision"] == {
        "form": "accept",
        "status": "applied",
        "hunks": 3,
        "kept_hunks": 2,
        "instructions": {"text": CANARY, "redacted": False, "truncated": False},
    }
    assert "decline" not in json.dumps(body)
