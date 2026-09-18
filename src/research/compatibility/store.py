"""CRUD-style persistence helpers for capability receipts.

These functions take a caller-managed SQLAlchemy ``Session`` so the core
package never imports ``App`` or touches the application singleton. The router
is responsible for session lifecycle (``App.get_db_session`` /
``rollback`` / ``close``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, Sequence

from sqlalchemy import select

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.orm import Session

from database.research_schemas import AcpCapabilityReceipt

from .models import AcpCapabilityReceiptV1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def persist_receipt(session: Session, receipt: AcpCapabilityReceiptV1) -> AcpCapabilityReceipt:
    """Insert a receipt.

    The evidence digests are part of ``receipt_json.evidence[]`` (the canonical
    content), so no child rows are written. Raises
    ``sqlalchemy.exc.IntegrityError`` if the ``content_hash`` already exists; the
    caller decides whether that is a conflict or an idempotent retry.
    """
    environment = receipt.environment
    agent = receipt.agent
    row = AcpCapabilityReceipt(
        receipt_id=receipt.receipt_id,
        captured_at=receipt.captured_at,
        ide_build=environment.ide_build,
        ai_assistant_build=environment.ai_assistant_build,
        plugin_version=environment.plugin_version,
        os=environment.os,
        arch=environment.arch,
        agent_id=agent.agent_id,
        agent_version=agent.agent_version,
        adapter_version=agent.adapter_version,
        protocol_version=receipt.protocol_version,
        status=receipt.status.value,
        content_hash=receipt.content_hash,
        receipt_json=receipt.model_dump(mode="json"),
        created_at=_now(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_receipt(session: Session, receipt_id: uuid.UUID) -> Optional[AcpCapabilityReceipt]:
    """Fetch a receipt row by primary key, or ``None``."""
    return session.get(AcpCapabilityReceipt, receipt_id)


def list_receipts(
    session: Session, *, limit: int = 100, offset: int = 0
) -> Sequence[AcpCapabilityReceipt]:
    """List receipts newest-first."""
    statement = (
        select(AcpCapabilityReceipt)
        .order_by(AcpCapabilityReceipt.captured_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.execute(statement).scalars().all())


def row_to_receipt(row: AcpCapabilityReceipt) -> AcpCapabilityReceiptV1:
    """Rehydrate the stored receipt JSON into the versioned model."""
    return AcpCapabilityReceiptV1.model_validate(row.receipt_json)


def row_to_summary(row: AcpCapabilityReceipt) -> dict[str, Any]:
    """Return a compact, non-secret summary safe for list responses."""
    captured_at = row.captured_at
    created_at = row.created_at
    return {
        "receipt_id": str(row.receipt_id),
        "captured_at": captured_at.isoformat() if captured_at else None,
        "created_at": (
            created_at.isoformat() if isinstance(created_at, datetime) else None
        ),
        "status": row.status,
        "content_hash": row.content_hash,
        "protocol_version": row.protocol_version,
        "environment": {
            "ide_build": row.ide_build,
            "ai_assistant_build": row.ai_assistant_build,
            "plugin_version": row.plugin_version,
            "os": row.os,
            "arch": row.arch,
        },
        "agent": {
            "agent_id": row.agent_id,
            "agent_version": row.agent_version,
            "adapter_version": row.adapter_version,
        },
    }
