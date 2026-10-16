"""Owner-only trace of one participant chat (prompts, reasoning, tool calls).

Separate from the analytics router because a trace returns stored content:
what the study's telemetry policy and the participant's consent kept, with
``[REDACTED]`` values reported as not captured. Study owner or administrator
only, never cached, and every read is logged.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid  # noqa: TC003 - FastAPI resolves the path-parameter annotations at runtime
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from database.db_schemas import Study as StudyRow
from database.research_schemas import ResearchEnrollment
from research.analysis.study_analytics import store as analytics_store
from research.analysis.study_traces.assemble import build_page
from research.analysis.study_traces.store import iter_chat_events
from research.study.protocol import store as study_store

router = APIRouter()

MAX_TURNS_PER_PAGE = 100


def _authorize(db: Any, current_user: AuthenticatedUser, study_id: uuid.UUID) -> None:
    from backend.routers.research.access import require_study_owner

    study = study_store.get_study(db, study_id)
    if study is None:
        raise HTTPException(status_code=404, detail="Study not found")
    require_study_owner(current_user, study)


def _invalid_cursor() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"code": "INVALID_CURSOR", "message": "the page cursor is not valid"},
    )


def encode_cursor(row: Any, next_index: int) -> str:
    occurred_at, emitter_id, emitter_sequence, event_id = row.key
    raw = json.dumps(
        {"t": occurred_at.isoformat(), "e": emitter_id, "s": emitter_sequence, "i": event_id, "n": next_index},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[tuple[datetime, str, int, str], int]:
    """``(position of the page's first event, turns before the page)``."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        position = (
            datetime.fromisoformat(data["t"]),
            str(data["e"]),
            int(data["s"]),
            str(uuid.UUID(str(data["i"]))),
        )
        next_index = int(data["n"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError) as error:
        raise _invalid_cursor() from error
    if next_index < 0:
        raise _invalid_cursor()
    return position, next_index


@router.get(
    "/{study_id}/enrollments/{enrollment_id}/trace",
    summary="One participant chat as turns of prompts, reasoning, messages and tool calls (owner/admin)",
)
def study_chat_trace(
    study_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    chat_id: str = Query(..., min_length=1, max_length=256, description="The ACP chat (session) id"),
    cursor: Optional[str] = Query(None, max_length=1024),
    limit: int = Query(20, ge=1, le=MAX_TURNS_PER_PAGE, description="Turns per page"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        _authorize(db, current_user, study_id)
        if analytics_store.enrollment_study_id(db, enrollment_id) != study_id:
            raise HTTPException(
                status_code=404,
                detail={"code": "ENROLLMENT_NOT_FOUND", "message": "no such enrollment in this study"},
            )
        start_index = 0
        position = None
        if cursor:
            position, start_index = decode_cursor(cursor)
        turns, next_row, next_index = build_page(
            iter_chat_events(db, study_id, enrollment_id, chat_id, start_at=position),
            limit=limit,
            start_index=start_index,
        )
        if not cursor and not turns:
            raise HTTPException(
                status_code=404,
                detail={"code": "CHAT_NOT_FOUND", "message": "no events of this chat in this enrollment"},
            )
        study_row = db.get(StudyRow, study_id)
        policy = ((getattr(study_row, "research_config_json", None) or {}).get("telemetry_policy")) or {}
        participant_code = db.get(ResearchEnrollment, enrollment_id).participant_code
        logging.info(
            "[Research/trace] user %s read chat trace study=%s enrollment=%s turns=%d",
            current_user.user_id,
            study_id,
            enrollment_id,
            len(turns),
        )
        response = JsonResponseWithStatus(
            status_code=200,
            content={
                "study_id": str(study_id),
                "enrollment_id": str(enrollment_id),
                "participant_code": participant_code,
                "chat_id": chat_id,
                "content_capture_enabled": isinstance(policy, dict) and policy.get("content_capture") is True,
                "turns": turns,
                "next_cursor": encode_cursor(next_row, next_index) if next_row is not None else None,
                "has_more": next_row is not None,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    finally:
        db.close()
