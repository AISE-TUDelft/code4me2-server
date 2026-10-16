"""Seed one account's complete data footprint across the classic, agent and research tables.

``row_counts`` then reports, table by table, how many rows still belong to that
footprint, so a test can assert exactly what an erasure removed and what it kept.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from research.budget import ledger as budget_ledger

from ..research._ui_overhaul_seed import (
    seed_account,
    seed_assignment,
    seed_enrollment,
    seed_event,
    seed_participant,
    seed_research_session,
)


@dataclass(frozen=True)
class Footprint:
    user_id: uuid.UUID
    email: str
    live_session_id: uuid.UUID
    ended_session_id: uuid.UUID
    project_id: uuid.UUID
    completion_query_id: uuid.UUID
    chat_query_id: uuid.UUID
    chat_id: uuid.UUID
    context_id: uuid.UUID
    contextual_telemetry_id: uuid.UUID
    behavioral_telemetry_id: uuid.UUID
    agent_task_id: uuid.UUID
    agent_memory_id: str
    study_id: uuid.UUID
    participant_id: Optional[uuid.UUID]
    enrollment_id: Optional[uuid.UUID]
    participant_code: Optional[str]
    research_session_id: Optional[uuid.UUID]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seed_footprint(
    session,
    *,
    email: str,
    study_id: uuid.UUID,
    profile_id: uuid.UUID,
    enrolled: bool = True,
    store_context: bool = False,
) -> Footprint:
    user_id = seed_account(session, email)
    if store_context:
        session.execute(
            text('UPDATE public."user" SET preference = :preference WHERE user_id = :user_id'),
            {"preference": json.dumps({"store_context": True}), "user_id": user_id},
        )
    ids = {
        name: uuid.uuid4()
        for name in (
            "live", "ended", "project", "completion", "chat_query", "chat",
            "context", "contextual", "behavioral", "task", "agent_event", "edit",
        )
    }
    memory_id = f"agent-session-{uuid.uuid4().hex[:12]}"
    params = {**ids, "user_id": user_id, "user_text": str(user_id), "study_id": study_id,
              "memory_id": memory_id, "project_text": str(ids["project"])}
    statements = [
        "INSERT INTO public.session (session_id, user_id, start_time, end_time) VALUES "
        "(:live, :user_id, now(), NULL), "
        "(:ended, :user_id, now() - interval '1 day', now() - interval '23 hours')",
        "INSERT INTO public.project (project_id, project_name, multi_file_contexts, "
        "multi_file_context_changes, created_at) VALUES (:project, 'demo', "
        "'{\"main.py\": [\"api_key = 1\"]}', '{\"main.py\": []}', now())",
        "INSERT INTO public.project_users (project_id, user_id, joined_at) VALUES (:project, :user_id, now())",
        "INSERT INTO public.session_projects (session_id, project_id) VALUES (:live, :project), (:ended, :project)",
        "INSERT INTO public.context (context_id, prefix, suffix, file_name) VALUES (:context, 'def f(', '):', 'main.py')",
        "INSERT INTO public.contextual_telemetry (contextual_telemetry_id, version_id, trigger_type_id, "
        "language_id, file_path) VALUES (:contextual, 1, 1, 1, '/home/someone/main.py')",
        "INSERT INTO public.behavioral_telemetry (behavioral_telemetry_id, typing_speed) VALUES (:behavioral, 1.5)",
        "INSERT INTO public.meta_query (meta_query_id, user_id, contextual_telemetry_id, behavioral_telemetry_id, "
        "context_id, session_id, project_id, timestamp, query_type) VALUES (:completion, :user_id, :contextual, "
        ":behavioral, :context, :ended, :project, now(), 'completion')",
        "INSERT INTO public.completion_query (meta_query_id) VALUES (:completion)",
        "INSERT INTO public.had_generation (meta_query_id, model_id, completion, generation_time, shown_at, "
        "was_accepted, logprobs) VALUES (:completion, 1, 'x = 1', 10, ARRAY[now()], true, ARRAY[0.1])",
        "INSERT INTO public.ground_truth (completion_query_id, truth_timestamp, ground_truth) "
        "VALUES (:completion, now(), 'x = 2')",
        "INSERT INTO public.chat (chat_id, project_id, user_id, title, created_at) "
        "VALUES (:chat, :project, :user_id, 'Help me', now())",
        "INSERT INTO public.meta_query (meta_query_id, user_id, session_id, project_id, timestamp, query_type) "
        "VALUES (:chat_query, :user_id, :live, :project, now(), 'chat')",
        "INSERT INTO public.chat_query (meta_query_id, chat_id) VALUES (:chat_query, :chat)",
        "INSERT INTO public.had_generation (meta_query_id, model_id, completion, generation_time, shown_at, "
        "was_accepted, logprobs) VALUES (:chat_query, 3, 'Sure.', 10, ARRAY[now()], false, ARRAY[0.1])",
        "INSERT INTO public.config_assignment_history (user_id, study_id, assigned_config_id) "
        "VALUES (:user_id, :study_id, (SELECT config_id FROM public.\"user\" WHERE user_id = :user_id))",
        "INSERT INTO public.agent_task (task_id, session_id, owner_user_id, study_id, agent_profile, model, "
        "approval_policy, tools_json, status, created_at, task_description) VALUES (:task, :live, :user_id, "
        ":study_id, 'arm', 'model', 'auto', '[]', 'completed', now(), 'refactor my code')",
        "INSERT INTO public.agent_event (event_id, task_id, event_index, event_type) "
        "VALUES (:agent_event, :task, 0, 'llm_call')",
        # The cyclic latest-event pointer must not block the task's deletion.
        "UPDATE public.agent_task SET latest_event_id = :agent_event WHERE task_id = :task",
        "INSERT INTO public.agent_edit (edit_id, task_id, file_path) VALUES (:edit, :task, '/home/someone/main.py')",
        "INSERT INTO public.agent_memory (session_id, owner_user_id, owner_project_id, memory_json, created_at, "
        "updated_at) VALUES (:memory_id, :user_text, :project_text, '{\"messages\": [\"secret\"]}', now(), now())",
    ]
    for statement in statements:
        session.execute(text(statement), params)
    session.commit()

    participant_id = enrollment_id = research_session_id = participant_code = None
    if enrolled:
        participant_id = seed_participant(session, user_id)
        participant_code = f"p_{uuid.uuid4().hex[:24]}"
        enrollment_id = seed_enrollment(
            session,
            participant_id=participant_id,
            study_id=study_id,
            participant_code=participant_code,
            consent_accepted_at=_now(),
        )
        assignment_id = seed_assignment(
            session,
            enrollment_id=enrollment_id,
            study_id=study_id,
            profile_id=profile_id,
            snapshot={"name": "arm"},
        )
        research_session_id = seed_research_session(
            session, enrollment_id=enrollment_id, study_id=study_id, state="running"
        )
        # One event reaches the enrollment through its session only.
        seed_event(session, enrollment_id=enrollment_id, study_id=study_id,
                   research_session_id=research_session_id, event_type="agent.prompt.submitted",
                   sequence=1, occurred_at=_now(), payload={"account_id": str(user_id)})
        seed_event(session, enrollment_id=None, study_id=study_id,
                   research_session_id=research_session_id, event_type="agent.turn.completed",
                   sequence=2, occurred_at=_now())
        research_params = {
            "enrollment_id": enrollment_id, "study_id": study_id,
            "session_id": research_session_id, "assignment_id": assignment_id,
            "run_id": uuid.uuid4(), "receipt_id": uuid.uuid4(), "batch_id": f"batch-{uuid.uuid4()}",
            "job_id": uuid.uuid4(), "adjustment_id": uuid.uuid4(), "task": ids["task"],
        }
        budget_ledger.create_balance(
            session, enrollment_id=enrollment_id, study_id=study_id, limit_micro_usd=1_000_000
        )
        for statement in (
            "UPDATE public.agent_task SET enrollment_id = :enrollment_id, research_session_id = :session_id, "
            "study_assignment_id = :assignment_id WHERE task_id = :task",
            "INSERT INTO public.research_agent_run (agent_run_id, research_session_id, started_at) "
            "VALUES (:run_id, :session_id, now())",
            "INSERT INTO public.telemetry_batch_receipt (receipt_id, batch_id, enrollment_id, research_session_id, "
            "accepted_at, receipt_json) VALUES (:receipt_id, :batch_id, :enrollment_id, :session_id, now(), '{}')",
            "INSERT INTO public.research_retention_job (job_id, enrollment_id, action, state, created_at, "
            "evidence_json) VALUES (:job_id, :enrollment_id, 'RETAIN_ANONYMIZED', 'PENDING', now(), '{}')",
            "INSERT INTO public.inference_budget_adjustment (adjustment_id, enrollment_id, study_id, kind, "
            "delta_micro_usd, limit_before_micro_usd, limit_after_micro_usd, occurred_at) VALUES "
            "(:adjustment_id, :enrollment_id, :study_id, 'TOP_UP', 10, 1000000, 1000010, now())",
        ):
            session.execute(text(statement), research_params)
        session.commit()

    return Footprint(
        user_id=user_id,
        email=email,
        live_session_id=ids["live"],
        ended_session_id=ids["ended"],
        project_id=ids["project"],
        completion_query_id=ids["completion"],
        chat_query_id=ids["chat_query"],
        chat_id=ids["chat"],
        context_id=ids["context"],
        contextual_telemetry_id=ids["contextual"],
        behavioral_telemetry_id=ids["behavioral"],
        agent_task_id=ids["task"],
        agent_memory_id=memory_id,
        study_id=study_id,
        participant_id=participant_id,
        enrollment_id=enrollment_id,
        participant_code=participant_code,
        research_session_id=research_session_id,
    )


def row_counts(session, footprint: Footprint) -> dict[str, int]:
    """Rows still present per table for this footprint (research rows only if enrolled)."""
    checks = {
        "user": 'SELECT count(*) FROM public."user" WHERE user_id = :user_id',
        "live_session": "SELECT count(*) FROM public.session WHERE session_id = :live",
        "ended_session": "SELECT count(*) FROM public.session WHERE session_id = :ended",
        "project": "SELECT count(*) FROM public.project WHERE project_id = :project",
        "project_users": "SELECT count(*) FROM public.project_users WHERE user_id = :user_id",
        "meta_query": "SELECT count(*) FROM public.meta_query WHERE meta_query_id = ANY(:queries)",
        "completion_query": "SELECT count(*) FROM public.completion_query WHERE meta_query_id = ANY(:queries)",
        "chat_query": "SELECT count(*) FROM public.chat_query WHERE meta_query_id = ANY(:queries)",
        "had_generation": "SELECT count(*) FROM public.had_generation WHERE meta_query_id = ANY(:queries)",
        "ground_truth": "SELECT count(*) FROM public.ground_truth WHERE completion_query_id = ANY(:queries)",
        "context": "SELECT count(*) FROM public.context WHERE context_id = :context",
        "contextual_telemetry": (
            "SELECT count(*) FROM public.contextual_telemetry WHERE contextual_telemetry_id = :contextual"
        ),
        "behavioral_telemetry": (
            "SELECT count(*) FROM public.behavioral_telemetry WHERE behavioral_telemetry_id = :behavioral"
        ),
        "chat": "SELECT count(*) FROM public.chat WHERE chat_id = :chat",
        "config_assignment_history": (
            "SELECT count(*) FROM public.config_assignment_history WHERE user_id = :user_id"
        ),
        "agent_task": "SELECT count(*) FROM public.agent_task WHERE task_id = :task",
        "agent_event": "SELECT count(*) FROM public.agent_event WHERE task_id = :task",
        "agent_edit": "SELECT count(*) FROM public.agent_edit WHERE task_id = :task",
        "agent_memory": "SELECT count(*) FROM public.agent_memory WHERE session_id = :memory_id",
    }
    if footprint.enrollment_id is not None:
        checks.update({
            "research_participant": (
                "SELECT count(*) FROM public.research_participant WHERE account_id = :user_id"
            ),
            "research_enrollment": (
                "SELECT count(*) FROM public.research_enrollment WHERE enrollment_id = :enrollment_id"
            ),
            "study_assignment": (
                "SELECT count(*) FROM public.study_assignment WHERE enrollment_id = :enrollment_id"
            ),
            "research_session": (
                "SELECT count(*) FROM public.research_session WHERE session_id = :research_session_id"
            ),
            "research_agent_run": (
                "SELECT count(*) FROM public.research_agent_run "
                "WHERE research_session_id = :research_session_id"
            ),
            "research_event": (
                "SELECT count(*) FROM public.research_event WHERE enrollment_id = :enrollment_id "
                "OR research_session_id = :research_session_id OR envelope_json::text LIKE :user_like"
            ),
            "telemetry_batch_receipt": (
                "SELECT count(*) FROM public.telemetry_batch_receipt WHERE enrollment_id = :enrollment_id "
                "OR research_session_id = :research_session_id"
            ),
            "research_retention_job": (
                "SELECT count(*) FROM public.research_retention_job WHERE enrollment_id = :enrollment_id"
            ),
            "enrollment_inference_balance": (
                "SELECT count(*) FROM public.enrollment_inference_balance WHERE enrollment_id = :enrollment_id"
            ),
            "inference_budget_adjustment": (
                "SELECT count(*) FROM public.inference_budget_adjustment WHERE enrollment_id = :enrollment_id"
            ),
        })
    params = {
        "user_id": footprint.user_id, "live": footprint.live_session_id,
        "ended": footprint.ended_session_id, "project": footprint.project_id,
        "queries": [footprint.completion_query_id, footprint.chat_query_id],
        "context": footprint.context_id, "contextual": footprint.contextual_telemetry_id,
        "behavioral": footprint.behavioral_telemetry_id, "chat": footprint.chat_id,
        "task": footprint.agent_task_id, "memory_id": footprint.agent_memory_id,
        "enrollment_id": footprint.enrollment_id, "research_session_id": footprint.research_session_id,
        "user_like": f"%{footprint.user_id}%",
    }
    return {name: session.execute(text(sql), params).scalar_one() for name, sql in checks.items()}
