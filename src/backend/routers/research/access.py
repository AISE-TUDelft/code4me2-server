"""Researcher/administrator authorization helpers (consolidation phase 01).

There is exactly one researcher authority: the ``user.can_research`` flag that an
administrator enables (P3). There is no per-study role table. Study ownership is
``study.created_by == user.user_id``; profile ownership is
``agent_profile.owner_user_id``. Administrators bypass every ownership check.
No route may let a non-administrator change ``can_research`` or ``is_admin``.

This is also the single funded-access gate (phase 03): every funded operation
(participant relay inference, managed ACP inference, self-report run
creation/replay, bootstrap session creation, ACP grant minting) resolves the same
current server state here — the account is a participant, has a live (``ACTIVE``)
enrollment, the research study window is open, and no operator kill switch is
engaged. Server time is authoritative; a previously issued capability never
substitutes for this live re-check.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from fastapi import HTTPException

from backend.routers.analytics.auth_utils import AuthenticatedUser
from database.db_schemas import Study as StudyRow
from database.research_schemas import ResearchSessionV1 as ResearchSessionRow
from research.participants import identity as identity_store
from research.participants.enums import EnrollmentStatus
from research.study.protocol import store as protocol_store

__all__ = [
    "is_researcher",
    "require_researcher",
    "is_owner",
    "require_owner",
    "is_study_owner",
    "require_study_owner",
    "FundedAccessRefused",
    "require_live_enrollment",
    "ResearchBinding",
    "resolve_research_binding",
]


def is_researcher(user: AuthenticatedUser) -> bool:
    """Whether the caller may act as a researcher (admin or enabled account)."""
    return bool(
        getattr(user, "is_admin", False) or getattr(user, "can_research", False)
    )


def require_researcher(user: AuthenticatedUser) -> AuthenticatedUser:
    """Reject a caller who is neither an administrator nor an enabled researcher."""
    if not is_researcher(user):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "RESEARCHER_REQUIRED",
                "message": (
                    "an administrator must enable this account for research"
                ),
            },
        )
    return user


def is_owner(user: AuthenticatedUser, owner_user_id) -> bool:
    """Whether ``user`` owns a row (administrators own everything)."""
    if getattr(user, "is_admin", False):
        return True
    if owner_user_id is None:
        return False
    return str(owner_user_id) == str(user.user_id)


def require_owner(user: AuthenticatedUser, owner_user_id, *, subject: str = "resource"):
    """Reject a non-owner, non-administrator caller."""
    if not is_owner(user, owner_user_id):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN_OWNER",
                "message": f"not authorized for this {subject}",
            },
        )


def is_study_owner(user: AuthenticatedUser, study_row) -> bool:
    """Study ownership is ``study.created_by`` (admin bypasses)."""
    return is_owner(user, getattr(study_row, "created_by", None))


def require_study_owner(user: AuthenticatedUser, study_row):
    """Reject a caller who neither owns the study nor is an administrator."""
    if not is_study_owner(user, study_row):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN_STUDY",
                "message": "not authorized for this study",
            },
        )


# ---------------------------------------------------------------------------
# Funded-access gate (phase 03; folded from research/runtime/funding.py)
# ---------------------------------------------------------------------------


class FundedAccessRefused(Exception):
    """A typed refusal for a funded path (mapped to HTTP 403 by routers)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def require_live_enrollment(
    db: Any,
    *,
    account_id: Optional[uuid.UUID],
    study_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
    kill_switch_check: Optional[Callable[[], bool]] = None,
):
    """Return the account's live enrollment or raise :class:`FundedAccessRefused`.

    ``study_id`` (when given) must match the live enrollment's study, so a task
    minted for one study cannot be funded by an enrollment in another.
    """
    if account_id is None:
        raise FundedAccessRefused(
            "ACCOUNT_REQUIRED", "funded research use requires an authenticated account"
        )

    participant_row = identity_store.get_participant_by_account(db, account_id)
    if participant_row is None:
        raise FundedAccessRefused(
            "NOT_A_PARTICIPANT", "this account has no research participant mapping"
        )

    active = None
    for row in identity_store.list_enrollments(db, participant_row.participant_id):
        if row.status == EnrollmentStatus.ACTIVE.value:
            active = row
            break
    if active is None:
        raise FundedAccessRefused(
            "ENROLLMENT_NOT_ACTIVE",
            "this account has no active study enrollment",
        )

    if study_id is not None and active.study_id != study_id:
        raise FundedAccessRefused(
            "ENROLLMENT_STUDY_MISMATCH",
            "the active enrollment is not for this study",
        )

    study = db.get(StudyRow, active.study_id)
    if not protocol_store.research_study_is_open(study, _now(now)):
        # Lazy terminal sweep: a study that ended or was terminated is marked
        # completed here (COMPLETED enrollments, revoked sessions, bumped epoch)
        # so a previously issued capability cannot keep funded access alive.
        _sweep_ended_study(db, study, active.study_id, now=_now(now))
        raise FundedAccessRefused(
            "STUDY_NOT_OPEN",
            "the study is not currently open (not started, ended, or terminated)",
        )

    if kill_switch_check is not None and kill_switch_check():
        raise FundedAccessRefused(
            "KILL_SWITCH_ENGAGED",
            "an operator kill switch is engaged for this study",
        )

    return active


