"""Admin and researcher API for the agent registry and capability contract.

Mounted under ``/api/research/agents``. Release import (manifest + verified
archive bytes), platform tests and operator views are admin-only; the
release catalogue and the distribution views are read-only, non-secret and
researcher-readable. The handlers are intentionally thin: identity derivation,
platform selection and coverage live in :mod:`research.study.agents.registry`
and :mod:`research.study.agents.manifest_import`, and the SQLAlchemy session is
supplied by ``App.get_db_session`` and passed to the store helpers.

Release records are digest-pinned and immutable. Editing a release's bytes
requires a new ``release_id``. There is no unverified registration path: the
only way a release enters the catalogue is ``POST /releases/import`` with the
exact archive bytes, whose digests become the plugin's bootstrap pins.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
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


class ResolveArtifactRequest(BaseModel):
    """Select the distribution artifact for one participant platform."""

    os: str
    arch: str


class SnapshotUploadRequest(BaseModel):
    """Upload a declared-vs-observed capability snapshot for a release."""

    snapshot: CapabilitySnapshotV1


#: Chunk size used while streaming an uploaded archive to disk.
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
    """``(max archive bytes, max total bytes)`` for one multipart import."""
    return (
        _positive_int_env("RESEARCH_IMPORT_MAX_ARCHIVE_BYTES", 256 * 1024 * 1024),
        _positive_int_env("RESEARCH_IMPORT_MAX_TOTAL_BYTES", 1024 * 1024 * 1024),
    )


def _parse_manifest_field(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_MANIFEST",
                "message": f"the manifest field is not valid JSON: {error}",
                "field": "manifest",
            },
        ) from error
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_MANIFEST",
                "message": "the manifest field must be a JSON object",
                "field": "manifest",
            },
        )
    return parsed


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
    producer test results so legacy rows stay unqualified.
    """
    release = store.row_to_release(row)
    summary = store.release_summary(row)
    return {
        "release_id": release.release_id,
        "version": release.version,
        "agent_id": release.agent_id,
        "distribution_mode": release.distribution_mode.value,
        "qualification_status": release.qualification_status.value,
        "supported_platforms": distribution_supported_platforms(release),
        "verified_approval_options": verified_approval_options(row.release_json),
        "tests": summary["tests"],
        "is_byoa": release.is_byoa,
    }


