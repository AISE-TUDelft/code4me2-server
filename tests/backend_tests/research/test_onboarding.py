"""Researcher -> participant onboarding: join codes, self-enrollment, RBAC.

Route handlers are exercised by calling the functions directly with a
``MagicMock`` app/session (no PostgreSQL/Redis in this environment). The
identity service and the join-code generator are pure and run for real; only
row persistence is patched.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError as PydanticValidationError

from backend.routers.analytics.auth_utils import AuthenticatedUser
from backend.routers.research import join as join_module
from backend.routers.research.join import (
    JoinRequestBody,
    redeem_join_code,
    resolve_join_code,
)
from backend.routers.research.researchers import (
    ResearcherEnableRequest,
    set_researcher_enabled,
)
from backend.routers.research.studies import (
    get_study_join_code,
    list_studies,
)
from research.participants.enums import EnrollmentStatus
from research.participants.identity import EnrollmentOpenResult
from research.participants.models import (
    Enrollment,
    Participant,
    ResearchEligibility,
)
from research.study.protocol import store as protocol_store
from research.study.protocol.enums import RevisionStatus
from research.study.protocol.join_code import (
    JOIN_CODE_ALPHABET,
    JOIN_CODE_LENGTH,
    generate_join_code,
    is_well_formed_join_code,
    normalize_join_code,
)
from research.study.protocol.models import StudyProtocolV1
from research.study.protocol.publication import StudyRevision

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
PROTOCOL_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "research"
    / "protocol"
    / "approved_study_protocol_v1.json"
)
CONSENT_DOC_ID = "consent-onboarding-v1"
CONSENT_DOC_VERSION = "1"
CONSENT_DOC_DIGEST = "sha256:" + "c" * 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def onboarding__protocol() -> StudyProtocolV1:
    return StudyProtocolV1.model_validate(json.loads(PROTOCOL_FIXTURE.read_text()))


def onboarding__admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(), is_admin=True, email="admin@example.com", name="Admin"
    )


def onboarding__researcher(account_id: uuid.UUID | None = None) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=account_id or uuid.uuid4(),
        is_admin=False,
        email="researcher@example.com",
        name="Researcher",
        can_research=True,
    )


def onboarding__revision(*, status: RevisionStatus = RevisionStatus.PUBLISHED) -> StudyRevision:
    return StudyRevision(
        revision_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        revision_number=1,
        status=status,
        protocol_json={
            "schema_version": "1",
            "metadata": {"name": "Onboarding study"},
            "consent": {
                "document_id": CONSENT_DOC_ID,
                "version": CONSENT_DOC_VERSION,
                "digest": CONSENT_DOC_DIGEST,
            },
            "privacy_policy": {"retention_action": "RETAIN_ANONYMIZED"},
        },
        protocol_digest="sha256:" + "a" * 64,
        published_at=NOW if status == RevisionStatus.PUBLISHED else None,
        created_at=NOW,
    )


def onboarding__revision_row(
    revision: StudyRevision, *, join_code: str = "ABCD2345"
) -> SimpleNamespace:
    return SimpleNamespace(
        revision_id=revision.revision_id,
        study_id=revision.study_id,
        revision_number=revision.revision_number,
        status=revision.status.value,
        join_code=join_code,
        protocol_json=revision.protocol_json,
        protocol_digest=revision.protocol_digest,
        published_at=revision.published_at,
        supersedes_revision_id=None,
        created_at=revision.created_at,
    )


def onboarding__enrollment(
    revision: StudyRevision,
    participant_id: uuid.UUID,
    status: EnrollmentStatus,
) -> Enrollment:
    return Enrollment(
        enrollment_id=uuid.uuid4(),
        participant_id=participant_id,
        study_id=revision.study_id,
        study_revision_id=revision.revision_id,
        participant_code="p_onboarding",
        status=status,
        eligibility=ResearchEligibility(eligible=True, evaluated_at=NOW),
        enrolled_at=NOW,
        updated_at=NOW,
    )


# ---------------------------------------------------------------------------
# Join-code generation
# ---------------------------------------------------------------------------


def onboarding__open_result(
    participant: Participant, enrollment: Enrollment, *, created: bool
):
    """Build the shared enrollment-service result the routers consume."""
    return EnrollmentOpenResult(
        participant=participant,
        enrollment=enrollment,
        created=created,
        reused=not created,
        issue=None,
    )


def test_generated_join_codes_are_short_well_formed_and_unique():
    codes = {generate_join_code() for _ in range(500)}

    assert len(codes) == 500
    for code in codes:
        assert len(code) == JOIN_CODE_LENGTH
        assert is_well_formed_join_code(code)
        assert set(code) <= set(JOIN_CODE_ALPHABET)
        # Opaque: never a UUID or a readable study/agent handle.
        assert str(uuid.UUID(int=0)) not in code


def test_join_code_alphabet_excludes_ambiguous_characters():
    assert not ({"I", "L", "O", "U"} & set(JOIN_CODE_ALPHABET))


def test_join_code_normalization_folds_case_separators_and_lookalikes():
    assert normalize_join_code(" abcd-2345 ") == "ABCD2345"
    # I/L -> 1, O -> 0, U -> V (Crockford look-alikes).
    assert normalize_join_code("il0o") == "1100"
    assert normalize_join_code("u") == "V"
    assert normalize_join_code("") == ""


def test_published_revision_persists_a_join_code_but_a_draft_does_not():
    session = MagicMock()
    session.execute.return_value.scalars.return_value.first.return_value = None

    protocol_store.persist_revision(session, onboarding__revision())
    published_row = session.add.call_args.args[0]
    assert published_row.status == RevisionStatus.PUBLISHED.value
    assert published_row.join_code is not None
    assert is_well_formed_join_code(published_row.join_code)

    draft_session = MagicMock()
    protocol_store.create_draft(
        draft_session,
        draft_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        name="draft",
        protocol=onboarding__protocol(),
    )
    draft_row = draft_session.add.call_args.args[0]
    assert draft_row.status == RevisionStatus.DRAFT.value
    assert draft_row.join_code is None


def test_resolve_rejects_an_empty_code_without_querying():
    session = MagicMock()
    assert protocol_store.get_revision_by_join_code(session, "   ") is None
    session.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Study index and join-code read endpoints (admin-only)
# ---------------------------------------------------------------------------


def test_study_index_lists_latest_revision_and_join_code():
    app = MagicMock()
    revision = onboarding__revision()
    row = onboarding__revision_row(revision, join_code="ZZZZ9999")
    study = protocol_store.StudyView(
        study_id=revision.study_id,
        name="Onboarding study",
        description="a study",
        owner="owner@example.com",
        created_at=NOW,
    )

    with patch(
        "backend.routers.research.studies.store.list_studies", return_value=[study]
    ), patch(
        "backend.routers.research.studies.store.get_latest_published_revision",
        return_value=row,
    ):
        response = list_studies(onboarding__admin(), app)

    body = json.loads(response.body)
    assert response.status_code == 200
    entry = body["studies"][0]
    assert entry["study_id"] == str(study.study_id)
    assert entry["name"] == "Onboarding study"
    assert entry["join_code"] == "ZZZZ9999"
    assert entry["latest_revision"]["revision_id"] == str(revision.revision_id)
    assert entry["latest_revision"]["revision_number"] == 1
    assert entry["latest_revision"]["status"] == RevisionStatus.PUBLISHED.value
    assert entry["latest_revision"]["protocol_digest"] == revision.protocol_digest


def test_study_index_requires_an_enabled_researcher():
    app = MagicMock()
    participant = AuthenticatedUser(
        user_id=uuid.uuid4(), is_admin=False, email="p@example.com", name="P"
    )
    with pytest.raises(HTTPException) as error:
        list_studies(participant, app)
    assert error.value.status_code == 403
    app.get_db_session.assert_not_called()


def test_get_study_join_code_returns_current_code_and_revision():
    app = MagicMock()
    revision = onboarding__revision()
    row = onboarding__revision_row(revision, join_code="CODE1234")

    with patch(
        "backend.routers.research.studies.store.get_study_join_code", return_value=row
    ):
        response = get_study_join_code(revision.study_id, onboarding__admin(), app)

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["join_code"] == "CODE1234"
    assert body["revision"]["revision_id"] == str(revision.revision_id)


def test_get_study_join_code_is_404_without_a_published_revision():
    app = MagicMock()
    with patch(
        "backend.routers.research.studies.store.get_study_join_code", return_value=None
    ):
        with pytest.raises(HTTPException) as error:
            get_study_join_code(uuid.uuid4(), onboarding__admin(), app)
    assert error.value.status_code == 404


def test_onboarding_routes_are_wired():
    from backend.routers import router as api_router

    paths = {route.path for route in api_router.routes}
    assert "/research/studies" in paths
    assert "/research/studies/{study_id}/join-code" in paths
    assert "/research/join" in paths
    assert "/research/join/{join_code}" in paths
    assert "/research/researcher-roles" not in paths
    assert "/research/researchers" in paths
    assert "/research/provider-connections" in paths


# ---------------------------------------------------------------------------
# Join-code resolution (any authenticated user)
# ---------------------------------------------------------------------------


def test_resolve_join_code_returns_study_revision_and_consent_without_secrets():
    app = MagicMock()
    revision = onboarding__revision()
    row = onboarding__revision_row(revision, join_code="ABCD2345")
    study = protocol_store.StudyView(
        study_id=revision.study_id,
        name="Onboarding study",
        description=None,
        owner=None,
        created_at=NOW,
    )

    with patch.object(
        join_module.protocol_store, "get_revision_by_join_code", return_value=row
    ), patch.object(
        join_module.protocol_store, "row_to_revision", return_value=revision
    ), patch.object(
        join_module.protocol_store, "get_study", return_value=study
    ):
        response = resolve_join_code("abcd-2345", onboarding__researcher(), app)

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["join_code"] == "ABCD2345"
    assert body["study"]["study_id"] == str(revision.study_id)
    assert body["revision"]["status"] == RevisionStatus.PUBLISHED.value
    assert body["consent"]["text"].strip()
    serialized = json.dumps(body)
    assert "account_id" not in serialized
    assert "participant_id" not in serialized


def test_redeem_join_code_refuses_a_non_published_revision():
    app = MagicMock()
    revision = onboarding__revision(status=RevisionStatus.RETIRED)
    row = onboarding__revision_row(revision, join_code="ABCD2345")

    with patch.object(
        join_module.protocol_store, "get_revision_by_join_code", return_value=row
    ), patch.object(
        join_module.protocol_store, "row_to_revision", return_value=revision
    ):
        with pytest.raises(HTTPException) as error:
            redeem_join_code(
                JoinRequestBody(join_code="ABCD2345"), onboarding__researcher(), app
            )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "UNKNOWN_REVISION"


def test_join_request_forbids_client_chosen_revision_and_eligibility():
    # The revision always comes from the join code, and eligibility is evaluated
    # server-side: both are rejected outright if a client tries to send them.
    with pytest.raises(PydanticValidationError):
        JoinRequestBody(join_code="ABCD2345", revision_id=uuid.uuid4())
    with pytest.raises(PydanticValidationError):
        JoinRequestBody(join_code="ABCD2345", eligibility={"eligible": True})


# ---------------------------------------------------------------------------
# Researcher RBAC
# ---------------------------------------------------------------------------


def test_researcher_enablement_requires_admin():
    app = MagicMock()
    payload = ResearcherEnableRequest(can_research=True)
    with pytest.raises(HTTPException) as error:
        set_researcher_enabled(uuid.uuid4(), payload, onboarding__researcher(), app)
    assert error.value.status_code == 403
    app.get_db_session.assert_not_called()


def test_enable_researcher_round_trip_and_participant_cannot_self_promote():
    app = MagicMock()
    target = uuid.uuid4()
    user = SimpleNamespace(
        user_id=target,
        email="r@example.com",
        name="R",
        is_admin=False,
        can_research=True,
    )
    with patch(
        "backend.routers.research.researchers.crud.set_user_can_research",
        return_value=user,
    ) as toggle:
        response = set_researcher_enabled(
            target, ResearcherEnableRequest(can_research=True), onboarding__admin(), app
        )
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["user"]["can_research"] is True
    toggle.assert_called_once()

    # A participant can never flip their own flag (admin-only route).
    participant = AuthenticatedUser(
        user_id=target, is_admin=False, email="p@example.com", name="P"
    )
    with pytest.raises(HTTPException) as error:
        set_researcher_enabled(
            target, ResearcherEnableRequest(can_research=True), participant, app
        )
    assert error.value.status_code == 403


def test_study_ownership_authorization():
    from backend.routers.research.access import is_study_owner, require_study_owner

    owner = onboarding__researcher()
    study = protocol_store.StudyView(
        study_id=uuid.uuid4(),
        name="s",
        description=None,
        owner=None,
        created_by=owner.user_id,
    )
    assert is_study_owner(owner, study) is True
    require_study_owner(owner, study)

    other = onboarding__researcher()
    with pytest.raises(HTTPException) as error:
        require_study_owner(other, study)
    assert error.value.status_code == 403

    # An administrator bypasses ownership.
    require_study_owner(onboarding__admin(), study)


def test_participant_cannot_act_as_researcher():
    from backend.routers.research.access import require_researcher

    participant = AuthenticatedUser(
        user_id=uuid.uuid4(), is_admin=False, email="p@example.com", name="P"
    )
    with pytest.raises(HTTPException) as error:
        require_researcher(participant)
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "RESEARCHER_REQUIRED"


def test_read_model_authorization_is_owner_scoped():
    from backend.routers.research.read_models import _authorize

    study_id = uuid.uuid4()
    owner = onboarding__researcher()
    db = MagicMock()
    study = protocol_store.StudyView(
        study_id=study_id,
        name="s",
        description=None,
        owner=None,
        created_by=owner.user_id,
    )
    with patch(
        "backend.routers.research.read_models.protocol_store.get_study",
        return_value=study,
    ):
        _authorize(db, owner, study_id)
        with pytest.raises(HTTPException) as error:
            _authorize(db, onboarding__researcher(), study_id)
    assert error.value.status_code == 403

