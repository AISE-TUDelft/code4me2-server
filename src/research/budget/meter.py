"""The meter the relay endpoints wrap around one upstream inference call.

``reserve`` runs before the request is serialised (it caps the body in place);
exactly one ``resolve_*`` runs afterwards. All database work happens in the
threadpool on a fresh session (``App.get_db_session`` is a thread-local scoped
session, so the event-loop thread must never touch the ledger), and
resolutions are shielded from cancellation and never raise: the ledger's
``state = 'RESERVED'`` guard and the lazy expiry are the safety net.
"""

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from typing import Any, Callable, Optional

import anyio
import httpx
from fastapi import Response
from fastapi.concurrency import run_in_threadpool

from . import ledger, pricing
from .errors import BalanceMissing, BudgetRefused, PriceMissing
from .models import ModelPrice, Reservation
from .settings import BudgetSettings
from .usage import extract_usage_from_json, extract_usage_from_sse

__all__ = [
    "AVAILABLE_HEADER",
    "InferenceMeter",
    "budget_unavailable_response",
    "budget_unconfigured_response",
    "format_usd",
    "price_missing_response",
    "quota_refusal_response",
]

logger = logging.getLogger(__name__)

AVAILABLE_HEADER = "X-Inference-Budget-Available-Micro-USD"

# Nothing was sent to the provider, so nothing can have been billed.
_VOID_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
    httpx.InvalidURL,
    httpx.ProxyError,
)


def format_usd(micro_usd: int) -> str:
    amount = Decimal(int(micro_usd)) / pricing.MICRO_USD_PER_USD
    if amount != 0 and abs(amount) < Decimal("0.01"):
        return f"${amount:.4f}"
    return f"${amount:.2f}"


def quota_refusal_response(refused: BudgetRefused) -> Response:
    """HTTP 402 in the OpenAI error shape (Goose shows the message; it does not retry)."""
    available = max(refused.available_micro_usd, 0)
    message = (
        "Your study's AI budget is used up "
        f"(available {format_usd(available)}; this request needs at least "
        f"{format_usd(refused.needed_micro_usd)}). Ask the study team for a top-up."
    )
    body = {
        "error": {
            "message": message,
            "type": "insufficient_quota",
            "code": "quota_exhausted",
            "available_micro_usd": available,
            "needed_micro_usd": refused.needed_micro_usd,
        }
    }
    return Response(content=json.dumps(body), status_code=402, media_type="application/json")


def price_missing_response() -> Response:
    """HTTP 503: the arm's model has no price. Participant-facing: no model name."""
    body = {
        "error": {
            "message": (
                "The study's model has no budget price configured on its provider "
                "connection, so metered inference is refused. Ask the study team."
            ),
            "type": "server_error",
            "code": "price_missing",
        }
    }
    return Response(content=json.dumps(body), status_code=503, media_type="application/json")


def budget_unconfigured_response() -> Response:
    """HTTP 402: the enrollment has no balance row (fail closed, no amounts)."""
    body = {
        "error": {
            "message": (
                "No AI budget has been set up for this study enrollment yet, so the "
                "request was refused. Ask the study team."
            ),
            "type": "insufficient_quota",
            "code": "quota_exhausted",
        }
    }
    return Response(content=json.dumps(body), status_code=402, media_type="application/json")


def budget_unavailable_response() -> Response:
    """HTTP 503: the ledger could not be consulted; refuse rather than spend unmetered."""
    body = {
        "error": {
            "message": "The study budget could not be checked right now. Try again shortly.",
            "type": "server_error",
            "code": "budget_unavailable",
        }
    }
    return Response(content=json.dumps(body), status_code=503, media_type="application/json")