@router.post(
    "/releases/import",
    summary="Import a release from a build manifest and its verified archives",
)
async def import_release(
    manifest: str = Form(...),
    archives: Optional[list[UploadFile]] = File(None),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Import a build manifest **and its archive bytes** as a pinned release.

    Multipart: ``manifest`` is the producer manifest JSON and ``archives`` are
    the exact ZIPs it declares (basenames). Every file is streamed to a bounded
    temporary file, then its size and SHA-256 are recomputed and must match the
    manifest. Exactly the declared basenames are required: a missing, duplicate
    or unexpected upload, a digest/size mismatch, or a placeholder digest
    rejects the entire import and **no release row is created**.

    The release identity is the canonical manifest digest; each artifact's
    digest is the ZIP digest the plugin's bootstrap manifest pins. Admin-only;
    this is the only way a release enters the catalogue.
    """
    require_admin(current_user)

    parsed = _parse_manifest_field(manifest)
    max_archive, max_total = import_limits()
    uploaded = archives or []
    temp_root = Path(tempfile.mkdtemp(prefix="code4me-release-import-"))
    stored: dict[str, Path] = {}
    total_bytes = 0
    try:
        for upload in uploaded:
            name = upload.filename or ""
            if not name or name in {".", ".."} or name != Path(name).name or "\\" in name:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "INVALID_UPLOAD",
                        "message": "every uploaded archive needs a filename",
                        "field": "archives",
                    },
                )
            if name in stored:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "DUPLICATE_ARCHIVE",
                        "message": f"archive {name!r} was uploaded more than once",
                        "field": "archives",
                    },
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
                            detail={
                                "code": "ARCHIVE_TOO_LARGE",
                                "message": (
                                    f"archive {name!r} exceeds the {max_archive}-byte "
                                    "per-archive import limit"
                                ),
                                "field": "archives",
                            },
                        )
                    if total_bytes > max_total:
                        raise HTTPException(
                            status_code=413,
                            detail={
                                "code": "IMPORT_TOO_LARGE",
                                "message": (
                                    f"the import exceeds the {max_total}-byte total limit"
                                ),
                                "field": "archives",
                            },
                        )
                    sink.write(chunk)
            stored[name] = destination

        try:
            plan = build_manifest_release(parsed, archives=stored)
        except ManifestImportError as error:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": error.code,
                    "message": error.message,
                    "field": error.field,
                },
            ) from error

        db = app.get_db_session()
        try:
            rows = []
            created = False
            for release in [plan.release, *plan.byoa_releases]:
                existing = store.get_release(db, release.release_id)
                if existing is not None and (
                    existing.agent_id != release.agent_id
                    or existing.source_manifest_digest != release.source_manifest_digest
                ):
                    raise HTTPException(status_code=409, detail={"code": "DUPLICATE_RELEASE"})
                result = AgentRegistry().register_release(release)
                if not result.accepted:
                    raise HTTPException(status_code=422, detail=_issue_payload(result.issue))
                created |= existing is None
                rows.append(store.upsert_release(db, release, commit=False))
            db.commit()
            return JsonResponseWithStatus(
                status_code=201 if created else 200,
                content={
                    "accepted": True,
                    "created": created,
                    "release": store.release_summary(rows[0]),
                    "releases": [store.release_summary(row) for row in rows],
                    "verified_artifacts": plan.verified_artifacts,
                },
            )
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(status_code=409, detail="release identity already exists") from error
        except HTTPException:
            db.rollback()
            raise
        finally:
            db.close()
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


class ReleaseUrlImport(BaseModel):
    manifest_url: str
    archive_urls: list[str] = Field(min_length=1, max_length=16)


@router.post("/releases/import-url", summary="Import immutable release assets over HTTPS")
async def import_release_url(
    payload: ReleaseUrlImport,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_admin(current_user)
    allowed_hosts = {
        host.strip().lower() for host in os.environ.get(
            "RESEARCH_RELEASE_HOSTS",
            "github.com,release-assets.githubusercontent.com,objects.githubusercontent.com",
        ).split(",") if host.strip()
    }
    max_archive, max_total = import_limits()
    total = 0
    uploads = []
    with tempfile.TemporaryDirectory(prefix="code4me-release-url-") as directory:
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                for index, source in enumerate([payload.manifest_url, *payload.archive_urls]):
                    parsed = urlsplit(source)
                    name = unquote(parsed.path.rsplit("/", 1)[-1])
                    if not name or name in {".", ".."} or "/" in name or "\\" in name:
                        raise HTTPException(422, detail={"code": "INVALID_UPLOAD"})
                    if index and any(upload.filename == name for upload in uploads):
                        raise HTTPException(422, detail={"code": "DUPLICATE_ARCHIVE"})
                    destination = Path(directory) / str(index)
                    for redirect in range(6):
                        parsed = urlsplit(source)
                        if (parsed.scheme != "https" or parsed.hostname not in allowed_hosts
                                or parsed.username or parsed.password or parsed.port not in (None, 443)):
                            raise HTTPException(422, detail={"code": "UNTRUSTED_RELEASE_URL"})
                        async with client.stream("GET", source) as response:
                            if response.is_redirect:
                                if redirect == 5 or not response.headers.get("location"):
                                    raise HTTPException(422, detail={"code": "INVALID_REDIRECT"})
                                source = urljoin(source, response.headers["location"])
                                continue
                            response.raise_for_status()
                            size = 0
                            with destination.open("wb") as sink:
                                async for chunk in response.aiter_bytes(_IMPORT_CHUNK_BYTES):
                                    size += len(chunk)
                                    total += len(chunk)
                                    if size > (max_archive if index else 1024 * 1024) or total > max_total:
                                        raise HTTPException(413, detail={"code": "IMPORT_TOO_LARGE"})
                                    sink.write(chunk)
                            break
                    if index:
                        uploads.append(UploadFile(destination.open("rb"), filename=name))
                manifest = (Path(directory) / "0").read_text(encoding="utf-8")
                return await import_release(manifest, uploads, current_user, app)
        except (httpx.HTTPError, UnicodeError, ValueError) as error:
            raise HTTPException(422, detail={"code": "RELEASE_DOWNLOAD_FAILED"}) from error
        finally:
            for upload in uploads:
                await upload.close()


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


@router.post("/releases/{release_id}/disable", summary="Permanently disable a release")
def disable_release(
    release_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_admin(current_user)
    db = app.get_db_session()
    try:
        row = store.disable_release(db, release_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Agent release not found")
        return JsonResponseWithStatus(status_code=200, content={"release": store.release_summary(row)})
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
