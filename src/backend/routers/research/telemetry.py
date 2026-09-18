"""Telemetry ingestion API (Issue 09).

Mounted under ``/api/research/telemetry``. Batch submission is authenticated
solely by the scoped session capability carried in the request body — a normal
account/session cookie is never accepted in its place. Receipt inspection is
admin/researcher-only.
"""

from __future__ import annotations

import logging
import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
)
from backend.routers.research.bootstrap import BOOTSTRAP_SIGNING_SECRET
from research.analysis.operations import store as operations_store
from research.participants import identity as identity_store
from research.runtime.bootstrap.capability import verify_capability
from research.runtime.bootstrap.models import (
    CapabilityVerification,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
    SessionCapability,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
)
from research.runtime.sessions import store as session_store
from research.study.protocol import store as protocol_store
from research.telemetry.ingestion.models import (
    TelemetryBatchAckV1,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
    TelemetryBatchRequestV1,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
)
from research.telemetry.ingestion.service import ingest_batch
from research.telemetry.ingestion.store import SqlAlchemyIngestionStore, get_receipt_by_id

router = APIRouter()

logger = logging.getLogger(__name__)

AUDIENCE = "research-runtime"
SCOPE_WRITE = "telemetry:write"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log_ack(ack: TelemetryBatchAckV1) -> None:
    """Emit exactly one compact line per batch so stalled uploads are visible.

    Records only ids, counts and the distinct reason codes — never event
    payloads — so a wedged spool (`docker logs backend`) is diagnosable.
    """
    rejected_reasons = sorted(
        {event.reason.value for event in ack.rejected if event.reason is not None}
    )
    retryable_reasons = sorted(
        {event.reason.value for event in ack.retryable if event.reason is not None}
    )
    logger.warning(
        "telemetry batch %s ack: accepted=%d duplicate=%d rejected=%d retryable=%d "
        "rejected_reasons=%s retryable_reasons=%s",
        ack.batch_id,
        len(ack.accepted),
        len(ack.duplicate),
        len(ack.rejected),
        len(ack.retryable),
        rejected_reasons or "-",
        retryable_reasons or "-",
    )


def _verify_session_capability(
    capability: SessionCapability,
    *,
    now: datetime,
    current_revocation_epoch: int,
    expected_enrollment_id: Optional[uuid.UUID] = None,
    expected_research_session_id: Optional[uuid.UUID] = None,
    expected_revision_id: Optional[uuid.UUID] = None,
) -> CapabilityVerification:
    """Verify the batch capability against the subject resolved from its events."""
    return verify_capability(
        capability,
        BOOTSTRAP_SIGNING_SECRET or "",
        expected_audience=AUDIENCE,
        expected_scope=[SCOPE_WRITE],
        now=now,
        current_revocation_epoch=current_revocation_epoch,
        expected_enrollment_id=expected_enrollment_id,
        expected_research_session_id=expected_research_session_id,
        expected_revision_id=expected_revision_id,
    )


def _ingestion_kill_switch_check(db, payload: TelemetryBatchRequestV1):
    """DB-backed kill-switch predicate scoped to the batch's active subject.

    The batch is anchored by its first in-scope event (matching the ingestion
    service's anchor rule), so the check blocks exactly the study/revision/
    enrollment the accepted events would be attributed to.
    """
    for event in payload.events:
        if (
            event.enrollment_id is None
            or event.research_session_id is None
            or event.revision_id is None
        ):
            continue
        session_row = session_store.get_session(db, event.research_session_id)
        if session_row is None:
            continue
        enrollment_row = identity_store.get_enrollment(db, event.enrollment_id)
        if enrollment_row is None:
            continue
        revision_row = protocol_store.get_revision(db, session_row.study_revision_id)
        study_id = revision_row.study_id if revision_row is not None else None
        return operations_store.db_kill_switch_check(
            db,
            study_id=study_id,
            revision_id=session_row.study_revision_id,
            enrollment_id=event.enrollment_id,
        )

    def _never_engaged() -> bool:
        return False

    return _never_engaged


def _enrollment_resolver(db):
    def resolve(enrollment_id: uuid.UUID):
        # Lock the enrollment row so concurrent batches for the same subject
        # serialize inside the ingestion transaction.
        row = identity_store.get_enrollment(db, enrollment_id, for_update=True)
        return identity_store.row_to_enrollment(row) if row is not None else None

    return resolve


def _session_resolver(db):
    def resolve(research_session_id: uuid.UUID):
        row = session_store.get_session(db, research_session_id, for_update=True)
        return session_store.row_to_session(row) if row is not None else None

    return resolve


@router.post("/batches", summary="Submit a canonical telemetry batch")
def submit_telemetry_batch(
    payload: TelemetryBatchRequestV1,
    app: App = Depends(App.get_instance),
):
    """Ingest a batch authenticated by its scoped session capability."""
    db = app.get_db_session()
    try:
        store = SqlAlchemyIngestionStore(db)
        ack = ingest_batch(
            payload,
            capability_verifier=_verify_session_capability,
            enrollment_resolver=_enrollment_resolver(db),
            session_resolver=_session_resolver(db),
            store=store,
            now=_now(),
            kill_switch_check=_ingestion_kill_switch_check(db, payload),
        )
        _log_ack(ack)
        return JsonResponseWithStatus(
            status_code=200, content=ack.model_dump(mode="json")
        )
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(
            status_code=503, detail="telemetry store is unavailable"
        ) from error
    finally:
        db.close()


@router.get("/receipts/{receipt_id}", summary="Fetch an immutable batch receipt")
def get_batch_receipt(
    receipt_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Admin/researcher view of a stored batch receipt."""
    require_admin(current_user)
    db = app.get_db_session()
    try:
        receipt = get_receipt_by_id(db, receipt_id)
        if receipt is None:
            raise HTTPException(status_code=404, detail="Batch receipt not found")
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "receipt": receipt.ack.model_dump(mode="json"),
                "enrollment_id": (
                    str(receipt.enrollment_id)
                    if receipt.enrollment_id is not None
                    else None
                ),
                "research_session_id": (
                    str(receipt.research_session_id)
                    if receipt.research_session_id is not None
                    else None
                ),
            },
        )
    finally:
        db.close()