class InferenceMeter:
    """Reserve → forward → resolve for one call on one enrollment."""

    def __init__(
        self,
        *,
        app: Any,
        enrollment_id: uuid.UUID,
        study_id: uuid.UUID,
        connection_id: Optional[uuid.UUID],
        model: str,
        entry_point: str,
        research_session_id: Optional[uuid.UUID] = None,
        settings: Optional[BudgetSettings] = None,
        price: Optional[ModelPrice] = None,
    ) -> None:
        self.app = app
        self.enrollment_id = enrollment_id
        self.study_id = study_id
        self.connection_id = connection_id
        self.model = model
        self.entry_point = entry_point
        self.research_session_id = research_session_id
        self.settings = settings or BudgetSettings.from_env()
        self._price = price
        self._reservation: Optional[Reservation] = None
        self._resolved = False

    # -- state -------------------------------------------------------------

    @property
    def reservation(self) -> Optional[Reservation]:
        return self._reservation

    @property
    def resolved(self) -> bool:
        return self._resolved

    @property
    def available_after_reserve(self) -> Optional[int]:
        return None if self._reservation is None else self._reservation.available_after_micro_usd

    def response_headers(self) -> dict[str, str]:
        available = self.available_after_reserve
        return {} if available is None else {AVAILABLE_HEADER: str(available)}

    # -- reserve -----------------------------------------------------------

    def _reserve_sync(self, openai_body: dict, request_id: str, upstream_base_url: Optional[str]):
        db = self.app.get_db_session()
        try:
            if self._price is None:
                self._price = pricing.get_model_price(db, self.connection_id, self.model)
            if not ledger.ensure_balance(db, self.enrollment_id):
                raise BalanceMissing(self.enrollment_id)
            return ledger.reserve(
                db,
                enrollment_id=self.enrollment_id,
                study_id=self.study_id,
                connection_id=self.connection_id,
                model=self.model,
                entry_point=self.entry_point,
                request_id=request_id,
                openai_body=openai_body,
                upstream_base_url=upstream_base_url,
                price=self._price,
                settings=self.settings,
                research_session_id=self.research_session_id,
            )
        finally:
            db.close()

    async def reserve(
        self, openai_body: dict, *, request_id: str, upstream_base_url: Optional[str]
    ) -> Optional[Response]:
        """Hold the worst-case cost; return a refusal response or ``None`` on success."""
        try:
            self._reservation = await run_in_threadpool(
                self._reserve_sync, openai_body, request_id, upstream_base_url
            )
        except PriceMissing as exc:
            # The model name stays in the server log; the participant sees no arm detail.
            logger.error("[Budget] %s (entry_point=%s)", exc, self.entry_point)
            return price_missing_response()
        except BalanceMissing as exc:
            logger.error("[Budget] %s (entry_point=%s)", exc, self.entry_point)
            return budget_unconfigured_response()
        except BudgetRefused as refused:
            logger.info(
                "[Budget] refused enrollment=%s available=%d needed=%d entry_point=%s",
                self.enrollment_id,
                refused.available_micro_usd,
                refused.needed_micro_usd,
                self.entry_point,
            )
            return quota_refusal_response(refused)
        except Exception:  # noqa: BLE001 - never spend unmetered on a ledger failure
            logger.exception(
                "[Budget] reservation failed for enrollment=%s entry_point=%s; refusing",
                self.enrollment_id,
                self.entry_point,
            )
            return budget_unavailable_response()
        logger.info(
            "[Budget] reserved %d micro-USD (cap=%d tokens, est_in=%d) enrollment=%s request=%s",
            self._reservation.hold_micro_usd,
            self._reservation.output_cap_tokens,
            self._reservation.estimated_prompt_tokens,
            self.enrollment_id,
            request_id,
        )
        return None

    # -- resolve -----------------------------------------------------------

    async def _run_resolution(self, fn: Callable[[], Any], label: str) -> None:
        if self._reservation is None or self._resolved:
            return
        self._resolved = True
        with anyio.CancelScope(shield=True):
            try:
                await run_in_threadpool(fn)
            except Exception:  # noqa: BLE001 - never let bookkeeping break the response
                logger.exception(
                    "[Budget] %s failed for reservation %s; lazy expiry will charge the hold",
                    label,
                    self._reservation.reservation_id,
                )

    def _with_session(self, fn: Callable[[Any], Any]) -> Callable[[], Any]:
        def run():
            db = self.app.get_db_session()
            try:
                return fn(db)
            finally:
                db.close()

        return run

    async def resolve_usage(self, usage, *, upstream_status: Optional[int], missing_reason: str) -> None:
        reservation = self._reservation
        if reservation is None:
            return
        if usage is None or not usage.has_tokens:
            await self._run_resolution(
                self._with_session(
                    lambda db: ledger.forfeit(
                        db, reservation.reservation_id, reason=missing_reason,
                        upstream_status=upstream_status,
                    )
                ),
                "forfeit",
            )
            return
        price = self._price
        await self._run_resolution(
            self._with_session(
                lambda db: ledger.settle(
                    db, reservation.reservation_id, usage=usage, price=price,
                    upstream_status=upstream_status,
                )
            ),
            "settle",
        )

    async def resolve_stream(self, raw_sse: str, *, upstream_status: Optional[int]) -> None:
        """Settle from the buffered stream; no usage chunk means the hold is forfeited."""
        await self.resolve_usage(
            extract_usage_from_sse(raw_sse or ""),
            upstream_status=upstream_status,
            missing_reason="usage_missing",
        )

    async def resolve_response(self, body: Any, *, upstream_status: Optional[int]) -> None:
        await self.resolve_usage(
            extract_usage_from_json(body), upstream_status=upstream_status,
            missing_reason="usage_missing",
        )

    async def resolve_transport_error(self, error: BaseException) -> None:
        """Connect-phase failures are voided; anything after the request left is forfeited."""
        reservation = self._reservation
        if reservation is None:
            return
        reason = f"transport:{type(error).__name__}"
        if isinstance(error, _VOID_TRANSPORT_ERRORS):
            await self._run_resolution(
                self._with_session(
                    lambda db: ledger.void(db, reservation.reservation_id, reason=reason)
                ),
                "void",
            )
            return
        await self._run_resolution(
            self._with_session(
                lambda db: ledger.forfeit(db, reservation.reservation_id, reason=reason)
            ),
            "forfeit",
        )

    async def resolve_upstream_error(self, status: int) -> None:
        """An upstream 4xx/5xx before generation: nothing billed, release the hold."""
        reservation = self._reservation
        if reservation is None:
            return
        await self._run_resolution(
            self._with_session(
                lambda db: ledger.void(
                    db, reservation.reservation_id, reason="upstream_status",
                    upstream_status=int(status),
                )
            ),
            "void",
        )

    async def resolve_timeout(self) -> None:
        reservation = self._reservation
        if reservation is None:
            return
        await self._run_resolution(
            self._with_session(
                lambda db: ledger.forfeit(db, reservation.reservation_id, reason="stream_timeout")
            ),
            "forfeit",
        )
