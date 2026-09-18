"""CRUD-style persistence helpers for study protocols and revisions.

These functions take a caller-managed SQLAlchemy ``Session`` so the core package
never imports ``App`` or touches the application singleton. The router is
responsible for session lifecycle (``App.get_db_session`` / ``rollback`` /
``close``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, Sequence

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from .models import StudyProtocolV1
    from .publication import AuditRecord, StudyRevision

from database.research_schemas import (
    RECORD_KIND_STUDY_PUBLICATION,
    ResearchRecord,
)
from database.db_schemas import Study as StudyRow

from .canonical import protocol_digest
from .enums import RevisionStatus
from .join_code import generate_join_code, normalize_join_code

#: How many times to retry a join-code allocation on the (astronomically
#: unlikely) event of a collision before failing closed.
_JOIN_CODE_ALLOCATION_ATTEMPTS = 8


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DraftView:
    """A draft is an unpublished ``study_revision`` row (``status = DRAFT``)."""

    draft_id: uuid.UUID
    study_id: uuid.UUID
    name: str
    schema_version: str
    protocol_json: dict[str, Any]
    created_at: Optional[datetime]


@dataclass(frozen=True)
class StudyView:
    """Study identity, backed by the real ``public.study`` row."""

    study_id: uuid.UUID
    name: str
    description: Optional[str]
    owner: Optional[str]
    created_by: Optional[uuid.UUID] = None
    is_research: bool = False
    is_active: bool = False
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


def _study_row_view(row: StudyRow) -> StudyView:
    return StudyView(
        study_id=row.study_id,
        name=row.name,
        description=row.description,
        owner=None,
        created_by=row.created_by,
        is_research=bool(row.is_research),
        is_active=bool(row.is_active),
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        created_at=row.created_at,
    )


def _draft_view(row: StudyRevisionRow) -> DraftView:
    protocol_json = row.protocol_json or {}
    metadata = protocol_json.get("metadata") or {}
    return DraftView(
        draft_id=row.revision_id,
        study_id=row.study_id,
        name=str(metadata.get("name", "")),
        schema_version=str(protocol_json.get("schema_version", "1")),
        protocol_json=protocol_json,
        created_at=row.created_at,
    )


def create_study(
    session: Session,
    *,
    study_id: uuid.UUID,
    name: str,
    description: Optional[str] = None,
    owner: Optional[str] = None,
    created_by: Optional[uuid.UUID] = None,
    starts_at: Optional[datetime] = None,
    ends_at: Optional[datetime] = None,
    is_research: bool = True,
    default_config_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> StudyView:
    """Insert the real ``public.study`` identity row.

    Agent research studies set ``is_research`` and never fabricate a completion
    ``default_config_id``. The study starts inactive; publication reserves the
    owner's one live-study slot.
    """
    timestamp = now or _now()
    row = StudyRow(
        study_id=study_id,
        name=name,
        description=description,
        created_by=created_by,
        starts_at=starts_at or timestamp,
        ends_at=ends_at,
        is_active=False,
        default_config_id=default_config_id,
        is_research=is_research,
        created_at=timestamp,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _study_row_view(row)


def get_study(session: Session, study_id: uuid.UUID) -> Optional[StudyView]:
    """Fetch a study identity row by id, or ``None``."""
    row = session.get(StudyRow, study_id)
    return _study_row_view(row) if row is not None else None


def list_studies(
    session: Session, owner_user_id: Optional[uuid.UUID] = None
) -> Sequence[StudyView]:
    """List studies, owner-scoped when ``owner_user_id`` is given."""
    statement = select(StudyRow)
    if owner_user_id is not None:
        statement = statement.where(StudyRow.created_by == owner_user_id)
    statement = statement.order_by(StudyRow.created_at.asc())
    return [_study_row_view(row) for row in session.execute(statement).scalars().all()]


def set_study_active(
    session: Session, study_id: uuid.UUID, active: bool
) -> Optional[StudyRow]:
    """Reserve/release a research study's live slot.

    The partial unique index ``uq_study_owner_live_research`` makes activation
    DB-safe; a concurrent activation raises ``IntegrityError``, which the caller
    turns into a clear conflict.
    """
    row = session.get(StudyRow, study_id)
    if row is None:
        return None
    row.is_active = active
    session.commit()
    session.refresh(row)
    return row


def get_live_research_study(
    session: Session, owner_user_id: uuid.UUID
) -> Optional[StudyRow]:
    """Return the owner's currently live research study, or ``None``."""
    statement = select(StudyRow).where(
        StudyRow.created_by == owner_user_id,
        StudyRow.is_research.is_(True),
        StudyRow.is_active.is_(True),
    )
    return session.execute(statement).scalars().first()


