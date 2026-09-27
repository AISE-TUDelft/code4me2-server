"""Erase one account's research participation (GDPR Art. 17).

Hard-deletes everything the research tables hold for the account's participant:
the research events, batch receipts and retention jobs of its enrollments and
sessions, then the participant row itself, which cascades to the enrollments,
sessions, agent runs, assignments and inference-budget rows. Telemetry rows use
``SET NULL`` foreign keys (retention policy, not a parent-row delete, decides
when events disappear), so they are deleted explicitly first: a parent delete
alone would orphan them with their content.

Every erased enrollment leaves one content-free deletion-ledger record (study,
time and counts; never the account, email or participant code) so researchers
can still account for attrition.

Like the other persistence helpers here, these functions take a caller-managed
session and never commit: an account erasure is one transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import delete, func, or_, select

from database.research_schemas import (
    ResearchEnrollment,
    ResearchEvent,
    ResearchParticipant,
    ResearchRetentionJob,
    TelemetryBatchReceipt,
)
from database.research_schemas import (
    ResearchSessionV1 as ResearchSessionRow,
)
from research.canonical import canonical_hash
from research.study.protocol.enums import RetentionAction

from .identity import (
    deletion_ledger_record,
    get_participant_by_account,
    lock_participant_by_account,
)
from .models import DeletionLedgerEntry

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Ledger ``reason`` distinguishing a participant's erasure request from a
# study's scheduled retention.
ERASURE_LEDGER_REASON = "participant_erasure"


@dataclass(frozen=True)
class ParticipantDataCounts:
    enrollments: int = 0
    events: int = 0


def _event_scope(enrollment_ids: list[uuid.UUID], session_ids: list[uuid.UUID]):
    """Events belong to an enrollment directly or through one of its sessions."""
    condition = ResearchEvent.enrollment_id.in_(enrollment_ids)
    if session_ids:
        condition = or_(condition, ResearchEvent.research_session_id.in_(session_ids))
    return condition


def participant_data_counts(session: Session, account_id: uuid.UUID) -> ParticipantDataCounts:
    """Count the research data held for an account (for the privacy summary)."""
    participant = get_participant_by_account(session, account_id)
    if participant is None:
        return ParticipantDataCounts()
    enrollment_ids = list(
        session.execute(
            select(ResearchEnrollment.enrollment_id).where(
                ResearchEnrollment.participant_id == participant.participant_id
            )
        ).scalars()
    )
    if not enrollment_ids:
        return ParticipantDataCounts()
    session_ids = list(
        session.execute(
            select(ResearchSessionRow.session_id).where(
                ResearchSessionRow.enrollment_id.in_(enrollment_ids)
            )
        ).scalars()
    )
    events = session.execute(
        select(func.count()).select_from(ResearchEvent).where(
            _event_scope(enrollment_ids, session_ids)
        )
    ).scalar_one()
    return ParticipantDataCounts(enrollments=len(enrollment_ids), events=int(events))


def erase_participant_data(
    session: Session, account_id: uuid.UUID, *, now: Optional[datetime] = None
) -> ParticipantDataCounts:
    """Delete the account's research participation and telemetry; return counts."""
    # Pending ORM changes (e.g. a withdrawal in the same transaction) must reach
    # the database before the bulk deletes remove their rows.
    session.flush()
    participant = lock_participant_by_account(session, account_id)
    if participant is None:
        return ParticipantDataCounts()
    timestamp = now or datetime.now(timezone.utc)
    enrollments = session.execute(
        select(ResearchEnrollment.enrollment_id, ResearchEnrollment.study_id).where(
            ResearchEnrollment.participant_id == participant.participant_id
        )
    ).all()
    erased_events = 0
    for enrollment_id, study_id in enrollments:
        session_ids = list(
            session.execute(
                select(ResearchSessionRow.session_id).where(
                    ResearchSessionRow.enrollment_id == enrollment_id
                )
            ).scalars()
        )
        events = session.execute(
            delete(ResearchEvent).where(_event_scope([enrollment_id], session_ids))
        ).rowcount
        receipt_scope = TelemetryBatchReceipt.enrollment_id == enrollment_id
        if session_ids:
            receipt_scope = or_(
                receipt_scope, TelemetryBatchReceipt.research_session_id.in_(session_ids)
            )
        session.execute(delete(TelemetryBatchReceipt).where(receipt_scope))
        session.execute(
            delete(ResearchRetentionJob).where(
                ResearchRetentionJob.enrollment_id == enrollment_id
            )
        )
        entry = DeletionLedgerEntry(
            ledger_id=uuid.uuid4(),
            enrollment_id=enrollment_id,
            action=RetentionAction.DELETE_ALL,
            applied_at=timestamp,
            affected_count=events,
            evidence_digest=canonical_hash(
                {
                    "action": RetentionAction.DELETE_ALL.value,
                    "enrollment_id": str(enrollment_id),
                    "reason": ERASURE_LEDGER_REASON,
                    "remaining": [],
                }
            ),
        )
        session.add(
            deletion_ledger_record(entry, study_id=study_id, reason=ERASURE_LEDGER_REASON)
        )
        erased_events += events
    session.execute(
        delete(ResearchParticipant).where(
            ResearchParticipant.participant_id == participant.participant_id
        )
    )
    return ParticipantDataCounts(enrollments=len(enrollments), events=erased_events)
