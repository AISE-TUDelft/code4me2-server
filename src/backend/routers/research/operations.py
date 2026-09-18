"""Kill-switch API (Issue 13).

These admin-only routes are merged with the Issue 12 read-model routes behind
the single ``/research/operations`` router (see
:mod:`backend.routers.research`). All handlers are thin; the domain logic lives
in :mod:`research.analysis.operations`. Every endpoint here requires an
operator/admin identity because it engages/releases an enforcement control.
"""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import AuthenticatedUser, require_admin
from research.analysis.operations import store as operations_store
from research.analysis.operations.enums import (
    KillSwitchScopeKind,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
)
from research.analysis.operations.models import (
    KillSwitchRecord,
    KillSwitchScope,
)

operations_router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class KillSwitchEngageRequest(BaseModel):
    """Engage a kill switch at a study or enrollment scope."""

    scope_kind: KillSwitchScopeKind
    scope_id: uuid.UUID
    reason: str
    effective_until: Optional[datetime] = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


@operations_router.get("/kill-switch", summary="List kill-switch records")
def list_kill_switches(
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        records = operations_store.kill_switch_records(db)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "records": [record.model_dump(mode="json") for record in records],
                "engaged": [
                    str(record.switch_id)
                    for record in records
                    if record.is_engaged(_now())
                ],
            },
        )
    finally:
        db.close()


@operations_router.post("/kill-switch", summary="Engage a kill switch")
def engage_kill_switch(
    payload: KillSwitchEngageRequest,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        if payload.scope_kind == KillSwitchScopeKind.STUDY:
            from research.study.protocol import store as study_store

            study = study_store.get_study(db, payload.scope_id)
            if study is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "STUDY_NOT_FOUND", "message": "Study not found"},
                )
            if getattr(study, "research_status", None) == "STUDY_STOPPED":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "STUDY_STOPPED",
                        "message": "The study has stopped and cannot accept a kill switch",
                    },
                )
        record = KillSwitchRecord(
            switch_id=uuid.uuid4(),
            scope=KillSwitchScope(
                kind=payload.scope_kind, scope_id=payload.scope_id
            ),
            reason=payload.reason,
            actor=current_user.email,
            engaged_at=_now(),
            effective_until=payload.effective_until,
        )
        row = operations_store.engage_kill_switch(db, record)
        return JsonResponseWithStatus(
            status_code=201, content={"switch_id": str(row.switch_id), "reason": row.reason}
        )
    finally:
        db.close()


@operations_router.post(
    "/kill-switch/{switch_id}/release", summary="Release a kill switch"
)
def release_kill_switch_endpoint(
    switch_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        row = operations_store.release_kill_switch(db, switch_id, released_at=_now())
        if row is None:
            raise HTTPException(status_code=404, detail="Kill switch not found")
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "switch_id": str(row.switch_id),
                "released_at": row.released_at.isoformat() if row.released_at else None,
            },
        )
    finally:
        db.close()
