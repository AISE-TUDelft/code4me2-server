"""Runtime package API (Issue 11).

Mounted under ``/research/packages``. All endpoints are admin-only: they record
artifact/manifest evidence, so they are never exposed to participants. Handlers
are thin; the packaging domain logic lives in :mod:`research.study.packaging`.

Package metadata cannot create or qualify a release; import verified bytes first.
"""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime

from fastapi import APIRouter, Depends, HTTPException

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import AuthenticatedUser, require_admin
from research.study.agents import store as agents_store
from research.study.packaging import store as packaging_store
from research.study.packaging.models import (
    RuntimeManifestV2,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
)

packages_router = APIRouter()


@packages_router.get("", summary="List registered runtime packages")
def list_packages(
    release_id: str | None = None,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        rows = packaging_store.list_packages(db, release_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={"packages": [packaging_store.package_summary(row) for row in rows]},
        )
    finally:
        db.close()


@packages_router.post("", summary="Register a runtime package manifest")
def register_package(
    payload: RuntimeManifestV2,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        if not payload.digest_matches():
            raise HTTPException(
                status_code=422,
                detail="manifest_digest does not match the canonical manifest content",
            )
        if agents_store.get_release(db, payload.release_id) is None:
            raise HTTPException(status_code=404, detail="Import the verified release first")
        row = packaging_store.upsert_package(db, payload)
        return JsonResponseWithStatus(
            status_code=201, content=packaging_store.package_summary(row)
        )
    finally:
        db.close()


@packages_router.get("/{package_id}", summary="Get a runtime package")
def get_package(
    package_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        row = packaging_store.get_package(db, package_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Runtime package not found")
        return JsonResponseWithStatus(
            status_code=200, content=packaging_store.package_summary(row)
        )
    finally:
        db.close()
