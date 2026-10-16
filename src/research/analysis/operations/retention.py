"""Deletion-retention drill verification (Issue 13).

Given an enrollment, its pre-deletion records, the observed post-deletion
records, and (optionally) the exported record ids, verify that the declared
retention action was actually executed and that exports exclude deleted data.

A partial failure does **not** silently close out: the verification status is
``PARTIAL``/``FAILED``, ``close_out_allowed`` is ``False``, and ledger evidence
(the expected deletion ledger plus an evidence digest) is retained.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, Sequence
from uuid import UUID

from research.canonical import canonical_hash
from research.participants import identity as participants_identity
from research.telemetry.enums import CoverageState

from .enums import OperationsReasonCode, RetentionVerificationStatus
from .models import DeleteVerification, OperationsIssue, RetentionVerification

if TYPE_CHECKING:
    from research.participants.models import Enrollment, PseudonymousRecord
    from research.study.protocol.enums import RetentionAction

__all__ = ["verify_deletion"]

_LINKABLE = ("account_id", "email", "session_token")


def _issue(
    code: OperationsReasonCode, message: str, field: str = ""
) -> OperationsIssue:
    return OperationsIssue(code=code, message=message, field=field)


def _has_linkable(record: PseudonymousRecord) -> bool:
    return any(getattr(record, name) is not None for name in _LINKABLE)


def verify_deletion(
    enrollment: Enrollment,
    *,
    action: RetentionAction,
    before_records: Sequence[PseudonymousRecord],
    after_records: Optional[Sequence[PseudonymousRecord]] = None,
    export_record_ids: Optional[Sequence[str]] = None,
    verified_at: Optional[datetime] = None,
    verification_id: Optional[UUID] = None,
) -> RetentionVerification:
    """Verify a declared retention action was executed for one enrollment."""
    timestamp = verified_at or datetime.now(timezone.utc)
    expected = participants_identity.apply_retention(
        enrollment, list(before_records), action, now=timestamp
    )
    observed = (
        list(after_records) if after_records is not None else list(expected.records)
    )

    expected_by_id = {record.record_id: record for record in expected.records}
    observed_by_id = {record.record_id: record for record in observed}

    before_ids = {record.record_id for record in before_records}
    expected_ids = set(expected_by_id)
    deleted_ids = before_ids - expected_ids
    observed_ids = set(observed_by_id)

    checks: list[DeleteVerification] = []
    missing_present = sorted(expected_ids - observed_ids)
    extra_present = sorted(observed_ids - expected_ids)
    linkable_left = sorted(
        record_id
        for record_id, record in observed_by_id.items()
        if _has_linkable(record)
    )

    for record_id in sorted(before_ids | observed_ids):
        should_exist = record_id in expected_ids
        does_exist = record_id in observed_by_id
        checks.append(
            DeleteVerification(
                record_id=record_id,
                expected_state="present" if should_exist else "deleted",
                observed_state="present" if does_exist else "deleted",
                verified=should_exist == does_exist,
            )
        )

    reasons: list[OperationsIssue] = []
    if missing_present:
        status = RetentionVerificationStatus.FAILED
        reasons.append(
            _issue(
                OperationsReasonCode.RETENTION_FAILED,
                f"{len(missing_present)} retained record(s) were missing after deletion",
                "after_records",
            )
        )
    elif extra_present or linkable_left:
        status = RetentionVerificationStatus.PARTIAL
        if extra_present:
            reasons.append(
                _issue(
                    OperationsReasonCode.RETENTION_PARTIAL,
                    f"{len(extra_present)} record(s) were not deleted",
                    "after_records",
                )
            )
        if linkable_left:
            reasons.append(
                _issue(
                    OperationsReasonCode.RETENTION_PARTIAL,
                    f"{len(linkable_left)} record(s) still expose account-linkable fields",
                    "after_records",
                )
            )
    else:
        status = RetentionVerificationStatus.VERIFIED
        reasons.append(
            _issue(
                OperationsReasonCode.RETENTION_VERIFIED,
                "declared retention action executed for every record",
            )
        )

    export_exclusion_verified: Optional[bool] = None
    if export_record_ids is not None:
        exported = set(export_record_ids)
        export_exclusion_verified = not (deleted_ids & exported)
        if not export_exclusion_verified:
            status = RetentionVerificationStatus.PARTIAL
            reasons.append(
                _issue(
                    OperationsReasonCode.EXPORT_EXCLUSION_UNVERIFIED,
                    "an export still references deleted record(s)",
                    "export_record_ids",
                )
            )

    coverage = (
        CoverageState.AVAILABLE
        if status == RetentionVerificationStatus.VERIFIED
        else CoverageState.PARTIAL
    )
    close_out_allowed = (
        status == RetentionVerificationStatus.VERIFIED
        and export_exclusion_verified is not False
    )

    evidence_digest = canonical_hash(
        {
            "action": action.value,
            "enrollment_id": str(enrollment.enrollment_id),
            "expected_ledger": expected.ledger.model_dump(mode="json"),
            "observed_ids": sorted(observed_ids),
            "deleted_ids": sorted(deleted_ids),
            "status": status.value,
        }
    )

    return RetentionVerification(
        verification_id=verification_id or uuid.uuid4(),
        enrollment_id=enrollment.enrollment_id,
        action=action,
        status=status,
        evidence_digest=evidence_digest,
        verified_at=timestamp,
        deleted_count=len(deleted_ids),
        remaining_count=len(observed_ids),
        expected_remaining_count=len(expected_ids),
        export_exclusion_verified=export_exclusion_verified,
        coverage=coverage,
        close_out_allowed=close_out_allowed,
        reasons=reasons,
        record_checks=checks,
    )
