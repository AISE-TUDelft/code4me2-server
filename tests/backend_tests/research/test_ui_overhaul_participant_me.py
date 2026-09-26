"""``GET /api/research/participants/me`` — the participant's "My studies" view.

Additive over the study-local projection the IntelliJ plugin parses: each
enrollment adds ``consent_accepted_at``, the public ``study`` facts with the
frozen collection policy, the ``runtime`` kind, and ``sessions`` / ``activity``
summaries. The participant stays blind to the arm: no profile name/id, model
or digest is ever returned.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

from backend.routers.research.participants import _my_study_payload
from research.analysis.study_analytics import metrics as analytics_metrics
from research.analysis.study_analytics import store as analytics_store
from research.participants.identity import participant_runtime_view

from ._ui_overhaul_seed import (
    now,
    parse_iso,
    participant_user,
    seed_account,
    seed_assignment,
    seed_enrollment,
    seed_event,
    seed_participant,
    seed_profile,
    seed_research_session,
    seed_study,
)
from .test_research_api_contract import http_runtime  # noqa: F401 - fixture

ME_PATH = "/api/research/participants/me"
PROJECTION_KEYS = {
    "enrollment_id",
    "participant_code",
    "study_id",
    "status",
    "eligible",
    "revocation_epoch",
    "retention_action",
    "enrolled_at",
    "updated_at",
}
ADDED_KEYS = {"consent_accepted_at", "study", "runtime", "sessions", "activity", "budget"}
TELEMETRY_POLICY = {
    "allowed_field_classes": ["STRUCTURAL", "METRICS", "CONTENT"],
    "content_capture": True,
}


def _seed_my_studies(session) -> SimpleNamespace:
    t0 = now()
    owner = seed_account(session, "me-owner@example.com", can_research=True)
    me = seed_account(session, "me-participant@example.com")
    other = seed_account(session, "me-other@example.com")
    study = seed_study(
        session,
        owner_id=owner,
        name="Agent Onboarding Study",
        description="How developers use a coding agent.",
        telemetry_policy=TELEMETRY_POLICY,
        starts_at=t0 - timedelta(days=7),
        ends_at=t0 + timedelta(days=21),
        join_code="UIMYSTDY",
    )
    arm = seed_profile(
        session,
        owner_id=owner,
        framework_version="goose",
        name="secret-arm-profile",
        model="secret-model-x",
    )
    my_participant = seed_participant(session, me)
    other_participant = seed_participant(session, other)

    # An older, completed enrollment whose study row no longer exists.
    missing_study = uuid.uuid4()
    orphan = seed_enrollment(
        session,
        participant_id=my_participant,
        study_id=missing_study,
        status="COMPLETED",
        enrolled_at=t0 - timedelta(days=30),
    )
    enrollment = seed_enrollment(
        session,
        participant_id=my_participant,
        study_id=study,
        status="ACTIVE",
        enrolled_at=t0 - timedelta(days=2),
        consent_accepted_at=t0 - timedelta(days=2),
        participant_code="p_mine",
    )
    assignment = seed_assignment(
        session,
        enrollment_id=enrollment,
        study_id=study,
        profile_id=arm,
        snapshot={
            "profile_id": str(arm),
            "name": "secret-arm-profile",
            "model": "secret-model-x",
            "framework_version": "goose",
            "tools_json": "[]",
            "approval_policy": "auto",
            "max_steps": 5,
        },
        profile_digest="secret-digest-123",
    )
    running = seed_research_session(
        session,
        enrollment_id=enrollment,
        study_id=study,
        state="running",
        last_activity_at=t0 - timedelta(hours=1),
    )
    ended = seed_research_session(
        session,
        enrollment_id=enrollment,
        study_id=study,
        state="ended",
        last_activity_at=t0 - timedelta(hours=5),
    )

    sequence = iter(range(1, 100))

    def event(event_type, minutes_ago, *, in_session="running", **kwargs):
        return seed_event(
            session,
            enrollment_id=enrollment,
            study_id=study,
            research_session_id=running if in_session == "running" else in_session,
            event_type=event_type,
            sequence=next(sequence),
            occurred_at=t0 - timedelta(minutes=minutes_ago),
            **kwargs,
        )

    event("agent.message.started", 50)
    # Streamed assistant/thought chunks share the prompt event type (the ACP
    # proxy marks them with message_kind or the "started" lifecycle state).
    event("agent.message.started", 49, payload={"message_kind": "agent_message_chunk"})
    event("agent.message.started", 49, payload={"message_kind": "agent_thought_chunk"})
    event("agent.message.started", 49, lifecycle_state="started")
    event("tool.created", 49, tool_call_id="call-1")
    event("tool.started", 48, tool_call_id="call-1")
    event("tool.completed", 47, tool_call_id="call-1")
    event("tool.started", 46, tool_call_id="call-2")
    event("tool.failed", 45, tool_call_id="call-2")
    # The proxy's tool_call_update often carries the id in the payload only.
    event("tool.created", 44, payload={"tool_call_id": "call-4"})
    event("tool.completed", 43, payload={"tool_call_id": "call-4"})
    # terminal/create has no id: it belongs to the execute call that owns it.
    event("tool.started", 43)
    # An empty id is no id.
    event("tool.created", 43, payload={"tool_call_id": ""})
    # Ids are compared whole: these are two calls, whatever the separators.
    event("tool.created", 43, emitter_id="x", tool_call_id="a|b")
    event("tool.created", 43, emitter_id="x|a", tool_call_id="b")
    # The relay also reports a call of this ACP-observed session: the proxy's
    # calls are the authoritative ones there, so it is not counted again.
    event("tool.completed", 42, source="relay", emitter_id="relay", tool_call_id="call-1")
    # A session without ACP calls counts the relay's: one per id-less event and
    # one per id across the relay's emitters. Relay model calls are never prompts.
    event("tool.completed", 42, in_session=ended, source="relay", emitter_id="relay")
    event("tool.completed", 42, in_session=ended, source="relay", emitter_id="relay", tool_call_id="r-1")
    event("tool.completed", 42, in_session=ended, source="relay", emitter_id="relay-2", tool_call_id="r-1")
    event("agent.message.started", 41, in_session=ended, source="relay", emitter_id="relay")
    # Events without a research session form one group of their own: an ACP
    # call there makes the relay's copy of it redundant.
    event("tool.created", 41, in_session=None, emitter_id="proxy-n", tool_call_id="n-1")
    event("tool.completed", 41, in_session=None, source="relay", emitter_id="relay", tool_call_id="n-1")
    event("agent.message.started", 40)
    event("agent.message.completed", 30)
    # Retention tombstones never count, even as the latest event.
    event("agent.message.started", 5, retention_state="DELETED")
    event("tool.created", 4, tool_call_id="call-3", retention_state="DELETED")

    # Another participant's activity in the same study stays out of my view.
    theirs = seed_enrollment(
        session, participant_id=other_participant, study_id=study, participant_code="p_theirs"
    )
    seed_event(
        session,
        enrollment_id=theirs,
        study_id=study,
        research_session_id=None,
        event_type="agent.message.started",
        sequence=1,
        occurred_at=t0,
        emitter_id="other-emitter",
    )
    return SimpleNamespace(
        t0=t0,
        me=me,
        study=study,
        arm=arm,
        orphan=orphan,
        missing_study=missing_study,
        enrollment=enrollment,
        assignment=assignment,
    )


def test_my_studies_adds_study_runtime_sessions_and_activity(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_my_studies(session)
    current_user["value"] = participant_user(seeded.me)

    response = client.get(ME_PATH)
    assert response.status_code == 200, response.text
    enrollments = response.json()["enrollments"]
    # Oldest first, as before; only the caller's own enrollments.
    assert [item["enrollment_id"] for item in enrollments] == [
        str(seeded.orphan),
        str(seeded.enrollment),
    ]
    orphan, mine = enrollments
    for item in enrollments:
        assert set(item) == PROJECTION_KEYS | ADDED_KEYS

    # The existing projection fields the plugin parses are unchanged.
    assert mine["study_id"] == str(seeded.study)
    assert mine["status"] == "ACTIVE"
    assert mine["participant_code"] == "p_mine"
    assert parse_iso(mine["consent_accepted_at"]) == seeded.t0 - timedelta(days=2)

    assert mine["study"] == {
        "study_id": str(seeded.study),
        "name": "Agent Onboarding Study",
        "description": "How developers use a coding agent.",
        "research_status": "ACTIVE",
        "starts_at": mine["study"]["starts_at"],
        "ends_at": mine["study"]["ends_at"],
        "collection": {
            "content_capture": True,
            "allowed_field_classes": ["STRUCTURAL", "METRICS", "CONTENT"],
        },
    }
    assert parse_iso(mine["study"]["starts_at"]) == seeded.t0 - timedelta(days=7)
    assert parse_iso(mine["study"]["ends_at"]) == seeded.t0 + timedelta(days=21)
    assert mine["runtime"] == {
        "framework_version": "goose",
        "display_name": "Goose (install on your machine)",
        # The study provides model access through its own key (shared);
        # Codex would be "own" (ChatGPT login).
        "credentials": "shared",
    }
    # No balance row was seeded for this enrollment, so the budget is null and
    # no arm detail leaks through it.
    assert mine["budget"] is None
    assert mine["sessions"]["total"] == 2
    assert mine["sessions"]["active"] == 1
    assert parse_iso(mine["sessions"]["last_activity_at"]) == seeded.t0 - timedelta(hours=1)
    # Two real prompts (no chunks, no relay rows). Tool calls: call-1, call-2,
    # call-4 and the two separator ids in the ACP session, the relay-only
    # session's id-less execution and r-1 (once across both emitters), and the
    # session-less ACP call n-1.
    assert mine["activity"]["prompts"] == 2
    assert mine["activity"]["tool_calls"] == 8
    # The researcher's analytics count the same enrollment the same way.
    with session_factory() as db:
        rows = analytics_store.load_events(db, seeded.study, enrollment_id=seeded.enrollment)
    analysis = analytics_metrics.analyze_participant(rows, [])
    assert (len(analysis.prompts), len(analysis.tool_calls)) == (2, 8)
    assert parse_iso(mine["activity"]["last_event_at"]) == seeded.t0 - timedelta(minutes=30)

    # A missing study row, no assignment and no telemetry degrade to null/zero.
    assert orphan["study_id"] == str(seeded.missing_study)
    assert orphan["consent_accepted_at"] is None
    assert orphan["study"] is None
    assert orphan["runtime"] is None
    assert orphan["sessions"] == {"total": 0, "active": 0, "last_activity_at": None}
    assert orphan["activity"] == {"prompts": 0, "tool_calls": 0, "last_event_at": None}

    # Blind to the arm: no profile identity, model or digest.
    for secret in (
        "secret-arm-profile",
        "secret-model-x",
        "secret-digest-123",
        str(seeded.arm),
        str(seeded.assignment),
        "p_theirs",
    ):
        assert secret not in response.text


def test_collection_matches_the_consent_shown_at_join(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_my_studies(session)
    current_user["value"] = participant_user(seeded.me)

    mine = client.get(ME_PATH).json()["enrollments"][1]
    consent = client.get("/api/research/join/UIMYSTDY")
    assert consent.status_code == 200, consent.text
    assert mine["study"]["collection"] == consent.json()["consent"]["collection_policy"]
    # The notice is composed from the same frozen policy.
    from backend.routers.research.join import consent_text

    assert consent.json()["consent"]["text"] == consent_text(
        {"allowed_field_classes": mine["study"]["collection"]["allowed_field_classes"], "content_capture": True}
    )
    assert "tool calls and its error messages" in consent.json()["consent"]["text"]


def test_http_content_capture_is_the_explicit_flag_only(http_runtime):
    """Flag true → captured; CONTENT listed without the flag → not captured.

    ``allowed_field_classes`` is the raw frozen list (empty when the policy
    declares none), never an invented server default.
    """
    client, session_factory, current_user = http_runtime
    t0 = now()
    policies = {
        "flagged": {"allowed_field_classes": ["STRUCTURAL"], "content_capture": True},
        "listed-only": {"allowed_field_classes": ["STRUCTURAL", "CONTENT"]},
        "undeclared": {"metadata_only": True},
    }
    with session_factory() as session:
        owner = seed_account(session, "flag-owner@example.com", can_research=True)
        me = seed_account(session, "flag-participant@example.com")
        participant = seed_participant(session, me)
        for offset, (name, policy) in enumerate(policies.items()):
            study = seed_study(
                session,
                owner_id=owner,
                name=name,
                research_status="STUDY_STOPPED",
                telemetry_policy=policy,
            )
            seed_enrollment(
                session,
                participant_id=participant,
                study_id=study,
                status="COMPLETED",
                enrolled_at=t0 - timedelta(days=10 - offset),
            )
    current_user["value"] = participant_user(me)

    response = client.get(ME_PATH)
    assert response.status_code == 200, response.text
    collections = {
        item["study"]["name"]: item["study"]["collection"]
        for item in response.json()["enrollments"]
    }
    assert collections == {
        "flagged": {"content_capture": True, "allowed_field_classes": ["STRUCTURAL"]},
        "listed-only": {
            "content_capture": False,
            "allowed_field_classes": ["STRUCTURAL", "CONTENT"],
        },
        "undeclared": {"content_capture": False, "allowed_field_classes": []},
    }


def test_content_capture_follows_the_declared_flag_like_consent():
    """CONTENT in the class list without the explicit flag is not captured.

    Ingestion (``PrivacyPolicy.from_study_policy``) and the join consent text
    both key content capture off ``content_capture``; the view must agree.
    """
    study = SimpleNamespace(
        study_id=uuid.uuid4(),
        name="n",
        description=None,
        research_status="DRAFT",
        starts_at=None,
        ends_at=None,
        research_config_json={"telemetry_policy": {"allowed_field_classes": ["CONTENT"]}},
    )
    assert _my_study_payload(study)["collection"] == {
        "content_capture": False,
        "allowed_field_classes": ["CONTENT"],
    }
    assert _my_study_payload(None) is None


def test_runtime_view_names_only_the_runtime_kind():
    assert participant_runtime_view({"framework_version": "code4me2-agent"}) == {
        "framework_version": "code4me2-agent",
        "display_name": "Code4Me agent (built-in)",
        "credentials": "shared",
    }
    assert participant_runtime_view(
        {"framework_version": "codex", "name": "arm", "model": "m"}
    ) == {
        "framework_version": "codex",
        "display_name": "Codex (install on your machine)",
        "credentials": "own",
    }
    assert participant_runtime_view({"framework_version": "other-agent"}) == {
        "framework_version": "other-agent",
        "display_name": "other-agent",
        "credentials": "own",
    }
    assert participant_runtime_view({}) is None
    assert participant_runtime_view(None) is None


def test_account_without_participant_mapping_has_no_enrollments(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        account = seed_account(session, "me-nobody@example.com")
    current_user["value"] = participant_user(account)
    response = client.get(ME_PATH)
    assert response.status_code == 200, response.text
    assert response.json() == {"enrollments": []}
