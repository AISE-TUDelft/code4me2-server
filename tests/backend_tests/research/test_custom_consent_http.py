"""HTTP contracts for custom consent forms: create, review, accept and read back."""

from __future__ import annotations

from sqlalchemy import text

from research.study.consent import view_digest

from .test_research_api_contract import (  # noqa: F401 - the fixture is used by name
    VALID_SESSION_POLICY,
    _owner,
    _participant,
    _profile,
    _seed_user,
    http_runtime,
)

FORM = {
    "document": "Purpose\n\nWe compare two coding agents.\nQuestions: https://example.org/contact",
    "statements": [
        {"id": "participate", "text": "I agree to take part in this study.", "required": True},
        {"id": "future-use", "text": "My anonymised data may be reused.", "required": False},
    ],
}


def _create(client, profile_id, **extra):
    return client.post(
        "/api/research/studies",
        json={
            "name": "Consent study",
            "default_budget_usd": "10",
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [str(profile_id)],
            **extra,
        },
    )


def _enrollment_consent(session_factory, enrollment_id):
    session = session_factory()
    try:
        return session.execute(
            text(
                "SELECT consent_digest, consent_snapshot_json FROM public.research_enrollment "
                "WHERE enrollment_id = :id"
            ),
            {"id": enrollment_id},
        ).one()
    finally:
        session.close()


def test_custom_consent_is_frozen_reviewed_and_recorded(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "consent-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "consent-participant@example.com")
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    invalid = _create(client, profile_id, consent={"document": "", "statements": FORM["statements"]})
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "CONSENT_INVALID"
    assert invalid.json()["detail"]["field"] == "document"

    created = _create(client, profile_id, consent=FORM)
    assert created.status_code == 201, created.text
    study = created.json()["study"]
    owner_view = study["consent"]
    assert owner_view["custom"] is True
    assert owner_view["document"] == FORM["document"]
    assert [item["id"] for item in owner_view["statements"]] == ["participate", "future-use"]
    assert "This study collects metadata" in owner_view["notice"]

    current_user["value"] = _participant(participant_id)
    review = client.get(f"/api/research/join/{study['join_code']}")
    assert review.status_code == 200, review.text
    consent = review.json()["consent"]
    assert consent["custom"] is True
    assert consent["digest"] == owner_view["digest"]
    assert consent["text"] == consent["notice"]
    digest = consent["digest"]
    assert digest == view_digest(
        {key: consent[key] for key in ("document", "notice", "statements")} | {"version": 1}
    )

    def join(**body):
        return client.post(
            "/api/research/join",
            json={"join_code": study["join_code"], "accept_consent": True, **body},
        )

    def code(response, status):
        assert response.status_code == status, response.text
        return response.json()["detail"]["code"]

    assert code(join(), 409) == "CONSENT_CHANGED"
    assert code(join(consent_digest="0" * 64, consent_statements={"participate": True}), 409) == (
        "CONSENT_CHANGED"
    )
    assert code(join(consent_digest=digest, consent_statements={"nope": True}), 422) == (
        "CONSENT_STATEMENTS_INVALID"
    )
    missing = join(consent_digest=digest, consent_statements={"future-use": True})
    assert code(missing, 409) == "CONSENT_STATEMENTS_REQUIRED"
    assert missing.json()["detail"]["missing"] == ["participate"]

    joined = join(consent_digest=digest, consent_statements={"participate": True})
    assert joined.status_code == 201, joined.text
    enrollment_id = joined.json()["enrollment_id"]
    stored_digest, snapshot = _enrollment_consent(session_factory, enrollment_id)
    assert stored_digest == digest
    assert snapshot["answers"] == {"participate": True, "future-use": False}
    assert snapshot["document"] == FORM["document"]

    mine = client.get("/api/research/participants/me")
    assert mine.status_code == 200, mine.text
    record = mine.json()["enrollments"][0]["consent"]
    assert record["digest"] == digest
    assert record["answers"] == {"participate": True, "future-use": False}
    assert record["document"] == FORM["document"]

    current_user["value"] = _owner(owner_id)
    row = client.get(
        f"/api/research/studies/{study['study_id']}/analytics/participants"
    ).json()["participants"][0]
    assert row["consent_digest"] == digest
    assert row["consent_answers"] == {"participate": True, "future-use": False}


def test_stock_consent_join_without_a_digest_still_records_the_version(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "stock-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "stock-participant@example.com")
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = _create(client, profile_id)
    assert created.status_code == 201, created.text
    study = created.json()["study"]
    assert study["consent"]["custom"] is False
    assert study["consent"]["statements"] == [
        {"id": "accept", "text": "I accept the study consent notice.", "required": True}
    ]

    current_user["value"] = _participant(participant_id)
    stock = client.get(f"/api/research/join/{study['join_code']}").json()["consent"]
    joined = client.post(
        "/api/research/join", json={"join_code": study["join_code"], "accept_consent": True}
    )
    assert joined.status_code == 201, joined.text
    stored_digest, snapshot = _enrollment_consent(session_factory, joined.json()["enrollment_id"])
    assert stored_digest == stock["digest"]
    assert snapshot["answers"] == {"accept": True}
    assert snapshot["document"] is None
