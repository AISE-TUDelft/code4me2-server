"""Participant budgets for studies on the shared provider key.

Mounted under ``/api/research/studies``. Study owners and administrators
manage the study default, apply it to participants still on the default, and
adjust (top up / set) individual limits with an audited reason and an
idempotency key. All amounts travel as decimal USD strings at the boundary and
are stored as integer micro-USD; the ledger (``research.budget.ledger``) owns
every mutation, so the limit is enforced exactly once, at reservation time.
"""

from __future__ import annotations

import re
import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from backend.routers.research.access import require_study_owner
from database.db_schemas import Study as StudyRow
from database.research_schemas import (
    ADJUSTMENT_APPLY_DEFAULT,
    ADJUSTMENT_SET_LIMIT,
    ADJUSTMENT_TOP_UP,
    LIMIT_SOURCE_ADJUSTED,
)
from research.analysis.study_analytics import store as analytics_store
from research.budget import ledger, study_policy
from research.budget.errors import (
    AdjustmentInvalid,
    BalanceMissing,
    IdempotencyConflict,
)
from research.budget.pricing import MICRO_USD_PER_USD, parse_usd_amount, usd_to_micro
from research.study.protocol import store as study_store

router = APIRouter()

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _detail(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def _authorize(db: Any, current_user: AuthenticatedUser, study_id: uuid.UUID):
    """404 for an unknown study, 403 ``FORBIDDEN_STUDY`` for a non-owner."""
    study = study_store.get_study(db, study_id)
    if study is None:
        raise HTTPException(status_code=404, detail="Study not found")
    require_study_owner(current_user, study)
    return db.get(StudyRow, study_id)


def _require_enrollment(db: Any, study_id: uuid.UUID, enrollment_id: uuid.UUID) -> None:
    if analytics_store.enrollment_study_id(db, enrollment_id) != study_id:
        raise HTTPException(
            status_code=404,
            detail=_detail("ENROLLMENT_NOT_FOUND", "no such enrollment in this study"),
        )


def _stopped(study: Any) -> bool:
    return getattr(study, "research_status", None) == "STUDY_STOPPED"


def _usd(micro: Optional[int]) -> Optional[str]:
    return study_policy.micro_to_usd_string(micro)


def _amount(value: Optional[str], *, field: str = "amount_usd") -> int:
    try:
        return usd_to_micro(parse_usd_amount(value))
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=_detail("BUDGET_INVALID", str(error), field=field),
        ) from error


def _reason(value: Optional[str]) -> str:
    text = (value or "").strip()
    if not text or len(text) > 500:
        raise HTTPException(
            status_code=422,
            detail=_detail(
                "REASON_REQUIRED", "a reason of 1-500 characters is required", field="reason"
            ),
        )
    return text


def _idempotency_key(value: Optional[str]) -> str:
    text = (value or "").strip()
    if not _IDEMPOTENCY_KEY.match(text):
        raise HTTPException(
            status_code=422,
            detail=_detail(
                "IDEMPOTENCY_KEY_INVALID",
                "idempotency_key must be 8-128 characters of [A-Za-z0-9_-]",
                field="idempotency_key",
            ),
        )
    return text


def _adjustment_payload(record) -> dict[str, Any]:
    return {
        "adjustment_id": str(record.adjustment_id),
        "enrollment_id": str(record.enrollment_id),
        "kind": record.kind,
        "delta_micro_usd": record.delta_micro_usd,
        "delta_usd": _usd(record.delta_micro_usd),
        "limit_before_micro_usd": record.limit_before_micro_usd,
        "limit_after_micro_usd": record.limit_after_micro_usd,
        "in_flight_micro_usd": record.in_flight_micro_usd,
        "reason": record.reason,
        "actor": record.actor,
        "idempotency_key": record.idempotency_key,
        "occurred_at": record.occurred_at.isoformat() if record.occurred_at else None,
    }


