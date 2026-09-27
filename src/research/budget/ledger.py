"""The budget ledger: reservations, settlement, adjustments and read models.

Every function takes a SQLAlchemy ``Session`` and owns its transaction: it
commits on success and rolls back on failure, so callers hand in a fresh
session and close it afterwards. Amounts are integer micro-USD.

The limit is enforced in exactly one place, the reservation statement

    UPDATE enrollment_inference_balance
       SET reserved_micro_usd = reserved_micro_usd + :hold
     WHERE enrollment_id = :e
       AND limit_micro_usd - settled_micro_usd - reserved_micro_usd >= :hold

executed after ``SELECT ... FOR UPDATE`` on the same row and committed together
with the ledger row. Concurrent reservers serialise on the row lock, the guard
is re-evaluated on the locked, current row, and a crash between the two
statements leaves neither. Holds are released only with proof that nothing was
generated (VOIDED); an unknown outcome charges the full hold (FORFEITED, or
EXPIRED when the next reservation finds a hold past its deadline).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database.research_schemas import (
    ADJUSTMENT_APPLY_DEFAULT,
    ADJUSTMENT_KINDS,
    ADJUSTMENT_SET_LIMIT,
    ADJUSTMENT_TOP_UP,
    BUDGET_UNIT_MICRO_USD,
    LIMIT_SOURCE_ADJUSTED,
    LIMIT_SOURCE_BACKFILL,
    LIMIT_SOURCE_STUDY_DEFAULT,
    RESERVATION_EXPIRED,
    RESERVATION_FORFEITED,
    RESERVATION_RESERVED,
    RESERVATION_SETTLED,
    RESERVATION_VOIDED,
)
from research.canonical import canonical_json

from .errors import AdjustmentInvalid, BalanceMissing, BudgetRefused, IdempotencyConflict
from .estimate import (
    apply_output_cap,
    client_output_cap,
    estimate_prompt_tokens,
    minimal_call_micro_usd,
    plan_hold,
)
from .models import (
    AdjustmentRecord,
    BalanceView,
    ModelPrice,
    Reservation,
    SettlementOutcome,
    UsageSnapshot,
)
from .pricing import charge_for_usage
from .settings import BudgetSettings

__all__ = [
    "adjust",
    "balance_summary",
    "balance_view",
    "create_balance",
    "ensure_balance",
    "expire_stale_reservations",
    "forfeit",
    "list_adjustments",
    "list_reservations",
    "list_study_balances",
    "participant_view",
    "participant_views",
    "reserve",
    "settle",
    "study_spend_summary",
    "void",
]

logger = logging.getLogger(__name__)

_USAGE_SOURCE_FORFEIT = "hold_forfeit"


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Balance rows
# ---------------------------------------------------------------------------


def create_balance(
    db,
    *,
    enrollment_id: uuid.UUID,
    study_id: uuid.UUID,
    limit_micro_usd: int,
    limit_source: str = LIMIT_SOURCE_STUDY_DEFAULT,
    now: Optional[datetime] = None,
    commit: bool = False,
) -> None:
    """Insert a balance row inside the caller's transaction (enrollment creation)."""
    if int(limit_micro_usd) < 0:
        raise AdjustmentInvalid("a budget limit cannot be negative")
    timestamp = _now(now)
    db.execute(
        text(
            """
            INSERT INTO public.enrollment_inference_balance
                (enrollment_id, study_id, unit, limit_micro_usd, limit_source, created_at, updated_at)
            VALUES (:enrollment_id, :study_id, :unit, :limit, :source, :now, :now)
            ON CONFLICT (enrollment_id) DO NOTHING
            """
        ),
        {
            "enrollment_id": enrollment_id,
            "study_id": study_id,
            "unit": BUDGET_UNIT_MICRO_USD,
            "limit": int(limit_micro_usd),
            "source": limit_source,
            "now": timestamp,
        },
    )
    if commit:
        db.commit()


