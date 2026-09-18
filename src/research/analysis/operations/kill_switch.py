"""Operator kill switch (Issue 13).

A kill switch is engaged at a study, revision, or enrollment scope and is
auditable (actor, reason, engaged_at). While engaged it blocks new session
bootstraps and rejects new telemetry batches; those services consult it through
an injected zero-argument predicate so they keep no operations dependency.

The registry is pure/in-memory; the router can rebuild it from persisted rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional
from uuid import UUID

from .enums import OperationsReasonCode
from .models import KillSwitchRecord, KillSwitchScope, OperationsIssue

__all__ = [
    "KillSwitchRegistry",
    "engage_kill_switch",
    "is_engaged",
    "kill_switch_check",
    "kill_switch_issue",
    "release_kill_switch",
]


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _matches(
    record: KillSwitchRecord,
    *,
    study_id: Optional[UUID],
    revision_id: Optional[UUID],
    enrollment_id: Optional[UUID],
) -> bool:
    return record.scope.matches(
        study_id=study_id, revision_id=revision_id, enrollment_id=enrollment_id
    )


class KillSwitchRegistry:
    """In-memory collection of kill-switch records."""

    def __init__(self, records: Optional[Iterable[KillSwitchRecord]] = None) -> None:
        self._records: list[KillSwitchRecord] = list(records or [])

    @property
    def records(self) -> list[KillSwitchRecord]:
        """All records, newest engagement first."""
        return sorted(self._records, key=lambda record: record.engaged_at, reverse=True)

    def add(self, record: KillSwitchRecord) -> KillSwitchRecord:
        """Register a record (e.g. rehydrated from storage)."""
        self._records.append(record)
        return record

    def engage(
        self,
        scope: KillSwitchScope,
        reason: str,
        *,
        actor: Optional[str] = None,
        now: Optional[datetime] = None,
        effective_until: Optional[datetime] = None,
    ) -> KillSwitchRecord:
        """Engage a kill switch at ``scope``."""
        record = KillSwitchRecord(
            switch_id=uuid.uuid4(),
            scope=scope,
            reason=reason,
            actor=actor,
            engaged_at=_now(now),
            effective_until=effective_until,
        )
        self._records.append(record)
        return record

    def release(
        self, switch_id: UUID, *, now: Optional[datetime] = None
    ) -> Optional[KillSwitchRecord]:
        """Release a switch, returning the updated record or ``None``."""
        for index, record in enumerate(self._records):
            if record.switch_id == switch_id:
                released = record.model_copy(update={"released_at": _now(now)})
                self._records[index] = released
                return released
        return None

    def active(self, *, now: Optional[datetime] = None) -> list[KillSwitchRecord]:
        """Currently engaged records."""
        timestamp = _now(now)
        return [record for record in self.records if record.is_engaged(timestamp)]

    def is_engaged(
        self,
        *,
        study_id: Optional[UUID] = None,
        revision_id: Optional[UUID] = None,
        enrollment_id: Optional[UUID] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """Whether any active switch covers the requested identifiers."""
        timestamp = _now(now)
        return any(
            record.is_engaged(timestamp)
            and _matches(
                record,
                study_id=study_id,
                revision_id=revision_id,
                enrollment_id=enrollment_id,
            )
            for record in self._records
        )


def engage_kill_switch(
    registry: KillSwitchRegistry,
    scope: KillSwitchScope,
    reason: str,
    *,
    actor: Optional[str] = None,
    now: Optional[datetime] = None,
    effective_until: Optional[datetime] = None,
) -> KillSwitchRecord:
    """Engage a kill switch (auditable scope + reason + actor)."""
    return registry.engage(
        scope, reason, actor=actor, now=now, effective_until=effective_until
    )


def release_kill_switch(
    registry: KillSwitchRegistry, switch_id: UUID, *, now: Optional[datetime] = None
) -> Optional[KillSwitchRecord]:
    """Release a previously engaged switch."""
    return registry.release(switch_id, now=now)


def is_engaged(
    registry: KillSwitchRegistry,
    *,
    study_id: Optional[UUID] = None,
    revision_id: Optional[UUID] = None,
    enrollment_id: Optional[UUID] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Whether a kill switch blocks the requested scope."""
    return registry.is_engaged(
        study_id=study_id,
        revision_id=revision_id,
        enrollment_id=enrollment_id,
        now=now,
    )


def kill_switch_issue(
    registry: KillSwitchRegistry,
    *,
    study_id: Optional[UUID] = None,
    revision_id: Optional[UUID] = None,
    enrollment_id: Optional[UUID] = None,
    now: Optional[datetime] = None,
) -> Optional[OperationsIssue]:
    """Typed ``KILL_SWITCH_ENGAGED`` issue when the scope is blocked."""
    if is_engaged(
        registry,
        study_id=study_id,
        revision_id=revision_id,
        enrollment_id=enrollment_id,
        now=now,
    ):
        return OperationsIssue(
            code=OperationsReasonCode.KILL_SWITCH_ENGAGED,
            message="an operator kill switch is engaged for this scope",
            field="kill_switch",
        )
    return None


def kill_switch_check(
    registry: KillSwitchRegistry,
    *,
    study_id: Optional[UUID] = None,
    revision_id: Optional[UUID] = None,
    enrollment_id: Optional[UUID] = None,
    now: Optional[datetime] = None,
) -> Callable[[], bool]:
    """Return an injected predicate for bootstrap/ingestion."""

    def check() -> bool:
        return is_engaged(
            registry,
            study_id=study_id,
            revision_id=revision_id,
            enrollment_id=enrollment_id,
            now=now,
        )

    return check
