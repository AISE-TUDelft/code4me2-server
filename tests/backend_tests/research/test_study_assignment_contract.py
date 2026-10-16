"""HTTP contracts for the assignment policy: salted-hash draw and the manual override."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from research.canonical import canonical_hash
from research.runtime.assignment.hashing import hash_arm_index

from .test_research_api_contract import (  # noqa: F401 - the fixture is used by name
    VALID_SESSION_POLICY,
    _owner,
    _participant,
    _profile,
    _seed_retained_event,
    _seed_user,
    http_runtime,
)


def _create_study(client, profile_ids, *, manual: bool = False, name: str = "Assignment study"):
    response = client.post(
        "/api/research/studies",
        json={
            "name": name,
            "default_budget_usd": "10",
            "session_policy": VALID_SESSION_POLICY,
            "telemetry_policy": {"allowed_field_classes": ["STRUCTURAL", "BEHAVIORAL"]},
            "profile_ids": [str(item) for item in profile_ids],
            "allow_manual_assignment": manual,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["study"]


def _join(client, join_code: str) -> dict:
    response = client.post(
        "/api/research/join", json={"join_code": join_code, "accept_consent": True}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _assignment_row(session_factory, enrollment_id: str):
    session = session_factory()
    try:
        return session.execute(
            text(
                "SELECT a.agent_profile_id, a.strategy, a.randomization_epoch, e.revocation_epoch "
                "FROM public.study_assignment a JOIN public.research_enrollment e "
                "ON e.enrollment_id = a.enrollment_id WHERE a.enrollment_id = :id"
            ),
            {"id": enrollment_id},
        ).one()
    finally:
        session.close()


def test_new_study_freezes_and_echoes_its_policies_and_hash_assignment(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "hash-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "hash-participant@example.com")
        first, second, third = (_profile(session, owner_id) for _ in range(3))
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    study = _create_study(client, [first, second, third])
    assert study["assignment_policy"] == {"strategy": "DETERMINISTIC_HASH", "manual_override": False}

    # The detail read echoes the frozen policies (it returned {} before).
    detail = client.get(f"/api/research/studies/{study['study_id']}").json()["study"]
    assert detail["session_policy"]["idle_timeout_seconds"] == 600
    assert detail["telemetry_policy"]["allowed_field_classes"]
    session = session_factory()
    try:
        config, digest = session.execute(
            text(
                "SELECT research_config_json, research_config_digest FROM public.study "
                "WHERE study_id = :id"
            ),
            {"id": study["study_id"]},
        ).one()
    finally:
        session.close()
    assert config["assignment"] == {"strategy": "DETERMINISTIC_HASH", "manual_override": False}
    assert digest == canonical_hash(config)

    current_user["value"] = _participant(participant_id)
    joined = _join(client, study["join_code"])
    arms = [str(first), str(second), str(third)]
    expected = arms[hash_arm_index(study["study_id"], 0, joined["enrollment_id"], len(arms))]
    assert joined["agent_profile_id"] == expected
    profile_id, strategy, epoch, _ = _assignment_row(session_factory, joined["enrollment_id"])
    assert (str(profile_id), strategy, epoch) == (expected, "DETERMINISTIC_HASH", 0)


def test_studies_without_an_assignment_policy_keep_the_random_equal_draw(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "legacy-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "legacy-participant@example.com")
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    study = _create_study(client, [profile_id])
    session = session_factory()
    try:
        # A study created before the policy decision has no assignment block.
        session.execute(
            text(
                "UPDATE public.study SET research_config_json = research_config_json - 'assignment' "
                "WHERE study_id = :id"
            ),
            {"id": study["study_id"]},
        )
        session.commit()
    finally:
        session.close()
    assert client.get(f"/api/research/studies/{study['study_id']}").json()["study"][
        "assignment_policy"
    ] == {"strategy": "RANDOM_EQUAL", "manual_override": False}

    current_user["value"] = _participant(participant_id)
    joined = _join(client, study["join_code"])
    _, strategy, _, _ = _assignment_row(session_factory, joined["enrollment_id"])
    assert strategy == "RANDOM_EQUAL"


def test_manual_override_matrix(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "override-owner@example.com", can_research=True)
        other_owner_id = _seed_user(session, "override-other@example.com", can_research=True)
        participants = [_seed_user(session, f"override-p{index}@example.com") for index in range(3)]
        first, second = _profile(session, owner_id), _profile(session, owner_id)
        foreign_profile = _profile(session, other_owner_id)
        locked_first = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    manual = _create_study(client, [first, second], manual=True, name="Manual allowed")
    locked = _create_study(client, [locked_first], name="Manual off")
    assert manual["assignment_policy"]["manual_override"] is True

    current_user["value"] = _participant(participants[0])
    consent = client.get(f"/api/research/join/{manual['join_code']}").json()["consent"]["text"]
    assert "at random or by the research team" in consent
    assert "You are randomly assigned" not in consent
    fresh = _join(client, manual["join_code"])
    current_user["value"] = _participant(participants[1])
    used = _join(client, manual["join_code"])
    current_user["value"] = _participant(participants[2])
    locked_join = client.get(f"/api/research/join/{locked['join_code']}").json()["consent"]["text"]
    assert "You are randomly assigned" in locked_join
    off = _join(client, locked["join_code"])
    _seed_retained_event(
        session_factory, study_id=manual["study_id"], enrollment_id=used["enrollment_id"]
    )

    def put(study_id, enrollment_id, profile_id):
        return client.put(
            f"/api/research/studies/{study_id}/enrollments/{enrollment_id}/assignment",
            json={"profile_id": str(profile_id)},
        )

    current_user["value"] = _participant(participants[0])
    assert put(manual["study_id"], fresh["enrollment_id"], second).status_code == 403

    current_user["value"] = _owner(owner_id)
    rows = {
        row["enrollment_id"]: row
        for row in client.get(
            f"/api/research/studies/{manual['study_id']}/analytics/participants"
        ).json()["participants"]
    }
    assert rows[fresh["enrollment_id"]]["reassignable"] is True
    assert rows[used["enrollment_id"]]["reassignable"] is False

    def error_code(response, status):
        assert response.status_code == status, response.text
        return response.json()["detail"]["code"]

    assert error_code(put(locked["study_id"], off["enrollment_id"], locked_first), 409) == (
        "MANUAL_ASSIGNMENT_DISABLED"
    )
    assert error_code(put(manual["study_id"], str(uuid.uuid4()), second), 404) == (
        "ENROLLMENT_NOT_FOUND"
    )
    assert error_code(put(manual["study_id"], fresh["enrollment_id"], foreign_profile), 422) == (
        "PROFILE_NOT_IN_STUDY"
    )
    assert error_code(put(manual["study_id"], used["enrollment_id"], first), 409) == (
        "ASSIGNMENT_IN_USE"
    )

    before = _assignment_row(session_factory, fresh["enrollment_id"])
    target = first if str(before[0]) == str(second) else second
    response = put(manual["study_id"], fresh["enrollment_id"], target)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] is True
    assert body["assignment"]["profile_id"] == str(target)
    assert body["assignment"]["strategy"] == "MANUAL"
    after = _assignment_row(session_factory, fresh["enrollment_id"])
    assert (str(after[0]), after[1], after[2]) == (str(target), "MANUAL", 0)
    assert after[3] == before[3] + 1

    again = put(manual["study_id"], fresh["enrollment_id"], target)
    assert again.status_code == 200 and again.json()["changed"] is False
    assert _assignment_row(session_factory, fresh["enrollment_id"])[3] == after[3]

    session = session_factory()
    try:
        audit = session.execute(
            text(
                "SELECT actor, payload_json FROM public.research_record "
                "WHERE kind = 'STUDY_LIFECYCLE' AND scope_type = 'enrollment' AND scope_id = :id"
            ),
            {"id": fresh["enrollment_id"]},
        ).all()
    finally:
        session.close()
    assert len(audit) == 1
    assert audit[0][0] == "http-owner@example.com"
    assert audit[0][1]["event"] == "ASSIGNMENT_OVERRIDDEN"
    assert audit[0][1]["from_profile_id"] == str(before[0])
    assert audit[0][1]["to_profile_id"] == str(target)

    row = next(
        item
        for item in client.get(
            f"/api/research/studies/{manual['study_id']}/analytics/participants"
        ).json()["participants"]
        if item["enrollment_id"] == fresh["enrollment_id"]
    )
    assert row["arm"]["profile_id"] == str(target)
    assert row["arm"]["strategy"] == "MANUAL"

    revoked = client.post(
        f"/api/research/studies/{manual['study_id']}/enrollments/{fresh['enrollment_id']}/revoke",
        json={},
    )
    assert revoked.status_code == 200, revoked.text
    assert error_code(put(manual["study_id"], fresh["enrollment_id"], first), 409) == (
        "ENROLLMENT_NOT_ACTIVE"
    )

    stopped = client.post(f"/api/research/studies/{manual['study_id']}/stop", json={})
    assert stopped.status_code == 200, stopped.text
    assert error_code(put(manual["study_id"], used["enrollment_id"], first), 409) == "STUDY_STOPPED"


def test_clone_records_the_config_digest(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "clone-digest-owner@example.com", can_research=True)
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    study = _create_study(client, [profile_id])
    assert client.post(f"/api/research/studies/{study['study_id']}/stop", json={}).status_code == 200
    cloned = client.post(
        f"/api/research/studies/{study['study_id']}/clone",
        json={"profile_ids": [str(profile_id)]},
    )
    assert cloned.status_code == 201, cloned.text
    session = session_factory()
    try:
        config, digest = session.execute(
            text(
                "SELECT research_config_json, research_config_digest FROM public.study "
                "WHERE study_id = :id"
            ),
            {"id": cloned.json()["study"]["study_id"]},
        ).one()
    finally:
        session.close()
    assert digest == canonical_hash(config)
    assert config["assignment"]["strategy"] == "DETERMINISTIC_HASH"
