"""HTTP contract for the administrator account listing's filters and enrollments.

``GET /api/research/researchers`` keeps its shape and adds, per account, the
join day (UTC date only) and a newest-first list of enrollments (study
name/status and enrollment status), plus search/role/enrollment/study filters
and paging. The study-local participant code, the enrollment id and time, and
the assigned profile never appear.
"""

from __future__ import annotations

import uuid
from datetime import timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import event

from ._ui_overhaul_seed import (
    admin_user,
    now,
    seed_account,
    seed_assignment,
    seed_enrollment,
    seed_participant,
    seed_profile,
    seed_study,
)
from .test_research_api_contract import http_runtime  # noqa: F401 - fixture

ACCOUNTS_PATH = "/api/research/researchers"
ENROLLMENT_KEYS = {
    "study_id",
    "study_name",
    "study_status",
    "status",
}


def _seed_directory(session) -> SimpleNamespace:
    """Five accounts (one per role/enrollment shape) and two studies."""
    t0 = now()
    admin = seed_account(
        session,
        "root-admin@example.com",
        name="Root Admin",
        is_admin=True,
        can_research=True,
        joined_at=t0 - timedelta(hours=5),
    )
    researcher = seed_account(
        session,
        "Rita.Researcher@Example.com",
        name="Rita Researcher",
        can_research=True,
        joined_at=t0 - timedelta(hours=4),
    )
    active = seed_account(
        session, "pat@example.com", name="Pat Active", joined_at=t0 - timedelta(hours=3)
    )
    finished = seed_account(
        session,
        "fin@example.com",
        name="Fin 100%_done",
        joined_at=t0 - timedelta(hours=2),
    )
    newcomer = seed_account(
        session, "new@example.com", name="Newcomer", joined_at=t0 - timedelta(hours=1)
    )
    study_a = seed_study(session, owner_id=researcher, name="Study A")
    study_b = seed_study(
        session, owner_id=researcher, name="Study B", research_status="STUDY_STOPPED"
    )
    active_participant = seed_participant(session, active)
    finished_participant = seed_participant(session, finished)
    old_enrollment = seed_enrollment(
        session,
        participant_id=active_participant,
        study_id=study_b,
        status="COMPLETED",
        enrolled_at=t0 - timedelta(days=10),
        participant_code="p_secretcode_old",
    )
    new_enrollment = seed_enrollment(
        session,
        participant_id=active_participant,
        study_id=study_a,
        status="ACTIVE",
        enrolled_at=t0 - timedelta(days=1),
        participant_code="p_secretcode_new",
    )
    finished_enrollment = seed_enrollment(
        session,
        participant_id=finished_participant,
        study_id=study_a,
        status="COMPLETED",
        enrolled_at=t0 - timedelta(days=2),
        participant_code="p_secretcode_fin",
    )
    arm = seed_profile(session, owner_id=researcher, name="secret-arm-profile")
    seed_assignment(
        session,
        enrollment_id=new_enrollment,
        study_id=study_a,
        profile_id=arm,
        snapshot={"name": "secret-arm-profile", "framework_version": "code4me2-agent"},
    )
    return SimpleNamespace(
        t0=t0,
        admin=admin,
        researcher=researcher,
        active=active,
        finished=finished,
        newcomer=newcomer,
        study_a=study_a,
        study_b=study_b,
        old_enrollment=old_enrollment,
        new_enrollment=new_enrollment,
        finished_enrollment=finished_enrollment,
        arm=arm,
    )


def _ids(response) -> list[str]:
    return [row["user_id"] for row in response.json()["researchers"]]


