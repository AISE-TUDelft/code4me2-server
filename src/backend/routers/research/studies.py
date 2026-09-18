"""API for versioned study protocols and immutable publication.

Mounted under ``/api/research/studies``. The handlers are intentionally thin:
validation, canonicalization, publication and lineage live in
:mod:`research.study.protocol`, and the SQLAlchemy session is supplied by
``App.get_db_session`` and passed to the store helpers.

Authorization is per-study RBAC rather than blanket admin: creating/validating
drafts and publishing/superseding/retiring revisions require the ``OWNER`` role
on the target study, while reading revisions/drafts is open to ``OWNER``,
``ANALYST`` and ``VIEWER``. An administrator may do everything. The study index
and the join-code read endpoints remain admin-only because they enumerate every
study on the deployment.

Drafts are editable; published revisions are immutable and content-addressed.
Editing a published revision goes through ``/revisions/{id}/supersede`` and
always creates a successor revision. Publishing mints the study's join code.
"""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
)
from backend.routers.research.access import (
    is_study_owner,
    require_researcher,
    require_study_owner,
)
from database import crud
from research.participants import identity as identity_store
from research.study.agents import store as registry_store
from research.study.agents.distributions import resolve_distribution_view
from research.study.agents.registry import approval_option_verified
from research.study.protocol import store
from research.study.protocol.canonical import protocol_digest
from research.study.protocol.enums import PublicationOutcome
from research.study.protocol.models import (
    StudyProtocolV1,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
)
from research.study.protocol.publication import (
    AuditRecord,
    RevisionLineage,
    lineage_from_revisions,
    publish_revision,
    retire_revision,
)
from research.study.protocol.validation import (
    DistributionResolution,
    ValidationError,
    blocking_errors,
    scan_document_safety,
    validate_protocol,
    warnings,
)

router = APIRouter()


class DraftCreateRequest(BaseModel):
    """Create (or replace) an editable draft for a study."""

    study_id: uuid.UUID
    name: str
    protocol: StudyProtocolV1


class ValidateRequest(BaseModel):
    """Validate a protocol document without persisting it."""

    protocol: StudyProtocolV1


class PublishRequest(BaseModel):
    """Publish a protocol document as a new immutable revision."""

    protocol: StudyProtocolV1
    # Optimistic concurrency: the caller's view of the current revision number.
    expected_revision_number: Optional[int] = None
    # Optional client-side canonical digest; a mismatch is rejected.
    expected_protocol_digest: Optional[str] = None


class RetireRequest(BaseModel):
    """Retire a published revision without changing its stored content."""

    actor: Optional[str] = Field(default=None)


class StudyCreateRequest(BaseModel):
    """Create a new study identity without pasting a pre-generated UUID."""

    name: str = Field(..., min_length=1)
    description: Optional[str] = Field(default=None)
    # Defaults to the creating researcher's email when omitted.
    owner: Optional[str] = Field(default=None)
    # Fixed study window; a draft that omits these starts now and has no end.
    starts_at: Optional[datetime] = Field(default=None)
    ends_at: Optional[datetime] = Field(default=None)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


def _distribution_resolver(
    db: Any, funding_owner_user_id: Any = None
) -> "_DbDistributionResolver":
    """A DB-backed resolver for one request's condition distributions."""
    return _DbDistributionResolver(db, funding_owner_user_id)


