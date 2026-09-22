"""ACP-proxied telemetry runs become linkable ``agent_task`` rows (D-2, C20).

The research telemetry route accepts canonical events whose payload carries an
``agent_run_id`` stamped by the proxy/IPC. The dashboards join
``research_event.agent_run_id = agent_task.external_run_id`` and scope ownership
by ``agent_task.owner_user_id``; without a task the run is invisible. These tests
drive the real HTTP route against PostgreSQL and assert the row is created
exactly once, attributed to the authorized enrollment/study/session, owned by
the participant account, and visible in the owner's agent overview.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import text

from backend.routers.research.bootstrap import BOOTSTRAP_SIGNING_SECRET
from research.analysis.read_models.dashboard import agent_overview
from research.runtime.bootstrap.service import BootstrapSigningContext

from .test_study_lifecycle_bootstrap_session_telemetry_http import (  # noqa: F401
    _owner,
    _participant,
    _qualified_packaged_release,
    _release_profile,
    _seed_user,
    _telemetry_event,
    http_runtime,
)


def _prepare_enrolled_running_session(client, session_factory, current_user, name):
    """Drive study -> join -> bootstrap -> session RUNNING over real HTTP."""
    session = session_factory()
    try:
        owner_id = _seed_user(session, f"{name}-owner@example.com", can_research=True)
        participant_id = _seed_user(session, f"{name}-participant@example.com")
        release_id = _qualified_packaged_release(session)
        profile_id, _connection_id = _release_profile(session, owner_id, release_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/research/studies",
        json={
            "name": f"{name} study",
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
    context_id = f"{name}-context"
    with patch(
        "backend.routers.research.bootstrap._SIGNER",
        BootstrapSigningContext(secret=signing_secret),
    ):
        bootstrap = client.post(
            "/api/research/bootstrap/research-sessions",
            json={
                "enrollment_id": enrollment_id,
                "context_id": context_id,
                "environment": {"os": "macos", "arch": "arm64"},
            },
        )
    assert bootstrap.status_code == 201, bootstrap.text
    manifest = bootstrap.json()["manifest"]
    capability = manifest["session_capability"]
    research_session_id = manifest["research_session"]["research_session_id"]

    opened = client.post(
        "/api/research/sessions/",
        json={
            "capability": capability,
            "enrollment_id": enrollment_id,
            "study_id": study["study_id"],
            "manifest_digest": manifest["manifest_digest"],
            "context_id": context_id,
        },
    )
    assert opened.status_code == 200, opened.text
    activity = client.post(
        "/api/research/sessions/activity",
        json={
            "capability": capability,
            "research_session_id": research_session_id,
        },
    )
    assert activity.status_code == 200, activity.text

    return {
        "owner_id": owner_id,
        "participant_id": participant_id,
        "study_id": study["study_id"],
        "enrollment_id": enrollment_id,
        "research_session_id": research_session_id,
        "capability": capability,
    }


def _run_event(ctx, *, event_id, run_id, emitter_id, payload=None, study_id=None):
    event = _telemetry_event(
        study_id=study_id or ctx["study_id"],
        enrollment_id=ctx["enrollment_id"],
        research_session_id=ctx["research_session_id"],
        event_id=event_id,
        payload=payload,
    )
    event["emitter_id"] = emitter_id
    if run_id is not None:
        event["agent_run_id"] = run_id
    return event


def _batch_body(ctx, *, batch_id, events, capability=None):
    return {
        "batch_id": batch_id,
        "session_capability": capability or ctx["capability"],
        "client_instance_id": "acp-task-link-client",
        "events": events,
    }


def _post_batch(client, ctx, *, batch_id, events, capability=None):
    return client.post(
        "/api/research/telemetry/batches",
        json=_batch_body(ctx, batch_id=batch_id, events=events, capability=capability),
    )


def _tasks_for_run(session_factory, run_id):
    session = session_factory()
    try:
        return (
            session.execute(
                text(
                    "SELECT * FROM public.agent_task "
                    "WHERE external_run_id = :run_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )
    finally:
        session.close()


def _task_count(session_factory):
    session = session_factory()
    try:
        return session.execute(
            text("SELECT count(*) FROM public.agent_task")
        ).scalar_one()
    finally:
        session.close()


def test_accepted_agent_run_creates_one_task_and_is_visible_to_owner(http_runtime):
    client, session_factory, current_user = http_runtime
    ctx = _prepare_enrolled_running_session(
        client, session_factory, current_user, "acp-link"
    )
    run_id = f"acp-run-{uuid.uuid4().hex}"
    event_id = str(uuid.uuid4())

    first = _post_batch(
        client,
        ctx,
        batch_id=str(uuid.uuid4()),
        events=[
            _run_event(
                ctx,
                event_id=event_id,
                run_id=run_id,
                emitter_id="acp-emitter",
                payload={"session_id": "acp-native-session"},
            )
        ],
    )
    assert first.status_code == 200, first.text
    assert [entry["event_id"] for entry in first.json()["accepted"]] == [event_id]

    rows = _tasks_for_run(session_factory, run_id)
    assert len(rows) == 1, "an accepted run must yield exactly one linkable task"
    row = rows[0]
    assert str(row["study_id"]) == ctx["study_id"]
    assert str(row["enrollment_id"]) == ctx["enrollment_id"]
    assert str(row["research_session_id"]) == ctx["research_session_id"]
    assert str(row["owner_user_id"]) == str(ctx["participant_id"])
    assert row["source"] == "research-acp"
    assert row["status"] == "running"
    # The run's real profile is not resolvable on this route; non-secret
    # placeholders are stored so the run is still visible.
    assert row["agent_profile"] == "acp-external"
    assert row["model"] == ""
    assert row["approval_policy"] == "unknown"
    assert row["tools_json"] == "[]"
    assert row["framework_version"] == "acp-external"

    # Re-posting the identical batch returns the stored receipt and creates no
    # second row.
    replay = _post_batch(
        client,
        ctx,
        batch_id=first.json()["batch_id"],
        events=[
            _run_event(
                ctx,
                event_id=event_id,
                run_id=run_id,
                emitter_id="acp-emitter",
                payload={"session_id": "acp-native-session"},
            )
        ],
    )
    assert replay.status_code == 200, replay.text
    assert len(_tasks_for_run(session_factory, run_id)) == 1

    # A fresh batch carrying the same run id (a later observation) is accepted
    # but still links to the single existing task.
    later = _post_batch(
        client,
        ctx,
        batch_id=str(uuid.uuid4()),
        events=[
            _run_event(
                ctx,
                event_id=str(uuid.uuid4()),
                run_id=run_id,
                emitter_id="acp-emitter-later",
            )
        ],
    )
    assert later.status_code == 200, later.text
    assert later.json()["accepted"], later.text
    assert len(_tasks_for_run(session_factory, run_id)) == 1

    # The owner's overview includes the ACP run (and no other task).
    session = session_factory()
    try:
        # ``agent_task.created_at`` is stored naive (``datetime.now``), so the
        # window bound is passed naive too: this asserts the join/ownership, not
        # the pre-existing local-vs-UTC default.
        overview = agent_overview(
            session,
            _participant(ctx["participant_id"]),
            time_window="7d",
            now=datetime.now(),
        )
    finally:
        session.close()
    assert overview["summary"]["total_tasks"] == 1
    run_task_ids = {entry["task_id"] for entry in overview["recent_runs"]}
    assert str(row["task_id"]) in run_task_ids
    assert any(
        entry["profile_name"] == "acp-external" for entry in overview["recent_runs"]
    )


def test_null_and_rejected_run_ids_create_no_task(http_runtime):
    client, session_factory, current_user = http_runtime
    ctx = _prepare_enrolled_running_session(
        client, session_factory, current_user, "acp-null"
    )
    baseline = _task_count(session_factory)

    # A null agent_run_id is not linkable: no task is created.
    null_run = _post_batch(
        client,
        ctx,
        batch_id=str(uuid.uuid4()),
        events=[
            _run_event(
                ctx,
                event_id=str(uuid.uuid4()),
                run_id=None,
                emitter_id="null-run-emitter",
            )
        ],
    )
    assert null_run.status_code == 200, null_run.text
    assert null_run.json()["accepted"], null_run.text
    assert _task_count(session_factory) == baseline

    # A rejected event (attribution mismatch) with a run id creates no task.
    rejected_run_id = f"acp-rejected-{uuid.uuid4().hex}"
    rejected = _post_batch(
        client,
        ctx,
        batch_id=str(uuid.uuid4()),
        events=[
            _run_event(
                ctx,
                event_id=str(uuid.uuid4()),
                run_id=rejected_run_id,
                emitter_id="rejected-run-emitter",
                study_id=str(uuid.uuid4()),
            )
        ],
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["rejected"], rejected.text
    assert rejected.json()["accepted"] == []
    assert _tasks_for_run(session_factory, rejected_run_id) == []
    assert _task_count(session_factory) == baseline