def deactivate_expired_research_studies(
    session: Session, owner_user_id: uuid.UUID, *, now: Optional[datetime] = None
) -> int:
    """Flip the owner's past-end-date research studies to inactive.

    A partial unique index on ``is_active`` cannot react to time passing, so an
    expired study must be actively released before a new publication reserves the
    owner's slot. Only studies with a non-null, past ``ends_at`` are released.
    Returns the number deactivated.
    """
    timestamp = now or _now()
    statement = select(StudyRow).where(
        StudyRow.created_by == owner_user_id,
        StudyRow.is_research.is_(True),
        StudyRow.is_active.is_(True),
        StudyRow.ends_at.is_not(None),
        StudyRow.ends_at < timestamp,
    )
    rows = list(session.execute(statement).scalars().all())
    for row in rows:
        row.is_active = False
    if rows:
        session.commit()
    return len(rows)


def research_study_is_open(study: Any, now: Optional[datetime] = None) -> bool:
    """Whether a research study is currently open for execution.

    ``is_active`` alone is a writer-set flag; an ended window must refuse
    behaviourally even before a writer flips the flag, so the reader path checks
    the schedule too.
    """
    if study is None or not getattr(study, "is_research", False):
        return False
    if not getattr(study, "is_active", False):
        return False
    timestamp = now or _now()
    starts_at = getattr(study, "starts_at", None)
    ends_at = getattr(study, "ends_at", None)
    if starts_at is not None and timestamp < starts_at:
        return False
    if ends_at is not None and timestamp > ends_at:
        return False
    return True


def _next_draft_number(session: Session, study_id: uuid.UUID) -> int:
    """Return a draft-only revision number that never collides with publication.

    Published revisions use positive numbers; drafts use 0 or negative numbers so
    publishing after a draft can never hit the ``(study_id, revision_number)``
    uniqueness constraint.
    """
    statement = select(StudyRevisionRow).where(StudyRevisionRow.study_id == study_id)
    numbers = [
        row.revision_number
        for row in session.execute(statement).scalars().all()
        if isinstance(getattr(row, "revision_number", None), int)
    ]
    lowest = min(numbers) if numbers else 0
    return min(lowest, 0) - 1