def _sweep_ended_study(db, study, study_id, *, now: datetime) -> None:
    """Complete enrollments for a study that is no longer open for execution.

    Only sweeps when the study is an ended/terminated research study (not merely
    pre-start), so a scheduled future study's pre-start enrollments are left
    intact.
    """
    if study is None or not getattr(study, "is_research", False):
        return
    ends_at = getattr(study, "ends_at", None)
    ended = ends_at is not None and now > ends_at
    terminated = not getattr(study, "is_active", False)
    if not (ended or terminated):
        return
    try:
        identity_store.complete_enrollments_for_study(db, study_id, now=now)
    except Exception as error:  # noqa: BLE001 - the refusal above is decisive
        logging.warning(
            "[Funding] could not complete enrollments for closed study %s: %s",
            study_id,
            error,
        )


# ---------------------------------------------------------------------------
# Explicit research attribution (phase 05)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchBinding:
    """An agent task's explicit research attribution.

    Resolved server-side from the *authorized* account and the frozen
    assignment's study; never supplied by a caller and never inferred from a
    time window. ``research_session_id`` is ``None`` when the enrollment has no
    single active session (none, or more than one open window) — an explicit
    "unknown context" state rather than a guessed one.
    """

    enrollment_id: uuid.UUID
    study_revision_id: uuid.UUID
    research_session_id: Optional[uuid.UUID]


def resolve_research_binding(
    db: Any, *, account_id: Optional[uuid.UUID], study_id: Optional[uuid.UUID]
) -> Optional[ResearchBinding]:
    """Resolve the account's live research attribution for ``study_id``.

    Returns ``None`` (no research context) when the account has no participant
    mapping or no ACTIVE enrollment in that study. A single active session for
    the enrollment is bound explicitly; zero or multiple active sessions leave
    ``research_session_id`` unknown.
    """
    if account_id is None or study_id is None:
        return None
    participant_row = identity_store.get_participant_by_account(db, account_id)
    if participant_row is None:
        return None
    active = next(
        (
            row
            for row in identity_store.list_enrollments(db, participant_row.participant_id)
            if row.status == EnrollmentStatus.ACTIVE.value
            and getattr(row, "study_id", None) == study_id
        ),
        None,
    )
    if active is None:
        return None

    session_rows = (
        db.query(ResearchSessionRow)
        .filter(
            ResearchSessionRow.enrollment_id == active.enrollment_id,
            ResearchSessionRow.state.notin_(("ended", "revoked")),
        )
        .all()
    )
    research_session_id = (
        session_rows[0].session_id if len(session_rows) == 1 else None
    )
    return ResearchBinding(
        enrollment_id=active.enrollment_id,
        study_revision_id=active.study_revision_id,
        research_session_id=research_session_id,
    )

