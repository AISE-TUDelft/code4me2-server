"""Participant self-enrollment by study join code.

Mounted under ``/api/research/join``:

* ``GET /{join_code}`` resolves a shared code to study/revision metadata and the
  single global consent text for any authenticated user. It never returns
  account, participant or secret material.
* ``POST ""`` redeems the code for the *caller*: it creates (or reuses) their
  participant mapping and ACTIVE enrollment for the code's exact revision, and
  returns the enrollment identity. Consent is a single acceptance recorded once
  here (``consent_accepted_at``); there is no document identity, re-consent or
  withdrawal. Posting is idempotent: a repeat returns the same enrollment and
  never mints a second one.

The request can never choose the revision or the eligibility verdict: the
revision comes from the join code and is always derived server-side.
"""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
)
from research.participants import identity as identity_store
from research.study.protocol import store as protocol_store
from research.study.protocol.enums import RevisionStatus

if TYPE_CHECKING:
    from research.participants.models import IdentityIssue

router = APIRouter()

# The one global consent policy. There is deliberately no per-study document
# identity/version/digest and no consent table: a participant accepts this text
# once when they join, and that acceptance is recorded on the enrollment.
GLOBAL_CONSENT_TEXT = (
    "This study collects metadata about how you use the research agent and the "
    "IDE (for example which tools run, timings, token usage and file-level "
    "activity). Prompts, source code and tool output are collected only if the "
    "study's published telemetry policy allows content. Provider credentials are "
    "never collected. Your study-local pseudonym is used in all research data; "
    "your account identity is stored only in the private enrollment mapping."
)


class JoinRequestBody(BaseModel):
    """Redeem a join code for the authenticated account.

    ``extra="forbid"`` is deliberate: a client cannot smuggle in a different
    ``revision_id``, an ``eligibility`` verdict or a consent document; those are
    always derived server-side from the code's published revision.
    """

    model_config = ConfigDict(extra="forbid")

    join_code: str = Field(min_length=1)
    accept_consent: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _issue_payload(issue: Optional[IdentityIssue]) -> dict[str, Any]:
    if issue is None:
        return {}
    return issue.model_dump(mode="json")


def _join_payload(enrollment: Any, *, created: bool, reused: bool) -> dict[str, Any]:
    return {
        "enrollment_id": str(enrollment.enrollment_id),
        "study_id": str(enrollment.study_id),
        "revision_id": str(enrollment.study_revision_id),
        "status": enrollment.status.value,
        "created": created,
        "reused": reused,
    }


def _load_revision_by_code(db: Any, join_code: str):
    """Return ``(row, domain revision)`` for a stored code, or ``None``."""
    row = protocol_store.get_revision_by_join_code(db, join_code)
    if row is None:
        return None
    return row, protocol_store.row_to_revision(row)


@router.get(
    "/{join_code}", summary="Resolve a study join code to revision metadata"
)
def resolve_join_code(
    join_code: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Any authenticated user: study/revision metadata for a shared code.

    Only non-secret study metadata, the revision identity/digest and the global
    consent text are returned; no account or participant data.
    """
    db = app.get_db_session()
    try:
        loaded = _load_revision_by_code(db, join_code)
        if loaded is None:
            raise HTTPException(status_code=404, detail="Join code not found")
        row, revision = loaded
        study = protocol_store.get_study(db, revision.study_id)
        published_at = revision.published_at
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "join_code": row.join_code,
                "study": {
                    "study_id": str(revision.study_id),
                    "name": study.name if study is not None else "",
                },
                "revision": {
                    "revision_id": str(revision.revision_id),
                    "revision_number": revision.revision_number,
                    "status": revision.status.value,
                    "protocol_digest": revision.protocol_digest,
                    "published_at": (
                        published_at.isoformat()
                        if isinstance(published_at, datetime)
                        else None
                    ),
                },
                "consent": {"text": GLOBAL_CONSENT_TEXT},
            },
        )
    finally:
        db.close()


@router.post("", summary="Redeem a join code and enroll the caller")
def redeem_join_code(
    payload: JoinRequestBody,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Idempotently enroll the caller in the code's published revision."""
    now = _now()
    db = app.get_db_session()
    try:
        loaded = _load_revision_by_code(db, payload.join_code)
        if loaded is None:
            raise HTTPException(status_code=404, detail="Join code not found")
        _row, revision = loaded
        if revision.status != RevisionStatus.PUBLISHED:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "UNKNOWN_REVISION",
                    "message": "the join code's revision is not published",
                },
            )

        if not payload.accept_consent:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CONSENT_REQUIRED",
                    "message": "joining a study requires accepting the consent text",
                },
            )

        ref = identity_store.revision_ref_from_study_revision(revision)

        # Shared account-wide enrollment owner: locks the participant, enforces
        # one live enrollment and refuses a rejoin to a completed study.
        opened = identity_store.open_enrollment(db, current_user.user_id, ref, now=now)
        if opened.issue is not None or opened.enrollment is None:
            raise HTTPException(status_code=409, detail=_issue_payload(opened.issue))

        return JsonResponseWithStatus(
            status_code=201 if opened.created else 200,
            content=_join_payload(
                opened.enrollment,
                created=opened.created,
                reused=not opened.created,
            ),
        )
    finally:
        db.close()
