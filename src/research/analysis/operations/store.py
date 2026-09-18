"""Persistence helpers for pilot operations, release gate and kill switch.

Every helper takes a caller-supplied SQLAlchemy ``Session`` and returns domain
models (never raw ORM rows) so the domain layer stays free of DB/framework
imports. Kill-switch engagements, health snapshots, release evidence and pilot
runs are all consolidated into the single generic ``research_record`` table.

Deletion-drill *verification* is a pure function
(:func:`research.analysis.operations.retention.verify_deletion`); the single
retention-evidence writer is the identity package's ``insert_deletion_ledger``,
which writes ``research_record`` (``kind = RETENTION_EVIDENCE``) and merges onto
``research_retention_job.evidence_json``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from sqlalchemy import select

from database.research_schemas import (
    RECORD_KIND_HEALTH,
    RECORD_KIND_KILL_SWITCH,
    RECORD_KIND_PILOT_RUN,
    RECORD_KIND_RELEASE_EVIDENCE,
    ResearchRecord,
)
from research.analysis.operations.kill_switch import KillSwitchRegistry

from .models import (
    KillSwitchRecord,
    OperationalHealthV1,
    PilotRunV1,
    ReleaseEvidenceV1,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from .enums import PilotClassification

__all__ = [
    "create_health",
    "create_pilot_run",
    "create_release_evidence",
    "db_kill_switch_check",
    "engage_kill_switch",
    "get_health",
    "get_kill_switch",
    "get_pilot_run",
    "get_release_evidence",
    "is_kill_switch_engaged",
    "kill_switch_records",
    "list_health",
    "list_pilot_runs",
    "list_release_evidence",
    "release_kill_switch",
    "row_to_kill_switch",
    "set_release_decision",
]


def _record(
    *,
    record_id: uuid.UUID,
    kind: str,
    occurred_at: datetime,
    payload_json: dict,
    scope_type: Optional[str] = None,
    scope_id: Optional[uuid.UUID] = None,
    study_id: Optional[uuid.UUID] = None,
    actor: Optional[str] = None,
) -> ResearchRecord:
    return ResearchRecord(
        record_id=record_id,
        kind=kind,
        scope_type=scope_type,
        scope_id=scope_id,
        study_id=study_id,
        actor=actor,
        occurred_at=occurred_at,
        payload_json=payload_json,
    )


# ---------------------------------------------------------------------------
# Operational health
# ---------------------------------------------------------------------------


def create_health(session: Session, snapshot: OperationalHealthV1) -> ResearchRecord:
    """Insert one health snapshot (the full model is stored as JSON)."""
    row = _record(
        record_id=uuid.uuid4(),
        kind=RECORD_KIND_HEALTH,
        occurred_at=snapshot.captured_at,
        payload_json=snapshot.model_dump(mode="json"),
        scope_type="study",
        scope_id=snapshot.study_id,
        study_id=snapshot.study_id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _row_to_health(row: ResearchRecord) -> OperationalHealthV1:
    return OperationalHealthV1.model_validate(row.payload_json)


def get_health(
    session: Session, health_id: uuid.UUID
) -> Optional[OperationalHealthV1]:
    """Fetch one health snapshot by id, or ``None``."""
    row = session.get(ResearchRecord, health_id)
    if row is None or row.kind != RECORD_KIND_HEALTH:
        return None
    return _row_to_health(row)


def list_health(
    session: Session, study_id: uuid.UUID
) -> Sequence[OperationalHealthV1]:
    """List a study's health snapshots, oldest-first."""
    statement = (
        select(ResearchRecord)
        .where(
            ResearchRecord.study_id == study_id,
            ResearchRecord.kind == RECORD_KIND_HEALTH,
        )
        .order_by(ResearchRecord.occurred_at.asc())
    )
    return [_row_to_health(row) for row in session.execute(statement).scalars().all()]


