"""Administrator account management (P3).

Mounted under ``/api/research/researchers``. An administrator lists every
account and flips the single ``user.can_research`` flag that lets an account own
private profiles and create studies. There is no self-service route: no
non-administrator can change ``can_research`` or ``is_admin``, and there is no
admin-promotion endpoint.

The listing is administrator-only, so it may link an account to the studies it
is enrolled in (study name/status, enrollment status). It never includes the
study-local participant code, the enrollment id, the assigned profile or the
enrollment time, and the account's join time is reduced to its UTC date.

This is not unlinkability. Administrators are trusted operators (they also
operate the database): study membership, the enrollment status, the join day
and the listing order (newest account first) can still single out a
participant. For example, someone who signs up just before joining has a join
day that matches the enrollment time the study analytics show, and in a small
study the order alone may match. The pseudonymisation keeps identities from
researchers, whose views never show an account.
"""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    require_admin,
)
from database import crud

router = APIRouter()

#: Upper bound so one administrator request can never materialize every account
#: ever created into a single response.
MAX_ACCOUNT_PAGE = 500

#: ``researcher`` = ``can_research`` and not admin; ``participant`` = neither.
AccountRoleFilter = Literal["all", "admin", "researcher", "participant"]
#: ``enrolled`` = has an ACTIVE enrollment; ``not_enrolled`` = has none.
AccountEnrollmentFilter = Literal["any", "enrolled", "not_enrolled"]


class ResearcherEnableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    can_research: bool


def _utc_date(value: Any) -> Optional[str]:
    """The UTC calendar day of ``value``; the listing needs no finer time."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.date().isoformat()


def _user_payload(user) -> dict:
    return {
        "user_id": str(user.user_id),
        "email": user.email,
        "name": user.name,
        "is_admin": bool(user.is_admin),
        "can_research": bool(user.can_research),
        "verified": bool(user.verified),
    }


def _enrollment_summary(row: Any) -> dict[str, Any]:
    """One enrollment of an account: study identity and statuses only.

    The enrollment id and the enrollment time are left out: study telemetry is
    keyed by the id, and the study analytics list each pseudonymous participant
    with its exact enrollment time, so either would link an account to its
    pseudonym. The list is still ordered newest first.
    """
    return {
        "study_id": str(row.study_id),
        "study_name": row.study_name,
        "study_status": row.study_status,
        "status": row.status,
    }


def _account_payload(user, enrollments: list) -> dict[str, Any]:
    payload = _user_payload(user)
    payload["joined_at"] = _utc_date(getattr(user, "joined_at", None))
    payload["enrollments"] = [_enrollment_summary(row) for row in enrollments]
    return payload


@router.get("", summary="List accounts (admin)")
def list_accounts(
    q: Optional[str] = Query(
        None,
        max_length=200,
        description="Case-insensitive substring of the account email or name.",
    ),
    role: AccountRoleFilter = Query(
        "all",
        description="admin | researcher (can_research, not admin) | "
        "participant (neither) | all",
    ),
    enrollment: AccountEnrollmentFilter = Query(
        "any",
        description="enrolled (has an ACTIVE enrollment) | not_enrolled | any",
    ),
    study_id: Optional[uuid.UUID] = Query(
        None, description="Only accounts with any enrollment in this study."
    ),
    limit: int = Query(100, ge=1, le=MAX_ACCOUNT_PAGE),
    offset: int = Query(0, ge=0),
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    """All accounts (newest first), so an administrator can enable researchers.

    Despite the route name this is the full account list, not only the accounts
    already enabled for research: the toggle in the admin UI needs both states.
    ``total`` counts every account matching the filters before paging. Each row
    lists the account's enrollments (newest first) with the study name and
    status, loaded for the whole page in one query (no per-account queries).
    """
    require_admin(current_user)
    db = app.get_db_session()
    try:
        filters = {
            "q": q,
            "role": role,
            "enrollment": enrollment,
            "study_id": study_id,
        }
        total = crud.count_accounts(db, **filters)
        users = crud.list_accounts(db, limit, offset=offset, **filters)
        enrollments = crud.list_account_enrollments(
            db, [user.user_id for user in users]
        )
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "researchers": [
                    _account_payload(user, enrollments.get(user.user_id, []))
                    for user in users
                ],
                "total": total,
                "limit": limit,
                "offset": offset,
            },
        )
    finally:
        db.close()


@router.put("/{user_id}", summary="Enable or disable a researcher account (admin)")
def set_researcher_enabled(
    user_id: uuid.UUID,
    payload: ResearcherEnableRequest,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    require_admin(current_user)
    db = app.get_db_session()
    try:
        user = crud.set_user_can_research(db, user_id, payload.can_research)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return JsonResponseWithStatus(
            status_code=200, content={"user": _user_payload(user)}
        )
    finally:
        db.close()