def test_account_rows_add_joined_at_and_enrollments_without_pseudonyms(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_directory(session)

    current_user["value"] = admin_user(seeded.admin)
    response = client.get(ACCOUNTS_PATH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 5
    assert body["limit"] == 100
    assert body["offset"] == 0
    # Newest account first, unchanged.
    assert _ids(response) == [
        str(seeded.newcomer),
        str(seeded.finished),
        str(seeded.active),
        str(seeded.researcher),
        str(seeded.admin),
    ]
    rows = {row["user_id"]: row for row in body["researchers"]}
    active = rows[str(seeded.active)]
    assert set(active) == {
        "user_id",
        "email",
        "name",
        "is_admin",
        "can_research",
        "verified",
        "joined_at",
        "enrollments",
    }
    # Only the UTC day: an exact join time could match an enrollment time.
    assert active["joined_at"] == (seeded.t0 - timedelta(hours=3)).astimezone(timezone.utc).date().isoformat()
    # Newest enrollment first, with the study's lifecycle status.
    assert [item["study_id"] for item in active["enrollments"]] == [
        str(seeded.study_a),
        str(seeded.study_b),
    ]
    # Telemetry is keyed by enrollment id: never paired with an account here.
    for enrollment_id in (seeded.new_enrollment, seeded.old_enrollment):
        assert str(enrollment_id) not in response.text
    for item in active["enrollments"]:
        assert set(item) == ENROLLMENT_KEYS
    newest, oldest = active["enrollments"]
    assert newest["study_id"] == str(seeded.study_a)
    assert newest["study_name"] == "Study A"
    assert newest["study_status"] == "ACTIVE"
    assert newest["status"] == "ACTIVE"
    # No enrollment time either: the analytics show it next to the pseudonym.
    assert "enrolled_at" not in newest
    assert oldest["study_name"] == "Study B"
    assert oldest["study_status"] == "STUDY_STOPPED"
    assert oldest["status"] == "COMPLETED"
    assert rows[str(seeded.finished)]["enrollments"][0]["status"] == "COMPLETED"
    assert rows[str(seeded.newcomer)]["enrollments"] == []
    assert rows[str(seeded.admin)]["enrollments"] == []

    # The admin view never links an account to its pseudonym or its arm.
    assert "p_secretcode" not in response.text
    assert "participant_code" not in response.text
    assert "secret-arm-profile" not in response.text
    assert str(seeded.arm) not in response.text


def test_enrollment_of_a_missing_study_keeps_a_null_study_name(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        admin = seed_account(session, "missing-admin@example.com", is_admin=True)
        account = seed_account(session, "orphan@example.com")
        participant = seed_participant(session, account)
        missing_study = uuid.uuid4()
        seed_enrollment(session, participant_id=participant, study_id=missing_study)

    current_user["value"] = admin_user(admin)
    response = client.get(ACCOUNTS_PATH, params={"q": "orphan"})
    assert response.status_code == 200, response.text
    (row,) = response.json()["researchers"]
    (item,) = row["enrollments"]
    assert item["study_id"] == str(missing_study)
    assert item["study_name"] is None
    assert item["study_status"] is None
    assert item["status"] == "ACTIVE"


def test_account_filters_and_paging(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_directory(session)
    current_user["value"] = admin_user(seeded.admin)

    def listing(**params):
        response = client.get(ACCOUNTS_PATH, params=params)
        assert response.status_code == 200, response.text
        return response

    # q: case-insensitive substring over email and name.
    rita = listing(q="rita.RESEARCHER")
    assert _ids(rita) == [str(seeded.researcher)]
    assert rita.json()["total"] == 1
    assert _ids(listing(q="pat active")) == [str(seeded.active)]
    assert listing(q="EXAMPLE.COM").json()["total"] == 5
    # LIKE wildcards are matched literally, never as patterns.
    assert _ids(listing(q="%")) == [str(seeded.finished)]
    assert _ids(listing(q="0%_d")) == [str(seeded.finished)]
    assert listing(q="   ").json()["total"] == 5

    # role: admin / researcher (not admin) / participant (neither).
    assert _ids(listing(role="admin")) == [str(seeded.admin)]
    assert _ids(listing(role="researcher")) == [str(seeded.researcher)]
    assert _ids(listing(role="participant")) == [
        str(seeded.newcomer),
        str(seeded.finished),
        str(seeded.active),
    ]
    assert listing(role="all").json()["total"] == 5

    # enrollment: ACTIVE enrollment or not.
    assert _ids(listing(enrollment="enrolled")) == [str(seeded.active)]
    assert _ids(listing(enrollment="not_enrolled")) == [
        str(seeded.newcomer),
        str(seeded.finished),
        str(seeded.researcher),
        str(seeded.admin),
    ]
    assert listing(enrollment="any").json()["total"] == 5

    # study_id: any enrollment (any status) in that study.
    assert _ids(listing(study_id=str(seeded.study_a))) == [
        str(seeded.finished),
        str(seeded.active),
    ]
    assert _ids(listing(study_id=str(seeded.study_b))) == [str(seeded.active)]
    nobody = listing(study_id=str(uuid.uuid4()))
    assert nobody.json()["researchers"] == []
    assert nobody.json()["total"] == 0

    # Filters combine.
    assert _ids(listing(role="participant", enrollment="not_enrolled")) == [
        str(seeded.newcomer),
        str(seeded.finished),
    ]
    assert _ids(
        listing(study_id=str(seeded.study_a), enrollment="not_enrolled")
    ) == [str(seeded.finished)]

    # Paging: total counts every match before paging.
    page = listing(limit=2, offset=1)
    assert _ids(page) == [str(seeded.finished), str(seeded.active)]
    assert page.json()["total"] == 5
    assert page.json()["limit"] == 2
    assert page.json()["offset"] == 1
    assert _ids(listing(limit=2, offset=4)) == [str(seeded.admin)]
    beyond = listing(offset=10)
    assert beyond.json()["researchers"] == []
    assert beyond.json()["total"] == 5


def test_invalid_account_filters_are_rejected_with_422(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        admin = seed_account(session, "validation-admin@example.com", is_admin=True)
    current_user["value"] = admin_user(admin)

    for params, field in (
        ({"role": "owner"}, "role"),
        ({"enrollment": "maybe"}, "enrollment"),
        ({"study_id": "not-a-uuid"}, "study_id"),
        ({"offset": "-1"}, "offset"),
        ({"limit": "0"}, "limit"),
        ({"limit": "501"}, "limit"),
        ({"q": "x" * 201}, "q"),
    ):
        response = client.get(ACCOUNTS_PATH, params=params)
        assert response.status_code == 422, (params, response.text)
        detail = response.json()["detail"]
        # FastAPI's standard validation shape, as for the existing ``limit``.
        assert isinstance(detail, list)
        assert any(entry["loc"][-1] == field for entry in detail), detail


def test_enrollments_load_with_a_constant_number_of_queries(http_runtime):
    """One query for the page and one for all of its enrollments (no N+1)."""
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_directory(session)
    current_user["value"] = admin_user(seeded.admin)
    engine = session_factory.kw["bind"]
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    def selects_for_one_listing():
        statements.clear()
        event.listen(engine, "before_cursor_execute", record)
        try:
            response = client.get(ACCOUNTS_PATH)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert response.status_code == 200, response.text
        selects = sum(
            1 for statement in statements if statement.lstrip().upper().startswith("SELECT")
        )
        return selects, response.json()["researchers"]

    small, _ = selects_for_one_listing()
    with session_factory() as session:
        bulk = []
        for index in range(6):
            account = seed_account(session, f"bulk-{index}@example.com")
            participant = seed_participant(session, account)
            seed_enrollment(
                session,
                participant_id=participant,
                study_id=seeded.study_a,
                enrolled_at=seeded.t0 - timedelta(days=2),
            )
            seed_enrollment(
                session,
                participant_id=participant,
                study_id=seeded.study_b,
                status="COMPLETED",
                enrolled_at=seeded.t0 - timedelta(days=1),
            )
            bulk.append(str(account))
    large, rows = selects_for_one_listing()
    by_id = {row["user_id"]: row for row in rows}
    # Every new account's two enrollments are present, newest first...
    for account in bulk:
        assert [item["study_name"] for item in by_id[account]["enrollments"]] == [
            "Study B",
            "Study A",
        ]
    # ...without any per-account query: total count + page + their enrollments.
    assert small == large
    assert large <= 3