class _DbDistributionResolver:
    """Resolve a condition's ``distribution_id`` to its non-secret view.

    The distribution is an ``AgentProfile`` row; its pin is resolved against the
    immutable release registry and its ``verified`` flag is *derived* from
    conformance evidence. No secret is ever read here.
    """

    def __init__(self, db: Any, funding_owner_user_id: Any = None) -> None:
        self._db = db
        self._funding_owner_user_id = funding_owner_user_id

    def resolve(self, distribution_id: Any):
        profile = crud.get_agent_profile_by_id(self._db, distribution_id)
        if profile is None:
            return DistributionResolution(
                found=False, distribution_id=distribution_id
            )
        release = None
        release_id = getattr(profile, "release_id", None)
        if release_id:
            row = registry_store.get_release(self._db, release_id)
            if row is not None:
                release = registry_store.row_to_release(row)
        connection = None
        connection_id = getattr(profile, "connection_id", None)
        if connection_id is not None:
            connection = crud.get_provider_connection(self._db, connection_id)
        return resolve_distribution_view(
            profile,
            release,
            distribution_id=distribution_id,
            connection=connection,
            funding_owner_user_id=self._funding_owner_user_id,
        )


def _distribution_errors(
    db: Any, protocol: StudyProtocolV1, *, actor_is_admin: bool
) -> list[ValidationError]:
    """Resolve every condition's distribution and return typed reasons."""
    return validate_protocol(
        protocol,
        distribution_resolver=_distribution_resolver(db),
        actor_is_admin=actor_is_admin,
    )


def _condition_authorization_errors(
    db: Any, protocol: StudyProtocolV1, current_user: AuthenticatedUser
) -> None:
    """Reject foreign-owner profiles or unauthorized connections (C12).

    A condition may only reference a profile owned by the caller (administrators
    may reference any profile), and that profile's connection must be granted to
    the profile owner (administrators implicitly hold every connection).
    """
    problems: list[dict[str, str]] = []
    for condition in protocol.conditions:
        field = f"conditions[{condition.condition_id}].distribution_id"
        profile = crud.get_agent_profile_by_id(db, condition.distribution_id)
        if profile is None:
            problems.append(
                {
                    "code": "AGENT_PROFILE_NOT_FOUND",
                    "field": field,
                    "message": "the referenced profile does not exist",
                }
            )
            continue
        if not current_user.is_admin and profile.owner_user_id != current_user.user_id:
            problems.append(
                {
                    "code": "PROFILE_FOREIGN_OWNER",
                    "field": field,
                    "message": "the referenced profile belongs to another researcher",
                }
            )
        connection_id = getattr(profile, "connection_id", None)
        if connection_id is None:
            problems.append(
                {
                    "code": "CONNECTION_MISSING",
                    "field": field,
                    "message": "the referenced profile has no provider connection",
                }
            )
            continue
        connection = crud.get_provider_connection(db, connection_id)
        if connection is None:
            problems.append(
                {
                    "code": "CONNECTION_UNRESOLVED",
                    "field": field,
                    "message": "the referenced provider connection does not exist",
                }
            )
            continue
        if not current_user.is_admin and not crud.provider_connection_is_available(
            db, connection.connection_id, profile.owner_user_id
        ):
            problems.append(
                {
                    "code": "CONNECTION_NOT_GRANTED",
                    "field": field,
                    "message": "the profile owner is not authorized for this connection",
                }
            )
        # The selected approval policy must be verified for this exact release.
        # Only enforced when real evidence is resolvable (a JSON object); a
        # missing/legacy release is already rejected by distribution resolution.
        release_id = getattr(profile, "release_id", None)
        if release_id:
            release_row = registry_store.get_release(db, release_id)
            release_json = (
                getattr(release_row, "release_json", None)
                if release_row is not None
                else None
            )
            if isinstance(release_json, dict) and not approval_option_verified(
                release_json, profile.approval_policy
            ):
                problems.append(
                    {
                        "code": "APPROVAL_NOT_VERIFIED",
                        "field": field,
                        "message": (
                            f"approval policy {profile.approval_policy!r} is not "
                            f"verified for release {release_id!r}"
                        ),
                    }
                )
    if problems:
        raise HTTPException(status_code=422, detail=problems)


def _load_study_row(db: Any, study_id: uuid.UUID):
    study = store.get_study(db, study_id)
    if study is None:
        raise HTTPException(status_code=404, detail="Study not found")
    return study


