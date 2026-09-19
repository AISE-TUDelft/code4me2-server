"""ISSUE-01 matrix: one study-policy authority for content storage.

Content storage for a research-bound context is allowed only by an ACTIVE
enrollment with accepted consent under a frozen policy that grants content
capture. Missing/malformed policy denies; the legacy account preference only
applies without a study binding.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from research.participants.enums import EnrollmentStatus
from research.telemetry.content_policy import (
    DENY_NO_ACTIVE_ENROLLMENT,
    DENY_POLICY_DENIES_CONTENT,
    DENY_POLICY_MALFORMED,
    LEGACY_PREFERENCE,
    STUDY_POLICY,
    resolve_study_content_policy,
)

ACCOUNT_ID = uuid.uuid4()
STUDY_ID = uuid.uuid4()
ENROLLMENT_ID = uuid.uuid4()


class _FakeDb:
    def __init__(self, config: dict) -> None:
        self.config = config

    def get(self, _model, _study_id):
        return SimpleNamespace(research_config_json=self.config)


def _enrollment(status: EnrollmentStatus, *, consent: bool = True):
    from datetime import datetime, timezone

    return SimpleNamespace(
        enrollment_id=ENROLLMENT_ID,
        study_id=STUDY_ID,
        status=status.value,
        consent_accepted_at=datetime.now(timezone.utc) if consent else None,
    )


def _resolve(config: dict, enrollment):
    with patch(
        "research.telemetry.content_policy.identity_store.get_participant_by_account",
        return_value=SimpleNamespace(participant_id=uuid.uuid4()),
    ), patch(
        "research.telemetry.content_policy.identity_store.list_enrollments",
        return_value=[enrollment] if enrollment is not None else [],
    ):
        return resolve_study_content_policy(
            _FakeDb(config), account_id=ACCOUNT_ID, study_id=STUDY_ID
        )


def test_metadata_only_policy_denies_content():
    decision = _resolve(
        {"telemetry_policy": {"allowed_field_classes": ["SYSTEM", "BEHAVIORAL"]}},
        _enrollment(EnrollmentStatus.ACTIVE),
    )
    assert decision.allowed is False
    assert decision.reason == DENY_POLICY_DENIES_CONTENT


def test_content_enabled_policy_with_active_consent_allows_content():
    decision = _resolve(
        {"telemetry_policy": {"content_capture": True}},
        _enrollment(EnrollmentStatus.ACTIVE),
    )
    assert decision.allowed is True
    assert decision.reason == STUDY_POLICY
    assert decision.policy_digest


def test_content_enabled_policy_without_accepted_consent_denies_content():
    decision = _resolve(
        {"telemetry_policy": {"content_capture": True}},
        _enrollment(EnrollmentStatus.ACTIVE, consent=False),
    )
    assert decision.allowed is False


def test_missing_or_malformed_policy_denies_content():
    assert _resolve({}, _enrollment(EnrollmentStatus.ACTIVE)).allowed is False
    malformed = _resolve(
        {"telemetry_policy": ["not", "a", "mapping"]},
        _enrollment(EnrollmentStatus.ACTIVE),
    )
    assert malformed.allowed is False
    assert malformed.reason == DENY_POLICY_MALFORMED


def test_terminal_enrollment_denies_content():
    decision = _resolve(
        {"telemetry_policy": {"content_capture": True}},
        _enrollment(EnrollmentStatus.REVOKED),
    )
    assert decision.allowed is False
    assert decision.reason == DENY_NO_ACTIVE_ENROLLMENT


def test_no_enrollment_denies_content():
    decision = _resolve(
        {"telemetry_policy": {"content_capture": True}}, None
    )
    assert decision.allowed is False
    assert decision.reason == DENY_NO_ACTIVE_ENROLLMENT


def test_without_a_study_the_legacy_preference_applies():
    """Non-research usage keeps its documented account preference."""
    from backend.routers.agent.consent import resolve_store_agent_content_for_user

    user = SimpleNamespace(preference='{"store_agent_content": true}')
    with patch(
        "backend.routers.agent.consent.crud.get_user_by_id", return_value=user
    ):
        assert resolve_store_agent_content_for_user(object(), ACCOUNT_ID) is True

    with patch(
        "backend.routers.agent.consent.crud.get_user_by_id", return_value=user
    ):
        assert (
            resolve_store_agent_content_for_user(
                object(), ACCOUNT_ID, study_id=STUDY_ID
            )
            is False
        ), "a study binding must never fall back to the legacy preference"
