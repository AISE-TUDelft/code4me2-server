"""Idle-expired context sessions rotate at bootstrap/session create (D-01/C-02).

A returning client used to be handed its previous context session even when
that session had already exceeded the study's idle timeout; the first heartbeat
then ended it and collection stopped until the participant acted. Over real
HTTP against PostgreSQL: the idle session is ended (``idle_timeout``), a fresh
one is opened, and the ended session's tail is still accepted under the new
capability while later events are rejected ``SESSION_TERMINAL``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import text

from backend.routers.research.bootstrap import BOOTSTRAP_SIGNING_SECRET
from research.runtime.bootstrap.service import BootstrapSigningContext

from .test_study_lifecycle_bootstrap_session_telemetry_http import (  # noqa: F401 - fixture import
    _owner,
    _participant,
    _qualified_packaged_release,
    _release_profile,
    _seed_user,
    _telemetry_event,
    http_runtime,
)

CONTEXT = "idle-rotation-context"


def _bootstrap(client, enrollment_id: str):
    signing_secret = BOOTSTRAP_SIGNING_SECRET
    assert signing_secret, "BOOTSTRAP_SIGNING_SECRET must be configured for this suite"
    with patch(
        "backend.routers.research.bootstrap._SIGNER",
        BootstrapSigningContext(secret=signing_secret),
    ):
        response = client.post(
            "/api/research/bootstrap/research-sessions",
            json={
                "enrollment_id": enrollment_id,
                "context_id": CONTEXT,
                "environment": {
                    "os": "macos",
                    "arch": "arm64",
                    "ai_assistant_version": "262.8665.344",
                },
            },
        )
    assert response.status_code == 201, response.text
    return response.json()["manifest"]


def _session_row(session_factory, session_id: str):
    session = session_factory()
    try:
        return session.execute(
            text(
                "SELECT state, close_reason, closed_at FROM public.research_session "
                "WHERE session_id = :id"
            ),
            {"id": session_id},
        ).one()
    finally:
        session.close()


def test_http_idle_expired_context_session_rotates_and_its_tail_is_accepted(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "idle-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "idle-participant@example.com")
        release_id = _qualified_packaged_release(session)
        profile_id, _connection_id = _release_profile(session, owner_id, release_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "Idle rotation study",
            "default_budget_usd": "10",
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

    first_manifest = _bootstrap(client, enrollment_id)
    first_capability = first_manifest["session_capability"]
    first_session_id = first_manifest["research_session"]["research_session_id"]

    # Start the session with real activity, then let it go idle for two hours.
    activity = client.post(
        "/api/research/sessions/activity",
        json={"capability": first_capability, "research_session_id": first_session_id},
    )
    assert activity.status_code == 200, activity.text
    assert activity.json()["session"]["state"] == "running"
    db = session_factory()
    try:
        db.execute(
            text(
                "UPDATE public.research_session SET last_activity_at = :stale "
                "WHERE session_id = :id"
            ),
            {"stale": datetime.now(timezone.utc) - timedelta(hours=2), "id": first_session_id},
        )
        db.commit()
    finally:
        db.close()

    # Session create for the same context ends the idle session and opens a
    # fresh one instead of handing the stale one back.
    reopened = client.post(
        "/api/research/sessions/",
        json={
            "capability": first_capability,
            "enrollment_id": enrollment_id,
            "study_id": study_id,
            "manifest_digest": first_manifest["manifest_digest"],
            "context_id": CONTEXT,
        },
    )
    assert reopened.status_code == 201, reopened.text
    assert reopened.json()["created"] is True
    second_session_id = reopened.json()["session"]["research_session_id"]
    assert second_session_id != first_session_id
    ended = _session_row(session_factory, first_session_id)
    assert ended.state == "ended"
    assert ended.close_reason == "idle_timeout"
    assert ended.closed_at is not None

    # Bootstrap now names the fresh session (not the ended one) and issues a
    # capability bound to it.
    second_manifest = _bootstrap(client, enrollment_id)
    assert second_manifest["research_session"]["research_session_id"] == second_session_id
    second_capability = second_manifest["session_capability"]
    assert second_capability["research_session_id"] == second_session_id

    # The old session is terminal for liveness signals ...
    stale_heartbeat = client.post(
        "/api/research/sessions/heartbeat",
        json={"capability": first_capability, "research_session_id": first_session_id},
    )
    assert stale_heartbeat.status_code == 409, stale_heartbeat.text
    assert stale_heartbeat.json()["detail"]["code"] == "SESSION_TERMINAL"

    # ... but its tail (events that occurred around the close) is still stored,
    # delivered under the NEW session's capability.
    tail_event_id = str(uuid.uuid4())
    late_event_id = str(uuid.uuid4())
    late_event = _telemetry_event(
        study_id=study_id,
        enrollment_id=enrollment_id,
        research_session_id=first_session_id,
        event_id=late_event_id,
    )
    late_event["occurred_at"] = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    late_event["emitter_sequence"] = 2
    tail = client.post(
        "/api/research/telemetry/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "session_capability": second_capability,
            "client_instance_id": "idle-client",
            "events": [
                _telemetry_event(
                    study_id=study_id,
                    enrollment_id=enrollment_id,
                    research_session_id=first_session_id,
                    event_id=tail_event_id,
                ),
                late_event,
            ],
        },
    )
    assert tail.status_code == 200, tail.text
    body = tail.json()
    assert [entry["event_id"] for entry in body["accepted"]] == [tail_event_id]
    assert [(entry["event_id"], entry["reason"]) for entry in body["rejected"]] == [
        (late_event_id, "SESSION_TERMINAL")
    ]
    assert body["retryable"] == []

    # And the fresh session collects as usual.
    fresh_event_id = str(uuid.uuid4())
    fresh = client.post(
        "/api/research/telemetry/batches",
        json={
            "batch_id": str(uuid.uuid4()),
            "session_capability": second_capability,
            "client_instance_id": "idle-client",
            "events": [
                _telemetry_event(
                    study_id=study_id,
                    enrollment_id=enrollment_id,
                    research_session_id=second_session_id,
                    event_id=fresh_event_id,
                )
            ],
        },
    )
    assert fresh.status_code == 200, fresh.text
    assert [entry["event_id"] for entry in fresh.json()["accepted"]] == [fresh_event_id]
    db = session_factory()
    try:
        stored = db.execute(
            text(
                "SELECT research_session_id::text FROM public.research_event "
                "WHERE event_id IN (:a, :b) ORDER BY accepted_at"
            ),
            {"a": tail_event_id, "b": fresh_event_id},
        ).all()
    finally:
        db.close()
    assert {row[0] for row in stored} == {first_session_id, second_session_id}


def test_http_bootstrap_alone_rotates_an_idle_expired_context_session(http_runtime):
    """Re-bootstrapping (the plugin's capability refresh) must not hand back an
    idle-expired session either: it ends it and mints a fresh one in the same
    unit of work as the manifest."""
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "idle-bootstrap-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "idle-bootstrap-participant@example.com")
        release_id = _qualified_packaged_release(session)
        profile_id, _connection_id = _release_profile(session, owner_id, release_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": "Idle bootstrap study",
            "default_budget_usd": "10",
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

    first_manifest = _bootstrap(client, enrollment_id)
    first_session_id = first_manifest["research_session"]["research_session_id"]
    activity = client.post(
        "/api/research/sessions/activity",
        json={
            "capability": first_manifest["session_capability"],
            "research_session_id": first_session_id,
        },
    )
    assert activity.status_code == 200, activity.text

    # A still-fresh session is reused idempotently ...
    assert _bootstrap(client, enrollment_id)["research_session"]["research_session_id"] == first_session_id

    # ... an idle-expired one is ended and replaced.
    db = session_factory()
    try:
        db.execute(
            text("UPDATE public.research_session SET last_activity_at = :stale WHERE session_id = :id"),
            {"stale": datetime.now(timezone.utc) - timedelta(hours=2), "id": first_session_id},
        )
        db.commit()
    finally:
        db.close()
    second_manifest = _bootstrap(client, enrollment_id)
    second_session_id = second_manifest["research_session"]["research_session_id"]
    assert second_session_id != first_session_id
    assert second_manifest["session_capability"]["research_session_id"] == second_session_id
    ended = _session_row(session_factory, first_session_id)
    assert ended.state == "ended"
    assert ended.close_reason == "idle_timeout"
    fresh = _session_row(session_factory, second_session_id)
    assert fresh.state == "not_started"
