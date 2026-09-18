"""Persistence helpers for runtime packages and conformance receipts (Issue 11).

Packaging evidence is not a table: a release's ``RuntimeManifestV2`` lives in
``agent_release.release_json.package_json`` and its immutable conformance
receipts live in ``agent_release.release_json.conformance[]``. Every helper
takes a caller-supplied SQLAlchemy ``Session`` and works in domain models
(:class:`RuntimeManifestV2`, :class:`ConformanceReceiptV1`), never raw ORM rows.
The packaging package itself imports no FastAPI/App.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, Sequence

from sqlalchemy import select

from database.research_schemas import AgentRelease
from research.study.agents.registry import derive_qualification_status

from .models import ConformanceReceiptV1, RuntimeManifestV2, normalize_sha256

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = [
    "PackageView",
    "conformance_coverage",
    "get_package",
    "get_package_by_digest",
    "get_receipt",
    "insert_receipt",
    "list_packages",
    "list_receipts",
    "package_summary",
    "row_to_manifest",
    "row_to_receipt",
    "upsert_package",
]

#: ``release_json`` keys that carry packaging evidence.
PACKAGE_KEY = "package_json"
CONFORMANCE_KEY = "conformance"


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
    release is created in ``DRAFT`` if it is not registered yet). ``package_id``
    is derived from the manifest digest, so it is deterministic.
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


def _same_digest(left: Optional[str], right: Optional[str]) -> bool:
    return normalize_sha256(left) is not None and normalize_sha256(
        left
    ) == normalize_sha256(right)


def _find_release_for_artifact(
    session: Session, artifact_digest: str
) -> Optional[AgentRelease]:
    """Find the release that owns an exact artifact/adapter digest."""
    statement = select(AgentRelease)
    for row in session.execute(statement).scalars().all():
        data = row.release_json or {}
        for artifact in data.get("artifacts") or []:
            if _same_digest(artifact.get("sha256"), artifact_digest):
                return row
        manifest = data.get(PACKAGE_KEY) or {}
        for component in manifest.get("components") or []:
            if _same_digest(component.get("sha256"), artifact_digest):
                return row
        # A BYOA release has no distribution artifact, so its conformance
        # evidence binds to the release's own distribution digest instead. This
        # keeps evidence-driven qualification working for participant-installed
        # agents without inventing a fake artifact.
        if not (data.get("artifacts") or []) and _same_digest(
            data.get("source_manifest_digest"), artifact_digest
        ):
            return row
    return None


def insert_receipt(
    session: Session, receipt: ConformanceReceiptV1
) -> ConformanceReceiptV1:
    """Append one immutable conformance receipt to its release's ``release_json``.

    The release is located by the exact artifact digest the receipt is bound to.
    """
    row = _find_release_for_artifact(session, receipt.artifact_digest)
    if row is None:
        raise LookupError(
            "no registered release owns artifact digest "
            f"{receipt.artifact_digest!r}"
        )
    data = dict(row.release_json or {})
    conformance = [
        item
        for item in (data.get(CONFORMANCE_KEY) or [])
        if str(item.get("receipt_id")) != str(receipt.receipt_id)
    ]
    conformance.append(receipt.model_dump(mode="json"))
    data[CONFORMANCE_KEY] = conformance
    # Qualification is derived: recording a passing receipt is the only way a
    # release becomes QUALIFIED, and the derived value is persisted so the read
    # path never has to trust a caller-supplied status.
    derived = derive_qualification_status(data)
    data["qualification_status"] = derived.value
    row.status = derived.value
    row.release_json = data
    session.add(row)
    session.commit()
    session.refresh(row)
    return receipt


def _iter_receipts(session: Session) -> list[ConformanceReceiptV1]:
    statement = select(AgentRelease)
    receipts: list[ConformanceReceiptV1] = []
    for row in session.execute(statement).scalars().all():
        for item in (row.release_json or {}).get(CONFORMANCE_KEY) or []:
            receipts.append(ConformanceReceiptV1.model_validate(item))
    return receipts


def get_receipt(
    session: Session, receipt_id: uuid.UUID
) -> Optional[ConformanceReceiptV1]:
    """Fetch a receipt by id, or ``None``."""
    for receipt in _iter_receipts(session):
        if receipt.receipt_id == receipt_id:
            return receipt
    return None


def list_receipts(
    session: Session, artifact_digest: Optional[str] = None
) -> Sequence[ConformanceReceiptV1]:
    """List receipts, optionally for one artifact digest, oldest-first."""
    receipts = [
        receipt
        for receipt in _iter_receipts(session)
        if artifact_digest is None
        or _same_digest(receipt.artifact_digest, artifact_digest)
    ]
    return sorted(receipts, key=lambda receipt: receipt.created_at)


def row_to_receipt(row: Any) -> ConformanceReceiptV1:
    """Rehydrate a receipt from a row, a stored dict, or a model."""
    if isinstance(row, ConformanceReceiptV1):
        return row
    if isinstance(row, dict):
        return ConformanceReceiptV1.model_validate(row)
    return ConformanceReceiptV1.model_validate(row.receipt_json)


def conformance_coverage(
    session: Session, release_id: Optional[str] = None
) -> dict[str, Any]:
    """A coverage view: receipt counts by status and by ``(os, arch)`` host.

    ``release_id`` filters to that release's stored receipts; without it every
    stored receipt is summarized. The view is explicit: a platform with no
    ``PASS`` receipt is reported as uncovered, not omitted.
    """
    statement = select(AgentRelease)
    rows = list(session.execute(statement).scalars().all())
    receipts: list[ConformanceReceiptV1] = []
    for row in rows:
        if release_id is not None and row.release_id != release_id:
            continue
        for item in (row.release_json or {}).get(CONFORMANCE_KEY) or []:
            receipts.append(ConformanceReceiptV1.model_validate(item))

    by_status: dict[str, int] = {}
    by_host: dict[str, dict[str, int]] = {}
    for receipt in receipts:
        status = receipt.status.value
        by_status[status] = by_status.get(status, 0) + 1
        host = by_host.setdefault(receipt.host.key, {})
        host[status] = host.get(status, 0) + 1
    return {
        "release_id": release_id,
        "total": len(receipts),
        "by_status": by_status,
        "by_host": {
            host: {
                "pass": counts.get("PASS", 0),
                "fail": counts.get("FAIL", 0),
                "unsupported": counts.get("UNSUPPORTED", 0),
                "unknown": counts.get("UNKNOWN", 0),
                "blocked": counts.get("BLOCKED", 0),
            }
            for host, counts in by_host.items()
        },
    }
