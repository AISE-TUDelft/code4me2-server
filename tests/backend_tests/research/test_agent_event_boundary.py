"""Canonical research events are the only research event authority (Task 06 §8).

The legacy ``agent_event`` table stays for genuinely non-research operational
data. A research-bound task must canonicalize through the one ingestion writer
and must not also write ``agent_event``. These tests pin the boundary at the
adapter (behaviour) and at the import graph (no research module may reach for the
legacy table).
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from research.telemetry import adapters
from research.telemetry.ingestion.models import TelemetryBatchAckV1

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
SRC_ROOT = pathlib.Path(__file__).resolve().parents[3] / "src"


def _fact() -> adapters.LegacyFact:
    return adapters.LegacyFact(kind="tool_call", occurred_at=NOW)


def test_a_task_without_a_research_binding_keeps_the_legacy_path():
    task = SimpleNamespace(research_session_id=None, enrollment_id=None)

    assert adapters.research_bound(task) is False
    assert adapters.record_legacy_facts(object(), task=task, facts=[_fact()]) is None


def test_an_enrollment_only_task_is_still_research_bound():
    """ISSUE-02 decision: either attribution id makes a task research-bound.

    A binding without an unambiguous session must still use the canonical
    authority and must never silently fall back to the legacy table.
    """
    task = SimpleNamespace(research_session_id=None, enrollment_id=uuid.uuid4())

    assert adapters.research_bound(task) is True


def test_a_research_bound_task_records_only_through_the_canonical_writer():
    enrollment = SimpleNamespace(revocation_epoch=0)
    session = SimpleNamespace()
    task = SimpleNamespace(
        research_session_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        next_event_index=0,
    )
    ack = TelemetryBatchAckV1(
        receipt_id=uuid.uuid4(),
        batch_id=uuid.uuid4(),
        server_time=NOW,
    )

    with patch.object(
        adapters.identity_store, "get_enrollment", return_value=object()
    ), patch.object(
        adapters.session_store, "get_session", return_value=object()
    ), patch.object(
        adapters.identity_store, "row_to_enrollment", return_value=enrollment
    ), patch.object(
        adapters.session_store, "row_to_session", return_value=session
    ), patch.object(
        adapters,
        "resolve_study_content_policy",
        return_value=SimpleNamespace(policy=None, allowed=True),
    ), patch.object(
        adapters, "_kill_switch_check", return_value=lambda: False
    ), patch.object(adapters, "ingest_events_for_context", return_value=ack) as canonical_writer:
        recorded = adapters.record_legacy_facts(object(), task=task, facts=[_fact()])

    assert recorded is not None and recorded.written is True
    canonical_writer.assert_called_once()


def test_a_canonical_write_failure_never_falls_back_to_the_legacy_table():
    task = SimpleNamespace(
        research_session_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        next_event_index=0,
    )

    with patch.object(
        adapters.identity_store, "get_enrollment", return_value=object()
    ), patch.object(
        adapters.session_store, "get_session", return_value=object()
    ), patch.object(
        adapters.identity_store,
        "row_to_enrollment",
        return_value=SimpleNamespace(revocation_epoch=0),
    ), patch.object(
        adapters.session_store, "row_to_session", return_value=SimpleNamespace()
    ), patch.object(
        adapters,
        "resolve_study_content_policy",
        return_value=SimpleNamespace(policy=None, allowed=False),
    ), patch.object(
        adapters, "_kill_switch_check", return_value=lambda: False
    ), patch.object(
        adapters, "ingest_events_for_context", side_effect=RuntimeError("db down")
    ):
        result = adapters.record_legacy_facts(object(), task=task, facts=[_fact()])

    assert result is not None
    assert result.written is False
    assert result.retryable is True, "a research-bound failure must stay retryable"
    assert result.reason == "STORE_UNAVAILABLE"


def test_a_missing_binding_row_is_a_retryable_failure_not_a_legacy_fallback():
    task = SimpleNamespace(
        research_session_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        next_event_index=0,
    )

    with patch.object(adapters.identity_store, "get_enrollment", return_value=None), patch.object(
        adapters.session_store, "get_session", return_value=None
    ):
        result = adapters.record_legacy_facts(object(), task=task, facts=[_fact()])

    assert result is not None and result.written is False
    assert result.retryable is True
    assert result.reason == "RESEARCH_CONTEXT_UNAVAILABLE"


def test_no_research_module_imports_the_legacy_agent_event_table():
    """Research code must never reach the legacy completion event table directly."""
    offenders: list[str] = []
    for path in SRC_ROOT.joinpath("research").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported = {alias.name for alias in node.names}
                if "AgentEvent" in imported:
                    offenders.append(f"{path}:{node.lineno} import")
            elif isinstance(node, ast.Import):
                if any(alias.name.endswith("AgentEvent") for alias in node.names):
                    offenders.append(f"{path}:{node.lineno} import")
            elif isinstance(node, ast.Attribute) and node.attr == "AgentEvent":
                offenders.append(f"{path}:{node.lineno} attribute")
    assert offenders == [], f"research modules must not touch the legacy table: {offenders}"


def test_a_research_bound_model_call_never_reaches_the_legacy_append():
    """The caller must skip its legacy agent_event write when canonicalization wins."""
    from agents import event_writer
    from agents.telemetry import InferenceRecord

    task = SimpleNamespace(
        research_session_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        next_event_index=0,
    )
    record = InferenceRecord(
        request_id="req-1",
        model="m1",
        streaming=False,
        message_count=1,
        latency_ms=5,
        upstream_status=200,
    )
    app = SimpleNamespace(get_db_session=lambda: MagicMock())

    with patch.object(event_writer.crud, "get_agent_task", return_value=task), patch.object(
        event_writer,
        "record_legacy_facts",
        return_value=adapters.CanonicalRecordResult(written=True),
    ) as canonical_write, patch.object(event_writer.crud, "append_agent_event") as legacy_append, patch.object(
        event_writer.crud, "reserve_agent_event_indexes"
    ) as reserve:
        event_writer.write_model_call_event(
            app, uuid.uuid4(), record, latency_ms=5, span=None, extra=None
        )

    canonical_write.assert_called_once()
    legacy_append.assert_not_called()
    reserve.assert_not_called()


def test_canonical_events_carry_the_run_correlation_for_analytics():
    """ISSUE-02: canonical facts carry agent_run_id so the analytics join works.

    The dashboard joins ``agent_task.external_run_id`` to
    ``research_event.agent_run_id``; a research-bound fact must therefore be
    stamped with the run id, not only its study/enrollment/session scope.
    """
    task = SimpleNamespace(
        research_session_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        external_run_id="run-42",
        next_event_index=3,
    )

    events = adapters.build_legacy_events(task, [_fact()])

    assert len(events) == 1
    event = events[0]
    assert event.agent_run_id == "run-42"
    assert event.study_id == task.study_id
    assert event.enrollment_id == task.enrollment_id
    assert event.research_session_id == task.research_session_id
    assert event.emitter_sequence == 4


def test_record_legacy_facts_locks_the_lifecycle_rows():
    """ISSUE-07: enrollment/session reads take row locks against stop/revoke."""
    from types import SimpleNamespace as _SimpleNamespace

    enrollment = _SimpleNamespace(revocation_epoch=0)
    session = _SimpleNamespace()
    task = _SimpleNamespace(
        research_session_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        next_event_index=0,
    )
    ack = TelemetryBatchAckV1(
        receipt_id=uuid.uuid4(), batch_id=uuid.uuid4(), server_time=NOW
    )

    with patch.object(
        adapters.identity_store, "get_enrollment", return_value=object()
    ) as get_enrollment, patch.object(
        adapters.session_store, "get_session", return_value=object()
    ) as get_session, patch.object(
        adapters.identity_store, "row_to_enrollment", return_value=enrollment
    ), patch.object(
        adapters.session_store, "row_to_session", return_value=session
    ), patch.object(
        adapters,
        "resolve_study_content_policy",
        return_value=_SimpleNamespace(policy=None, allowed=False),
    ), patch.object(
        adapters, "_kill_switch_check", return_value=lambda: False
    ), patch.object(adapters, "ingest_events_for_context", return_value=ack):
        result = adapters.record_legacy_facts(object(), task=task, facts=[_fact()])

    assert result is not None and result.written is True
    assert get_enrollment.call_args.kwargs["for_update"] is True
    assert get_session.call_args.kwargs["for_update"] is True


def test_a_non_bound_task_still_writes_the_legacy_operational_row():
    """ISSUE-005: the non-research operational write path stays working.

    A task with no research binding must reach ``crud.append_agent_event`` —
    this is the positive counterpart to the boundary assertion that the
    adapter returns ``None`` for such tasks.
    """
    from agents import event_writer
    from agents.telemetry import InferenceRecord

    task = SimpleNamespace(research_session_id=None, enrollment_id=None)
    record = InferenceRecord(
        request_id="req-legacy",
        model="m1",
        streaming=False,
        message_count=1,
        latency_ms=5,
        upstream_status=200,
    )
    app = SimpleNamespace(get_db_session=lambda: MagicMock())

    with patch.object(event_writer.crud, "get_agent_task", return_value=task), patch.object(
        event_writer.crud, "reserve_agent_event_indexes", return_value=7
    ) as reserve, patch.object(event_writer.crud, "append_agent_event") as legacy_append:
        event_writer.write_model_call_event(
            app, uuid.uuid4(), record, latency_ms=5, span=None, extra=None
        )

    reserve.assert_called_once()
    legacy_append.assert_called_once()
    assert legacy_append.call_args.kwargs["event_type"] == "model_call"
