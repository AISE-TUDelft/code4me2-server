"""Phase-04 approval-option verification (D09) and publication enforcement."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.routers.research import studies as studies_router
from research.study.agents import registry as approvals


def _release_json(*cases, status="PASS"):
    return {
        "conformance": [
            {
                "status": status,
                "case_results": [
                    {"case_id": case_id, "status": case_status}
                    for case_id, case_status in cases
                ],
            }
        ]
    }


def test_auto_is_always_available_and_gated_options_need_evidence():
    assert approvals.verified_approval_options(None) == ["auto"]
    assert approvals.verified_approval_options({}) == ["auto"]
    # A passing smoke-only receipt never verifies a permission/edit option.
    assert approvals.verified_approval_options(_release_json(("e2e.smoke", "PASS"))) == [
        "auto"
    ]


def test_passing_permission_case_verifies_per_step_only():
    options = approvals.verified_approval_options(
        _release_json(("acp.permission.request", "PASS"))
    )
    assert "per_step" in options
    assert "suggestion_only" not in options


def test_passing_edit_case_verifies_suggestion_only_only():
    options = approvals.verified_approval_options(
        _release_json(("acp.edit.proposal", "PASS"), ("acp.diff", "PASS"))
    )
    assert "suggestion_only" in options
    assert "per_step" not in options


def test_non_passing_cases_never_verify_an_option():
    assert approvals.verified_approval_options(
        _release_json(("acp.permission.request", "FAIL"))
    ) == ["auto"]
    assert approvals.verified_approval_options(
        _release_json(("acp.permission.request", "PASS"), status="FAIL")
    ) == ["auto"]


def test_approval_option_verified_matches_the_option_list():
    release_json = _release_json(("acp.permission.request", "PASS"))
    assert approvals.approval_option_verified(release_json, "auto") is True
    assert approvals.approval_option_verified(release_json, "per_step") is True
    assert approvals.approval_option_verified(release_json, "suggestion_only") is False


def _protocol_with_one_condition(distribution_id):
    from research.study.protocol.models import StudyProtocolV1

    return StudyProtocolV1.model_validate(
        {
            "schema_version": "1",
            "study_id": str(uuid.uuid4()),
            "metadata": {"name": "Approval study"},
            "schedule": {
                "kind": "FIXED",
                "start_at": "2026-09-01T00:00:00+00:00",
                "end_at": "2027-09-01T00:00:00+00:00",
            },
            "assignment": {"unit": "ENROLLMENT", "strategy": "WEIGHTED_RANDOM"},
            "conditions": [
                {
                    "condition_id": "control",
                    "weight": 1.0,
                    "distribution_id": str(distribution_id),
                }
            ],
            "privacy_policy": {"retention_action": "RETAIN_ANONYMIZED"},
            "consent": {
                "document_id": "d",
                "version": "1",
                "digest": "sha256:" + "c" * 64,
            },
            "environment_requirements": {"expected_protocol_version": "1"},
        }
    )


def _owner():
    return SimpleNamespace(
        user_id=uuid.uuid4(), is_admin=False, email="r@example.com", name="R", can_research=True
    )


def _call_condition_auth(profile, release_row):
    owner = _owner()
    # The condition's profile must be owned by the caller.
    profile.owner_user_id = owner.user_id
    distribution_id = profile.profile_id
    protocol = _protocol_with_one_condition(distribution_id)
    db = MagicMock()
    connection = SimpleNamespace(connection_id=uuid.uuid4(), label="c", is_active=True)
    with patch.object(
        studies_router.crud, "get_agent_profile_by_id", return_value=profile
    ), patch.object(
        studies_router.crud, "get_provider_connection", return_value=connection
    ), patch.object(
        studies_router.crud, "user_has_connection_grant", return_value=True
    ), patch.object(
        studies_router.registry_store, "get_release", return_value=release_row
    ):
        studies_router._condition_authorization_errors(db, protocol, owner)


def _profile(approval_policy, release_id="rel-1"):
    return SimpleNamespace(
        profile_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        release_id=release_id,
        approval_policy=approval_policy,
    )


def test_publication_rejects_an_unverified_approval_policy():
    release_row = SimpleNamespace(release_json=_release_json(("e2e.smoke", "PASS")))
    with pytest.raises(HTTPException) as error:
        _call_condition_auth(_profile("per_step"), release_row)
    assert error.value.status_code == 422
    assert any(p["code"] == "APPROVAL_NOT_VERIFIED" for p in error.value.detail)


def test_publication_allows_a_verified_approval_policy():
    release_row = SimpleNamespace(
        release_json=_release_json(("acp.permission.request", "PASS"))
    )
    # per_step is verified; no approval complaint should be raised.
    _call_condition_auth(_profile("per_step"), release_row)


def test_publication_allows_auto_for_any_qualified_release():
    release_row = SimpleNamespace(release_json=_release_json(("e2e.smoke", "PASS")))
    _call_condition_auth(_profile("auto"), release_row)
