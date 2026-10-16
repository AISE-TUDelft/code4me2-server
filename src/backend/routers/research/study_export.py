"""Owner-only raw data export of a study (ZIP of CSV or JSONL files).

Participants appear only by enrollment id and study-local code; retention
tombstones are never exported. Content (prompts, reasoning, tool data) is
included only on request, only for a study that captured it, and only in JSONL.
Every export is logged. The whole ZIP is built from one database snapshot
before it is streamed.
"""

from __future__ import annotations

import logging
import re
import uuid  # noqa: TC003 - FastAPI resolves the path-parameter annotations at runtime
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from App import App
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from backend.routers.research.study_analytics import _window
from database.db_schemas import Study as StudyRow
from database.research_schemas import StudyAgentProfile
from research.analysis.study_analytics import metrics as analytics
from research.analysis.study_analytics import store as analytics_store
from research.analysis.study_export.build import (
    CHAT_COLUMNS,
    DATASETS,
    EVENT_CSV_COLUMNS,
    EVENT_JSONL_COLUMNS,
    FORMATS,
    PARTICIPANT_COLUMNS,
    SESSION_COLUMNS,
    ExportWriter,
    dictionary,
    event_csv_row,
    export_envelope,
    metric_columns,
    optional_iso,
)
from research.analysis.study_export.store import EVENT_CATEGORIES, iter_export_events
from research.runtime.assignment.hashing import (
    assignment_strategy,
    hash_arm_index,
    manual_override_enabled,
)
from research.study.protocol import store as study_store
from research.study.protocol.enums import AssignmentStrategy

router = APIRouter()

EXPORT_VERSION = 1
ASSIGNMENT_FORMULA = (
    "arm index k = floor(K * h / 2^64), h = first 8 bytes (big-endian) of "
    "SHA-256('{study_id}:{randomization_epoch}:{enrollment_id}'), over the arms ordered by selection_order "
    "(UUIDs lowercase and hyphenated, randomization_epoch 0)"
)


def _unprocessable(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code, "message": message})


def _filename(name: str, now: datetime) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name or "").strip("-")[:60] or "study"
    return f"{slug}-export-{now.strftime('%Y%m%d-%H%M')}.zip"


def _stream(file: Any):
    try:
        while chunk := file.read(1024 * 1024):
            yield chunk
    finally:
        file.close()


