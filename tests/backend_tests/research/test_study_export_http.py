"""HTTP contract of the owner-only study export."""

from __future__ import annotations

import csv
import io
import json
import uuid
import zipfile
from datetime import timedelta

from sqlalchemy import text

from research.runtime.assignment.hashing import hash_arm_index

from .test_research_api_contract import (  # noqa: F401 - the fixture is used by name
    _owner,
    http_runtime,
)
from .test_study_traces_http import CANARY, T0, _chat, _seed, _setup


def _url(study_id):
    return f"/api/research/studies/{study_id}/export"


def _zip(response) -> zipfile.ZipFile:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith('attachment; filename="Trace-study-export-')
    return zipfile.ZipFile(io.BytesIO(response.content))


def _csv(archive, name):
    return list(csv.DictReader(io.StringIO(archive.read(name).decode("utf-8"))))


def test_owner_exports_every_dataset_without_identity_or_deleted_events(http_runtime):
    client, session_factory, current_user = http_runtime
    _, other_id, study_id, (first, second) = _setup(client, session_factory, current_user, content_capture=True)
    events = _chat([{"type": "text", "text": CANARY}])
    events.append({"type": "agent.error", "at": 30, "payload": {"error_code": "=HYPERLINK(1)"}})
    ids = _seed(session_factory, study_id, first, events)
    _seed(session_factory, study_id, second, _chat([{"type": "text", "text": "second participant"}]))
    session = session_factory()
    try:
        session.execute(
            text("UPDATE public.research_event SET retention_state = 'DELETED' WHERE event_id = :id"),
            {"id": ids[0]},
        )
        session.commit()
    finally:
        session.close()

    archive = _zip(client.get(_url(study_id)))
    assert set(archive.namelist()) == {
        "participants.csv",
        "participant_metrics.csv",
        "sessions.csv",
        "chats.csv",
        "events.csv",
        "manifest.json",
    }
    everything = b"".join(archive.read(name) for name in archive.namelist()).decode("utf-8")
    assert CANARY not in everything  # CSV is metadata only
    assert "@example.com" not in everything
    for forbidden in ("account_id", "participant_id", "user_id", "session_token"):
        assert forbidden not in everything

    manifest = json.loads(archive.read("manifest.json"))
    events_csv = _csv(archive, "events.csv")
    assert manifest["files"]["events.csv"] == len(events_csv) == len(ids) - 1 + 9
    assert ids[0] not in {row["event_id"] for row in events_csv}
    assert manifest["assignment"]["strategy"] == "DETERMINISTIC_HASH"
    assert "SHA-256" in manifest["assignment"]["formula"]
    assert manifest["telemetry_policy"]["content_capture"] is True
    assert set(manifest["columns"]) == {"participants", "participant_metrics", "sessions", "chats", "events"}
    error = next(row for row in events_csv if row["event_type"] == "agent.error")
    assert error["error_code"] == "'=HYPERLINK(1)"

    participants = _csv(archive, "participants.csv")
    assert {row["enrollment_id"] for row in participants} == {first, second}
    arms = [arm["profile_id"] for arm in manifest["assignment"]["arms"]]
    for row in participants:
        assert row["assignment_strategy"] == "DETERMINISTIC_HASH"
        assert row["randomized_profile_id"] == arms[hash_arm_index(study_id, 0, row["enrollment_id"], len(arms))]
        assert row["randomized_profile_id"] == row["arm_profile_id"]
    chats = _csv(archive, "chats.csv")
    assert {(row["enrollment_id"], row["chat_id"]) for row in chats} == {(first, "chat-1"), (second, "chat-1")}

    # A filtered JSONL export with content carries the captured text.
    filtered = _zip(
        client.get(
            _url(study_id),
            params={
                "datasets": ["events"],
                "format": "jsonl",
                "participant": first,
                "event_categories": ["conversation"],
                "include_content": "true",
            },
        )
    )
    lines = [json.loads(line) for line in filtered.read("events.jsonl").decode("utf-8").splitlines()]
    assert lines and all(line["enrollment_id"] == first for line in lines)
    assert all(line["event_type"].startswith("agent.message.") for line in lines)
    assert CANARY in json.dumps(lines)
    assert json.loads(filtered.read("manifest.json"))["filters"]["include_content"] is True

    refused = client.get(_url(study_id), params={"include_content": "true"})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "CONTENT_REQUIRES_JSONL"

    without = _zip(client.get(_url(study_id), params={"datasets": "events", "format": "jsonl"}))
    assert CANARY not in without.read("events.jsonl").decode("utf-8")

    current_user["value"] = _owner(other_id)
    assert client.get(_url(study_id)).status_code == 403


def test_export_requests_are_validated(http_runtime):
    client, session_factory, current_user = http_runtime
    _, _, study_id, _ = _setup(client, session_factory, current_user, content_capture=False)

    def code(params):
        response = client.get(_url(study_id), params=params)
        assert response.status_code == 422, response.text
        return response.json()["detail"]["code"]

    assert code({"datasets": "everything"}) == "UNKNOWN_DATASET"
    assert code({"format": "xlsx"}) == "UNKNOWN_FORMAT"
    assert code({"event_categories": "secrets"}) == "UNKNOWN_EVENT_CATEGORY"
    assert code({"participant": str(uuid.uuid4())}) == "UNKNOWN_FILTER_VALUE"
    assert code({"include_content": "true", "format": "jsonl"}) == "CONTENT_NOT_CAPTURED"
    assert code({"start": "2026-13-01"}) == "INVALID_DATE"


def test_exported_sessions_carry_the_heartbeat_and_the_counted_session_time(http_runtime):
    client, session_factory, current_user = http_runtime
    _, _, study_id, (first, _) = _setup(client, session_factory, current_user, content_capture=False)
    _seed(session_factory, study_id, first, _chat("[REDACTED]"))
    # The IDE last reported ten minutes in; the server closed the session as idle
    # three days later, when the participant came back.
    opened = T0 - timedelta(days=3)
    session = session_factory()
    try:
        session.execute(
            text(
                "UPDATE public.research_session SET state = 'ended', close_reason = 'idle_timeout', "
                "opened_at = :opened, closed_at = :closed, last_activity_at = :closed, "
                "last_heartbeat_at = :heartbeat WHERE enrollment_id = :enrollment"
            ),
            {"opened": opened, "closed": T0, "heartbeat": opened + timedelta(minutes=10), "enrollment": first},
        )
        session.commit()
    finally:
        session.close()

    archive = _zip(client.get(_url(study_id), params={"datasets": "sessions", "participant": first}))
    (row,) = _csv(archive, "sessions.csv")
    assert row["session_seconds"] == "600.0"
    assert row["last_heartbeat_at"] == (opened + timedelta(minutes=10)).isoformat()
    assert row["closed_at"] == T0.isoformat()


def test_exported_metrics_match_the_analytics_participant_metrics(http_runtime):
    client, session_factory, current_user = http_runtime
    _, _, study_id, (first, _) = _setup(client, session_factory, current_user, content_capture=False)
    events = _chat("[REDACTED]")
    # The participant's own edits only reach the metrics through the per-day aggregates.
    events += [{"type": "ide.document.changed", "at": 40 + index, "source": "ide"} for index in range(3)]
    _seed(session_factory, study_id, first, events)

    analytics = client.get(f"/api/research/studies/{study_id}/analytics/participants/{first}")
    assert analytics.status_code == 200, analytics.text
    expected = analytics.json()["metrics"]
    assert expected["ide_edits_per_session_hour"] == 3.0

    archive = _zip(client.get(_url(study_id), params={"datasets": "participant_metrics", "participant": first}))
    (row,) = _csv(archive, "participant_metrics.csv")
    assert row["has_telemetry"] == "true"
    for key, value in expected.items():
        assert row[key] == ("" if value is None else str(value)), key