def _ledger_entry_payload(row: dict[str, Any]) -> dict[str, Any]:
    def iso(value: Any) -> Any:
        return value.isoformat() if isinstance(value, datetime) else value

    return {
        "reservation_id": str(row["reservation_id"]),
        "model": row["model"],
        "entry_point": row["entry_point"],
        "request_id": row["request_id"],
        "research_session_id": (
            str(row["research_session_id"]) if row["research_session_id"] else None
        ),
        "state": row["state"],
        "hold_micro_usd": row["hold_micro_usd"],
        "charged_micro_usd": row["charged_micro_usd"],
        "estimated_prompt_tokens": row["estimated_prompt_tokens"],
        "output_cap_tokens": row["output_cap_tokens"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "cached_prompt_tokens": row["cached_prompt_tokens"],
        "usage_source": row["usage_source"],
        "resolution_reason": row["resolution_reason"],
        "upstream_status": row["upstream_status"],
        "finish_reason": row["finish_reason"],
        "reserved_at": iso(row["reserved_at"]),
        "deadline_at": iso(row["deadline_at"]),
        "resolved_at": iso(row["resolved_at"]),
    }


def _cursor(value: Optional[str]) -> Optional[datetime]:
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise HTTPException(
            status_code=422, detail=_detail("INVALID_CURSOR", "cursor must be an ISO timestamp")
        ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Study-level policy
# ---------------------------------------------------------------------------


class StudyBudgetUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_budget_usd: Optional[str] = None
    warning_fraction: Optional[float] = Field(default=None, gt=0, le=1)


class ApplyDefaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str
    idempotency_key: str


def _study_budget_payload(db: Any, study: Any) -> dict[str, Any]:
    selections = study_policy.metered_selections(db, study.study_id)
    policy = study_policy.budget_policy_payload(study, selections)
    default = policy["default_budget_micro_usd"]
    balances = ledger.list_study_balances(db, study.study_id)
    on_default = [b for b in balances if b.limit_source != LIMIT_SOURCE_ADJUSTED]
    spend = ledger.study_spend_summary(db, study.study_id)
    missing = study_policy.missing_prices(db, selections)
    return {
        "study_id": str(study.study_id),
        **policy,
        "editable": not _stopped(study),
        "participants": {
            "total": len(balances),
            "on_default": sum(1 for b in on_default if b.limit_micro_usd == default),
            "on_old_default": sum(1 for b in on_default if b.limit_micro_usd != default),
            "custom": sum(1 for b in balances if b.limit_source == LIMIT_SOURCE_ADJUSTED),
            "exhausted": sum(1 for b in balances if b.exhausted),
        },
        "metered_spend_micro_usd": spend["metered_spend_micro_usd"],
        "metered_spend_usd": _usd(spend["metered_spend_micro_usd"]),
        "metered_calls": spend["metered_calls"],
        "reserved_micro_usd": spend["reserved_micro_usd"],
        "pricing": {"complete": not missing, "missing": missing},
    }


@router.get("/{study_id}/budget", summary="Study budget policy and totals (owner/admin)")
def get_study_budget(
    study_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        study = _authorize(db, current_user, study_id)
        return JsonResponseWithStatus(status_code=200, content=_study_budget_payload(db, study))
    finally:
        db.close()


@router.patch("/{study_id}/budget", summary="Update the default participant budget (owner/admin)")
def update_study_budget(
    study_id: uuid.UUID,
    payload: StudyBudgetUpdateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        study = _authorize(db, current_user, study_id)
        if _stopped(study):
            raise HTTPException(
                status_code=409, detail=_detail("STUDY_STOPPED", "the study has been stopped")
            )
        if not study_policy.metered_selections(db, study_id):
            raise HTTPException(
                status_code=409,
                detail=_detail(
                    "STUDY_NOT_METERED",
                    "no selected profile runs Goose or the built-in agent; budgets do not apply",
                ),
            )
        if payload.default_budget_usd is None and payload.warning_fraction is None:
            raise HTTPException(
                status_code=422,
                detail=_detail("BUDGET_INVALID", "nothing to update"),
            )
        if payload.default_budget_usd is not None:
            study.inference_budget_default_micro_usd = _amount(
                payload.default_budget_usd, field="default_budget_usd"
            )
        if payload.warning_fraction is not None:
            study.inference_budget_warning_fraction = Decimal(str(round(payload.warning_fraction, 3)))
        study.inference_budget_updated_at = _now()
        study.inference_budget_updated_by = current_user.email
        db.add(study)
        db.commit()
        db.refresh(study)
        return JsonResponseWithStatus(status_code=200, content=_study_budget_payload(db, study))
    finally:
        db.close()


@router.post(
    "/{study_id}/budget/apply-default",
    summary="Apply the current default to participants still on the old default (owner/admin)",
)
def apply_default_budget(
    study_id: uuid.UUID,
    payload: ApplyDefaultRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        study = _authorize(db, current_user, study_id)
        if _stopped(study):
            raise HTTPException(
                status_code=409, detail=_detail("STUDY_STOPPED", "the study has been stopped")
            )
        if not study_policy.metered_selections(db, study_id):
            raise HTTPException(
                status_code=409,
                detail=_detail("STUDY_NOT_METERED", "budgets do not apply to this study"),
            )
        reason = _reason(payload.reason)
        key = _idempotency_key(payload.idempotency_key)
        default = int(getattr(study, "inference_budget_default_micro_usd", 0) or 0)
        applied = skipped = 0
        for balance in ledger.list_study_balances(db, study_id):
            if balance.limit_source == LIMIT_SOURCE_ADJUSTED or balance.limit_micro_usd == default:
                skipped += 1
                continue
            try:
                record = ledger.adjust(
                    db,
                    enrollment_id=balance.enrollment_id,
                    kind=ADJUSTMENT_APPLY_DEFAULT,
                    amount_micro_usd=default,
                    actor=current_user.email,
                    reason=reason,
                    idempotency_key=f"{key}:{balance.enrollment_id}",
                )
            except IdempotencyConflict as error:
                raise HTTPException(
                    status_code=409,
                    detail=_detail("IDEMPOTENCY_KEY_REUSED", str(error)),
                ) from error
            if record is None:
                skipped += 1
            else:
                applied += 1
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "applied": applied,
                "skipped": skipped,
                "default_budget_micro_usd": default,
                "default_budget_usd": _usd(default),
            },
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Per-enrollment balance, adjustments and ledger
# ---------------------------------------------------------------------------


class AdjustmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    amount_usd: str
    reason: str
    idempotency_key: str


def _enrollment_budget_payload(db: Any, study: Any, enrollment_id: uuid.UUID) -> dict[str, Any]:
    view = ledger.balance_view(db, enrollment_id)
    warning_fraction = float(getattr(study, "inference_budget_warning_fraction", None) or 0.8)
    if view is None:
        return {
            "enrollment_id": str(enrollment_id),
            "metered": False,
            "balance": None,
            "ledger_summary": None,
            "recent_adjustments": [],
        }
    entries = ledger.list_reservations(db, enrollment_id, limit=200)
    settled_states = {"SETTLED", "FORFEITED", "EXPIRED"}
    return {
        "enrollment_id": str(enrollment_id),
        "metered": True,
        "balance": {
            **ledger.balance_summary(view),
            "limit_usd": _usd(view.limit_micro_usd),
            "consumed_usd": _usd(view.settled_micro_usd),
            "remaining_usd": _usd(max(view.available_micro_usd, 0)),
            "warning_fraction": warning_fraction,
            "settled_prompt_tokens": view.settled_prompt_tokens,
            "settled_completion_tokens": view.settled_completion_tokens,
        },
        "ledger_summary": {
            "calls": len(entries),
            "settled_micro_usd": sum(
                int(e["charged_micro_usd"] or 0) for e in entries if e["state"] == "SETTLED"
            ),
            "forfeited_micro_usd": sum(
                int(e["charged_micro_usd"] or 0)
                for e in entries
                if e["state"] in {"FORFEITED", "EXPIRED"}
            ),
            "voided": sum(1 for e in entries if e["state"] == "VOIDED"),
            "open": sum(1 for e in entries if e["state"] == "RESERVED"),
            "charged_micro_usd": sum(
                int(e["charged_micro_usd"] or 0) for e in entries if e["state"] in settled_states
            ),
            "last_call_at": entries[0]["reserved_at"].isoformat() if entries else None,
        },
        "recent_adjustments": [
            _adjustment_payload(item) for item in ledger.list_adjustments(db, enrollment_id, limit=10)
        ],
    }


@router.get(
    "/{study_id}/enrollments/{enrollment_id}/budget",
    summary="One participant's budget balance (owner/admin)",
)
def get_enrollment_budget(
    study_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        study = _authorize(db, current_user, study_id)
        _require_enrollment(db, study_id, enrollment_id)
        return JsonResponseWithStatus(
            status_code=200, content=_enrollment_budget_payload(db, study, enrollment_id)
        )
    finally:
        db.close()


@router.post(
    "/{study_id}/enrollments/{enrollment_id}/budget/adjustments",
    summary="Top up or set one participant's budget (owner/admin)",
)
def adjust_enrollment_budget(
    study_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    payload: AdjustmentRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        study = _authorize(db, current_user, study_id)
        _require_enrollment(db, study_id, enrollment_id)
        if _stopped(study):
            raise HTTPException(
                status_code=409, detail=_detail("STUDY_STOPPED", "the study has been stopped")
            )
        kind = (payload.kind or "").strip().upper()
        if kind not in (ADJUSTMENT_TOP_UP, ADJUSTMENT_SET_LIMIT):
            raise HTTPException(
                status_code=422,
                detail=_detail("BUDGET_INVALID", "kind must be TOP_UP or SET_LIMIT", field="kind"),
            )
        amount = _amount(payload.amount_usd)
        reason = _reason(payload.reason)
        key = _idempotency_key(payload.idempotency_key)
        if ledger.balance_view(db, enrollment_id) is None:
            raise HTTPException(
                status_code=409,
                detail=_detail("ENROLLMENT_NOT_METERED", "this participant has no budget balance"),
            )
        try:
            record = ledger.adjust(
                db,
                enrollment_id=enrollment_id,
                kind=kind,
                amount_micro_usd=amount,
                actor=current_user.email,
                reason=reason,
                idempotency_key=key,
            )
        except IdempotencyConflict as error:
            raise HTTPException(
                status_code=409, detail=_detail("IDEMPOTENCY_KEY_REUSED", str(error))
            ) from error
        except AdjustmentInvalid as error:
            raise HTTPException(
                status_code=422, detail=_detail("BUDGET_INVALID", str(error))
            ) from error
        except BalanceMissing as error:
            raise HTTPException(
                status_code=409, detail=_detail("ENROLLMENT_NOT_METERED", str(error))
            ) from error
        body = _enrollment_budget_payload(db, study, enrollment_id)
        return JsonResponseWithStatus(
            status_code=200 if record.replayed else 201,
            content={
                "adjustment": _adjustment_payload(record),
                "replayed": bool(record.replayed),
                "balance": body["balance"],
            },
        )
    finally:
        db.close()


@router.get(
    "/{study_id}/enrollments/{enrollment_id}/budget/ledger",
    summary="One participant's metered calls, newest first (owner/admin)",
)
def get_enrollment_ledger(
    study_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        _authorize(db, current_user, study_id)
        _require_enrollment(db, study_id, enrollment_id)
        rows = ledger.list_reservations(db, enrollment_id, limit=limit, before=_cursor(cursor))
        entries = [_ledger_entry_payload(row) for row in rows]
        next_cursor = entries[-1]["reserved_at"] if len(entries) == limit else None
        return JsonResponseWithStatus(
            status_code=200, content={"entries": entries, "next_cursor": next_cursor}
        )
    finally:
        db.close()


@router.get(
    "/{study_id}/enrollments/{enrollment_id}/budget/adjustments",
    summary="One participant's budget adjustments, newest first (owner/admin)",
)
def get_enrollment_adjustments(
    study_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        _authorize(db, current_user, study_id)
        _require_enrollment(db, study_id, enrollment_id)
        records = ledger.list_adjustments(db, enrollment_id, limit=limit, before=_cursor(cursor))
        items = [_adjustment_payload(record) for record in records]
        next_cursor = items[-1]["occurred_at"] if len(items) == limit else None
        return JsonResponseWithStatus(
            status_code=200, content={"adjustments": items, "next_cursor": next_cursor}
        )
    finally:
        db.close()
