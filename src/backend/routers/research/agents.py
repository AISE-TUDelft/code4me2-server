"""Admin API for the agent registry and capability contract.

Mounted under ``/api/research/agents``. Every endpoint is admin-only. The
handlers are intentionally thin: registration, transitions, platform selection
and coverage live in :mod:`research.study.agents.registry`, and the SQLAlchemy session
is supplied by ``App.get_db_session`` and passed to the store helpers.

Release records are digest-pinned and immutable. Editing a release's bytes
requires a new ``release_id``; retirement preserves historical references.
"""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
)
from database import crud
from research.study.agents import store
from research.study.agents.distributions import (
    distribution_supported_platforms,
    resolve_distribution_view,
)
from research.study.agents.enums import DistributionMode, RegistryReasonCode
from research.study.agents.manifest_import import (
    ManifestImportError,
    build_manifest_release,
)
from research.study.agents.models import (
    AgentReleaseV1,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
    CapabilitySnapshotV1,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
)
from research.study.agents.registry import (
    AgentRegistry,
    coverage_report,
    verified_approval_options,
)

router = APIRouter()


class ReleaseRegisterRequest(BaseModel):
    """Register a digest-pinned agent release.

    ``AgentReleaseV1`` owns artifact/adapter identity only. Qualification is
    derived from verified conformance evidence, so a payload carrying a
    ``qualification_status`` is rejected rather than trusted.
    """

    release: AgentReleaseV1


class ResolveArtifactRequest(BaseModel):
    """Select the distribution artifact for one participant platform."""

    os: str
    arch: str


class SnapshotUploadRequest(BaseModel):
    """Upload a declared-vs-observed capability snapshot for a release."""

    snapshot: CapabilitySnapshotV1


class ReleaseImportRequest(BaseModel):
    """Import one build runtime manifest as a digest-pinned release.

    ``manifest`` is the packaging pipeline's manifest document. ``artifact_root``
    is an optional directory the manifest's relative ``archive`` paths resolve
    against; when a listed archive is present there its real digest/size are
    computed and the declared digest is verified. ``artifact_sizes`` supplies
    sizes for archives that are not available on disk -- a size is never guessed.
    """

    manifest: dict[str, Any]
    artifact_root: Optional[str] = None
    artifact_sizes: dict[str, int] = Field(default_factory=dict)


def _registry_from_db(db: Any) -> AgentRegistry:
    """Load a registry view so domain checks see existing releases."""
    registry = AgentRegistry()
    for row in store.list_releases(db):
        registry.register_release(store.row_to_release(row))
    return registry


def _registry_with_release(release: AgentReleaseV1) -> AgentRegistry:
    registry = AgentRegistry()
    registry.register_release(release)
    return registry


def _issue_payload(issue: Any) -> dict[str, Any]:
    return issue.model_dump(mode="json") if issue is not None else {}


def _release_for_profile(db: Any, profile: Any):
    """Rehydrate a PACKAGED distribution's release, or ``None``."""
    release_id = getattr(profile, "release_id", None)
    if not release_id:
        return None
    row = store.get_release(db, release_id)
    if row is None:
        return None
    return store.row_to_release(row)


def _distribution_payload(db: Any, profile: Any) -> dict[str, Any]:
    """The derived, read-only distribution view the UI consumes.

    Exposes the resolved ``release_id`` and ``version``, the DERIVED ``verified``
    boolean and the release's supported platforms. No secret is included: the
    provider block carries only non-secret identity (the connection id), never
    the endpoint URL or the secret reference/value.
    """
    release = _release_for_profile(db, profile)
    view = resolve_distribution_view(
        profile, release, distribution_id=profile.profile_id
    )
    release_row = (
        store.get_release(db, profile.release_id)
        if getattr(profile, "release_id", None)
        else None
    )
    return {
        "distribution_id": str(profile.profile_id),
        "name": profile.name,
        "distribution_mode": view.distribution_mode,
        "release_id": view.release_id,
        "release_version": view.version,
        "verified": view.verified,
        "supported_platforms": distribution_supported_platforms(release),
        # Approval options the release's conformance evidence actually verifies;
        # the editor must not offer an option this list does not contain.
        "verified_approval_options": verified_approval_options(
            getattr(release_row, "release_json", None)
            if release_row is not None
            else None
        ),
        "agent_package": view.agent_package,
        "agent_command": view.agent_command,
        "agent_command_args": view.agent_command_args,
        "provider": {
            "framework_version": profile.framework_version,
            "model": profile.model,
            "connection_id": (
                str(profile.connection_id)
                if getattr(profile, "connection_id", None) is not None
                else None
            ),
        },
    }


@router.post("/releases", summary="Register a digest-pinned agent release")
def register_release(
    payload: ReleaseRegisterRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Register a release; exact duplicate digest identity is rejected."""
    require_admin(current_user)

    if "qualification_status" in payload.release.model_fields_set:
        raise HTTPException(
            status_code=422,
            detail=(
                "qualification_status is derived from verified conformance "
                "evidence and cannot be supplied by a caller"
            ),
        )

    db = app.get_db_session()
    try:
        registry = _registry_from_db(db)
        result = registry.register_release(payload.release)
        if not result.accepted:
            status_code = (
                409
                if result.issue
                and result.issue.code == RegistryReasonCode.DUPLICATE_RELEASE
                else 422
            )
            raise HTTPException(
                status_code=status_code, detail=_issue_payload(result.issue)
            )
        try:
            row = store.upsert_release(db, payload.release)
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="release manifest digest already exists",
            ) from error
        return JsonResponseWithStatus(
            status_code=201,
            content={
                "accepted": True,
                "release": store.release_summary(row),
            },
        )
    finally:
        db.close()


@router.post(
    "/releases/import",
    summary="Register release(s) from a build runtime manifest",
)
def import_release(
    payload: ReleaseImportRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Import a build manifest as an idempotent, digest-pinned release.

    The release identity is derived from the manifest bytes, so re-importing the
    same manifest returns the existing release (never a duplicate/409). A
    manifest whose declared digest disagrees with an archive available under
    ``artifact_root`` is rejected loudly. Admin-only; takes a JSON body only, so
    CI can call it without any interactive prompt.
    """
    require_admin(current_user)

    try:
        plan = build_manifest_release(
            payload.manifest,
            artifact_root=payload.artifact_root,
            artifact_sizes=payload.artifact_sizes,
        )
    except ManifestImportError as error:
        raise HTTPException(
            status_code=422,
            detail={
                "code": error.code,
                "message": error.message,
                "field": error.field,
            },
        ) from error

    skipped = [
        {"platform": item.platform, "archive": item.archive, "reason": item.reason}
        for item in plan.skipped
    ]

    db = app.get_db_session()
    try:
        registry = _registry_from_db(db)
        existing = registry.get_release(
            plan.release.agent_id, plan.release.release_id
        )
        if existing is not None:
            if existing.digest_identity != plan.release.digest_identity:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": RegistryReasonCode.DUPLICATE_RELEASE.value,
                        "message": (
                            "release_id is already registered for this agent with "
                            "a different manifest digest"
                        ),
                        "field": "release_id",
                    },
                )
            row = store.get_release(db, plan.release.release_id)
            return JsonResponseWithStatus(
                status_code=200,
                content={
                    "accepted": True,
                    "created": False,
                    "release": store.release_summary(row) if row else {},
                    "skipped_artifacts": skipped,
                },
            )

        result = registry.register_release(plan.release)
        if not result.accepted:
            raise HTTPException(
                status_code=422, detail=_issue_payload(result.issue)
            )
        try:
            row = store.upsert_release(db, plan.release)
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="release manifest digest already exists",
            ) from error
        return JsonResponseWithStatus(
            status_code=201,
            content={
                "accepted": True,
                "created": True,
                "release": store.release_summary(row),
                "skipped_artifacts": skipped,
            },
        )
    finally:
        db.close()


@router.get("/distributions", summary="List distributions and their derived release view")
def list_distributions(
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """List every ``AgentProfile`` as a distribution with its resolved release.

    Readable by any authenticated user (it exposes no secret) so a researcher can
    see which distributions are VERIFIED before authoring a condition.
    """
    db = app.get_db_session()
    try:
        profiles = crud.list_agent_profiles(db)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "distributions": [
                    _distribution_payload(db, profile) for profile in profiles
                ]
            },
        )
    finally:
        db.close()


@router.get(
    "/distributions/{distribution_id}",
    summary="Fetch one distribution's derived release view",
)
def get_distribution(
    distribution_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        profile = crud.get_agent_profile_by_id(db, distribution_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Distribution not found")
        return JsonResponseWithStatus(
            status_code=200, content={"distribution": _distribution_payload(db, profile)}
        )
    finally:
        db.close()


@router.get("/releases", summary="List registered agent releases")
def list_releases(
    agent_id: Optional[str] = None,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_admin(current_user)
    db = app.get_db_session()
    try:
        rows = store.list_releases(db, agent_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={"releases": [store.release_summary(row) for row in rows]},
        )
    finally:
        db.close()


@router.get("/releases/{release_id}", summary="Fetch one registered release")
def get_release(
    release_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_admin(current_user)
    db = app.get_db_session()
    try:
        row = store.get_release(db, release_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Agent release not found")
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "release": store.release_summary(row),
                "model": store.row_to_release(row).model_dump(mode="json"),
            },
        )
    finally:
        db.close()


@router.post(
    "/releases/{release_id}/resolve",
    summary="Resolve the distribution artifact for a platform",
)
def resolve_artifact(
    release_id: str,
    payload: ResolveArtifactRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Return the exact platform artifact or a typed compatibility block."""
    require_admin(current_user)

    db = app.get_db_session()
    try:
        row = store.get_release(db, release_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Agent release not found")

        release = store.row_to_release(row)
        registry = _registry_with_release(release)
        result = registry.resolve_distribution(release, payload.os, payload.arch)
        if not result.resolved:
            raise HTTPException(
                status_code=409, detail=_issue_payload(result.issue)
            )
        content: dict[str, Any] = {
            "resolved": True,
            "distribution_mode": result.distribution_mode.value,
            "artifact": (
                result.artifact.model_dump(mode="json")
                if result.artifact is not None
                else None
            ),
        }
        if result.distribution_mode == DistributionMode.BYOA_EXTERNAL:
            # A BYOA release resolves to a participant-installable identity, not
            # a digest-pinned artifact.
            content["agent_command"] = result.agent_command
            content["agent_command_args"] = result.agent_command_args
            content["agent_package"] = result.agent_package
        return JsonResponseWithStatus(status_code=200, content=content)
    finally:
        db.close()


@router.post(
    "/releases/{release_id}/snapshots",
    summary="Upload an immutable capability snapshot",
)
def upload_snapshot(
    release_id: str,
    payload: SnapshotUploadRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Store a declared-vs-observed snapshot linked to the exact release."""
    require_admin(current_user)

    if payload.snapshot.release_id != release_id:
        raise HTTPException(
            status_code=422,
            detail="snapshot.release_id does not match the path release_id",
        )

    db = app.get_db_session()
    try:
        if store.get_release(db, release_id) is None:
            raise HTTPException(status_code=404, detail="Agent release not found")
        row = store.insert_snapshot(db, payload.snapshot)
        return JsonResponseWithStatus(
            status_code=201,
            content={"snapshot": store.snapshot_summary(row)},
        )
    finally:
        db.close()


@router.get(
    "/releases/{release_id}/snapshots",
    summary="List capability snapshots for a release",
)
def list_snapshots(
    release_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_admin(current_user)
    db = app.get_db_session()
    try:
        if store.get_release(db, release_id) is None:
            raise HTTPException(status_code=404, detail="Agent release not found")
        rows = store.list_snapshots(db, release_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={"snapshots": [store.snapshot_summary(row) for row in rows]},
        )
    finally:
        db.close()


@router.get(
    "/releases/{release_id}/coverage",
    summary="Summarize declared vs observed capability coverage",
)
def coverage(
    release_id: str,
    snapshot_id: Optional[uuid.UUID] = None,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Return the coverage matrix for one snapshot (default: the latest)."""
    require_admin(current_user)

    db = app.get_db_session()
    try:
        if store.get_release(db, release_id) is None:
            raise HTTPException(status_code=404, detail="Agent release not found")

        rows = list(store.list_snapshots(db, release_id))
        if snapshot_id is not None:
            rows = [
                row
                for row in rows
                if store.row_to_snapshot(row).snapshot_id == snapshot_id
            ]
        if not rows:
            raise HTTPException(
                status_code=404, detail="No capability snapshot found for release"
            )

        snapshot = store.row_to_snapshot(rows[-1])
        return JsonResponseWithStatus(
            status_code=200,
            content=coverage_report(snapshot).model_dump(mode="json"),
        )
    finally:
        db.close()
