"""Operational health aggregation and alert thresholds (Issue 13).

Health is computed from injected metadata inputs only. A monitoring outage marks
the affected window ``UNKNOWN`` and never invents health: no metric is inferred
from a missing observation, and missing coverage stays visible.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, ConfigDict, Field

from .enums import HealthSignalState, OperationsReasonCode
from .models import HealthSignal, OperationalHealthV1

if TYPE_CHECKING:
    from uuid import UUID

__all__ = [
    "HealthInputs",
    "HealthThresholds",
    "aggregate_health",
    "health_summary",
]

_BASE = ConfigDict(extra="forbid")

_MONITORING_OUTAGE_REASON = "monitoring outage: health is unknown for this window"


class HealthThresholds(BaseModel):
    """Threshold configuration for the operational signals."""

    model_config = _BASE

    spool_depth_degraded: int = 1_000
    spool_depth_critical: int = 10_000
    spool_age_degraded_seconds: int = 3_600
    spool_age_critical_seconds: int = 86_400
    # ACK rate is a ratio; lower is worse.
    upload_ack_rate_degraded: float = 0.99
    upload_ack_rate_critical: float = 0.95
    runtime_exits_degraded: int = 1
    runtime_exits_critical: int = 5
    capability_mismatch_degraded: int = 1
    capability_mismatch_critical: int = 5


class HealthInputs(BaseModel):
    """Metadata-only health inputs (no content fields)."""

    model_config = _BASE

    spool_depth: Optional[int] = None
    spool_age_seconds: Optional[int] = None
    upload_ack_rate: Optional[float] = None
    proxy_start_failures: Optional[int] = None
    runtime_exit_count: Optional[int] = None
    capability_mismatch_count: Optional[int] = None
    session_state_counts: dict[str, int] = Field(default_factory=dict)
    ingestion_reject_reasons: dict[str, int] = Field(default_factory=dict)
    retention_job_status: Optional[str] = None
    export_job_health: Optional[str] = None


def _signal(
    name: str,
    state: HealthSignalState,
    *,
    threshold: Optional[str] = None,
    observed: Optional[float] = None,
    reason: Optional[str] = None,
) -> HealthSignal:
    return HealthSignal(
        name=name,
        state=state,
        threshold=threshold,
        observed_value=None if observed is None else float(observed),
        reason=reason,
    )


def _missing(name: str) -> HealthSignal:
    return _signal(
        name,
        HealthSignalState.UNKNOWN,
        reason="no observation for this window; health is not inferred",
    )


def _upper_signal(
    name: str,
    value: Optional[int],
    *,
    degraded: int,
    critical: int,
) -> HealthSignal:
    if value is None:
        return _missing(name)
    threshold = f"degraded>={degraded}, critical>={critical}"
    if value >= critical:
        return _signal(name, HealthSignalState.CRITICAL, threshold=threshold, observed=value)
    if value >= degraded:
        return _signal(name, HealthSignalState.DEGRADED, threshold=threshold, observed=value)
    return _signal(name, HealthSignalState.HEALTHY, threshold=threshold, observed=value)


def _ack_rate_signal(value: Optional[float], thresholds: HealthThresholds) -> HealthSignal:
    if value is None:
        return _missing("upload_ack_rate")
    threshold = (
        f"degraded<={thresholds.upload_ack_rate_degraded}, "
        f"critical<={thresholds.upload_ack_rate_critical}"
    )
    if value <= thresholds.upload_ack_rate_critical:
        return _signal(
            "upload_ack_rate", HealthSignalState.CRITICAL, threshold=threshold, observed=value
        )
    if value <= thresholds.upload_ack_rate_degraded:
        return _signal(
            "upload_ack_rate", HealthSignalState.DEGRADED, threshold=threshold, observed=value
        )
    return _signal(
        "upload_ack_rate", HealthSignalState.HEALTHY, threshold=threshold, observed=value
    )


def _status_signal(name: str, value: Optional[str]) -> HealthSignal:
    if value is None:
        return _missing(name)
    normalized = value.strip().upper()
    if normalized in {"OK", "COMPLETE", "COMPLETED", "HEALTHY"}:
        state = HealthSignalState.HEALTHY
    elif normalized in {"PENDING", "RUNNING", "DEGRADED", "PARTIAL"}:
        state = HealthSignalState.DEGRADED
    else:
        state = HealthSignalState.CRITICAL
    return _signal(name, state, threshold="OK/COMPLETE healthy", observed=None)


def aggregate_health(
    study_id: UUID,
    window_start: datetime,
    window_end: datetime,
    inputs: HealthInputs,
    *,
    thresholds: Optional[HealthThresholds] = None,
    monitoring_available: bool = True,
    now: Optional[datetime] = None,
) -> OperationalHealthV1:
    """Build an :class:`OperationalHealthV1` from metadata inputs."""
    config = thresholds or HealthThresholds()
    captured = now or datetime.now(timezone.utc)

    if not monitoring_available:
        # Never invent health during a monitoring outage.
        signals = [
            _signal(
                name,
                HealthSignalState.UNKNOWN,
                threshold=None,
                observed=None,
                reason=_MONITORING_OUTAGE_REASON,
            )
            for name in (
                "spool_depth",
                "spool_age_seconds",
                "upload_ack_rate",
                "runtime_exit_count",
                "capability_mismatch_count",
                "retention_job_status",
                "export_job_health",
            )
        ]
        return OperationalHealthV1(
            study_id=study_id,
            window_start=window_start,
            window_end=window_end,
            session_state_counts={},
            ingestion_reject_reasons={},
            signals=signals,
            captured_at=captured,
        )

    signals = [
        _upper_signal(
            "spool_depth",
            inputs.spool_depth,
            degraded=config.spool_depth_degraded,
            critical=config.spool_depth_critical,
        ),
        _upper_signal(
            "spool_age_seconds",
            inputs.spool_age_seconds,
            degraded=config.spool_age_degraded_seconds,
            critical=config.spool_age_critical_seconds,
        ),
        _ack_rate_signal(inputs.upload_ack_rate, config),
        _upper_signal(
            "runtime_exit_count",
            inputs.runtime_exit_count,
            degraded=config.runtime_exits_degraded,
            critical=config.runtime_exits_critical,
        ),
        _upper_signal(
            "capability_mismatch_count",
            inputs.capability_mismatch_count,
            degraded=config.capability_mismatch_degraded,
            critical=config.capability_mismatch_critical,
        ),
        _status_signal("retention_job_status", inputs.retention_job_status),
        _status_signal("export_job_health", inputs.export_job_health),
    ]
    return OperationalHealthV1(
        study_id=study_id,
        window_start=window_start,
        window_end=window_end,
        spool_depth=inputs.spool_depth,
        spool_age_seconds=inputs.spool_age_seconds,
        upload_ack_rate=inputs.upload_ack_rate,
        proxy_start_failures=inputs.proxy_start_failures,
        runtime_exit_count=inputs.runtime_exit_count,
        capability_mismatch_count=inputs.capability_mismatch_count,
        session_state_counts=dict(inputs.session_state_counts),
        ingestion_reject_reasons=dict(inputs.ingestion_reject_reasons),
        retention_job_status=inputs.retention_job_status,
        export_job_health=inputs.export_job_health,
        signals=signals,
        captured_at=captured,
    )


_SEVERITY = {
    HealthSignalState.HEALTHY: 0,
    HealthSignalState.UNKNOWN: 1,
    HealthSignalState.DEGRADED: 2,
    HealthSignalState.CRITICAL: 3,
}


def health_summary(snapshot: OperationalHealthV1) -> dict:
    """Return the worst-state summary plus explicit per-state counts."""
    overall = HealthSignalState.HEALTHY
    counts = {state.value: 0 for state in HealthSignalState}
    reasons = []
    for signal in snapshot.signals:
        counts[signal.state.value] = counts.get(signal.state.value, 0) + 1
        if _SEVERITY[signal.state] > _SEVERITY[overall]:
            overall = signal.state
        if signal.state in (HealthSignalState.DEGRADED, HealthSignalState.CRITICAL):
            reasons.append(
                {
                    "code": (
                        OperationsReasonCode.THRESHOLD_CRITICAL.value
                        if signal.state == HealthSignalState.CRITICAL
                        else OperationsReasonCode.THRESHOLD_DEGRADED.value
                    ),
                    "signal": signal.name,
                    "observed_value": signal.observed_value,
                    "reason": signal.reason,
                }
            )
    return {
        "study_id": str(snapshot.study_id),
        "overall": overall.value,
        "counts": counts,
        "reasons": reasons,
        "signals": [signal.model_dump(mode="json") for signal in snapshot.signals],
    }
