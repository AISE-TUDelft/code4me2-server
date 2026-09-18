"""Runtime package and conformance API (Issue 11).

Mounted under ``/research/packages``. All endpoints are admin-only: they record
artifact/manifest evidence and can promote an agent release's qualification, so
they are never exposed to participants. Handlers are thin; the packaging domain
logic lives in :mod:`research.study.packaging`.
"""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import AuthenticatedUser, require_admin
from research.study.agents import store as agents_store
from research.study.packaging import store as packaging_store
from research.study.packaging.conformance import qualification_for_release
from research.study.packaging.models import (
    ConformanceReceiptV1,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
    RuntimeManifestV2,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
)

packages_router = APIRouter()


class QualificationLinkRequest(BaseModel):
    """Ask the registry to link conformance evidence to a release qualification."""

    release_id: str
    os: str
    arch: str
    required_cases: list[str] = Field(default_factory=list)


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
        row = packaging_store.upsert_package(db, payload)
        return JsonResponseWithStatus(
            status_code=201, content=packaging_store.package_summary(row)
        )
    finally:
        db.close()


@packages_router.get("/receipts", summary="List conformance receipts")
def list_receipts(
    artifact_digest: str | None = None,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        rows = packaging_store.list_receipts(db, artifact_digest)
        return JsonResponseWithStatus(
            status_code=200,
            content={"receipts": [row.model_dump(mode="json") for row in rows]},
        )
    finally:
        db.close()


@packages_router.post("/receipts", summary="Record a conformance receipt")
def create_receipt(
    payload: ConformanceReceiptV1,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        try:
            row = packaging_store.insert_receipt(db, payload)
        except LookupError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return JsonResponseWithStatus(
            status_code=201,
            content={
                "receipt_id": str(row.receipt_id),
                "status": row.status.value,
                "host": row.host.key,
                "artifact_digest": row.artifact_digest,
            },
        )
    finally:
        db.close()


@packages_router.get("/coverage", summary="Conformance coverage view")
def conformance_coverage(
    release_id: str | None = None,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        return JsonResponseWithStatus(
            status_code=200,
            content=packaging_store.conformance_coverage(db, release_id),
        )
    finally:
        db.close()


@packages_router.post(
    "/{package_id}/qualification", summary="Report derived qualification for a package"
)
def link_qualification(
    package_id: uuid.UUID,
    payload: QualificationLinkRequest,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    """Report whether a receipt proves all required cases PASS.

    Qualification is derived from the stored conformance evidence, so this
    endpoint no longer mutates a release (a caller can never mark a release
    QUALIFIED). Recording a passing receipt is the only promotion path; this
    handler evaluates the requested platform/case set against the already-stored
    receipts and reports both the decision and the release's derived status.
    """
    db = app.get_db_session()
    try:
        package_row = packaging_store.get_package(db, package_id)
        if package_row is None:
            raise HTTPException(status_code=404, detail="Runtime package not found")
        release_row = agents_store.get_release(db, payload.release_id)
        if release_row is None:
            raise HTTPException(status_code=404, detail="Agent release not found")
        release = agents_store.row_to_release(release_row)
        receipts = [
            packaging_store.row_to_receipt(row)
            for row in packaging_store.list_receipts(db)
        ]
        decision = qualification_for_release(
            release,
            receipts,
            required_cases=payload.required_cases,
            os=payload.os,
            arch=payload.arch,
        )
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "promote": decision.promote,
                "reason": decision.reason.value,
                "message": decision.message,
                "receipt_id": str(decision.receipt_id) if decision.receipt_id else None,
                "release_id": payload.release_id,
                "qualification_status": release.qualification_status.value,
            },
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
