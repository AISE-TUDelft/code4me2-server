"""Immutable publication and revision lineage for study protocols.

Publication is a pure function of a validated draft plus the study's existing
revision lineage:

* a new :class:`StudyRevision` is produced with a fresh ``revision_id``, a
  monotonic ``revision_number``, canonical ``protocol_json`` and its SHA-256
  ``protocol_digest``;
* the prior revision is never mutated; the new revision records
  ``supersedes_revision_id`` so lineage is auditable;
* publishing with a stale ``expected_revision_number`` returns a typed
  :class:`RevisionConflict` instead of overwriting a concurrent revision;
* an audit record is emitted for every state transition.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .canonical import canonicalize_protocol, protocol_digest
from .enums import PublicationOutcome, RevisionStatus
from .validation import (
    DistributionResolver,
    ValidationError,
    blocking_errors,
    freeze_protocol_distributions,
    validate_protocol,
)

if TYPE_CHECKING:
    from .models import StudyProtocolV1


class StudyRevision(BaseModel):
    """One immutable, content-addressed draft of a study's experimental intent."""

    model_config = ConfigDict(extra="forbid")

    revision_id: UUID
    study_id: UUID
    revision_number: int
    status: RevisionStatus
    # Canonical mapping (set-like arrays normalized) of the protocol document.
    protocol_json: dict[str, Any]
    protocol_digest: str
    published_at: Optional[datetime] = None
    supersedes_revision_id: Optional[UUID] = None
    created_at: Optional[datetime] = None


class RevisionLineage(BaseModel):
    """The lineage head a publication call must extend."""

    model_config = ConfigDict(extra="forbid")

    study_id: UUID
    latest_revision_id: Optional[UUID] = None
    latest_revision_number: int = 0
    latest_digest: Optional[str] = None
    latest_status: Optional[RevisionStatus] = None


class RevisionConflict(BaseModel):
    """Typed optimistic-concurrency conflict."""

    model_config = ConfigDict(extra="forbid")

    expected_revision_number: int
    actual_revision_number: int
    message: str = "a newer revision was published concurrently"


class AuditRecord(BaseModel):
    """Append-only record of a publication or lifecycle transition."""

    model_config = ConfigDict(extra="forbid")

    event_type: str
    study_id: UUID
    occurred_at: datetime
    revision_id: Optional[UUID] = None
    revision_number: Optional[int] = None
    protocol_digest: Optional[str] = None
    actor: Optional[str] = None
    detail: Optional[str] = None


class PublicationResult(BaseModel):
    """Typed outcome of a publication attempt."""

    model_config = ConfigDict(extra="forbid")

    outcome: PublicationOutcome
    revision: Optional[StudyRevision] = None
    errors: list[ValidationError] = Field(default_factory=list)
    conflict: Optional[RevisionConflict] = None
    audit: Optional[AuditRecord] = None


class RetirementResult(BaseModel):
    """Typed outcome of retiring a published revision."""

    model_config = ConfigDict(extra="forbid")

    retired: bool
    revision: Optional[StudyRevision] = None
    audit: Optional[AuditRecord] = None
    message: Optional[str] = None


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


class ImmutableRevisionError(ValueError):
    """Raised when a non-draft revision's content would be mutated in place.

    Publication is append-only: a material edit creates a successor revision.
    """


def assert_revision_mutable(revision: StudyRevision) -> StudyRevision:
    """Return ``revision`` when it is an editable draft, else raise.

    Only a ``DRAFT`` revision may be edited in place. A ``PUBLISHED`` (or
    ``RETIRED``) revision is immutable content; callers must publish a
    successor revision (which records ``supersedes_revision_id``) instead.
    """
    if revision.status != RevisionStatus.DRAFT:
        raise ImmutableRevisionError(
            f"revision {revision.revision_id} is {revision.status.value} and "
            "its content is immutable; create a successor revision instead"
        )
    return revision


def lineage_from_revisions(
    study_id: UUID, revisions: list[StudyRevision]
) -> RevisionLineage:
    """Build a :class:`RevisionLineage` from any ordering of revisions."""
    if not revisions:
        return RevisionLineage(study_id=study_id)
    latest = max(revisions, key=lambda revision: revision.revision_number)
    return RevisionLineage(
        study_id=study_id,
        latest_revision_id=latest.revision_id,
        latest_revision_number=latest.revision_number,
        latest_digest=latest.protocol_digest,
        latest_status=latest.status,
    )


def publish_revision(
    protocol: StudyProtocolV1,
    lineage: RevisionLineage,
    *,
    distribution_resolver: Optional[DistributionResolver] = None,
    actor_is_admin: bool = False,
    expected_revision_number: Optional[int] = None,
    actor: Optional[str] = None,
    now: Optional[datetime] = None,
) -> PublicationResult:
    """Validate and publish ``protocol`` as the next revision of its study.

    When a ``distribution_resolver`` is supplied, every condition's
    ``distribution_id`` is resolved and the exact pin is FROZEN into the stored
    revision before the digest is computed, so an immutable revision always
    records which artifact produced its data. The prior revision is never
    mutated. Validation is performed before the concurrency check so an unsafe
    document is always reported as ``VALIDATION_FAILED`` rather than as a
    conflict; only blocking errors stop publication (warnings are advisory).
    """
    timestamp = _now(now)

    frozen = (
        freeze_protocol_distributions(protocol, distribution_resolver, now=timestamp)
        if distribution_resolver is not None
        else protocol
    )
    errors = validate_protocol(
        frozen,
        distribution_resolver=distribution_resolver,
        actor_is_admin=actor_is_admin,
        now=timestamp,
    )
    blocking = blocking_errors(errors)
    if blocking:
        return PublicationResult(
            outcome=PublicationOutcome.VALIDATION_FAILED,
            errors=errors,
        )

    if (
        expected_revision_number is not None
        and expected_revision_number != lineage.latest_revision_number
    ):
        return PublicationResult(
            outcome=PublicationOutcome.CONFLICT,
            conflict=RevisionConflict(
                expected_revision_number=expected_revision_number,
                actual_revision_number=lineage.latest_revision_number,
            ),
            audit=AuditRecord(
                event_type="study.revision.publish_conflict",
                study_id=protocol.study_id,
                occurred_at=timestamp,
                actor=actor,
                detail=(
                    "expected revision "
                    f"{expected_revision_number}, found "
                    f"{lineage.latest_revision_number}"
                ),
            ),
        )

    digest = protocol_digest(frozen)
    revision = StudyRevision(
        revision_id=uuid4(),
        study_id=protocol.study_id,
        revision_number=lineage.latest_revision_number + 1,
        status=RevisionStatus.PUBLISHED,
        protocol_json=canonicalize_protocol(frozen),
        protocol_digest=digest,
        published_at=timestamp,
        supersedes_revision_id=lineage.latest_revision_id,
        created_at=timestamp,
    )
    audit = AuditRecord(
        event_type="study.revision.published",
        study_id=protocol.study_id,
        revision_id=revision.revision_id,
        revision_number=revision.revision_number,
        protocol_digest=digest,
        actor=actor,
        occurred_at=timestamp,
        detail=(
            f"supersedes revision {lineage.latest_revision_id}"
            if lineage.latest_revision_id
            else "initial publication"
        ),
    )
    return PublicationResult(
        outcome=PublicationOutcome.PUBLISHED,
        revision=revision,
        audit=audit,
    )


def retire_revision(
    revision: StudyRevision,
    *,
    actor: Optional[str] = None,
    now: Optional[datetime] = None,
) -> RetirementResult:
    """Return a retired copy of a published revision.

    The stored protocol bytes and digest are untouched; retirement is a
    lifecycle status transition only, and it never changes meaning for data
    already collected. The caller persists the transition via the store.
    """
    timestamp = _now(now)
    if revision.status == RevisionStatus.RETIRED:
        return RetirementResult(
            retired=False,
            revision=revision,
            message="revision is already retired",
        )

    retired = revision.model_copy(update={"status": RevisionStatus.RETIRED})
    audit = AuditRecord(
        event_type="study.revision.retired",
        study_id=revision.study_id,
        revision_id=revision.revision_id,
        revision_number=revision.revision_number,
        protocol_digest=revision.protocol_digest,
        actor=actor,
        occurred_at=timestamp,
    )
    return RetirementResult(retired=True, revision=retired, audit=audit)
