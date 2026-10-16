"""An opt-out (and so an erase) serializes with the writes that can race it.

The opt-out updates the account row first; joins and storage writers read it
with ``lock_collection_allowed`` (a share lock held until they commit). Each
test holds one side's lock in an open transaction against real PostgreSQL and
shows the other side cannot slip past it.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from privacy import collection, erasure

from ..research._ui_overhaul_seed import seed_account


def _short_lock_timeout(db):
    db.execute(text("SET LOCAL lock_timeout = '300ms'"))


def test_a_writer_cannot_pass_an_uncommitted_erase(session_factory):
    with session_factory() as setup:
        user_id = seed_account(setup, "racer@example.org")
    eraser, writer = session_factory(), session_factory()
    try:
        erasure.erase_collected_data(eraser, user_id)  # uncommitted: holds the row lock
        _short_lock_timeout(writer)
        with pytest.raises(OperationalError, match="lock"):
            collection.lock_collection_allowed(writer, user_id)
        writer.rollback()

        eraser.commit()

        assert collection.lock_collection_allowed(writer, user_id) is False
    finally:
        eraser.close()
        writer.close()


def test_an_opt_out_waits_for_a_writer_holding_the_share_lock(session_factory):
    with session_factory() as setup:
        user_id = seed_account(setup, "racer@example.org")
    writer, eraser = session_factory(), session_factory()
    try:
        assert collection.lock_collection_allowed(writer, user_id) is True
        _short_lock_timeout(eraser)
        with pytest.raises(OperationalError, match="lock"):
            collection.opt_out(eraser, user_id)
        eraser.rollback()

        writer.commit()
        collection.opt_out(eraser, user_id)
        eraser.commit()

        assert not collection.is_collection_allowed(eraser, user_id)
    finally:
        writer.close()
        eraser.close()


def test_a_row_written_under_the_lock_is_erased_by_the_erase_that_waited(session_factory):
    session_id, project_id = uuid.uuid4(), uuid.uuid4()
    with session_factory() as setup:
        user_id = seed_account(setup, "racer@example.org")
        setup.execute(
            text("INSERT INTO public.session (session_id, user_id, start_time) VALUES (:s, :u, now())"),
            {"s": session_id, "u": user_id},
        )
        setup.execute(
            text("INSERT INTO public.project (project_id, project_name, created_at) VALUES (:p, 'demo', now())"),
            {"p": project_id},
        )
        setup.commit()

    writer = session_factory()
    outcome = {}

    def erase():
        with session_factory() as eraser:
            outcome["erased"] = erasure.erase_collected_data(
                eraser, user_id, keep_session_ids=[session_id]
            )
            eraser.commit()

    try:
        assert collection.lock_collection_allowed(writer, user_id) is True
        thread = threading.Thread(target=erase)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive(), "the erase must wait for the writer's lock"

        writer.execute(
            text(
                "INSERT INTO public.meta_query (meta_query_id, user_id, session_id, project_id, "
                "timestamp, query_type) VALUES (:q, :u, :s, :p, now(), 'completion')"
            ),
            {"q": uuid.uuid4(), "u": user_id, "s": session_id, "p": project_id},
        )
        writer.commit()
        thread.join(timeout=30)
    finally:
        writer.close()

    assert not thread.is_alive()
    assert outcome["erased"].queries == 1
    with session_factory() as db:
        assert db.execute(
            text("SELECT count(*) FROM public.meta_query WHERE user_id = :u"), {"u": user_id}
        ).scalar_one() == 0


@pytest.mark.parametrize(
    "writer_check",
    [
        collection.lock_collection_allowed,
        collection.lock_account,
        collection.lock_context_storage_allowed,
        collection.discard_if_opted_out,
    ],
    ids=lambda check: check.__name__,
)
def test_every_writer_check_holds_the_account_row_until_commit(session_factory, writer_check):
    with session_factory() as setup:
        user_id = seed_account(setup, "racer@example.org")
    writer, eraser = session_factory(), session_factory()
    try:
        writer_check(writer, user_id)
        _short_lock_timeout(eraser)
        with pytest.raises(OperationalError, match="lock"):
            collection.opt_out(eraser, user_id)
    finally:
        writer.close()
        eraser.close()


@pytest.mark.parametrize("erased_while_waiting", [False, True])
def test_a_run_task_is_created_only_for_an_enrollment_that_survived_the_lock(
    session_factory, erased_while_waiting
):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from research.telemetry.ingestion.agent_tasks import ensure_agent_tasks_for_ack

    from ..research._ui_overhaul_seed import seed_event, seed_profile, seed_study
    from ._footprint import seed_footprint

    with session_factory() as setup:
        researcher_id = seed_account(setup, "researcher@example.org", can_research=True)
        study_id = seed_study(setup, owner_id=researcher_id, name="Agent study")
        profile_id = seed_profile(setup, owner_id=researcher_id)
        alice = seed_footprint(setup, email="alice@example.org", study_id=study_id, profile_id=profile_id)
        event_id = seed_event(
            setup, enrollment_id=alice.enrollment_id, study_id=study_id,
            research_session_id=alice.research_session_id, event_type="agent.prompt.submitted",
            sequence=3, occurred_at=datetime.now(timezone.utc),
        )
    payload = SimpleNamespace(events=[
        SimpleNamespace(event_id=event_id, agent_run_id="run-1", event_type="agent.prompt.submitted")
    ])
    ack = SimpleNamespace(accepted=[SimpleNamespace(event_id=event_id)], duplicate=[])
    locked = []

    def before_create(account_id):
        locked.append(account_id)
        if erased_while_waiting:  # the erase the lock waited for has just committed
            with session_factory() as eraser:
                erasure.erase_collected_data(eraser, account_id)
                eraser.commit()

    with session_factory() as db:
        created = ensure_agent_tasks_for_ack(db, payload, ack, before_create=before_create)
        tasks = db.execute(
            text("SELECT count(*) FROM public.agent_task WHERE external_run_id = 'run-1'")
        ).scalar_one()

    assert locked == [alice.user_id]
    assert (len(created), tasks) == ((0, 0) if erased_while_waiting else (1, 1))
