"""Persistence helpers for runtime packages (Issue 11).

A release's ``RuntimeManifestV2`` lives in
``agent_release.release_json.package_json``. Every helper takes a
caller-supplied SQLAlchemy ``Session`` and works in domain models
(:class:`RuntimeManifestV2`), never raw ORM rows. The packaging package itself
imports no FastAPI/App.

There is no conformance-receipt store: a release's usability comes from the
recipe self-check recorded on the release at import time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, Sequence

from sqlalchemy import select

from database.research_schemas import AgentRelease
from research.study.agents.registry import derive_qualification_status

from .models import RuntimeManifestV2

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = [
    "PackageView",
    "get_package",
    "get_package_by_digest",
    "list_packages",
    "package_summary",
    "row_to_manifest",
    "upsert_package",
]

#: ``release_json`` key that carries the packaging evidence.
PACKAGE_KEY = "package_json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PackageView:
    """One release's stored runtime package (a projection of ``release_json``)."""

    package_id: uuid.UUID
    release_id: str
    os: Optional[str]
    arch: Optional[str]
    manifest_digest: str
    manifest_json: dict[str, Any]
    signature: Optional[str]
    created_at: Optional[datetime]


def _package_id(manifest_digest: str) -> uuid.UUID:
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"code4me2://runtime-package/{manifest_digest}"
    )


def _package_from_release(row: AgentRelease) -> Optional[PackageView]:
    data = row.release_json or {}
    manifest = data.get(PACKAGE_KEY)
    if not isinstance(manifest, dict):
        return None
    platforms = manifest.get("supported_platforms") or []
    platform = platforms[0] if platforms else {}
    manifest_digest = str(manifest.get("manifest_digest") or "")
    return PackageView(
        package_id=_package_id(manifest_digest),
        release_id=row.release_id,
        os=platform.get("os"),
        arch=platform.get("arch"),
        manifest_digest=manifest_digest,
        manifest_json=manifest,
        signature=manifest.get("signature"),
        created_at=row.created_at,
    )


def _iter_packages(session: Session) -> list[PackageView]:
    statement = select(AgentRelease)
    views = [
        _package_from_release(row)
        for row in session.execute(statement).scalars().all()
    ]
    return [view for view in views if view is not None]


def upsert_package(
    session: Session,
    manifest: RuntimeManifestV2,
    *,
    package_id: Optional[uuid.UUID] = None,
) -> PackageView:
    """Store a release's package manifest, or return the existing digest view.

    The package is folded into ``agent_release.release_json.package_json`` (the
    release is created in ``UNQUALIFIED`` if it is not registered yet).
    ``package_id`` is derived from the manifest digest, so it is deterministic.
    """
    existing = get_package_by_digest(session, manifest.manifest_digest)
    if existing is not None:
        return existing
    row = session.get(AgentRelease, manifest.release_id)
    if row is None:
        row = AgentRelease(
            release_id=manifest.release_id,
            agent_id=manifest.agent_id,
            source_manifest_digest=manifest.manifest_digest,
            status="UNQUALIFIED",
            release_json={},
            created_at=_now(),
        )
        session.add(row)
    data = dict(row.release_json or {})
    data[PACKAGE_KEY] = manifest.model_dump(mode="json")
    derived = derive_qualification_status(data)
    data["qualification_status"] = derived.value
    row.status = derived.value
    row.release_json = data
    session.commit()
    session.refresh(row)
    view = _package_from_release(row)
    assert view is not None
    return view


def get_package(session: Session, package_id: uuid.UUID) -> Optional[PackageView]:
    """Fetch a package view by its deterministic package id, or ``None``."""
    for view in _iter_packages(session):
        if view.package_id == package_id:
            return view
    return None


def get_package_by_digest(
    session: Session, manifest_digest: str
) -> Optional[PackageView]:
    """Fetch a package view by its manifest digest, or ``None``."""
    for view in _iter_packages(session):
        if view.manifest_digest == manifest_digest:
            return view
    return None


def list_packages(
    session: Session, release_id: Optional[str] = None
) -> Sequence[PackageView]:
    """List packages, optionally for one release, oldest-first."""
    views = [
        view
        for view in _iter_packages(session)
        if release_id is None or view.release_id == release_id
    ]
    return sorted(
        views,
        key=lambda view: (view.created_at or datetime.min.replace(tzinfo=timezone.utc)),
    )


def row_to_manifest(view: PackageView) -> RuntimeManifestV2:
    """Rehydrate a package view into its manifest."""
    return RuntimeManifestV2.model_validate(view.manifest_json)


def package_summary(view: PackageView) -> dict[str, Any]:
    """Compact, non-secret package summary safe for responses."""
    manifest = view.manifest_json or {}
    return {
        "package_id": str(view.package_id),
        "release_id": view.release_id,
        "os": view.os,
        "arch": view.arch,
        "manifest_digest": view.manifest_digest,
        "signature": view.signature,
        "component_count": len(manifest.get("components") or []),
        "supported_platforms": manifest.get("supported_platforms") or [],
        "created_at": view.created_at.isoformat() if view.created_at else None,
    }