# ---------------------------------------------------------------------------
# Release evidence
# ---------------------------------------------------------------------------


def create_release_evidence(
    session: Session, evidence: ReleaseEvidenceV1
) -> ReleaseEvidenceV1:
    """Insert one immutable release-evidence record."""
    row = _record(
        record_id=evidence.release_id,
        kind=RECORD_KIND_RELEASE_EVIDENCE,
        occurred_at=evidence.recorded_at or datetime.now(),
        payload_json=evidence.model_dump(mode="json"),
        scope_type="study",
        scope_id=evidence.study_id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return evidence


def _row_to_release_evidence(row: ResearchRecord) -> ReleaseEvidenceV1:
    return ReleaseEvidenceV1.model_validate(row.payload_json)


def get_release_evidence(
    session: Session, release_id: uuid.UUID
) -> Optional[ReleaseEvidenceV1]:
    """Fetch one release-evidence record by id, or ``None``."""
    row = session.get(ResearchRecord, release_id)
    if row is None or row.kind != RECORD_KIND_RELEASE_EVIDENCE:
        return None
    return _row_to_release_evidence(row)


def list_release_evidence(
    session: Session, study_id: uuid.UUID
) -> Sequence[ReleaseEvidenceV1]:
    """List release evidence for a study, oldest-first."""
    statement = (
        select(ResearchRecord)
        .where(
            ResearchRecord.kind == RECORD_KIND_RELEASE_EVIDENCE,
            ResearchRecord.payload_json["study_id"].astext
            == str(study_id),
        )
        .order_by(ResearchRecord.occurred_at.asc())
    )
    return [
        _row_to_release_evidence(row)
        for row in session.execute(statement).scalars().all()
    ]


def set_release_decision(
    session: Session, evidence: ReleaseEvidenceV1
) -> ResearchRecord:
    """Record a gate decision on an existing evidence row.

    Only the gate output (the decision fields inside the stored JSON) is
    written; the immutable inputs are never changed.
    """
    row = session.get(ResearchRecord, evidence.release_id)
    if row is None or row.kind != RECORD_KIND_RELEASE_EVIDENCE:
        raise ValueError(f"release evidence {evidence.release_id} not found")
    row.payload_json = evidence.model_dump(mode="json")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Pilot runs
# ---------------------------------------------------------------------------


def create_pilot_run(session: Session, run: PilotRunV1) -> PilotRunV1:
    """Insert one recorded pilot run."""
    row = _record(
        record_id=run.pilot_run_id,
        kind=RECORD_KIND_PILOT_RUN,
        occurred_at=run.recorded_at,
        payload_json=run.model_dump(mode="json"),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return run


def _row_to_pilot_run(row: ResearchRecord) -> PilotRunV1:
    return PilotRunV1.model_validate(row.payload_json)


def get_pilot_run(
    session: Session, pilot_run_id: uuid.UUID
) -> Optional[PilotRunV1]:
    """Fetch one pilot run by id, or ``None``."""
    row = session.get(ResearchRecord, pilot_run_id)
    if row is None or row.kind != RECORD_KIND_PILOT_RUN:
        return None
    return _row_to_pilot_run(row)


def list_pilot_runs(
    session: Session, classification: Optional[PilotClassification] = None
) -> Sequence[PilotRunV1]:
    """List pilot runs, optionally filtered by classification."""
    statement = select(ResearchRecord).where(
        ResearchRecord.kind == RECORD_KIND_PILOT_RUN
    )
    if classification is not None:
        statement = statement.where(
            ResearchRecord.payload_json["classification"].astext
            == classification.value
        )
    statement = statement.order_by(ResearchRecord.occurred_at.asc())
    return [
        _row_to_pilot_run(row)
        for row in session.execute(statement).scalars().all()
    ]


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def engage_kill_switch(
    session: Session, record: KillSwitchRecord
) -> KillSwitchRecord:
    """Persist a kill-switch engagement."""
    row = _record(
        record_id=record.switch_id,
        kind=RECORD_KIND_KILL_SWITCH,
        occurred_at=record.engaged_at,
        payload_json=record.model_dump(mode="json"),
        scope_type=record.scope.kind.value,
        scope_id=record.scope.scope_id,
        actor=record.actor,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return record


def release_kill_switch(
    session: Session, switch_id: uuid.UUID, *,
    released_at: Optional[datetime] = None,
) -> Optional[KillSwitchRecord]:
    """Mark a switch released, returning the record or ``None``."""
    row = session.get(ResearchRecord, switch_id)
    if row is None or row.kind != RECORD_KIND_KILL_SWITCH:
        return None
    payload = dict(row.payload_json or {})
    payload["released_at"] = (released_at or datetime.now()).isoformat()
    row.payload_json = payload
    session.add(row)
    session.commit()
    session.refresh(row)
    return row_to_kill_switch(row)


def get_kill_switch(
    session: Session, switch_id: uuid.UUID
) -> Optional[KillSwitchRecord]:
    """Fetch a kill-switch record by id, or ``None``."""
    row = session.get(ResearchRecord, switch_id)
    if row is None or row.kind != RECORD_KIND_KILL_SWITCH:
        return None
    return row_to_kill_switch(row)


def row_to_kill_switch(row: ResearchRecord) -> KillSwitchRecord:
    """Rehydrate a kill-switch record row into a domain record."""
    return KillSwitchRecord.model_validate(row.payload_json)


def kill_switch_records(session: Session) -> Sequence[KillSwitchRecord]:
    """List all kill-switch records, newest engagement first."""
    statement = (
        select(ResearchRecord)
        .where(ResearchRecord.kind == RECORD_KIND_KILL_SWITCH)
        .order_by(ResearchRecord.occurred_at.desc())
    )
    return [
        row_to_kill_switch(row) for row in session.execute(statement).scalars().all()
    ]


def latest_study_kill_switch(
    session: Session, study_id: uuid.UUID
) -> Optional[KillSwitchRecord]:
    """Return the newest persisted study-scoped switch, if one exists."""
    statement = (
        select(ResearchRecord)
        .where(
            ResearchRecord.kind == RECORD_KIND_KILL_SWITCH,
            ResearchRecord.scope_type == "STUDY",
            ResearchRecord.scope_id == study_id,
        )
        .order_by(ResearchRecord.occurred_at.desc())
        .limit(1)
    )
    row = session.execute(statement).scalars().first()
    return row_to_kill_switch(row) if row is not None else None


def is_kill_switch_engaged(
    session: Session,
    *,
    study_id: Optional[uuid.UUID] = None,
    enrollment_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Whether a persisted ``KILL_SWITCH`` record covers the requested scope.

    This is the DB-backed counterpart of the in-memory
    :class:`~research.analysis.operations.kill_switch.KillSwitchRegistry`: it rebuilds the
    registry from the persisted records and evaluates the same scope matching
    (study/enrollment). A record whose scope kind does not cover the
    requested identifiers does not block.
    """
    registry = KillSwitchRegistry(kill_switch_records(session))
    return registry.is_engaged(
        study_id=study_id,
        enrollment_id=enrollment_id,
        now=now or datetime.now(timezone.utc),
    )


def db_kill_switch_check(
    session: Session,
    *,
    study_id: Optional[uuid.UUID] = None,
    enrollment_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
) -> Callable[[], bool]:
    """Return a zero-argument kill-switch predicate backed by the database.

    The predicate re-reads the persisted records on every call (the services
    invoke it at the point of decision), so an engagement committed between the
    request starting and the check taking effect is observed.
    """

    def check() -> bool:
        return is_kill_switch_engaged(
            session,
            study_id=study_id,
            enrollment_id=enrollment_id,
            now=now,
        )

    return check
