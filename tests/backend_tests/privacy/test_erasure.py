"""Opt-out and erasure against PostgreSQL: what goes, what stays, and whose.

Two participants of the same study each get a complete footprint. Erasing one
must remove every row of collected data linked to that account, keep the
account and its live sign-in session usable, and leave the other participant
and the researcher's study untouched.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from privacy import collection, erasure
from research.participants.erasure import ERASURE_LEDGER_REASON

from ..research._ui_overhaul_seed import seed_account, seed_profile, seed_study
from ._footprint import row_counts, seed_footprint

# Rows an erasure keeps so the account and its live IDE session keep working.
KEPT_AFTER_ERASE = {"user", "live_session", "project", "project_users"}


def _study_world(session):
    researcher_id = seed_account(session, "researcher@example.org", can_research=True)
    study_id = seed_study(session, owner_id=researcher_id, name="Agent study")
    profile_id = seed_profile(session, owner_id=researcher_id)
    return researcher_id, study_id, profile_id


def _scalar(session, sql, **params):
    return session.execute(text(sql), params).scalar_one()


def test_erase_removes_every_collected_row_and_keeps_the_account(session_factory):
    with session_factory() as db:
        researcher_id, study_id, profile_id = _study_world(db)
        alice = seed_footprint(db, email="alice@example.org", study_id=study_id, profile_id=profile_id)
        bob = seed_footprint(db, email="bob@example.org", study_id=study_id, profile_id=profile_id)
        bob_before = row_counts(db, bob)

        erased = erasure.erase_collected_data(
            db, alice.user_id, keep_session_ids=[alice.live_session_id]
        )
        db.commit()

        assert erased.as_dict() == {
            "queries": 2, "chats": 1, "agent_runs": 1, "study_enrollments": 1, "study_events": 2,
        }
        after = row_counts(db, alice)
        assert {table: count for table, count in after.items() if count} == dict.fromkeys(
            KEPT_AFTER_ERASE, 1
        )
        assert _scalar(
            db, "SELECT multi_file_contexts FROM public.project WHERE project_id = :p", p=alice.project_id
        ) == "{}"
        assert not collection.is_collection_allowed(db, alice.user_id)
        assert erasure.stored_data_summary(db, alice.user_id).as_dict() == dict.fromkeys(
            erased.as_dict(), 0
        )

        assert row_counts(db, bob) == bob_before
        assert collection.is_collection_allowed(db, bob.user_id)
        assert _scalar(db, "SELECT count(*) FROM public.study WHERE study_id = :s", s=study_id) == 1
        assert _scalar(
            db, "SELECT count(*) FROM public.agent_profile WHERE owner_user_id = :r", r=researcher_id
        ) == 1


def test_erasure_leaves_a_content_free_ledger_record_for_the_study(session_factory):
    with session_factory() as db:
        _, study_id, profile_id = _study_world(db)
        alice = seed_footprint(db, email="alice@example.org", study_id=study_id, profile_id=profile_id)

        erasure.erase_collected_data(db, alice.user_id)
        db.commit()

        rows = db.execute(
            text(
                "SELECT scope_id, study_id, actor, payload_json FROM public.research_record "
                "WHERE kind = 'RETENTION_EVIDENCE'"
            )
        ).all()
        assert len(rows) == 1
        scope_id, record_study_id, actor, payload = rows[0]
        assert (scope_id, record_study_id, actor) == (alice.enrollment_id, study_id, None)
        assert payload["reason"] == ERASURE_LEDGER_REASON
        assert payload["record"]["action"] == "DELETE_ALL"
        assert payload["record"]["affected_count"] == 2
        serialized = json.dumps(payload)
        for identifying in (str(alice.user_id), alice.email, alice.participant_code):
            assert identifying not in serialized


def test_erase_is_idempotent(session_factory):
    with session_factory() as db:
        _, study_id, profile_id = _study_world(db)
        alice = seed_footprint(db, email="alice@example.org", study_id=study_id, profile_id=profile_id)
        erasure.erase_collected_data(db, alice.user_id)
        db.commit()

        again = erasure.erase_collected_data(db, alice.user_id)
        db.commit()

        assert again.as_dict() == dict.fromkeys(again.as_dict(), 0)


def test_erase_is_one_transaction(session_factory, monkeypatch):
    with session_factory() as db:
        _, study_id, profile_id = _study_world(db)
        alice = seed_footprint(db, email="alice@example.org", study_id=study_id, profile_id=profile_id)
        before = row_counts(db, alice)

        def fail(*_args, **_kwargs):
            raise RuntimeError("storage failure after agent and research erasure")

        monkeypatch.setattr(erasure, "_erase_classic_data", fail)
        with pytest.raises(RuntimeError):
            erasure.erase_collected_data(db, alice.user_id)
        db.rollback()

        assert row_counts(db, alice) == before
        assert collection.is_collection_allowed(db, alice.user_id)


def test_opt_out_withdraws_the_active_enrollment_and_opt_in_does_not_rejoin(session_factory):
    with session_factory() as db:
        _, study_id, profile_id = _study_world(db)
        alice = seed_footprint(db, email="alice@example.org", study_id=study_id, profile_id=profile_id)

        withdrawn = collection.opt_out(db, alice.user_id)
        db.commit()
        first_opt_out = _scalar(
            db, 'SELECT data_collection_opted_out_at FROM public."user" WHERE user_id = :u', u=alice.user_id
        )

        assert withdrawn == [alice.enrollment_id]
        status, epoch = db.execute(
            text("SELECT status, revocation_epoch FROM public.research_enrollment WHERE enrollment_id = :e"),
            {"e": alice.enrollment_id},
        ).one()
        assert (status, epoch) == ("WITHDRAWN", 1)
        assert _scalar(
            db, "SELECT status FROM public.study_assignment WHERE enrollment_id = :e", e=alice.enrollment_id
        ) == "WITHDRAWN"
        state, transitions = db.execute(
            text("SELECT state, transitions_json FROM public.research_session WHERE session_id = :s"),
            {"s": alice.research_session_id},
        ).one()
        assert state == "revoked"
        assert transitions[-1]["evidence_ref"] == "participant_withdrawal"
        assert not collection.is_collection_allowed(db, alice.user_id)
        # Withdrawal alone keeps the collected data until the user erases it.
        assert row_counts(db, alice)["research_event"] == 2

        assert collection.opt_out(db, alice.user_id) == []
        db.commit()
        assert _scalar(
            db, 'SELECT data_collection_opted_out_at FROM public."user" WHERE user_id = :u', u=alice.user_id
        ) == first_opt_out

        collection.opt_in(db, alice.user_id)
        db.commit()
        assert collection.is_collection_allowed(db, alice.user_id)
        assert _scalar(
            db, "SELECT status FROM public.research_enrollment WHERE enrollment_id = :e", e=alice.enrollment_id
        ) == "WITHDRAWN"


def test_delete_account_removes_everything_of_that_account_only(session_factory):
    with session_factory() as db:
        researcher_id, study_id, profile_id = _study_world(db)
        alice = seed_footprint(db, email="alice@example.org", study_id=study_id, profile_id=profile_id)
        bob = seed_footprint(db, email="bob@example.org", study_id=study_id, profile_id=profile_id)
        bob_before = row_counts(db, bob)

        erasure.delete_account(db, alice.user_id)
        db.commit()

        assert {table: count for table, count in row_counts(db, alice).items() if count} == {}
        assert row_counts(db, bob) == bob_before
        assert _scalar(db, "SELECT count(*) FROM public.study WHERE study_id = :s", s=study_id) == 1
        assert _scalar(db, 'SELECT count(*) FROM public."user" WHERE user_id = :r', r=researcher_id) == 1


def test_delete_account_refuses_an_account_that_owns_research_resources(session_factory):
    with session_factory() as db:
        researcher_id, study_id, _ = _study_world(db)

        assert "1 research study and 1 agent profile" in erasure.account_deletion_blocker(db, researcher_id)
        with pytest.raises(erasure.AccountDeletionBlocked):
            erasure.delete_account(db, researcher_id)
        db.rollback()

        assert _scalar(db, 'SELECT count(*) FROM public."user" WHERE user_id = :r', r=researcher_id) == 1
        assert _scalar(db, "SELECT count(*) FROM public.study WHERE study_id = :s", s=study_id) == 1
        assert collection.is_collection_allowed(db, researcher_id)


@pytest.mark.parametrize("other_member_consents", [True, False])
def test_a_shared_project_keeps_context_only_while_another_member_consents(
    session_factory, other_member_consents
):
    with session_factory() as db:
        _, study_id, profile_id = _study_world(db)
        alice = seed_footprint(db, email="alice@example.org", study_id=study_id, profile_id=profile_id)
        carol = seed_footprint(
            db, email="carol@example.org", study_id=study_id, profile_id=profile_id,
            enrolled=False, store_context=other_member_consents,
        )
        db.execute(
            text("INSERT INTO public.project_users (project_id, user_id, joined_at) VALUES (:p, :u, now())"),
            {"p": alice.project_id, "u": carol.user_id},
        )
        db.commit()

        erasure.erase_collected_data(db, alice.user_id)
        db.commit()

        stored = _scalar(
            db, "SELECT multi_file_contexts FROM public.project WHERE project_id = :p", p=alice.project_id
        )
        assert (stored != "{}") is other_member_consents


def test_unknown_accounts_are_never_collected(session_factory):
    with session_factory() as db:
        assert not collection.is_collection_allowed(db, uuid.uuid4())