def ensure_balance(db, enrollment_id: uuid.UUID, *, now: Optional[datetime] = None) -> bool:
    """Create the balance row from the study default when missing; return whether it exists."""
    timestamp = _now(now)
    try:
        db.execute(
            text(
                """
                INSERT INTO public.enrollment_inference_balance
                    (enrollment_id, study_id, unit, limit_micro_usd, limit_source, created_at, updated_at)
                SELECT e.enrollment_id, e.study_id, :unit, s.inference_budget_default_micro_usd,
                       :source, :now, :now
                  FROM public.research_enrollment e
                  JOIN public.study s ON s.study_id = e.study_id
                 WHERE e.enrollment_id = :enrollment_id
                ON CONFLICT (enrollment_id) DO NOTHING
                """
            ),
            {
                "enrollment_id": enrollment_id,
                "unit": BUDGET_UNIT_MICRO_USD,
                "source": LIMIT_SOURCE_BACKFILL,
                "now": timestamp,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    exists = db.execute(
        text(
            "SELECT 1 FROM public.enrollment_inference_balance WHERE enrollment_id = :enrollment_id"
        ),
        {"enrollment_id": enrollment_id},
    ).scalar_one_or_none()
    return exists is not None


def _lock_balance(db, enrollment_id: uuid.UUID) -> Optional[dict]:
    row = (
        db.execute(
            text(
                """
                SELECT enrollment_id, study_id, limit_micro_usd, settled_micro_usd,
                       reserved_micro_usd, limit_source, exhausted_at
                  FROM public.enrollment_inference_balance
                 WHERE enrollment_id = :enrollment_id
                 FOR UPDATE
                """
            ),
            {"enrollment_id": enrollment_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def _expire_locked(db, enrollment_id: uuid.UUID, now: datetime) -> int:
    """Charge every hold past its deadline in full. Caller holds the row lock."""
    total = db.execute(
        text(
            """
            WITH expired AS (
                UPDATE public.inference_reservation
                   SET state = :expired,
                       charged_micro_usd = hold_micro_usd,
                       usage_source = :source,
                       resolution_reason = 'expired',
                       resolved_at = :now
                 WHERE enrollment_id = :enrollment_id
                   AND state = :reserved
                   AND deadline_at < :now
             RETURNING hold_micro_usd
            )
            SELECT coalesce(sum(hold_micro_usd), 0) FROM expired
            """
        ),
        {
            "enrollment_id": enrollment_id,
            "expired": RESERVATION_EXPIRED,
            "reserved": RESERVATION_RESERVED,
            "source": _USAGE_SOURCE_FORFEIT,
            "now": now,
        },
    ).scalar_one()
    total = int(total or 0)
    if total > 0:
        db.execute(
            text(
                """
                UPDATE public.enrollment_inference_balance
                   SET settled_micro_usd = settled_micro_usd + :total,
                       reserved_micro_usd = reserved_micro_usd - :total,
                       updated_at = :now
                 WHERE enrollment_id = :enrollment_id
                """
            ),
            {"enrollment_id": enrollment_id, "total": total, "now": now},
        )
        logger.warning(
            "[Budget] expired %d micro-USD of stale holds for enrollment %s",
            total,
            enrollment_id,
        )
    return total


def expire_stale_reservations(
    db, enrollment_id: uuid.UUID, *, now: Optional[datetime] = None
) -> int:
    """Standalone lazy expiry (also run inside every reservation)."""
    timestamp = _now(now)
    try:
        if _lock_balance(db, enrollment_id) is None:
            db.rollback()
            return 0
        total = _expire_locked(db, enrollment_id, timestamp)
        db.commit()
        return total
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# Reservation
# ---------------------------------------------------------------------------


def reserve(
    db,
    *,
    enrollment_id: uuid.UUID,
    study_id: uuid.UUID,
    connection_id: Optional[uuid.UUID],
    model: str,
    entry_point: str,
    request_id: str,
    openai_body: dict,
    upstream_base_url: Optional[str],
    price: ModelPrice,
    settings: BudgetSettings,
    research_session_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
) -> Reservation:
    """Hold the worst-case cost of ``openai_body`` or raise :class:`BudgetRefused`.

    On success the body has been capped in place (``apply_output_cap``) and a
    ``RESERVED`` ledger row exists. On refusal nothing is reserved (only
    ``refused_count`` is bumped), so a client retry of a 402 costs nothing.
    """
    timestamp = _now(now)
    estimated = estimate_prompt_tokens(openai_body, settings)
    client_cap = client_output_cap(openai_body)
    try:
        row = _lock_balance(db, enrollment_id)
        if row is None:
            raise BalanceMissing(enrollment_id)
        expired = _expire_locked(db, enrollment_id, timestamp)
        limit = int(row["limit_micro_usd"])
        settled = int(row["settled_micro_usd"]) + expired
        reserved = int(row["reserved_micro_usd"]) - expired
        available = limit - settled - reserved
        plan = plan_hold(
            price,
            available_micro_usd=available,
            estimated_prompt_tokens=estimated,
            client_cap=client_cap,
            settings=settings,
        )
        # ``call_count`` counts admitted calls (a later VOID keeps the count:
        # the call was sent); refusals are counted separately.
        available_after = db.execute(
            text(
                """
                UPDATE public.enrollment_inference_balance
                   SET reserved_micro_usd = reserved_micro_usd + :hold,
                       call_count = call_count + 1,
                       last_call_at = :now,
                       exhausted_at = NULL,
                       updated_at = :now
                 WHERE enrollment_id = :enrollment_id
                   AND limit_micro_usd - settled_micro_usd - reserved_micro_usd >= :hold
             RETURNING limit_micro_usd - settled_micro_usd - reserved_micro_usd
                """
            ),
            {"enrollment_id": enrollment_id, "hold": plan.hold_micro_usd, "now": timestamp},
        ).scalar_one_or_none()
        if available_after is None:
            raise BudgetRefused(available_micro_usd=available, needed_micro_usd=plan.hold_micro_usd)
        reservation_id = uuid.uuid4()
        deadline = timestamp + timedelta(seconds=settings.reservation_deadline_seconds)
        db.execute(
            text(
                """
                INSERT INTO public.inference_reservation
                    (reservation_id, enrollment_id, study_id, connection_id, model, entry_point,
                     request_id, research_session_id, state, hold_micro_usd,
                     estimated_prompt_tokens, output_cap_tokens, reserved_at, deadline_at)
                VALUES (:reservation_id, :enrollment_id, :study_id, :connection_id, :model,
                        :entry_point, :request_id, :research_session_id, :state, :hold,
                        :estimated, :cap, :now, :deadline)
                """
            ),
            {
                "reservation_id": reservation_id,
                "enrollment_id": enrollment_id,
                "study_id": study_id,
                "connection_id": connection_id,
                "model": model,
                "entry_point": entry_point,
                "request_id": request_id,
                "research_session_id": research_session_id,
                "state": RESERVATION_RESERVED,
                "hold": plan.hold_micro_usd,
                "estimated": plan.estimated_prompt_tokens,
                "cap": plan.output_cap_tokens,
                "now": timestamp,
                "deadline": deadline,
            },
        )
        db.commit()
    except BudgetRefused as refused:
        db.rollback()
        _record_refusal(db, enrollment_id, refused, price, settings, timestamp)
        raise
    except Exception:
        db.rollback()
        raise
    apply_output_cap(openai_body, plan.output_cap_tokens, upstream_base_url=upstream_base_url)
    return Reservation(
        reservation_id=reservation_id,
        enrollment_id=enrollment_id,
        study_id=study_id,
        hold_micro_usd=plan.hold_micro_usd,
        estimated_prompt_tokens=plan.estimated_prompt_tokens,
        output_cap_tokens=plan.output_cap_tokens,
        available_after_micro_usd=int(available_after),
        deadline_at=deadline,
    )


def _record_refusal(
    db,
    enrollment_id: uuid.UUID,
    refused: BudgetRefused,
    price: ModelPrice,
    settings: BudgetSettings,
    now: datetime,
) -> None:
    """Count the refusal; mark exhausted when not even a minimal call fits."""
    exhausted = refused.available_micro_usd < minimal_call_micro_usd(price, settings)
    try:
        db.execute(
            text(
                """
                UPDATE public.enrollment_inference_balance
                   SET refused_count = refused_count + 1,
                       exhausted_at = CASE WHEN :exhausted THEN coalesce(exhausted_at, :now)
                                           ELSE exhausted_at END,
                       updated_at = :now
                 WHERE enrollment_id = :enrollment_id
                """
            ),
            {"enrollment_id": enrollment_id, "exhausted": exhausted, "now": now},
        )
        db.commit()
    except Exception:  # noqa: BLE001 - bookkeeping only; the refusal already stands
        db.rollback()
        logger.exception("[Budget] could not record refusal for enrollment %s", enrollment_id)


# ---------------------------------------------------------------------------
# Resolution: settle / forfeit / void
# ---------------------------------------------------------------------------


def _resolve(
    db,
    reservation_id: uuid.UUID,
    *,
    state: str,
    charged_micro_usd: Optional[int],
    charge_full_hold: bool,
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    cached_prompt_tokens: Optional[int],
    usage_source: Optional[str],
    resolution_reason: Optional[str],
    upstream_status: Optional[int],
    finish_reason: Optional[str],
    now: datetime,
) -> SettlementOutcome:
    charged_sql = "hold_micro_usd" if charge_full_hold else ":charged"
    try:
        # Same lock order as ``reserve`` (balance row first, then the
        # reservation rows), so a settlement racing a lazy expiry of the same
        # hold serialises instead of deadlocking.
        owner = db.execute(
            text(
                "SELECT enrollment_id FROM public.inference_reservation WHERE reservation_id = :reservation_id"
            ),
            {"reservation_id": reservation_id},
        ).scalar_one_or_none()
        if owner is None:
            db.rollback()
            logger.warning("[Budget] reservation %s does not exist; %s ignored", reservation_id, state)
            return SettlementOutcome(
                reservation_id=reservation_id, state=state, charged_micro_usd=0, applied=False
            )
        _lock_balance(db, owner)
        row = (
            db.execute(
                text(
                    f"""
                    UPDATE public.inference_reservation
                       SET state = :state,
                           charged_micro_usd = {charged_sql},
                           prompt_tokens = :prompt_tokens,
                           completion_tokens = :completion_tokens,
                           cached_prompt_tokens = :cached_prompt_tokens,
                           usage_source = :usage_source,
                           resolution_reason = :resolution_reason,
                           upstream_status = :upstream_status,
                           finish_reason = :finish_reason,
                           resolved_at = :now
                     WHERE reservation_id = :reservation_id
                       AND state = :reserved
                 RETURNING enrollment_id, hold_micro_usd, charged_micro_usd
                    """
                ),
                {
                    "reservation_id": reservation_id,
                    "state": state,
                    "charged": int(charged_micro_usd or 0),
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cached_prompt_tokens": cached_prompt_tokens,
                    "usage_source": usage_source,
                    "resolution_reason": resolution_reason,
                    "upstream_status": upstream_status,
                    "finish_reason": finish_reason,
                    "now": now,
                    "reserved": RESERVATION_RESERVED,
                },
            )
            .mappings()
            .first()
        )
        if row is None:
            db.commit()
            logger.warning(
                "[Budget] reservation %s already resolved; %s ignored", reservation_id, state
            )
            return SettlementOutcome(
                reservation_id=reservation_id, state=state, charged_micro_usd=0, applied=False
            )
        hold = int(row["hold_micro_usd"])
        charged = int(row["charged_micro_usd"] or 0)
        db.execute(
            text(
                """
                UPDATE public.enrollment_inference_balance
                   SET reserved_micro_usd = reserved_micro_usd - :hold,
                       settled_micro_usd = settled_micro_usd + :charged,
                       settled_prompt_tokens = settled_prompt_tokens + :prompt_tokens,
                       settled_completion_tokens = settled_completion_tokens + :completion_tokens,
                       exhausted_at = CASE
                           WHEN limit_micro_usd - (settled_micro_usd + :charged)
                                - (reserved_micro_usd - :hold) <= 0
                           THEN coalesce(exhausted_at, :now)
                           ELSE NULL END,
                       updated_at = :now
                 WHERE enrollment_id = :enrollment_id
                """
            ),
            {
                "enrollment_id": row["enrollment_id"],
                "hold": hold,
                "charged": charged,
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "now": now,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return SettlementOutcome(
        reservation_id=reservation_id, state=state, charged_micro_usd=charged, applied=True
    )


def settle(
    db,
    reservation_id: uuid.UUID,
    *,
    usage: UsageSnapshot,
    price: ModelPrice,
    upstream_status: Optional[int] = None,
    now: Optional[datetime] = None,
) -> SettlementOutcome:
    """Replace the hold by the actual charge (which may exceed the hold)."""
    charged, source = charge_for_usage(price, usage)
    return _resolve(
        db,
        reservation_id,
        state=RESERVATION_SETTLED,
        charged_micro_usd=charged,
        charge_full_hold=False,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cached_prompt_tokens=usage.cached_prompt_tokens,
        usage_source=source,
        resolution_reason="settled",
        upstream_status=upstream_status,
        finish_reason=usage.finish_reason,
        now=_now(now),
    )


def forfeit(
    db,
    reservation_id: uuid.UUID,
    *,
    reason: str,
    upstream_status: Optional[int] = None,
    now: Optional[datetime] = None,
) -> SettlementOutcome:
    """Outcome unknown: charge the full hold."""
    return _resolve(
        db,
        reservation_id,
        state=RESERVATION_FORFEITED,
        charged_micro_usd=None,
        charge_full_hold=True,
        prompt_tokens=None,
        completion_tokens=None,
        cached_prompt_tokens=None,
        usage_source=_USAGE_SOURCE_FORFEIT,
        resolution_reason=reason,
        upstream_status=upstream_status,
        finish_reason=None,
        now=_now(now),
    )


def void(
    db,
    reservation_id: uuid.UUID,
    *,
    reason: str,
    upstream_status: Optional[int] = None,
    now: Optional[datetime] = None,
) -> SettlementOutcome:
    """Proof that nothing was generated: release the hold."""
    return _resolve(
        db,
        reservation_id,
        state=RESERVATION_VOIDED,
        charged_micro_usd=0,
        charge_full_hold=False,
        prompt_tokens=None,
        completion_tokens=None,
        cached_prompt_tokens=None,
        usage_source=None,
        resolution_reason=reason,
        upstream_status=upstream_status,
        finish_reason=None,
        now=_now(now),
    )


# ---------------------------------------------------------------------------
# Adjustments
# ---------------------------------------------------------------------------


def _adjustment_from_row(row: Any, *, replayed: bool = False) -> AdjustmentRecord:
    return AdjustmentRecord(
        adjustment_id=row["adjustment_id"],
        enrollment_id=row["enrollment_id"],
        study_id=row["study_id"],
        kind=row["kind"],
        delta_micro_usd=int(row["delta_micro_usd"]),
        limit_before_micro_usd=int(row["limit_before_micro_usd"]),
        limit_after_micro_usd=int(row["limit_after_micro_usd"]),
        in_flight_micro_usd=int(row["in_flight_micro_usd"]),
        reason=row["reason"],
        actor=row["actor"],
        idempotency_key=row["idempotency_key"],
        occurred_at=row["occurred_at"],
        replayed=replayed,
    )


def _find_adjustment(db, study_id: uuid.UUID, idempotency_key: str) -> Optional[dict]:
    row = (
        db.execute(
            text(
                """
                SELECT * FROM public.inference_budget_adjustment
                 WHERE study_id = :study_id AND idempotency_key = :key
                """
            ),
            {"study_id": study_id, "key": idempotency_key},
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def _request_digest(enrollment_id: uuid.UUID, kind: str, amount: int, reason: Optional[str]) -> str:
    payload = canonical_json(
        {"enrollment_id": str(enrollment_id), "kind": kind, "amount": int(amount), "reason": reason}
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def adjust(
    db,
    *,
    enrollment_id: uuid.UUID,
    kind: str,
    amount_micro_usd: int,
    actor: Optional[str],
    reason: Optional[str],
    idempotency_key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[AdjustmentRecord]:
    """Top up, set or apply-default the limit of one enrollment.

    ``TOP_UP`` adds ``amount`` (> 0); ``SET_LIMIT`` sets the limit to ``amount``
    (>= 0; lowering below committed spend is allowed and simply stops further
    calls); ``APPLY_DEFAULT`` sets the limit to ``amount`` only for rows still
    on the study default — ``STUDY_DEFAULT`` and ``BACKFILL`` rows, which were
    filled from the default too — (manually adjusted rows keep theirs) and
    returns ``None`` when nothing changed. A reused idempotency key replays the
    original adjustment (``replayed=True``) or raises
    :class:`IdempotencyConflict` when the body differs.
    """
    if kind not in (ADJUSTMENT_TOP_UP, ADJUSTMENT_SET_LIMIT, ADJUSTMENT_APPLY_DEFAULT):
        raise AdjustmentInvalid(f"unsupported adjustment kind {kind!r}")
    amount = int(amount_micro_usd)
    if kind == ADJUSTMENT_TOP_UP and amount <= 0:
        raise AdjustmentInvalid("a top-up must be positive")
    if kind != ADJUSTMENT_TOP_UP and amount < 0:
        raise AdjustmentInvalid("a budget limit cannot be negative")
    timestamp = _now(now)
    digest = _request_digest(enrollment_id, kind, amount, reason)
    try:
        row = _lock_balance(db, enrollment_id)
        if row is None:
            raise BalanceMissing(enrollment_id)
        study_id = row["study_id"]
        if idempotency_key:
            existing = _find_adjustment(db, study_id, idempotency_key)
            if existing is not None:
                db.rollback()
                if existing.get("request_digest") != digest:
                    raise IdempotencyConflict(
                        f"idempotency key {idempotency_key!r} was used with a different request"
                    )
                return _adjustment_from_row(existing, replayed=True)
        before = int(row["limit_micro_usd"])
        if kind == ADJUSTMENT_TOP_UP:
            after = before + amount
            source = LIMIT_SOURCE_ADJUSTED
        elif kind == ADJUSTMENT_SET_LIMIT:
            after = amount
            source = LIMIT_SOURCE_ADJUSTED
        else:
            if row["limit_source"] == LIMIT_SOURCE_ADJUSTED or before == amount:
                db.rollback()
                return None
            after = amount
            source = LIMIT_SOURCE_STUDY_DEFAULT
        in_flight = int(row["reserved_micro_usd"])
        db.execute(
            text(
                """
                UPDATE public.enrollment_inference_balance
                   SET limit_micro_usd = :after,
                       limit_source = :source,
                       exhausted_at = CASE
                           WHEN :after - settled_micro_usd - reserved_micro_usd > 0 THEN NULL
                           ELSE coalesce(exhausted_at, :now) END,
                       updated_at = :now
                 WHERE enrollment_id = :enrollment_id
                """
            ),
            {"enrollment_id": enrollment_id, "after": after, "source": source, "now": timestamp},
        )
        adjustment_id = uuid.uuid4()
        db.execute(
            text(
                """
                INSERT INTO public.inference_budget_adjustment
                    (adjustment_id, enrollment_id, study_id, kind, delta_micro_usd,
                     limit_before_micro_usd, limit_after_micro_usd, in_flight_micro_usd,
                     reason, actor, idempotency_key, request_digest, occurred_at)
                VALUES (:adjustment_id, :enrollment_id, :study_id, :kind, :delta, :before, :after,
                        :in_flight, :reason, :actor, :key, :digest, :now)
                """
            ),
            {
                "adjustment_id": adjustment_id,
                "enrollment_id": enrollment_id,
                "study_id": study_id,
                "kind": kind,
                "delta": after - before,
                "before": before,
                "after": after,
                "in_flight": in_flight,
                "reason": reason,
                "actor": actor,
                "key": idempotency_key,
                "digest": digest,
                "now": timestamp,
            },
        )
        db.commit()
    except IntegrityError:
        # Two identical requests raced on the same idempotency key: replay.
        db.rollback()
        if idempotency_key:
            existing = _find_adjustment(db, study_id, idempotency_key)
            if existing is not None and existing.get("request_digest") == digest:
                return _adjustment_from_row(existing, replayed=True)
        raise
    except Exception:
        db.rollback()
        raise
    return AdjustmentRecord(
        adjustment_id=adjustment_id,
        enrollment_id=enrollment_id,
        study_id=study_id,
        kind=kind,
        delta_micro_usd=after - before,
        limit_before_micro_usd=before,
        limit_after_micro_usd=after,
        in_flight_micro_usd=in_flight,
        reason=reason,
        actor=actor,
        idempotency_key=idempotency_key,
        occurred_at=timestamp,
    )


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------

_BALANCE_COLUMNS = """
    enrollment_id, study_id, unit, limit_micro_usd, settled_micro_usd, reserved_micro_usd,
    settled_prompt_tokens, settled_completion_tokens, call_count, refused_count, last_call_at,
    limit_source, exhausted_at, updated_at
"""


def _balance_from_row(row: Any) -> BalanceView:
    return BalanceView(
        enrollment_id=row["enrollment_id"],
        study_id=row["study_id"],
        unit=row["unit"],
        limit_micro_usd=int(row["limit_micro_usd"]),
        settled_micro_usd=int(row["settled_micro_usd"]),
        reserved_micro_usd=int(row["reserved_micro_usd"]),
        settled_prompt_tokens=int(row["settled_prompt_tokens"]),
        settled_completion_tokens=int(row["settled_completion_tokens"]),
        call_count=int(row["call_count"]),
        refused_count=int(row["refused_count"]),
        last_call_at=row["last_call_at"],
        limit_source=row["limit_source"],
        exhausted_at=row["exhausted_at"],
        updated_at=row["updated_at"],
    )


def balance_view(db, enrollment_id: uuid.UUID) -> Optional[BalanceView]:
    row = (
        db.execute(
            text(
                f"SELECT {_BALANCE_COLUMNS} FROM public.enrollment_inference_balance "
                "WHERE enrollment_id = :enrollment_id"
            ),
            {"enrollment_id": enrollment_id},
        )
        .mappings()
        .first()
    )
    return _balance_from_row(row) if row is not None else None


def balance_summary(view: BalanceView) -> dict:
    """Researcher-facing balance numbers (micro-USD integers)."""
    return {
        "unit": view.unit,
        "limit_micro_usd": view.limit_micro_usd,
        "consumed_micro_usd": view.settled_micro_usd,
        "reserved_micro_usd": view.reserved_micro_usd,
        "remaining_micro_usd": max(view.available_micro_usd, 0),
        "available_micro_usd": view.available_micro_usd,
        "limit_source": view.limit_source,
        "call_count": view.call_count,
        "refused_count": view.refused_count,
        "last_call_at": view.last_call_at.isoformat() if view.last_call_at else None,
        "exhausted": view.exhausted,
        "exhausted_at": view.exhausted_at.isoformat() if view.exhausted_at else None,
        "updated_at": view.updated_at.isoformat() if view.updated_at else None,
    }


def list_study_balances(db, study_id: uuid.UUID) -> list[BalanceView]:
    rows = (
        db.execute(
            text(
                f"SELECT {_BALANCE_COLUMNS} FROM public.enrollment_inference_balance "
                "WHERE study_id = :study_id ORDER BY updated_at DESC"
            ),
            {"study_id": study_id},
        )
        .mappings()
        .all()
    )
    return [_balance_from_row(row) for row in rows]


def participant_view(
    db, *, enrollment_id: uuid.UUID, now: Optional[datetime] = None
) -> Optional[dict]:
    """The arm-blind ``budget`` block shown to the participant and the plugin.

    Numbers only (unit, limit, consumed, reserved, remaining, fraction used,
    warning/exhausted flags); never a model, price or profile name.
    """
    row = (
        db.execute(
            text(
                """
                SELECT b.unit, b.limit_micro_usd, b.settled_micro_usd, b.reserved_micro_usd,
                       b.exhausted_at, b.updated_at, s.inference_budget_warning_fraction
                  FROM public.enrollment_inference_balance b
                  JOIN public.study s ON s.study_id = b.study_id
                 WHERE b.enrollment_id = :enrollment_id
                """
            ),
            {"enrollment_id": enrollment_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return _participant_block(row, _now(now))


def participant_views(
    db, enrollment_ids, *, now: Optional[datetime] = None
) -> dict[uuid.UUID, Optional[dict]]:
    """``participant_view`` for several enrollments (one query)."""
    ids = list(enrollment_ids)
    if not ids:
        return {}
    rows = (
        db.execute(
            text(
                """
                SELECT b.enrollment_id, b.unit, b.limit_micro_usd, b.settled_micro_usd,
                       b.reserved_micro_usd, b.exhausted_at, b.updated_at,
                       s.inference_budget_warning_fraction
                  FROM public.enrollment_inference_balance b
                  JOIN public.study s ON s.study_id = b.study_id
                 WHERE b.enrollment_id = ANY(:ids)
                """
            ),
            {"ids": ids},
        )
        .mappings()
        .all()
    )
    timestamp = _now(now)
    result: dict[uuid.UUID, Optional[dict]] = {enrollment_id: None for enrollment_id in ids}
    for row in rows:
        result[row["enrollment_id"]] = _participant_block(row, timestamp)
    return result


def _participant_block(row: Any, now: datetime) -> dict:
    limit = int(row["limit_micro_usd"])
    consumed = int(row["settled_micro_usd"])
    reserved = int(row["reserved_micro_usd"])
    available = limit - consumed - reserved
    committed = consumed + reserved
    fraction_used = 1.0 if limit <= 0 else min(1.0, committed / limit)
    warning_fraction = float(row["inference_budget_warning_fraction"] or 0)
    exhausted = row["exhausted_at"] is not None or available <= 0
    return {
        "unit": row["unit"],
        "limit": limit,
        "consumed": consumed,
        "reserved": reserved,
        "remaining": max(available, 0),
        "fraction_used": round(fraction_used, 4),
        "warning_fraction": warning_fraction,
        "warning": bool(exhausted or fraction_used >= warning_fraction),
        "exhausted": bool(exhausted),
        "exhausted_at": row["exhausted_at"].isoformat() if row["exhausted_at"] else None,
        "as_of": now.isoformat(),
    }


def study_spend_summary(db, study_id: uuid.UUID) -> dict:
    """Exact metered spend of a study from the ledger (settled + forfeited + expired)."""
    row = (
        db.execute(
            text(
                """
                SELECT coalesce(sum(charged_micro_usd), 0) AS spend,
                       count(*) FILTER (WHERE state <> :voided) AS calls,
                       coalesce(sum(prompt_tokens), 0) AS prompt_tokens,
                       coalesce(sum(completion_tokens), 0) AS completion_tokens
                  FROM public.inference_reservation
                 WHERE study_id = :study_id
                   AND state IN (:settled, :forfeited, :expired)
                """
            ),
            {
                "study_id": study_id,
                "voided": RESERVATION_VOIDED,
                "settled": RESERVATION_SETTLED,
                "forfeited": RESERVATION_FORFEITED,
                "expired": RESERVATION_EXPIRED,
            },
        )
        .mappings()
        .one()
    )
    open_holds = db.execute(
        text(
            "SELECT coalesce(sum(reserved_micro_usd), 0) FROM public.enrollment_inference_balance "
            "WHERE study_id = :study_id"
        ),
        {"study_id": study_id},
    ).scalar_one()
    return {
        "metered_spend_micro_usd": int(row["spend"]),
        "metered_calls": int(row["calls"]),
        "reserved_micro_usd": int(open_holds or 0),
        "prompt_tokens": int(row["prompt_tokens"]),
        "completion_tokens": int(row["completion_tokens"]),
    }


def list_reservations(
    db,
    enrollment_id: uuid.UUID,
    *,
    limit: int = 50,
    before: Optional[datetime] = None,
) -> list[dict]:
    """Newest-first ledger page for one enrollment (``before`` = cursor)."""
    rows = (
        db.execute(
            text(
                """
                SELECT reservation_id, model, entry_point, request_id, research_session_id, state,
                       hold_micro_usd, estimated_prompt_tokens, output_cap_tokens, charged_micro_usd,
                       prompt_tokens, completion_tokens, cached_prompt_tokens, usage_source,
                       resolution_reason, upstream_status, finish_reason, reserved_at, deadline_at,
                       resolved_at
                  FROM public.inference_reservation
                 WHERE enrollment_id = :enrollment_id
                   AND (CAST(:before AS timestamptz) IS NULL OR reserved_at < :before)
                 ORDER BY reserved_at DESC, reservation_id DESC
                 LIMIT :limit
                """
            ),
            {"enrollment_id": enrollment_id, "before": before, "limit": max(1, min(int(limit), 200))},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def list_adjustments(
    db,
    enrollment_id: uuid.UUID,
    *,
    limit: int = 50,
    before: Optional[datetime] = None,
) -> list[AdjustmentRecord]:
    rows = (
        db.execute(
            text(
                """
                SELECT * FROM public.inference_budget_adjustment
                 WHERE enrollment_id = :enrollment_id
                   AND (CAST(:before AS timestamptz) IS NULL OR occurred_at < :before)
                 ORDER BY occurred_at DESC, adjustment_id DESC
                 LIMIT :limit
                """
            ),
            {"enrollment_id": enrollment_id, "before": before, "limit": max(1, min(int(limit), 200))},
        )
        .mappings()
        .all()
    )
    return [_adjustment_from_row(dict(row)) for row in rows]


# Exported for callers that need the vocabulary without importing the ORM module.
KINDS = ADJUSTMENT_KINDS
