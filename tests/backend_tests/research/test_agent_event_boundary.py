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

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
SRC_ROOT = pathlib.Path(__file__).resolve().parents[3] / "src"


def _fact() -> adapters.LegacyFact:
    return adapters.LegacyFact(kind="tool_call", occurred_at=NOW)


def test_a_task_without_a_research_binding_keeps_the_legacy_path():
    task = SimpleNamespace(research_session_id=None)

    assert adapters.research_bound(task) is False
    assert adapters.record_legacy_facts(object(), task=task, facts=[_fact()]) is False


def test_a_research_bound_task_records_only_through_the_canonical_writer():
    enrollment = SimpleNamespace(revocation_epoch=0)
    session = SimpleNamespace()
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
        adapters.identity_store, "row_to_enrollment", return_value=enrollment
    ), patch.object(
        adapters.session_store, "row_to_session", return_value=session
    ), patch.object(adapters, "ingest_events_for_context") as canonical_writer:
        recorded = adapters.record_legacy_facts(object(), task=task, facts=[_fact()])

    assert recorded is True, "research-bound facts must canonicalize"
    canonical_writer.assert_called_once()


def test_a_canonical_write_failure_falls_back_without_raising():
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
        adapters, "ingest_events_for_context", side_effect=RuntimeError("db down")
    ):
        assert adapters.record_legacy_facts(object(), task=task, facts=[_fact()]) is False


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
        event_writer, "record_legacy_facts", return_value=True
    ), patch.object(event_writer.crud, "append_agent_event") as legacy_append, patch.object(
        event_writer.crud, "reserve_agent_event_indexes"
    ) as reserve:
        event_writer.write_model_call_event(
            app, uuid.uuid4(), record, latency_ms=5, span=None, extra=None
        )

    legacy_append.assert_not_called()
    reserve.assert_not_called()