def create_draft(
    session: Session,
    *,
    draft_id: uuid.UUID,
    study_id: uuid.UUID,
    name: str,
    protocol: StudyProtocolV1,
    now: Optional[datetime] = None,
) -> DraftView:
    """Insert an editable draft as an unpublished revision row.

    ``name`` is derived from ``protocol.metadata.name`` (the protocol document
    is the draft's canonical content); ``draft_id`` is the revision id.
    """
    timestamp = now or _now()
    protocol_json = protocol.model_dump(mode="json")
    row = StudyRevisionRow(
        revision_id=draft_id,
        study_id=study_id,
        revision_number=_next_draft_number(session, study_id),
        status=RevisionStatus.DRAFT.value,
        protocol_json=protocol_json,
        protocol_digest=protocol_digest(protocol),
        published_at=None,
        supersedes_revision_id=None,
        created_at=timestamp,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _draft_view(row)


def get_draft(
    session: Session, draft_id: uuid.UUID
) -> Optional[DraftView]:
    """Fetch a draft revision by id, or ``None`` if it is not a draft."""
    row = session.get(StudyRevisionRow, draft_id)
    if row is None or row.status != RevisionStatus.DRAFT.value:
        return None
    return _draft_view(row)


def list_drafts(
    session: Session, study_id: uuid.UUID
) -> Sequence[DraftView]:
    """List a study's draft revisions oldest-first."""
    statement = (
        select(StudyRevisionRow)
        .where(
            StudyRevisionRow.study_id == study_id,
            StudyRevisionRow.status == RevisionStatus.DRAFT.value,
        )
        .order_by(StudyRevisionRow.created_at.asc())
    )
    return [
        _draft_view(row) for row in session.execute(statement).scalars().all()
    ]


def persist_revision(
    session: Session, revision: StudyRevision
) -> StudyRevisionRow:
    """Insert one immutable revision.

    A ``PUBLISHED`` revision additionally receives a fresh, unique join code, so
    the participant onboarding handle is minted atomically with publication and
    can never point at a draft. Conditions are part of ``protocol_json`` (its
    canonical content), so no child rows are written. The caller owns the
    transaction boundary decisions around conflicts
    (``study_id``/``revision_number`` is unique).
    """
    created_at = revision.created_at or _now()
    published = revision.status == RevisionStatus.PUBLISHED
    join_code = allocate_join_code(session) if published else None
    row = StudyRevisionRow(
        revision_id=revision.revision_id,
        study_id=revision.study_id,
        revision_number=revision.revision_number,
        status=revision.status.value,
        join_code=join_code,
        protocol_json=revision.protocol_json,
        protocol_digest=revision.protocol_digest,
        published_at=revision.published_at,
        supersedes_revision_id=revision.supersedes_revision_id,
        created_at=created_at,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _join_code_taken(session: Session, join_code: str) -> bool:
    """Whether ``join_code`` is already allocated to a stored revision."""
    row = get_revision_by_join_code(session, join_code)
    if row is None:
        return False
    return getattr(row, "join_code", None) == join_code


def allocate_join_code(session: Session) -> str:
    """Return a join code not yet present in the store.

    Collisions are checked against the stored revisions (and additionally
    guarded by the ``uq_study_revision_join_code`` unique constraint). The loop
    is bounded so an exhausted allocator fails closed rather than spinning.
    """
    for _ in range(_JOIN_CODE_ALLOCATION_ATTEMPTS):
        candidate = generate_join_code()
        if not _join_code_taken(session, candidate):
            return candidate
    raise RuntimeError("unable to allocate a unique study join code")  # pragma: no cover


def get_revision_by_join_code(
    session: Session, join_code: str
) -> Optional[StudyRevisionRow]:
    """Fetch the revision a (possibly user-typed) join code resolves to.

    The code is normalized before lookup, so case/separator and Crockford
    look-alike differences still resolve. An empty code never resolves.
    """
    normalized = normalize_join_code(join_code)
    if not normalized:
        return None
    statement = select(StudyRevisionRow).where(
        StudyRevisionRow.join_code == normalized
    )
    return session.execute(statement).scalars().first()


def get_latest_published_revision(
    session: Session, study_id: uuid.UUID
) -> Optional[StudyRevisionRow]:
    """Fetch a study's current (highest-numbered) ``PUBLISHED`` revision.

    Retired revisions are excluded: the current onboarding handle is always the
    live published revision.
    """
    statement = (
        select(StudyRevisionRow)
        .where(
            StudyRevisionRow.study_id == study_id,
            StudyRevisionRow.status == RevisionStatus.PUBLISHED.value,
        )
        .order_by(StudyRevisionRow.revision_number.desc())
    )
    return session.execute(statement).scalars().first()


def get_study_join_code(
    session: Session, study_id: uuid.UUID
) -> Optional[StudyRevisionRow]:
    """Fetch the current join-code-bearing revision for a study, or ``None``."""
    row = get_latest_published_revision(session, study_id)
    if row is None or not getattr(row, "join_code", None):
        return None
    return row


def get_revision(
    session: Session, revision_id: uuid.UUID
) -> Optional[StudyRevisionRow]:
    """Fetch a revision row by primary key, or ``None``."""
    return session.get(StudyRevisionRow, revision_id)


def list_revisions(
    session: Session, study_id: uuid.UUID
) -> Sequence[StudyRevisionRow]:
    """List a study's published/retired revisions in ascending revision order.

    Draft rows share the table but are not revisions of the publication lineage,
    so they are excluded here (use :func:`list_drafts` for those).
    """
    statement = (
        select(StudyRevisionRow)
        .where(
            StudyRevisionRow.study_id == study_id,
            StudyRevisionRow.status != RevisionStatus.DRAFT.value,
        )
        .order_by(StudyRevisionRow.revision_number.asc())
    )
    return list(session.execute(statement).scalars().all())


def retire_revision(
    session: Session, revision_id: uuid.UUID, *, now: Optional[datetime] = None
) -> Optional[StudyRevisionRow]:
    """Flip a revision's lifecycle status to ``RETIRED`` without touching bytes."""
    row = session.get(StudyRevisionRow, revision_id)
    if row is None:
        return None
    row.status = RevisionStatus.RETIRED.value
    session.commit()
    session.refresh(row)
    return row


def persist_audit(
    session: Session, audit: AuditRecord
) -> ResearchRecord:
    """Append a publication/lifecycle audit record to the generic record table."""
    row = ResearchRecord(
        record_id=uuid.uuid4(),
        kind=RECORD_KIND_STUDY_PUBLICATION,
        scope_type="study",
        scope_id=audit.study_id,
        study_id=audit.study_id,
        actor=audit.actor,
        occurred_at=audit.occurred_at,
        payload_json={
            "action": audit.event_type,
            "revision_id": (
                str(audit.revision_id) if audit.revision_id is not None else None
            ),
            "revision_number": audit.revision_number,
            "protocol_digest": audit.protocol_digest,
            "detail": audit.detail,
        },
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def row_to_revision(row: StudyRevisionRow) -> StudyRevision:
    """Rehydrate a stored revision row into the domain model."""
    from .publication import StudyRevision as StudyRevisionModel

    status = row.status
    return StudyRevisionModel(
        revision_id=row.revision_id,
        study_id=row.study_id,
        revision_number=row.revision_number,
        status=status
        if isinstance(status, RevisionStatus)
        else RevisionStatus(status),
        protocol_json=row.protocol_json,
        protocol_digest=row.protocol_digest,
        published_at=row.published_at,
        supersedes_revision_id=row.supersedes_revision_id,
        created_at=row.created_at,
    )


def row_to_protocol(row: StudyRevisionRow) -> StudyProtocolV1:
    """Rehydrate and validate a stored revision's protocol document."""
    from .models import StudyProtocolV1 as StudyProtocolV1Model

    return StudyProtocolV1Model.model_validate(row.protocol_json)


def revision_summary(row: StudyRevisionRow) -> dict[str, Any]:
    """Return a compact, non-secret revision summary safe for list responses."""
    published_at = row.published_at
    created_at = row.created_at
    return {
        "revision_id": str(row.revision_id),
        "study_id": str(row.study_id),
        "revision_number": row.revision_number,
        "status": row.status.value if isinstance(row.status, RevisionStatus) else row.status,
        "join_code": getattr(row, "join_code", None),
        "protocol_digest": row.protocol_digest,
        "supersedes_revision_id": (
            str(row.supersedes_revision_id)
            if row.supersedes_revision_id is not None
            else None
        ),
        "published_at": (
            published_at.isoformat() if isinstance(published_at, datetime) else None
        ),
        "created_at": (
            created_at.isoformat() if isinstance(created_at, datetime) else None
        ),
    }
