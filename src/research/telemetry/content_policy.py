"""Single server-side authority for content-storage permission (ISSUE-01).

For a **research-bound** context the permission to persist prompts, tool
arguments/results and raw payloads is derived from the account's ACTIVE
enrollment plus the study's frozen telemetry policy. The legacy account
preference (``store_agent_content``) is *not* study consent and is only
consulted for genuinely non-research contexts, where there is no study policy
to enforce.

The decision is fail-closed:

* no participant mapping / no active enrollment / no study row -> deny;
* missing or malformed telemetry policy -> deny;
* policy without ``content_capture`` rights, or inactive consent -> deny;
* only an ACTIVE enrollment with accepted consent under a policy that grants
  content capture allows content.

Every persistence boundary (managed-agent ingest, proxy/plugin task creation,
legacy relay adapters and the ACP policy endpoints) calls this module so there
is exactly one place where the study decision is made.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from database.db_schemas import Study as StudyRow
from research.participants import identity as identity_store
from research.participants.enums import EnrollmentStatus
from research.telemetry.privacy import PrivacyPolicy

__all__ = [
    "ContentPolicyDecision",
    "DENY_LEGACY_PREFERENCE",
    "DENY_NO_ACTIVE_ENROLLMENT",
    "DENY_NO_PARTICIPANT",
    "DENY_POLICY_DENIES_CONTENT",
    "DENY_POLICY_MALFORMED",
    "DENY_STUDY_UNAVAILABLE",
    "LEGACY_PREFERENCE",
    "STUDY_POLICY",
    "resolve_study_content_policy",
]

STUDY_POLICY = "STUDY_POLICY"
LEGACY_PREFERENCE = "LEGACY_PREFERENCE"

DENY_NO_PARTICIPANT = "NO_PARTICIPANT"
DENY_NO_ACTIVE_ENROLLMENT = "NO_ACTIVE_ENROLLMENT"
DENY_STUDY_UNAVAILABLE = "STUDY_UNAVAILABLE"
DENY_POLICY_MALFORMED = "POLICY_MALFORMED"
DENY_POLICY_DENIES_CONTENT = "POLICY_DENIES_CONTENT"
DENY_LEGACY_PREFERENCE = "LEGACY_PREFERENCE_DENIED"


@dataclass(frozen=True)
class ContentPolicyDecision:
    """One resolved content-storage permission and its audit reason."""

    allowed: bool
    reason: str
    policy: Optional[PrivacyPolicy] = None
    enrollment_id: Optional[uuid.UUID] = None
    study_id: Optional[uuid.UUID] = None
    policy_digest: Optional[str] = None


def _deny(
    reason: str,
    *,
    policy: Optional[PrivacyPolicy] = None,
    enrollment_id: Optional[uuid.UUID] = None,
    study_id: Optional[uuid.UUID] = None,
) -> ContentPolicyDecision:
    return ContentPolicyDecision(
        allowed=False,
        reason=reason,
        policy=policy,
        enrollment_id=enrollment_id,
        study_id=study_id,
        policy_digest=policy.policy_digest if policy is not None else None,
    )


def _active_enrollment(
    db: Any,
    *,
    account_id: uuid.UUID,
    study_id: uuid.UUID,
    enrollment_id: Optional[uuid.UUID] = None,
    for_update: bool = False,
):
    participant = identity_store.get_participant_by_account(db, account_id)
    if participant is None:
        return None
    candidates = [
        row
        for row in identity_store.list_enrollments(db, participant.participant_id)
        if row.status == EnrollmentStatus.ACTIVE.value
        and getattr(row, "study_id", None) == study_id
    ]
    if enrollment_id is not None:
        candidates = [
            row for row in candidates if row.enrollment_id == enrollment_id
        ]
    if len(candidates) != 1:
        # Zero or ambiguous (>1) active enrollments: never guess a consent
        # subject; the caller gets a fail-closed decision.
        return None
    row = candidates[0]
    if for_update:
        return identity_store.get_enrollment(db, row.enrollment_id, for_update=True)
    return row


def resolve_study_content_policy(
    db: Any,
    *,
    account_id: Optional[uuid.UUID],
    study_id: uuid.UUID,
    enrollment_id: Optional[uuid.UUID] = None,
) -> ContentPolicyDecision:
    """Resolve content-storage permission from enrollment + frozen study policy.

    ``account_id`` is the authenticated account, ``study_id`` the frozen study
    binding of the task/run, and the optional ``enrollment_id`` pins one exact
    enrollment (a mismatch denies). Returns a decision; never raises for a
    missing/broken subject or policy.
    """
    if account_id is None:
        return _deny(DENY_NO_PARTICIPANT, study_id=study_id)

    try:
        enrollment = _active_enrollment(
            db,
            account_id=account_id,
            study_id=study_id,
            enrollment_id=enrollment_id,
        )
    except Exception as error:  # noqa: BLE001 - fail closed on store problems
        logging.warning(
            "[ContentPolicy] enrollment lookup failed for study %s: %s",
            study_id,
            error,
        )
        return _deny(DENY_NO_ACTIVE_ENROLLMENT, study_id=study_id)

    if enrollment is None:
        return _deny(DENY_NO_ACTIVE_ENROLLMENT, study_id=study_id)

    try:
        study = db.get(StudyRow, study_id)
    except Exception as error:  # noqa: BLE001 - fail closed on store problems
        logging.warning("[ContentPolicy] study lookup failed for %s: %s", study_id, error)
        return _deny(
            DENY_STUDY_UNAVAILABLE,
            enrollment_id=enrollment.enrollment_id,
            study_id=study_id,
        )
    if study is None:
        return _deny(
            DENY_STUDY_UNAVAILABLE,
            enrollment_id=enrollment.enrollment_id,
            study_id=study_id,
        )

    config = getattr(study, "research_config_json", None) or {}
    raw_policy = config.get("telemetry_policy")
    if not isinstance(raw_policy, Mapping):
        return _deny(
            DENY_POLICY_MALFORMED,
            enrollment_id=enrollment.enrollment_id,
            study_id=study_id,
        )

    consent_active = (
        enrollment.status == EnrollmentStatus.ACTIVE.value
        and getattr(enrollment, "consent_accepted_at", None) is not None
    )
    try:
        policy = PrivacyPolicy.from_study_policy(
            dict(raw_policy), consent_active=consent_active
        )
    except Exception as error:  # noqa: BLE001 - malformed policy denies
        logging.warning("[ContentPolicy] policy parse failed for study %s: %s", study_id, error)
        return _deny(
            DENY_POLICY_MALFORMED,
            enrollment_id=enrollment.enrollment_id,
            study_id=study_id,
        )

    allowed = bool(policy.content_allowed and policy.consent_active)
    return ContentPolicyDecision(
        allowed=allowed,
        reason=STUDY_POLICY if allowed else DENY_POLICY_DENIES_CONTENT,
        policy=policy,
        enrollment_id=enrollment.enrollment_id,
        study_id=study_id,
        policy_digest=policy.policy_digest,
    )
