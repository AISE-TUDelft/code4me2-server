"""Canonical dashboard analytics: counts, attribution and honest unavailability.

These drive the owner-scoped canonical query path with a stub session, asserting
the phase-06 semantics: dashboard counts come from persisted canonical facts,
missing usage is never turned into zero, edit acceptance is unavailable (no
canonical producer), permission and edit metrics stay separate, and ownership is
applied before the joins.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from research.analysis.read_models.dashboard import agent_overview, agent_run_detail

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _owner():
    return SimpleNamespace(user_id="11111111-1111-1111-1111-111111111111", is_admin=False)


def _route(sql: str):
    if "COUNT(*) AS total_tasks" in sql:
        return "tasks"
    if "legacy_kind" in sql and "COUNT(*)" in sql and "AS model_calls" in sql and "tool_schema_bytes" in sql:
        return "events"
    if "AS profile_name" in sql and "GROUP BY 1, 2, 3" in sql:
        return "profiles"
    if "AS tool_name" in sql and "AS calls" in sql:
        return "tools"
    if "AS model_calls" in sql and "LEFT JOIN research_event" in sql:
        return "runs"
    if "DATE_TRUNC('day'" in sql:
        return "trend"
    if "AS bucket" in sql:
        return "latency"
    if "COALESCE" in sql and "AS event_type" in sql and "AS events" in sql:
        return "event_types"
    if "AS reason" in sql:
        return "errors"
    if "t.external_run_id" in sql and "FROM agent_task t" in sql and "total_steps" in sql:
        return "task_row"
    if "FROM research_event e" in sql and "agent_run_id = :run_id" in sql:
        return "event_rows"
    return "unknown"


def _overview_db(
    *,
    model_calls=2,
    tool_calls=1,
    tool_failures=1,
    provider_tokens=None,
    latency=None,
    permission_decisions=0,
    permission_accepted=0,
):
    db = MagicMock()

    def execute(clause, params=None):
        kind = _route(str(clause))
        result = MagicMock()
        if kind == "tasks":
            result.one.return_value = SimpleNamespace(
                total_tasks=3, completed_tasks=2, failed_tasks=1, open_tasks=0,
                avg_steps=4.0, avg_task_duration_ms=1200.0,
            )
        elif kind == "events":
            result.one.return_value = SimpleNamespace(
                model_calls=model_calls, tool_calls=tool_calls,
                tool_failures=tool_failures, observed_failures=0,
                successful_model_calls=model_calls, rate_limit_retries=0,
                event_tokens=None, provider_input_tokens=provider_tokens,
                model_output_tokens=None, tool_schema_bytes=None,
                conversation_context_bytes=None, tool_result_bytes=None,
                avg_model_latency_ms=latency, p95_model_latency_ms=latency,
                permission_decisions=permission_decisions,
                permission_accepted=permission_accepted,
            )
        else:
            result.fetchall.return_value = []
            result.one_or_none.return_value = None
        return result

    db.execute.side_effect = execute
    return db


def test_overview_counts_come_from_canonical_facts():
    content = agent_overview(_overview_db(), _owner(), time_window="7d", now=NOW)
    summary = content["summary"]

    assert summary["model_calls"] == 2
    assert summary["tool_calls"] == 1
    assert summary["failures"] == 1
    assert summary["total_tasks"] == 3
    assert summary["completed_tasks"] == 2


def test_missing_usage_is_unavailable_never_zero():
    content = agent_overview(_overview_db(provider_tokens=None), _owner(), time_window="7d", now=NOW)
    summary = content["summary"]

    # A genuinely missing measurement is null, not 0.
    assert summary["provider_input_tokens"] is None
    assert summary["model_output_tokens"] is None
    assert summary["avg_model_latency_ms"] is None
    assert summary["tool_schema_tokens_estimated"] is None


def test_provider_reported_usage_is_distinguishable_from_estimates():
    content = agent_overview(
        _overview_db(provider_tokens=1234, latency=42.0), _owner(), time_window="7d", now=NOW
    )
    summary = content["summary"]

    assert summary["provider_input_tokens"] == 1234
    assert summary["avg_model_latency_ms"] == 42.0


def test_edit_acceptance_is_unavailable_not_computed():
    content = agent_overview(_overview_db(), _owner(), time_window="7d", now=NOW)
    summary = content["summary"]

    assert summary["edit_acceptance_rate"] is None
    assert summary["total_edits"] is None


def test_ownership_is_applied_before_the_joins():
    db = _overview_db()
    agent_overview(db, _owner(), time_window="7d", now=NOW)

    event_sql = [
        str(call.args[0])
        for call in db.execute.call_args_list
        if "FROM research_event e" in str(call.args[0])
    ]
    assert event_sql, "expected a canonical event query"
    for sql in event_sql:
        assert "t.owner_user_id = CAST(:user_id AS UUID)" in sql


def test_run_detail_uses_canonical_events_joined_by_run_binding():
    db = MagicMock()

    def execute(clause, params=None):
        kind = _route(str(clause))
        result = MagicMock()
        if kind == "task_row":
            result.one_or_none.return_value = SimpleNamespace(
                task_id="22222222-2222-2222-2222-222222222222",
                agent_profile="p", framework_version="0.1", model="m", status="done",
                total_steps=2, created_at=NOW, started_at=NOW, completed_at=NOW,
                external_run_id="run-1",
            )
        elif kind == "event_rows":
            result.fetchall.return_value = [
                SimpleNamespace(
                    event_type="tool_call", source="relay", latency_ms=5,
                    upstream_status=None, tool_name="read", model=None,
                    total_tokens=None, occurred_at=NOW, tool_call_id="t-1",
                    canonical_event_type="tool.completed",
                ),
            ]
        else:
            result.fetchall.return_value = []
            result.one_or_none.return_value = None
        return result

    db.execute.side_effect = execute
    content = agent_run_detail(db, _owner(), task_id="22222222-2222-2222-2222-222222222222")

    assert content is not None
    assert content["events"][0]["event_type"] == "tool_call"
    assert content["events"][0]["source"] == "relay"
    # The join is by the explicit run binding, not an ACP session id.
    event_sql = [
        str(call.args[0])
        for call in db.execute.call_args_list
        if "agent_run_id = :run_id" in str(call.args[0])
    ]
    assert event_sql
    assert "session_id" not in event_sql[0]


def test_edit_acceptance_computes_from_decided_rows():
    content = agent_overview(
        _overview_db(permission_decisions=4, permission_accepted=3),
        _owner(), time_window="7d", now=NOW)
    summary = content["summary"]
    assert summary["edit_acceptance_rate"] == 0.75
    assert summary["total_edits"] == 4
