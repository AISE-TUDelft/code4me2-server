"""Phase-03 unit/contract tests: enrollment, funding gate and terminal policy.

Real-PostgreSQL concurrency lives in
``tests/database_tests/test_phase03_concurrency.py``. These exercise the service
seams with fakes/MagicMock.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from research.participants import identity as identity_store
from research.participants.enums import EnrollmentStatus, IdentityReasonCode
from research.participants.models import Participant, ResearchEligibility
from backend.routers.research import access as funding

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _participant(account_id=None) -> Participant:
    return Participant(
        participant_id=uuid.uuid4(),
        account_id=account_id or uuid.uuid4(),
        created_at=NOW,
    )


def _revision_ref(study_id=None):
    from research.participants.models import RevisionRef
    from research.study.protocol.enums import RetentionAction

    return RevisionRef(
        revision_id=uuid.uuid4(),
        study_id=study_id or uuid.uuid4(),
        retention_action=RetentionAction.RETAIN_ANONYMIZED,
    )


def _enrollment_row(*, study_id, revision_id, status, participant_id):
    return SimpleNamespace(
        enrollment_id=uuid.uuid4(),
        participant_id=participant_id,
        study_id=study_id,
        study_revision_id=revision_id,
        status=status.value,
        revocation_epoch=0,
    )


# ---------------------------------------------------------------------------
# B1 — one active enrollment account-wide and no self-service rejoin
# ---------------------------------------------------------------------------


def test_open_enrollment_rejects_a_second_live_enrollment():
    account_id = uuid.uuid4()
    participant = _participant(account_id)
    revision = _revision_ref()
    other = _enrollment_row(
        study_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        status=EnrollmentStatus.ACTIVE,
        participant_id=participant.participant_id,
    )
    db = MagicMock()

    with patch.object(
        identity_store, "get_or_create_participant_row", return_value=SimpleNamespace()
    ), patch.object(
        identity_store, "row_to_participant", return_value=participant
    ), patch.object(
        identity_store, "get_enrollment_for_participant_study", return_value=None
    ), patch.object(
        identity_store, "get_enrollment_for_participant_revision", return_value=None
    ), patch.object(
        identity_store, "get_active_enrollment_for_participant", return_value=other
    ), patch.object(
        identity_store, "row_to_enrollment", return_value=SimpleNamespace(
            enrollment_id=other.enrollment_id,
            study_id=other.study_id,
            study_revision_id=other.study_revision_id,
            status=EnrollmentStatus.ACTIVE,
        )
    ), patch.object(identity_store, "create_enrollment") as create:
        result = identity_store.open_enrollment(db, account_id, revision, now=NOW)

    assert result.issue is not None
    assert result.issue.code == IdentityReasonCode.ALREADY_ENROLLED
    create.assert_not_called()


def test_open_enrollment_refuses_rejoin_to_a_withdrawn_study():
    account_id = uuid.uuid4()
    participant = _participant(account_id)
    study_id = uuid.uuid4()
    revision = _revision_ref(study_id)
    prior = _enrollment_row(
        study_id=study_id,
        revision_id=revision.revision_id,
        status=EnrollmentStatus.COMPLETED,
        participant_id=participant.participant_id,
    )
    db = MagicMock()

    with patch.object(
        identity_store, "get_or_create_participant_row", return_value=SimpleNamespace()
    ), patch.object(
        identity_store, "row_to_participant", return_value=participant
    ), patch.object(
        identity_store, "get_enrollment_for_participant_study", return_value=prior
    ), patch.object(identity_store, "create_enrollment") as create:
        result = identity_store.open_enrollment(db, account_id, revision, now=NOW)

    assert result.issue is not None
    assert result.issue.code == IdentityReasonCode.REJOIN_NOT_ALLOWED
    create.assert_not_called()


def test_open_enrollment_creates_when_no_live_enrollment_exists():
    account_id = uuid.uuid4()
    participant = _participant(account_id)
    revision = _revision_ref()
    db = MagicMock()

    with patch.object(
        identity_store, "get_or_create_participant_row", return_value=SimpleNamespace()
    ), patch.object(
        identity_store, "row_to_participant", return_value=participant
    ), patch.object(
        identity_store, "get_enrollment_for_participant_study", return_value=None
    ), patch.object(
        identity_store, "get_enrollment_for_participant_revision", return_value=None
    ), patch.object(
        identity_store, "get_active_enrollment_for_participant", return_value=None
    ), patch.object(identity_store, "create_enrollment") as create:
        result = identity_store.open_enrollment(db, account_id, revision, now=NOW)

    assert result.issue is None
    assert result.created is True
    assert result.enrollment is not None
    assert result.enrollment.status == EnrollmentStatus.ACTIVE
    create.assert_called_once()


# ---------------------------------------------------------------------------
# B5 — shared funded-access gate
# ---------------------------------------------------------------------------


def _active_enrollment_row(study_id, participant_id):
    return SimpleNamespace(
        enrollment_id=uuid.uuid4(),
        participant_id=participant_id,
        study_id=study_id,
        study_revision_id=uuid.uuid4(),
        status=EnrollmentStatus.ACTIVE.value,
        revocation_epoch=0,
    )


def test_funding_gate_refuses_without_a_participant_mapping():
    db = MagicMock()
    with patch.object(
        identity_store, "get_participant_by_account", return_value=None
    ):
        with pytest.raises(funding.FundedAccessRefused) as error:
            funding.require_live_enrollment(db, account_id=uuid.uuid4(), now=NOW)
    assert error.value.code == "NOT_A_PARTICIPANT"


def test_funding_gate_refuses_without_an_active_enrollment():
    db = MagicMock()
    withdrawn = SimpleNamespace(status=EnrollmentStatus.COMPLETED.value)
    with patch.object(
        identity_store,
        "get_participant_by_account",
        return_value=SimpleNamespace(participant_id=uuid.uuid4()),
    ), patch.object(identity_store, "list_enrollments", return_value=[withdrawn]):
        with pytest.raises(funding.FundedAccessRefused) as error:
            funding.require_live_enrollment(db, account_id=uuid.uuid4(), now=NOW)
    assert error.value.code == "ENROLLMENT_NOT_ACTIVE"


def test_funding_gate_refuses_a_closed_study_and_sweeps_enrollments():
    study_id = uuid.uuid4()
    participant_id = uuid.uuid4()
    active = _active_enrollment_row(study_id, participant_id)
    ended = SimpleNamespace(
        is_research=True,
        is_active=True,
        starts_at=NOW - timedelta(days=10),
        ends_at=NOW - timedelta(days=1),
    )
    db = MagicMock()
    db.get.return_value = ended

    with patch.object(
        identity_store,
        "get_participant_by_account",
        return_value=SimpleNamespace(participant_id=participant_id),
    ), patch.object(identity_store, "list_enrollments", return_value=[active]), patch.object(
        identity_store, "complete_enrollments_for_study"
    ) as complete:
        with pytest.raises(funding.FundedAccessRefused) as error:
            funding.require_live_enrollment(
                db, account_id=uuid.uuid4(), study_id=study_id, now=NOW
            )

    assert error.value.code == "STUDY_NOT_OPEN"
    # The lazy terminal sweep marked the ended study's enrollments complete.
    complete.assert_called_once()


def test_funding_gate_refuses_when_the_kill_switch_is_engaged():
    study_id = uuid.uuid4()
    participant_id = uuid.uuid4()
    active = _active_enrollment_row(study_id, participant_id)
    open_study = SimpleNamespace(
        is_research=True,
        is_active=True,
        starts_at=NOW - timedelta(days=1),
        ends_at=NOW + timedelta(days=1),
    )
    db = MagicMock()
    db.get.return_value = open_study

    with patch.object(
        identity_store,
        "get_participant_by_account",
        return_value=SimpleNamespace(participant_id=participant_id),
    ), patch.object(identity_store, "list_enrollments", return_value=[active]):
        with pytest.raises(funding.FundedAccessRefused) as error:
            funding.require_live_enrollment(
                db,
                account_id=uuid.uuid4(),
                study_id=study_id,
                now=NOW,
                kill_switch_check=lambda: True,
            )
    assert error.value.code == "KILL_SWITCH_ENGAGED"


def test_funding_gate_allows_an_open_study_with_live_enrollment():
    study_id = uuid.uuid4()
    participant_id = uuid.uuid4()
    active = _active_enrollment_row(study_id, participant_id)
    open_study = SimpleNamespace(
        is_research=True,
        is_active=True,
        starts_at=NOW - timedelta(days=1),
        ends_at=NOW + timedelta(days=1),
    )
    db = MagicMock()
    db.get.return_value = open_study

    with patch.object(
        identity_store,
        "get_participant_by_account",
        return_value=SimpleNamespace(participant_id=participant_id),
    ), patch.object(identity_store, "list_enrollments", return_value=[active]):
        resolved = funding.require_live_enrollment(
            db, account_id=uuid.uuid4(), study_id=study_id, now=NOW
        )
    assert resolved.enrollment_id == active.enrollment_id


def test_funding_gate_rejects_a_study_mismatch():
    study_id = uuid.uuid4()
    participant_id = uuid.uuid4()
    active = _active_enrollment_row(uuid.uuid4(), participant_id)
    db = MagicMock()

    with patch.object(
        identity_store,
        "get_participant_by_account",
        return_value=SimpleNamespace(participant_id=participant_id),
    ), patch.object(identity_store, "list_enrollments", return_value=[active]):
        with pytest.raises(funding.FundedAccessRefused) as error:
            funding.require_live_enrollment(
                db, account_id=uuid.uuid4(), study_id=study_id, now=NOW
            )
    assert error.value.code == "ENROLLMENT_STUDY_MISMATCH"


# ---------------------------------------------------------------------------
# B6 — terminal transition
# ---------------------------------------------------------------------------


def test_complete_enrollments_for_study_completes_and_bumps_epoch():
    db = MagicMock()
    participant_id = uuid.uuid4()
    row = SimpleNamespace(
        enrollment_id=uuid.uuid4(),
        participant_id=participant_id,
        study_id=uuid.uuid4(),
        status=EnrollmentStatus.ACTIVE.value,
        revocation_epoch=3,
        updated_at=None,
    )
    executor = MagicMock()
    executor.scalars.return_value.all.return_value = [row]
    db.execute.return_value = executor

    with patch.object(identity_store, "revoke_active_sessions", return_value=2) as revoke:
        completed = identity_store.complete_enrollments_for_study(
            db, row.study_id, now=NOW
        )

    assert completed == 1
    assert row.status == EnrollmentStatus.COMPLETED.value
    assert row.revocation_epoch == 4
    revoke.assert_called_once()
    db.commit.assert_called_once()


def test_complete_enrollments_for_study_is_idempotent_for_terminal_rows():
    db = MagicMock()
    executor = MagicMock()
    executor.scalars.return_value.all.return_value = []
    db.execute.return_value = executor

    completed = identity_store.complete_enrollments_for_study(
        db, uuid.uuid4(), now=NOW
    )
    assert completed == 0


def test_end_study_route_completes_enrollments_and_deactivates():
    import json

    from backend.routers.research import studies as studies_router
    from research.study.protocol.store import StudyView

    app = MagicMock()
    db = MagicMock()
    app.get_db_session.return_value = db
    study_id = uuid.uuid4()
    owner = uuid.uuid4()
    study = SimpleNamespace(
        study_id=study_id,
        name="Ended",
        description=None,
        owner=None,
        created_by=owner,
        is_research=True,
        is_active=True,
    )
    updated = StudyView(
        study_id=study_id,
        name="Ended",
        description=None,
        owner=None,
        created_by=owner,
        is_research=True,
        is_active=False,
    )
    admin = SimpleNamespace(
        user_id=uuid.uuid4(), is_admin=True, email="admin@example.com", name="Admin"
    )

    with patch.object(
        studies_router, "_authorize_study", return_value=study
    ), patch.object(
        studies_router.identity_store,
        "complete_enrollments_for_study",
        return_value=2,
    ) as complete, patch.object(
        studies_router.store, "set_study_active"
    ) as deactivate, patch.object(
        studies_router.store, "persist_audit"
    ) as audit, patch.object(
        studies_router.store, "get_study", return_value=updated
    ):
        response = studies_router.end_study(
            study_id, studies_router.EndStudyRequest(actor=None), admin, app
        )

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["completed_enrollments"] == 2
    assert body["study"]["is_active"] is False
    complete.assert_called_once()
    deactivate.assert_called_once_with(db, study_id, False)
    audit.assert_called_once()