def _authorize_study(
    db: Any,
    current_user: AuthenticatedUser,
    study_id: uuid.UUID,
) -> Any:
    """Authorize an action on one study by ownership (admin bypasses)."""
    study = _load_study_row(db, study_id)
    require_study_owner(current_user, study)
    return study


def _ensure_study_access(
    app: App,
    current_user: AuthenticatedUser,
    study_id: uuid.UUID,
) -> None:
    """Authorize before the handler opens its own session."""
    if current_user.is_admin:
        return
    db = app.get_db_session()
    try:
        _authorize_study(db, current_user, study_id)
    finally:
        db.close()


def _error_payload(errors: list[ValidationError]) -> list[dict[str, Any]]:
    return [error.model_dump(mode="json") for error in errors]


def _revision_payload(row: Any) -> dict[str, Any]:
    return store.revision_summary(row)


def _load_lineage(db: Any, study_id: uuid.UUID) -> RevisionLineage:
    rows = store.list_revisions(db, study_id)
    revisions = [store.row_to_revision(row) for row in rows]
    return lineage_from_revisions(study_id, revisions)


def _publish(
    db: Any,
    *,
    payload: PublishRequest,
    current_user: AuthenticatedUser,
) -> dict[str, Any]:
    """Shared publish/supersede body: digest check, then immutable publication.

    Every condition's distribution is resolved and its exact pin is frozen into
    the stored revision by :func:`publish_revision`. Foreign-owner profiles and
    unauthorized connections are rejected before publication.
    """
    actor = current_user.email
    actor_is_admin = current_user.is_admin
    computed_digest = protocol_digest(payload.protocol)
    if (
        payload.expected_protocol_digest is not None
        and payload.expected_protocol_digest != computed_digest
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "protocol_digest mismatch: document was not built with the "
                f"canonical serializer (expected {computed_digest})"
            ),
        )

    _condition_authorization_errors(db, payload.protocol, current_user)

    study_row = store.get_study(db, payload.protocol.study_id)
    if study_row is None:
        raise HTTPException(status_code=404, detail="Study not found")
    # The study owner (not the publishing actor) funds the connection grant; it
    # is frozen into every condition's agent config at publication.
    funding_owner_user_id = study_row.created_by

    resolver = _distribution_resolver(
        db, funding_owner_user_id=funding_owner_user_id
    )
    errors = validate_protocol(
        payload.protocol,
        distribution_resolver=resolver,
        actor_is_admin=actor_is_admin,
    )
    blocking = blocking_errors(errors)
    if blocking:
        raise HTTPException(status_code=422, detail=_error_payload(errors))

    lineage = _load_lineage(db, payload.protocol.study_id)
    result = publish_revision(
        payload.protocol,
        lineage,
        distribution_resolver=resolver,
        actor_is_admin=actor_is_admin,
        expected_revision_number=payload.expected_revision_number,
        actor=actor,
        now=datetime.now(timezone.utc),
    )

    if result.outcome == PublicationOutcome.VALIDATION_FAILED:
        raise HTTPException(
            status_code=422, detail=_error_payload(blocking_errors(result.errors))
        )

    advisory = _error_payload(warnings(errors))

    if result.outcome == PublicationOutcome.CONFLICT:
        return {
            "outcome": result.outcome.value,
            "revision": None,
            "conflict": (
                result.conflict.model_dump(mode="json") if result.conflict else None
            ),
            "errors": [],
            "warnings": advisory,
        }

    assert result.revision is not None  # PUBLISHED always carries a revision
    row = store.persist_revision(db, result.revision)

    # Publication reserves the owner's one live-study slot. A partial unique
    # index cannot react to time passing, so release any of the owner's studies
    # whose end date has passed before reserving the slot; the index then makes
    # concurrent publication DB-safe (a loser raises IntegrityError -> 409).
    from sqlalchemy.exc import IntegrityError

    if funding_owner_user_id is not None:
        store.deactivate_expired_research_studies(db, funding_owner_user_id)
    study_row = store.get_study(db, result.revision.study_id)
    if study_row is not None and not study_row.is_active:
        try:
            store.set_study_active(db, result.revision.study_id, True)
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "LIVE_STUDY_CONFLICT",
                    "message": (
                        "this researcher already has a live study; end it before "
                        "publishing another"
                    ),
                },
            )

    if result.audit is not None:
        store.persist_audit(db, result.audit)
    return {
        "outcome": result.outcome.value,
        "revision": _revision_payload(row),
        "conflict": None,
        "errors": [],
        "warnings": advisory,
    }