@router.get(
    "/{study_id}/export",
    summary="Export study data as a ZIP of CSV/JSONL files with a manifest (owner/admin)",
)
def export_study_data(
    study_id: uuid.UUID,
    datasets: Optional[list[str]] = Query(None, description=f"Repeatable; any of {', '.join(DATASETS)} (default all)"),
    export_format: str = Query("csv", alias="format", description="csv or jsonl"),
    arm: Optional[list[uuid.UUID]] = Query(None, description="Only these arms (profile ids); repeatable"),
    participant: Optional[list[uuid.UUID]] = Query(None, description="Only these participants; repeatable"),
    start: Optional[str] = Query(None, description="Inclusive UTC start date (YYYY-MM-DD) for events"),
    end: Optional[str] = Query(None, description="Inclusive UTC end date (YYYY-MM-DD) for events"),
    event_categories: Optional[list[str]] = Query(
        None, description=f"Repeatable; any of {', '.join([*EVENT_CATEGORIES, 'other'])} (default all)"
    ),
    include_content: bool = Query(False, description="Include captured content (JSONL only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    selected = list(dict.fromkeys(datasets or DATASETS))
    unknown = [name for name in selected if name not in DATASETS]
    if unknown:
        raise _unprocessable("UNKNOWN_DATASET", f"unknown datasets: {', '.join(unknown)}")
    if export_format not in FORMATS:
        raise _unprocessable("UNKNOWN_FORMAT", "format must be csv or jsonl")
    categories = list(dict.fromkeys(event_categories or []))
    unknown = [name for name in categories if name not in EVENT_CATEGORIES and name != "other"]
    if unknown:
        raise _unprocessable("UNKNOWN_EVENT_CATEGORY", f"unknown event categories: {', '.join(unknown)}")
    window = _window(start, end)

    db = app.get_db_session()
    try:
        # One snapshot for every dataset of the export.
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        from backend.routers.research.access import require_study_owner

        study = study_store.get_study(db, study_id)
        if study is None:
            raise HTTPException(status_code=404, detail="Study not found")
        require_study_owner(current_user, study)
        study_row = db.get(StudyRow, study_id)
        config = getattr(study_row, "research_config_json", None) or {}
        policy = config.get("telemetry_policy") if isinstance(config.get("telemetry_policy"), dict) else {}
        content_captured = policy.get("content_capture") is True
        if include_content and not content_captured:
            raise _unprocessable("CONTENT_NOT_CAPTURED", "this study does not capture content")
        if include_content and export_format != "jsonl":
            raise _unprocessable("CONTENT_REQUIRES_JSONL", "content is exported in the JSONL format only")

        full_frame = analytics_store.load_study_frame(db, study_id)
        try:
            frame, enrollment_ids, applied = analytics.resolve_filters(full_frame, arm, participant)
        except analytics.FilterError as error:
            raise _unprocessable(error.code, str(error)) from error
        scoped = [uuid.UUID(item) for item in enrollment_ids] if enrollment_ids is not None else None
        scan = scoped is None or bool(scoped)
        codes = {row.enrollment_id: row.participant_code for row in full_frame.enrollments}
        assignments = {row.enrollment_id: row for row in full_frame.assignments}
        arm_of = {row.enrollment_id: row.profile_id for row in full_frame.assignments}
        enrollments = sorted(frame.enrollments, key=lambda row: row.participant_code)
        strategy = assignment_strategy(config)
        arm_rows = db.execute(
            select(
                StudyAgentProfile.profile_id,
                StudyAgentProfile.selection_order,
                StudyAgentProfile.profile_digest,
            )
            .where(StudyAgentProfile.study_id == study_id)
            .order_by(StudyAgentProfile.selection_order.asc(), StudyAgentProfile.profile_id.asc())
        ).all()
        ordered_arms = [str(profile_id) for profile_id, _, _ in arm_rows]

        writer = ExportWriter(export_format)
        columns: dict[str, Any] = {}

        analyses: dict[str, Any] = {}
        if {"participant_metrics", "chats"} & set(selected):
            events = (
                analytics_store.load_events(db, study_id, enrollment_ids=scoped, window=window)
                if scan
                else []
            )
            # Presence and IDE edits come from the per-day aggregates over every
            # retained event, exactly as the analytics summary computes them.
            daily = (
                analytics_store.load_daily_event_counts(db, study_id, enrollment_ids=scoped, window=window)
                if scan
                else []
            )
            events_by_enrollment: dict[str, list] = defaultdict(list)
            for row in events:
                events_by_enrollment[row.enrollment_id].append(row)
            daily_by_enrollment: dict[str, list] = defaultdict(list)
            for row in daily:
                daily_by_enrollment[row.enrollment_id].append(row)
            sessions_by_enrollment: dict[str, list] = defaultdict(list)
            for row in frame.sessions:
                sessions_by_enrollment[row.enrollment_id].append(row)
            for enrollment in enrollments:
                analyses[enrollment.enrollment_id] = analytics.analyze_participant(
                    events_by_enrollment.get(enrollment.enrollment_id, ()),
                    sessions_by_enrollment.get(enrollment.enrollment_id, ()),
                    daily_by_enrollment.get(enrollment.enrollment_id) or None,
                    window=window,
                )

        if "participants" in selected:
            def randomized(enrollment_id: str) -> Optional[str]:
                if strategy != AssignmentStrategy.DETERMINISTIC_HASH.value or not ordered_arms:
                    return None
                return ordered_arms[hash_arm_index(study_id, 0, enrollment_id, len(ordered_arms))]

            columns["participants"] = PARTICIPANT_COLUMNS
            writer.write_rows(
                "participants",
                PARTICIPANT_COLUMNS,
                (
                    {
                        "enrollment_id": row.enrollment_id,
                        "participant_code": row.participant_code,
                        "status": row.status,
                        "enrolled_at": row.enrolled_at,
                        "consent_accepted_at": row.consent_accepted_at,
                        "consent_digest": row.consent_digest,
                        "consent_answers": row.consent_answers,
                        "arm_profile_id": getattr(assignments.get(row.enrollment_id), "profile_id", None),
                        "arm_name": getattr(assignments.get(row.enrollment_id), "name", None),
                        "arm_model": getattr(assignments.get(row.enrollment_id), "model", None),
                        "arm_runtime": getattr(assignments.get(row.enrollment_id), "framework_version", None),
                        "assignment_strategy": getattr(assignments.get(row.enrollment_id), "strategy", None),
                        "assigned_at": getattr(assignments.get(row.enrollment_id), "assigned_at", None),
                        "randomized_profile_id": randomized(row.enrollment_id),
                    }
                    for row in enrollments
                ),
            )
        if "participant_metrics" in selected:
            metric_spec = metric_columns()
            columns["participant_metrics"] = metric_spec
            writer.write_rows(
                "participant_metrics",
                metric_spec,
                (
                    {
                        "enrollment_id": row.enrollment_id,
                        "participant_code": row.participant_code,
                        "arm_profile_id": arm_of.get(row.enrollment_id),
                        "has_telemetry": analyses[row.enrollment_id].has_telemetry,
                        **analyses[row.enrollment_id].metrics(),
                    }
                    for row in enrollments
                ),
            )
        if "sessions" in selected:
            columns["sessions"] = SESSION_COLUMNS
            writer.write_rows(
                "sessions",
                SESSION_COLUMNS,
                (
                    {
                        "session_id": row.session_id,
                        "enrollment_id": row.enrollment_id,
                        "participant_code": codes.get(row.enrollment_id),
                        "state": row.state,
                        "opened_at": row.opened_at,
                        "closed_at": row.closed_at,
                        "last_activity_at": row.last_activity_at,
                        "last_heartbeat_at": row.last_heartbeat_at,
                        "close_reason": row.close_reason,
                        "session_seconds": round(analytics.session_seconds(row, window), 1),
                    }
                    for row in sorted(frame.sessions, key=lambda item: (item.opened_at is None, item.opened_at or datetime.min.replace(tzinfo=timezone.utc)))
                ),
            )
        if "chats" in selected:
            columns["chats"] = CHAT_COLUMNS
            writer.write_rows(
                "chats",
                CHAT_COLUMNS,
                (
                    {
                        "enrollment_id": row.enrollment_id,
                        "participant_code": row.participant_code,
                        "arm_profile_id": arm_of.get(row.enrollment_id),
                        **chat,
                    }
                    for row in enrollments
                    for chat in analytics.chat_rows(analyses[row.enrollment_id].chats, limit=10**9)
                ),
            )
        if "events" in selected:
            event_spec = EVENT_JSONL_COLUMNS if export_format == "jsonl" else EVENT_CSV_COLUMNS

            def event_rows():
                if not scan:
                    return
                for (
                    event_id,
                    occurred_at,
                    enrollment_id,
                    research_session_id,
                    event_type,
                    source,
                    emitter_id,
                    emitter_sequence,
                    envelope,
                ) in iter_export_events(
                    db, study_id, enrollment_ids=scoped, window=window, categories=categories or None
                ):
                    base = {
                        "event_id": event_id,
                        "occurred_at": occurred_at,
                        "enrollment_id": enrollment_id,
                        "participant_code": codes.get(enrollment_id),
                        "arm_profile_id": arm_of.get(enrollment_id),
                        "research_session_id": research_session_id,
                        "event_type": event_type,
                        "source": source,
                    }
                    envelope = envelope if isinstance(envelope, dict) else {}
                    if export_format == "jsonl":
                        yield {**base, "envelope": export_envelope(envelope, include_content=include_content)}
                    else:
                        yield {
                            **base,
                            "emitter_id": emitter_id,
                            "emitter_sequence": emitter_sequence,
                            **event_csv_row(envelope),
                        }

            columns["events"] = event_spec
            writer.write_rows("events", event_spec, event_rows())

        now = datetime.now(timezone.utc)
        writer.write_json(
            "manifest.json",
            {
                "export_version": EXPORT_VERSION,
                "generated_at": now.isoformat(),
                "study": {
                    "study_id": str(study_id),
                    "name": getattr(study_row, "name", None),
                    "research_status": getattr(study_row, "research_status", None),
                    "created_at": optional_iso(getattr(study_row, "created_at", None)),
                    "research_config_digest": getattr(study_row, "research_config_digest", None),
                },
                "telemetry_policy": {
                    "content_capture": content_captured,
                    "allowed_field_classes": list(policy.get("allowed_field_classes") or []),
                },
                "assignment": {
                    "strategy": strategy,
                    "manual_override": manual_override_enabled(config),
                    "formula": ASSIGNMENT_FORMULA if strategy == AssignmentStrategy.DETERMINISTIC_HASH.value else None,
                    "arms": [
                        {"index": index, "profile_id": str(profile_id), "selection_order": order, "profile_digest": digest}
                        for index, (profile_id, order, digest) in enumerate(arm_rows)
                    ],
                },
                "filters": {
                    "datasets": selected,
                    "format": export_format,
                    "arms": applied["arms"],
                    "participants": applied["participants"],
                    "start": start,
                    "end": end,
                    "event_categories": categories or "all",
                    "include_content": include_content,
                },
                "files": writer.counts,
                "columns": dictionary(columns),
                "notes": [
                    "Participants are identified only by enrollment id and study-local participant code.",
                    "Events deleted by retention or erasure are not included.",
                    "Metrics and chats are computed over the exported date range, like the analytics page.",
                ],
            },
        )
        logging.info(
            "[Research/export] user %s exported study=%s datasets=%s format=%s include_content=%s files=%s",
            current_user.user_id,
            study_id,
            ",".join(selected),
            export_format,
            include_content,
            writer.counts,
        )
        archive = writer.finish()
        filename = _filename(getattr(study_row, "name", ""), now)
    finally:
        db.close()
    return StreamingResponse(
        _stream(archive),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )
