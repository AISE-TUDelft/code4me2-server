"""Admin and researcher API for the agent registry and capability contract.

Mounted under ``/api/research/agents``. Release import and operator views are
admin-only; the release catalogue and the distribution views are read-only,
non-secret and researcher-readable. The handlers are intentionally thin:
identity derivation, platform selection and coverage live in
:mod:`research.study.agents.registry` and
:mod:`research.study.agents.manifest_import`, and the SQLAlchemy session is
supplied by ``App.get_db_session`` and passed to the store helpers.

A release enters the catalogue only through ``POST /releases/import`` (local
multipart recipe + archives) or ``POST /releases/import/deployed`` (recipe URL +
archive URLs the server downloads and hashes itself). The recipe's self-check
verdict is recorded as the release's usability. Release records are digest-pinned
and immutable; editing bytes requires a new ``release_id``. There is no separate
approval step, only a one-way administrator disable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
)
from backend.routers.research.access import require_researcher
from database import crud
from research.study.agents import store
from research.study.agents.distributions import (
    distribution_supported_platforms,
    resolve_distribution_view,
)
from research.study.agents.enums import DistributionMode, RegistryReasonCode
from research.study.agents.manifest_import import (
    ManifestImportError,
    build_manifest_releases,
    recipe_tests,
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


class ResolveArtifactRequest(BaseModel):
    """Select the distribution artifact for one participant platform."""

    os: str
    arch: str


class SnapshotUploadRequest(BaseModel):
    """Upload a declared-vs-observed capability snapshot for a release."""

    snapshot: CapabilitySnapshotV1


class DeployedImportRequest(BaseModel):
    """Import a recipe and its archives from URLs the server downloads itself.

    CI publishes the recipe and the platform ZIPs to a GitHub Release; the
    administrator supplies those URLs. The server downloads the recipe, then
    downloads every archive, recomputes its SHA-256 and size, and compares them
    with the recipe. The URLs are never trusted to be consistent with each other.
    """

    manifest_url: str
    archive_urls: list[str]


#: Chunk size used while streaming an uploaded/downloaded archive to disk.
_IMPORT_CHUNK_BYTES = 1024 * 1024


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def import_limits() -> tuple[int, int]:
    """``(max archive bytes, max total bytes)`` for one import."""
    return (
        _positive_int_env("RESEARCH_IMPORT_MAX_ARCHIVE_BYTES", 512 * 1024 * 1024),
        _positive_int_env("RESEARCH_IMPORT_MAX_TOTAL_BYTES", 2 * 1024 * 1024 * 1024),
    )


def _import_detail(code: str, message: str, field: str = "") -> dict[str, Any]:
    return {"code": code, "message": message, "field": field}


def _hash_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_IMPORT_CHUNK_BYTES), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _safe_origin(url: str) -> None:
    """Reject non-HTTPS (except loopback) or credential-bearing URLs."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise HTTPException(
            status_code=422,
            detail=_import_detail("INVALID_URL", f"unsupported URL {url!r}", "urls"),
        )
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(
            status_code=422,
            detail=_import_detail(
                "INVALID_URL", "plain HTTP is only permitted on loopback", "urls"
            ),
        )


def _download(url: str, destination: Path, max_bytes: int) -> tuple[str, int]:
    """Download ``url`` to ``destination`` and return its recomputed digest and size."""
    _safe_origin(url)
    request = Request(url, headers={"Accept": "*/*"})
    try:
        with build_opener().open(request, timeout=60) as response:
            size = 0
            with destination.open("wb") as sink:
                while True:
                    chunk = response.read(_IMPORT_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=_import_detail(
                                "ARCHIVE_TOO_LARGE",
                                f"{url!r} exceeds the {max_bytes}-byte import limit",
                                "archive_urls",
                            ),
                        )
                    sink.write(chunk)
    except HTTPException:
        raise
    except (HTTPError, URLError, OSError) as error:
        raise HTTPException(
            status_code=422,
            detail=_import_detail("DOWNLOAD_FAILED", f"could not fetch {url!r}: {error}", "urls"),
        ) from error
    return _hash_and_size(destination)


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


def _release_catalogue_entry(row: Any) -> dict[str, Any]:
    """One registered release as a non-secret catalogue entry.

    Derived, read-only fields only: no artifact bytes, command line, endpoint or
    credential material. ``qualification_status`` is recomputed from the stored
    conformance evidence so legacy rows stay truthful (ISSUE-11).
    """
    release = store.row_to_release(row)
    return {
        "release_id": release.release_id,
        "version": release.version,
        "agent_id": release.agent_id,
        "distribution_mode": release.distribution_mode.value,
        "qualification_status": release.qualification_status.value,
        "supported_platforms": distribution_supported_platforms(release),
        "verified_approval_options": verified_approval_options(row.release_json),
        "is_byoa": release.is_byoa,
    }


def _parse_recipe_field(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail=_import_detail(
                "INVALID_MANIFEST", f"the recipe field is not valid JSON: {error}", "recipe"
            ),
        ) from error
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=422,
            detail=_import_detail(
                "INVALID_MANIFEST", "the recipe field must be a JSON object", "recipe"
            ),
        )
    return parsed


def _persist_import(db: Any, plan: Any, tests: dict[str, Any]) -> tuple[list[Any], bool]:
    """Persist every release the plan describes, idempotently.

    Identity conflicts (same ``release_id``, different digest) are rejected; an
    exact re-import returns the existing rows. No row is written before the whole
    recipe and every archive have been verified.
    """
    registry = _registry_from_db(db)
    rows: list[Any] = []
    created = False
    for release in plan.releases:
        existing = registry.get_release(release.agent_id, release.release_id)
        if existing is not None:
            if existing.digest_identity != release.digest_identity:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": RegistryReasonCode.DUPLICATE_RELEASE.value,
                        "message": (
                            "release_id is already registered for this agent with "
                            "a different recipe digest"
                        ),
                        "field": "release_id",
                    },
                )
            row = store.get_release(db, release.release_id)
            if row is not None:
                rows.append(row)
            continue

        result = registry.register_release(release)
        if not result.accepted:
            raise HTTPException(status_code=422, detail=_issue_payload(result.issue))
        try:
            row = store.upsert_release(
                db, release, evidence={"tests": tests}
            )
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(
                status_code=409, detail="release recipe digest already exists"
            ) from error
        rows.append(row)
        created = True
    return rows, created


def _import_verified(recipe: dict[str, Any], verified: dict[str, tuple[str, int]], app: App):
    try:
        tests = recipe_tests(recipe)
        plan = build_manifest_releases(recipe, verified=verified)
    except ManifestImportError as error:
        raise HTTPException(
            status_code=422,
            detail=_import_detail(error.code, error.message, error.field),
        ) from error

    db = app.get_db_session()
    try:
        rows, created = _persist_import(db, plan, tests)
        return JsonResponseWithStatus(
            status_code=201 if created else 200,
            content={
                "accepted": True,
                "created": created,
                "releases": [store.release_summary(row) for row in rows],
                "verified_artifacts": plan.verified_artifacts,
            },
        )
    finally:
        db.close()


@router.post(
    "/releases/import",
    summary="Import a recipe and its archive bytes (multipart)",
)
async def import_release(
    recipe: str = Form(...),
    archives: Optional[list[UploadFile]] = File(None),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Import a producer recipe **and its archive bytes**.

    Multipart: ``recipe`` is the producer's recipe JSON and ``archives`` are the
    exact ZIPs it declares (basenames). Every file is streamed to a bounded
    temporary file, then its size and SHA-256 are recomputed and must match the
    recipe. A missing, duplicate or unexpected upload, a digest/size mismatch, a
    recipe whose self-check did not pass, or an import with no bytes rejects the
    entire import and **no release row is created**. Admin-only.
    """
    require_admin(current_user)

    parsed = _parse_recipe_field(recipe)
    max_archive, max_total = import_limits()
    uploaded = archives or []
    temp_root = Path(tempfile.mkdtemp(prefix="code4me-release-import-"))
    verified: dict[str, tuple[str, int]] = {}
    total_bytes = 0
    try:
        for upload in uploaded:
            name = Path(upload.filename or "").name
            if not name:
                raise HTTPException(
                    status_code=422,
                    detail=_import_detail(
                        "INVALID_UPLOAD", "every uploaded archive needs a filename", "archives"
                    ),
                )
            if name in verified:
                raise HTTPException(
                    status_code=422,
                    detail=_import_detail(
                        "DUPLICATE_ARCHIVE",
                        f"archive {name!r} was uploaded more than once",
                        "archives",
                    ),
                )
            destination = temp_root / name
            size = 0
            with destination.open("wb") as sink:
                while True:
                    chunk = await upload.read(_IMPORT_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    total_bytes += len(chunk)
                    if size > max_archive:
                        raise HTTPException(
                            status_code=413,
                            detail=_import_detail(
                                "ARCHIVE_TOO_LARGE",
                                f"archive {name!r} exceeds the {max_archive}-byte limit",
                                "archives",
                            ),
                        )
                    if total_bytes > max_total:
                        raise HTTPException(
                            status_code=413,
                            detail=_import_detail(
                                "IMPORT_TOO_LARGE",
                                f"the import exceeds the {max_total}-byte total limit",
                                "archives",
                            ),
                        )
                    sink.write(chunk)
            verified[name] = _hash_and_size(destination)

        if not verified:
            raise HTTPException(
                status_code=422,
                detail=_import_detail(
                    "ARTIFACT_MISSING",
                    "the import carries no archive bytes",
                    "archives",
                ),
            )
        return _import_verified(parsed, verified, app)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


@router.post(
    "/releases/import/deployed",
    summary="Import a recipe and its archives from URLs",
)
def import_release_deployed(
    payload: DeployedImportRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Import a recipe and its archives by URL (the deployed/CI path).

    The server downloads the recipe, then each archive, and **computes** the
    SHA-256 and size itself before comparing them with the recipe. Nothing about
    the URLs is trusted. Admin-only.
    """
    require_admin(current_user)

    max_archive, max_total = import_limits()
    temp_root = Path(tempfile.mkdtemp(prefix="code4me-release-deploy-"))
    verified: dict[str, tuple[str, int]] = {}
    total_bytes = 0
    try:
        recipe_path = temp_root / "recipe.json"
        _download(payload.manifest_url, recipe_path, max_archive)
        parsed = _parse_recipe_field(recipe_path.read_text(encoding="utf-8"))

        if not payload.archive_urls:
            raise HTTPException(
                status_code=422,
                detail=_import_detail(
                    "ARTIFACT_MISSING", "the import carries no archive URLs", "archive_urls"
                ),
            )
        for url in payload.archive_urls:
            name = Path(urlsplit(url).path).name
            if not name:
                raise HTTPException(
                    status_code=422,
                    detail=_import_detail(
                        "INVALID_URL", f"archive URL {url!r} has no filename", "archive_urls"
                    ),
                )
            if name in verified:
                raise HTTPException(
                    status_code=422,
                    detail=_import_detail(
                        "DUPLICATE_ARCHIVE",
                        f"archive {name!r} was supplied more than once",
                        "archive_urls",
                    ),
                )
            digest_hex, size = _download(url, temp_root / name, max_archive)
            total_bytes += size
            if total_bytes > max_total:
                raise HTTPException(
                    status_code=413,
                    detail=_import_detail(
                        "IMPORT_TOO_LARGE",
                        f"the import exceeds the {max_total}-byte total limit",
                        "archive_urls",
                    ),
                )
            verified[name] = (digest_hex, size)

        return _import_verified(parsed, verified, app)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


@router.post(
    "/releases/{release_id}/disable",
    summary="Disable a release (one-way, no re-approval)",
)
def disable_release(
    release_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Disable a release so it can no longer be resolved or bootstrapped.

    This is the only operator transition: it is one-way and there is no
    re-approval flow. Admin-only.
    """
    require_admin(current_user)
    db = app.get_db_session()
    try:
        row = store.disable_release(db, release_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Agent release not found")
        return JsonResponseWithStatus(
            status_code=200, content={"release": store.release_summary(row)}
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


@router.get(
    "/release-catalogue",
    summary="List releases for researcher profile authoring",
)
def list_release_catalogue(
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """The profile-independent release catalogue, readable by any researcher.

    A fresh installation must be able to author its first ``AgentProfile``
    before any profile row exists, so this view is derived only from registered
    releases (ISSUE-11). It is read-only and exposes no secret; importing and
    qualifying releases remains administrator-only under ``/releases``.
    """
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        rows = store.list_releases(db)
        return JsonResponseWithStatus(
            status_code=200,
            content={"releases": [_release_catalogue_entry(row) for row in rows]},
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