@router.post("/drafts", summary="Create an editable study protocol draft")
def create_draft(
    payload: DraftCreateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Persist a draft. Credential/identifier leakage is rejected outright."""
    require_researcher(current_user)
    _ensure_study_access(app, current_user, payload.study_id)

    safety_errors = scan_document_safety(payload.protocol)
    if safety_errors:
        raise HTTPException(status_code=422, detail=_error_payload(safety_errors))

    db = app.get_db_session()
    try:
        distribution_errors = _distribution_errors(
            db, payload.protocol, actor_is_admin=current_user.is_admin
        )
        if blocking_errors(distribution_errors):
            raise HTTPException(
                status_code=422, detail=_error_payload(distribution_errors)
            )
        if store.get_study(db, payload.study_id) is None:
            schedule = payload.protocol.schedule
            store.create_study(
                db,
                study_id=payload.study_id,
                name=payload.name,
                description=payload.protocol.metadata.description,
                created_by=current_user.user_id,
                starts_at=getattr(schedule, "start_at", None),
                ends_at=getattr(schedule, "end_at", None),
                is_research=True,
            )
        row = store.create_draft(
            db,
            draft_id=uuid.uuid4(),
            study_id=payload.study_id,
            name=payload.name,
            protocol=payload.protocol,
        )
        return JsonResponseWithStatus(
            status_code=201,
            content={
                "draft_id": str(row.draft_id),
                "study_id": str(row.study_id),
                "name": row.name,
                "schema_version": row.schema_version,
            },
        )
    finally:
        db.close()


@router.get("/drafts", summary="List editable study protocol drafts")
def list_drafts(
    study_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    _ensure_study_access(app, current_user, study_id)
    db = app.get_db_session()
    try:
        rows = store.list_drafts(db, study_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "drafts": [
                    {
                        "draft_id": str(row.draft_id),
                        "study_id": str(row.study_id),
                        "name": row.name,
                        "schema_version": row.schema_version,
                    }
                    for row in rows
                ]
            },
        )
    finally:
        db.close()


@router.post("/drafts/validate", summary="Validate a study protocol document")
def validate_draft(
    payload: ValidateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Return typed validation reasons; an empty list means publishable."""
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        existing = store.get_study(db, payload.protocol.study_id)
        if existing is not None:
            require_study_owner(current_user, existing)
        errors = _distribution_errors(
            db, payload.protocol, actor_is_admin=current_user.is_admin
        )
    finally:
        db.close()
    blocking = blocking_errors(errors)
    return JsonResponseWithStatus(
        status_code=200 if not blocking else 422,
        content={
            "valid": not blocking,
            "errors": _error_payload(errors),
            "warnings": _error_payload(warnings(errors)),
        },
    )


@router.get("/drafts/{draft_id}", summary="Fetch one study protocol draft")
def get_draft(
    draft_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        row = store.get_draft(db, draft_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Study protocol draft not found")
        _authorize_study(db, current_user, row.study_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "draft_id": str(row.draft_id),
                "study_id": str(row.study_id),
                "name": row.name,
                "schema_version": row.schema_version,
                "protocol": row.protocol_json,
            },
        )
    finally:
        db.close()


@router.post(
    "/drafts/{draft_id}/publish", summary="Publish a draft as an immutable revision"
)
def publish_draft(
    draft_id: uuid.UUID,
    payload: PublishRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Publish a protocol; a stale ``expected_revision_number`` yields a conflict."""
    require_researcher(current_user)
    _ensure_study_access(app, current_user, payload.protocol.study_id)
    db = app.get_db_session()
    try:
        if store.get_draft(db, draft_id) is None:
            raise HTTPException(
                status_code=404, detail="Study protocol draft not found"
            )
        content = _publish(
            db,
            payload=payload,
            current_user=current_user,
        )
        status_code = (
            409
            if content["outcome"] == PublicationOutcome.CONFLICT.value
            else 201
        )
        return JsonResponseWithStatus(status_code=status_code, content=content)
    finally:
        db.close()


@router.get("/revisions", summary="List a study's immutable revisions")
def list_revisions(
    study_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    _ensure_study_access(app, current_user, study_id)
    db = app.get_db_session()
    try:
        rows = store.list_revisions(db, study_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={"revisions": [_revision_payload(row) for row in rows]},
        )
    finally:
        db.close()


@router.get("/revisions/{revision_id}", summary="Fetch one immutable revision")
def get_revision(
    revision_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        row = store.get_revision(db, revision_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Study revision not found")
        _authorize_study(db, current_user, row.study_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "revision": _revision_payload(row),
                "protocol": row.protocol_json,
            },
        )
    finally:
        db.close()


@router.post(
    "/revisions/{revision_id}/supersede",
    summary="Publish a successor revision for an edited protocol",
)
def supersede_revision(
    revision_id: uuid.UUID,
    payload: PublishRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Create a successor revision; the prior revision is never mutated."""
    require_researcher(current_user)
    _ensure_study_access(app, current_user, payload.protocol.study_id)
    db = app.get_db_session()
    try:
        existing = store.get_revision(db, revision_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Study revision not found")
        content = _publish(
            db,
            payload=payload,
            current_user=current_user,
        )
        status_code = (
            409
            if content["outcome"] == PublicationOutcome.CONFLICT.value
            else 201
        )
        return JsonResponseWithStatus(status_code=status_code, content=content)
    finally:
        db.close()


@router.post(
    "/revisions/{revision_id}/retire", summary="Retire a published revision"
)
def retire_published_revision(
    revision_id: uuid.UUID,
    payload: RetireRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Transition a revision to ``RETIRED`` without changing its content.

    Retirement of the last published revision is the terminal "end study"
    action: it releases the owner's live-study slot (``is_active = false``).
    """
    db = app.get_db_session()
    try:
        row = store.get_revision(db, revision_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Study revision not found")
        _authorize_study(db, current_user, row.study_id)
        result = retire_revision(
            store.row_to_revision(row),
            actor=payload.actor or current_user.email,
            now=datetime.now(timezone.utc),
        )
        updated = store.retire_revision(db, revision_id)
        # Retiring the last published revision ends the study: it releases the
        # owner's single live-study slot and completes/revokes its enrollments.
        if result.retired and store.get_latest_published_revision(db, row.study_id) is None:
            store.set_study_active(db, row.study_id, False)
            identity_store.complete_enrollments_for_study(
                db, row.study_id, now=datetime.now(timezone.utc)
            )
        if result.audit is not None:
            store.persist_audit(db, result.audit)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "retired": result.retired,
                "revision": _revision_payload(updated) if updated is not None else None,
                "message": result.message,
            },
        )
    finally:
        db.close()


class EndStudyRequest(BaseModel):
    """Optional actor override for an explicit study end."""

    actor: Optional[str] = Field(default=None)


@router.post("/{study_id}/end", summary="End a research study")
def end_study(
    study_id: uuid.UUID,
    payload: EndStudyRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Explicitly end a study: release the slot and complete its enrollments.

    Owner/admin only. Marks every ACTIVE enrollment ``COMPLETED`` (terminal), bumps
    their revocation epoch, revokes their sessions, and frees the owner's
    live-study slot so all funded operations and previously issued capabilities
    for the study are refused.
    """
    now = datetime.now(timezone.utc)
    db = app.get_db_session()
    try:
        study = _authorize_study(db, current_user, study_id)
        completed = identity_store.complete_enrollments_for_study(
            db, study_id, now=now
        )
        store.set_study_active(db, study_id, False)
        store.persist_audit(
            db,
            AuditRecord(
                event_type="study.ended",
                study_id=study_id,
                actor=payload.actor or current_user.email,
                occurred_at=now,
                detail=f"completed {completed} enrollment(s)",
            ),
        )
        updated = store.get_study(db, study_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "study": _study_payload(updated) if updated is not None else None,
                "completed_enrollments": completed,
            },
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Study index and join code (admin: enumerates the whole deployment)
# ---------------------------------------------------------------------------


def _study_index_entry(db: Any, study: Any) -> dict[str, Any]:
    """Build one study's index row with its current published revision."""
    latest = store.get_latest_published_revision(db, study.study_id)
    return {
        "study_id": str(study.study_id),
        "name": study.name,
        "description": study.description,
        "created_by": (
            str(study.created_by) if getattr(study, "created_by", None) else None
        ),
        "is_research": bool(getattr(study, "is_research", False)),
        "is_active": bool(getattr(study, "is_active", False)),
        "join_code": getattr(latest, "join_code", None) if latest is not None else None,
        "latest_revision": (
            _revision_payload(latest) if latest is not None else None
        ),
    }


def _study_payload(study: Any) -> dict[str, Any]:
    created_at = study.created_at
    starts_at = getattr(study, "starts_at", None)
    ends_at = getattr(study, "ends_at", None)
    return {
        "study_id": str(study.study_id),
        "name": study.name,
        "description": study.description,
        "owner": study.owner,
        "created_by": (
            str(study.created_by) if getattr(study, "created_by", None) else None
        ),
        "is_research": bool(getattr(study, "is_research", False)),
        "is_active": bool(getattr(study, "is_active", False)),
        "starts_at": starts_at.isoformat() if isinstance(starts_at, datetime) else starts_at,
        "ends_at": ends_at.isoformat() if isinstance(ends_at, datetime) else ends_at,
        "created_at": (
            created_at.isoformat() if isinstance(created_at, datetime) else created_at
        ),
    }


@router.post("", summary="Create a study identity")
def create_study(
    payload: StudyCreateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Mint a real research Study row owned by the caller.

    An administrator may always create one; an enabled researcher may create
    their first study without any per-study role. The row starts inactive, so a
    draft does not reserve the owner's one live-study slot.
    """
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        study_id = uuid.uuid4()
        study = store.create_study(
            db,
            study_id=study_id,
            name=payload.name,
            description=payload.description,
            created_by=current_user.user_id,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            is_research=True,
        )
        return JsonResponseWithStatus(
            status_code=201, content={"study": _study_payload(study)}
        )
    finally:
        db.close()


@router.get("", summary="List the caller's studies")
def list_studies(
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Owner-scoped study index; administrators see every study."""
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        owner_scope = None if current_user.is_admin else current_user.user_id
        studies = store.list_studies(db, owner_scope)
        return JsonResponseWithStatus(
            status_code=200,
            content={"studies": [_study_index_entry(db, study) for study in studies]},
        )
    finally:
        db.close()


@router.get(
    "/{study_id}/join-code", summary="Get a study's current join code and revision"
)
def get_study_join_code(
    study_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Owner-scoped: the current join code bound to the live published revision."""
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        _authorize_study(db, current_user, study_id)
        row = store.get_study_join_code(db, study_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Study has no published revision with a join code",
            )
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "study_id": str(study_id),
                "join_code": row.join_code,
                "revision": _revision_payload(row),
            },
        )
    finally:
        db.close()
