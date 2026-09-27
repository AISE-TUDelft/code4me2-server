"""Budget ledger invariants on real PostgreSQL.

The one property that matters: the sum of holds and settled charges never
exceeds an enrollment's limit, whatever the interleaving. These tests race 16
reservers on one balance row, drive holds through every resolution (settle,
forfeit, void, lazy expiry), and check that duplicate or late resolutions are
no-ops. They also cover adjustments (top-up, set-limit, apply-default,
idempotency) and the backfill path.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database.migration.migration_manager import MigrationManager
from database.research_schemas import (
    ADJUSTMENT_APPLY_DEFAULT,
    ADJUSTMENT_SET_LIMIT,
    ADJUSTMENT_TOP_UP,
    LIMIT_SOURCE_ADJUSTED,
    LIMIT_SOURCE_BACKFILL,
)
from research.budget import (
    BudgetRefused,
    BudgetSettings,
    IdempotencyConflict,
    ModelPrice,
    PriceMissing,
    UsageSnapshot,
    get_model_price,
)
from research.budget import ledger

load_dotenv()

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)

SETTINGS = BudgetSettings()
INPUT_PRICE = Decimal("1.0")  # 1 micro-USD per prompt token
OUTPUT_PRICE = Decimal("4.0")  # 4 micro-USD per completion token


def _database():
    """Fresh schema plus a session factory over the disposable test database."""
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        connection.commit()
    os.environ.setdefault("TEST_MODE", "true")
    manager = MigrationManager(use_test_db=True)
    manager.init_migrations()
    manager.migrate()
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(scope="module")
def database():
    engine, factory = _database()
    yield factory
    engine.dispose()


class Seed:
    """One study, one priced connection, and enrollments created on demand."""

    def __init__(self, session_factory, *, study_default_micro_usd: int = 0):
        self.factory = session_factory
        self.connection_id = uuid.uuid4()
        self.study_id = uuid.uuid4()
        self.model = "test/model"
        now = datetime.now(timezone.utc)
        with session_factory() as db:
            config_id = db.execute(
                text("INSERT INTO public.config (config_data) VALUES ('{}') RETURNING config_id")
            ).scalar_one()
            self.owner_id = uuid.uuid4()
            db.execute(
                text(
                    'INSERT INTO public."user" (user_id, joined_at, email, name, password, config_id) '
                    "VALUES (:user_id, now(), :email, 'Owner', 'x', :config_id)"
                ),
                {"user_id": self.owner_id, "email": f"owner-{self.owner_id}@example.com", "config_id": config_id},
            )
            db.execute(
                text(
                    "INSERT INTO public.study (study_id, name, created_by, starts_at, is_research, "
                    "research_status, inference_budget_default_micro_usd) "
                    "VALUES (:study_id, :name, :owner, :starts_at, true, 'ACTIVE', :default_budget)"
                ),
                {
                    "study_id": self.study_id,
                    "name": f"budget study {self.study_id}",
                    "owner": self.owner_id,
                    "starts_at": now - timedelta(days=1),
                    "default_budget": study_default_micro_usd,
                },
            )
            db.execute(
                text(
                    "INSERT INTO public.provider_connection (connection_id, label, base_url, secret_ref, models_json, created_at) "
                    "VALUES (:id, :label, 'https://llm.example.org/v1', 'TEST_KEY', :models, now())"
                ),
                {"id": self.connection_id, "label": f"conn-{self.connection_id}", "models": json.dumps([self.model])},
            )
            db.execute(
                text(
                    "INSERT INTO public.provider_model_price (connection_id, model, input_usd_per_million, "
                    "output_usd_per_million, cached_input_usd_per_million, updated_at) "
                    "VALUES (:id, :model, :input, :output, NULL, now())"
                ),
                {"id": self.connection_id, "model": self.model, "input": INPUT_PRICE, "output": OUTPUT_PRICE},
            )
            db.commit()
            self.price = get_model_price(db, self.connection_id, self.model)

    def enrollment(self, *, limit_micro_usd: int | None = None) -> uuid.UUID:
        """A new enrollment; with ``limit_micro_usd`` its balance row is created too."""
        enrollment_id = uuid.uuid4()
        with self.factory() as db:
            config_id = db.execute(
                text("INSERT INTO public.config (config_data) VALUES ('{}') RETURNING config_id")
            ).scalar_one()
            account_id = uuid.uuid4()
            db.execute(
                text(
                    'INSERT INTO public."user" (user_id, joined_at, email, name, password, config_id) '
                    "VALUES (:user_id, now(), :email, 'Participant', 'x', :config_id)"
                ),
                {"user_id": account_id, "email": f"p-{account_id}@example.com", "config_id": config_id},
            )
            participant_id = uuid.uuid4()
            db.execute(
                text(
                    "INSERT INTO public.research_participant (participant_id, account_id, created_at) "
                    "VALUES (:pid, :aid, now())"
                ),
                {"pid": participant_id, "aid": account_id},
            )
            db.execute(
                text(
                    "INSERT INTO public.research_enrollment (enrollment_id, participant_id, study_id, participant_code, "
                    "status, revocation_epoch, eligibility_json, enrolled_at, updated_at, retention_action) "
                    "VALUES (:eid, :pid, :sid, :code, 'ACTIVE', 0, '{}', now(), now(), 'RETAIN')"
                ),
                {"eid": enrollment_id, "pid": participant_id, "sid": self.study_id, "code": f"p_{enrollment_id.hex[:8]}"},
            )
            if limit_micro_usd is not None:
                ledger.create_balance(
                    db, enrollment_id=enrollment_id, study_id=self.study_id, limit_micro_usd=limit_micro_usd
                )
            db.commit()
        return enrollment_id

    def reserve(self, db, enrollment_id, body=None, **overrides):
        body = body if body is not None else _body()
        kwargs = dict(
            enrollment_id=enrollment_id,
            study_id=self.study_id,
            connection_id=self.connection_id,
            model=self.model,
            entry_point="test",
            request_id=str(uuid.uuid4()),
            openai_body=body,
            upstream_base_url="https://llm.example.org/v1",
            price=self.price,
            settings=SETTINGS,
        )
        kwargs.update(overrides)
        return ledger.reserve(db, **kwargs), body

    def balance(self, enrollment_id):
        with self.factory() as db:
            return ledger.balance_view(db, enrollment_id)

    def reservation_row(self, reservation_id):
        with self.factory() as db:
            return dict(
                db.execute(
                    text("SELECT * FROM public.inference_reservation WHERE reservation_id = :id"),
                    {"id": reservation_id},
                ).mappings().one()
            )


def _body(content: str = "hello", max_tokens: int = SETTINGS.min_output_tokens) -> dict:
    return {"model": "ignored", "messages": [{"role": "user", "content": content}], "max_tokens": max_tokens, "stream": True}


@pytest.fixture(scope="module")
def seed(database):
    return Seed(database, study_default_micro_usd=7_000)


def _fixed_hold(seed: Seed) -> int:
    """The hold of the standard body, computed the same way the ledger does."""
    from research.budget.estimate import estimate_prompt_tokens

    estimated = estimate_prompt_tokens(_body(), SETTINGS)
    return estimated * int(INPUT_PRICE) + SETTINGS.min_output_tokens * int(OUTPUT_PRICE)


# --------------------------------------------------------------------------- basics


def test_reserve_caps_body_and_settle_replaces_hold_with_actual(seed):
    enrollment_id = seed.enrollment(limit_micro_usd=100_000)
    with seed.factory() as db:
        reservation, body = seed.reserve(db, enrollment_id, _body(max_tokens=5000))
    assert body["max_tokens"] <= SETTINGS.output_token_ceiling
    assert body["n"] == 1 and "usage" not in body  # not an OpenRouter host
    balance = seed.balance(enrollment_id)
    assert balance.reserved_micro_usd == reservation.hold_micro_usd
    assert balance.call_count == 1 and balance.settled_micro_usd == 0
    assert seed.reservation_row(reservation.reservation_id)["state"] == "RESERVED"

    with seed.factory() as db:
        outcome = ledger.settle(
            db, reservation.reservation_id,
            usage=UsageSnapshot(prompt_tokens=10, completion_tokens=5, finish_reason="stop"),
            price=seed.price, upstream_status=200,
        )
    assert outcome.applied and outcome.charged_micro_usd == 10 * 1 + 5 * 4
    balance = seed.balance(enrollment_id)
    assert balance.reserved_micro_usd == 0
    assert balance.settled_micro_usd == 30
    assert balance.settled_prompt_tokens == 10 and balance.settled_completion_tokens == 5
    assert balance.available_micro_usd == 100_000 - 30
    row = seed.reservation_row(reservation.reservation_id)
    assert row["state"] == "SETTLED" and row["usage_source"] == "price_table"
    assert row["finish_reason"] == "stop" and row["upstream_status"] == 200


def test_provider_cost_settles_exactly(seed):
    enrollment_id = seed.enrollment(limit_micro_usd=100_000)
    with seed.factory() as db:
        reservation, _ = seed.reserve(db, enrollment_id, upstream_base_url="https://openrouter.ai/api/v1")
        ledger.settle(
            db, reservation.reservation_id,
            usage=UsageSnapshot(1000, 1000, cost_usd=Decimal("0.000123")), price=seed.price,
        )
    assert seed.balance(enrollment_id).settled_micro_usd == 123
    assert seed.reservation_row(reservation.reservation_id)["usage_source"] == "provider_cost"


# --------------------------------------------------------------------------- the race


def test_concurrent_reservations_never_exceed_the_limit(seed):
    hold = _fixed_hold(seed)
    workers = 16
    affordable = 5
    enrollment_id = seed.enrollment(limit_micro_usd=affordable * hold + hold - 1)
    barrier = threading.Barrier(workers)
    reservations, refusals, errors = [], [], []

    def worker():
        db = seed.factory()
        try:
            barrier.wait(timeout=5)
            reservation, _ = seed.reserve(db, enrollment_id)
            reservations.append(reservation)
        except BudgetRefused as refused:
            refusals.append(refused)
        except BaseException as error:  # noqa: BLE001 - collected and asserted below
            errors.append(error)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive(), "a reserver retained the balance row lock"

    assert errors == [], [repr(error) for error in errors]
    assert len(reservations) == affordable
    assert len(refusals) == workers - affordable
    balance = seed.balance(enrollment_id)
    assert balance.reserved_micro_usd == affordable * hold
    assert balance.reserved_micro_usd <= balance.limit_micro_usd
    assert balance.call_count == affordable
    assert balance.refused_count == workers - affordable
    assert all(refused.available_micro_usd < hold for refused in refusals)
    with seed.factory() as db:
        open_rows = db.execute(
            text("SELECT count(*) FROM public.inference_reservation WHERE enrollment_id = :e AND state = 'RESERVED'"),
            {"e": enrollment_id},
        ).scalar_one()
    assert open_rows == affordable


# --------------------------------------------------------------------------- resolutions


def test_settle_is_idempotent_and_late_resolutions_are_ignored(seed):
    enrollment_id = seed.enrollment(limit_micro_usd=50_000)
    with seed.factory() as db:
        reservation, _ = seed.reserve(db, enrollment_id)
        first = ledger.settle(db, reservation.reservation_id, usage=UsageSnapshot(4, 4), price=seed.price)
        second = ledger.settle(db, reservation.reservation_id, usage=UsageSnapshot(400, 400), price=seed.price)
        late_forfeit = ledger.forfeit(db, reservation.reservation_id, reason="late")
    assert first.applied and not second.applied and not late_forfeit.applied
    balance = seed.balance(enrollment_id)
    assert balance.settled_micro_usd == 4 + 16
    assert balance.reserved_micro_usd == 0
    assert seed.reservation_row(reservation.reservation_id)["state"] == "SETTLED"


def test_forfeit_charges_the_full_hold_and_void_releases_it(seed):
    enrollment_id = seed.enrollment(limit_micro_usd=50_000)
    with seed.factory() as db:
        forfeited, _ = seed.reserve(db, enrollment_id)
        voided, _ = seed.reserve(db, enrollment_id)
        ledger.forfeit(db, forfeited.reservation_id, reason="usage_missing", upstream_status=200)
        ledger.void(db, voided.reservation_id, reason="upstream_status", upstream_status=429)
    balance = seed.balance(enrollment_id)
    assert balance.settled_micro_usd == forfeited.hold_micro_usd
    assert balance.reserved_micro_usd == 0
    forfeited_row = seed.reservation_row(forfeited.reservation_id)
    assert forfeited_row["state"] == "FORFEITED"
    assert forfeited_row["charged_micro_usd"] == forfeited.hold_micro_usd
    assert forfeited_row["usage_source"] == "hold_forfeit"
    voided_row = seed.reservation_row(voided.reservation_id)
    assert voided_row["state"] == "VOIDED" and voided_row["charged_micro_usd"] == 0
    assert voided_row["upstream_status"] == 429


def test_lazy_expiry_charges_stale_holds_in_full(seed):
    enrollment_id = seed.enrollment(limit_micro_usd=50_000)
    with seed.factory() as db:
        stale, _ = seed.reserve(db, enrollment_id)
        db.execute(
            text("UPDATE public.inference_reservation SET deadline_at = now() - interval '1 minute' WHERE reservation_id = :id"),
            {"id": stale.reservation_id},
        )
        db.commit()
        fresh, _ = seed.reserve(db, enrollment_id)  # runs the expiry under the lock
    balance = seed.balance(enrollment_id)
    assert balance.settled_micro_usd == stale.hold_micro_usd
    assert balance.reserved_micro_usd == fresh.hold_micro_usd
    row = seed.reservation_row(stale.reservation_id)
    assert row["state"] == "EXPIRED" and row["charged_micro_usd"] == stale.hold_micro_usd
    assert row["resolution_reason"] == "expired"
    with seed.factory() as db:
        late = ledger.settle(db, stale.reservation_id, usage=UsageSnapshot(1, 1), price=seed.price)
        assert not late.applied  # the hold stays charged
        assert ledger.expire_stale_reservations(db, enrollment_id) == 0
    assert seed.balance(enrollment_id).settled_micro_usd == stale.hold_micro_usd


def test_settlement_above_the_hold_drives_available_negative_and_refuses_next(seed):
    hold = _fixed_hold(seed)
    enrollment_id = seed.enrollment(limit_micro_usd=hold + 10)
    with seed.factory() as db:
        reservation, _ = seed.reserve(db, enrollment_id)
        ledger.settle(db, reservation.reservation_id, usage=UsageSnapshot(hold * 3, 0), price=seed.price)
        balance = ledger.balance_view(db, enrollment_id)
        assert balance.available_micro_usd < 0
        assert balance.exhausted and balance.exhausted_at is not None
        with pytest.raises(BudgetRefused):
            seed.reserve(db, enrollment_id)
    assert seed.balance(enrollment_id).refused_count == 1


# --------------------------------------------------------------------------- adjustments


def test_set_limit_below_spend_refuses_and_top_up_restores(seed):
    hold = _fixed_hold(seed)
    enrollment_id = seed.enrollment(limit_micro_usd=10 * hold)
    with seed.factory() as db:
        in_flight, _ = seed.reserve(db, enrollment_id)
        lowered = ledger.adjust(
            db, enrollment_id=enrollment_id, kind=ADJUSTMENT_SET_LIMIT, amount_micro_usd=0,
            actor="researcher@example.com", reason="stop spend", idempotency_key="k-lower",
        )
        assert lowered.limit_before_micro_usd == 10 * hold and lowered.limit_after_micro_usd == 0
        assert lowered.in_flight_micro_usd == in_flight.hold_micro_usd
        with pytest.raises(BudgetRefused):
            seed.reserve(db, enrollment_id)
        assert ledger.balance_view(db, enrollment_id).exhausted_at is not None
        # the in-flight call still settles at its actual amount
        ledger.settle(db, in_flight.reservation_id, usage=UsageSnapshot(2, 2), price=seed.price)
        topped = ledger.adjust(
            db, enrollment_id=enrollment_id, kind=ADJUSTMENT_TOP_UP, amount_micro_usd=3 * hold,
            actor="researcher@example.com", reason="top up", idempotency_key="k-top",
        )
        assert topped.delta_micro_usd == 3 * hold
        balance = ledger.balance_view(db, enrollment_id)
        assert balance.limit_source == LIMIT_SOURCE_ADJUSTED
        assert balance.exhausted_at is None and balance.available_micro_usd > 0
        again, _ = seed.reserve(db, enrollment_id)
        assert again.hold_micro_usd == hold
        history = ledger.list_adjustments(db, enrollment_id)
    assert [item.kind for item in history] == [ADJUSTMENT_TOP_UP, ADJUSTMENT_SET_LIMIT]


def test_adjust_idempotency_replays_or_conflicts(seed):
    enrollment_id = seed.enrollment(limit_micro_usd=1_000)
    with seed.factory() as db:
        first = ledger.adjust(
            db, enrollment_id=enrollment_id, kind=ADJUSTMENT_TOP_UP, amount_micro_usd=500,
            actor="a", reason="r", idempotency_key="same-key",
        )
        replay = ledger.adjust(
            db, enrollment_id=enrollment_id, kind=ADJUSTMENT_TOP_UP, amount_micro_usd=500,
            actor="a", reason="r", idempotency_key="same-key",
        )
        assert replay.replayed and replay.adjustment_id == first.adjustment_id
        assert ledger.balance_view(db, enrollment_id).limit_micro_usd == 1_500
        with pytest.raises(IdempotencyConflict):
            ledger.adjust(
                db, enrollment_id=enrollment_id, kind=ADJUSTMENT_TOP_UP, amount_micro_usd=999,
                actor="a", reason="r", idempotency_key="same-key",
            )
        assert ledger.balance_view(db, enrollment_id).limit_micro_usd == 1_500


def test_apply_default_touches_only_rows_still_on_the_default(seed):
    on_default = seed.enrollment(limit_micro_usd=7_000)
    adjusted = seed.enrollment(limit_micro_usd=7_000)
    with seed.factory() as db:
        ledger.adjust(db, enrollment_id=adjusted, kind=ADJUSTMENT_SET_LIMIT, amount_micro_usd=9_000, actor="a", reason="manual")
        applied = ledger.adjust(db, enrollment_id=on_default, kind=ADJUSTMENT_APPLY_DEFAULT, amount_micro_usd=12_000, actor="a", reason="new default")
        skipped = ledger.adjust(db, enrollment_id=adjusted, kind=ADJUSTMENT_APPLY_DEFAULT, amount_micro_usd=12_000, actor="a", reason="new default")
        unchanged = ledger.adjust(db, enrollment_id=on_default, kind=ADJUSTMENT_APPLY_DEFAULT, amount_micro_usd=12_000, actor="a", reason="again")
    assert applied is not None and applied.limit_after_micro_usd == 12_000
    assert skipped is None and unchanged is None
    assert seed.balance(on_default).limit_micro_usd == 12_000
    assert seed.balance(adjusted).limit_micro_usd == 9_000


# --------------------------------------------------------------------------- backfill, prices, views


def test_ensure_balance_backfills_from_the_study_default(seed):
    enrollment_id = seed.enrollment()  # no balance row
    assert seed.balance(enrollment_id) is None
    with seed.factory() as db:
        assert ledger.ensure_balance(db, enrollment_id) is True
        assert ledger.ensure_balance(db, enrollment_id) is True  # idempotent
        assert ledger.ensure_balance(db, uuid.uuid4()) is False  # unknown enrollment
    balance = seed.balance(enrollment_id)
    assert balance.limit_micro_usd == 7_000 and balance.limit_source == LIMIT_SOURCE_BACKFILL


def test_price_missing_fails_closed(seed):
    with seed.factory() as db:
        with pytest.raises(PriceMissing):
            get_model_price(db, seed.connection_id, "unpriced/model")
        with pytest.raises(PriceMissing):
            get_model_price(db, None, seed.model)
        price = get_model_price(db, seed.connection_id, seed.model)
    assert price == ModelPrice(seed.connection_id, seed.model, Decimal("1.000000"), Decimal("4.000000"), None)


def test_participant_view_and_study_summary(seed):
    enrollment_id = seed.enrollment(limit_micro_usd=10_000)
    with seed.factory() as db:
        reservation, _ = seed.reserve(db, enrollment_id)
        view = ledger.participant_view(db, enrollment_id=enrollment_id)
        assert view["limit"] == 10_000 and view["reserved"] == reservation.hold_micro_usd
        assert view["consumed"] == 0 and view["remaining"] == 10_000 - reservation.hold_micro_usd
        assert view["unit"] == "micro_usd" and not view["exhausted"]
        assert view["warning_fraction"] == 0.8
        ledger.settle(db, reservation.reservation_id, usage=UsageSnapshot(9000, 0), price=seed.price)
        view = ledger.participant_view(db, enrollment_id=enrollment_id)
        assert view["consumed"] == 9000 and view["warning"] and not view["exhausted"]
        assert view["fraction_used"] == 0.9
        assert ledger.participant_view(db, enrollment_id=uuid.uuid4()) is None
        summary = ledger.study_spend_summary(db, seed.study_id)
        assert summary["metered_spend_micro_usd"] >= 9000
        assert summary["metered_calls"] >= 1
        page = ledger.list_reservations(db, enrollment_id, limit=10)
        assert page and page[0]["state"] == "SETTLED"
        assert len(ledger.list_study_balances(db, seed.study_id)) >= 1
