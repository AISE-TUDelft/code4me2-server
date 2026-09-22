"""CRUD-style persistence helpers for the agent registry.

These functions take a caller-managed SQLAlchemy ``Session`` so the core package
never imports ``App`` or touches the application singleton. The router is
responsible for session lifecycle (``App.get_db_session`` / ``rollback`` /
``close``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, Sequence

from sqlalchemy import select

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.orm import Session

    from .models import AgentReleaseV1, CapabilitySnapshotV1

from database.research_schemas import AgentRelease, ResearchAgentRun

from .enums import QualificationStatus
from .registry import derive_qualification_status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def upsert_release(session: Session, release: AgentReleaseV1, *, commit: bool = True) -> AgentRelease:
    """Persist verified import results without overwriting an existing release."""
    row = session.get(AgentRelease, release.release_id)
    if row is not None:
        if (row.agent_id, row.source_manifest_digest) != (release.agent_id, release.source_manifest_digest):
            raise ValueError("release identity is immutable")
        return row
    payload = release.model_dump(mode="json")
    status = derive_qualification_status(payload)
    payload["qualification_status"] = status.value
    row = AgentRelease(
        release_id=release.release_id,
        agent_id=release.agent_id,
        source_manifest_digest=release.source_manifest_digest,
        status=status.value,
        release_json=payload,
        created_at=release.created_at or _now(),
    )
    session.add(row)
    if commit:
        session.commit()
        session.refresh(row)
    return row


def get_release(session: Session, release_id: str) -> Optional[AgentRelease]:
    """Fetch a release row by primary key, or ``None``."""
    return session.get(AgentRelease, release_id)


def list_releases(
    session: Session, agent_id: Optional[str] = None
) -> Sequence[AgentRelease]:
    """List release rows (optionally for one agent) in a stable order."""
    statement = select(AgentRelease)
    if agent_id is not None:
        statement = statement.where(AgentRelease.agent_id == agent_id)
    statement = statement.order_by(
        AgentRelease.agent_id.asc(), AgentRelease.release_id.asc()
    )
    return list(session.execute(statement).scalars().all())


def insert_snapshot(session: Session, snapshot: CapabilitySnapshotV1) -> ResearchAgentRun:
    """Store a capability snapshot on the run that captured it.

    A snapshot is a property of an agent run, so it is written to
    ``research_agent_run.snapshot_json``. The admin qualification upload has no
    participant session, so it records a detached run (``research_session_id``
    is ``None``) keyed by the snapshot id.
    """
    payload = snapshot.model_dump(mode="json")
    row = session.get(ResearchAgentRun, snapshot.snapshot_id)
    if row is None:
        row = ResearchAgentRun(
            agent_run_id=snapshot.snapshot_id,
            research_session_id=None,
            agent_release_id=snapshot.release_id,
            started_at=snapshot.captured_at,
            snapshot_json=payload,
            snapshot_captured_at=snapshot.captured_at,
        )
    else:
        row.snapshot_json = payload
        row.snapshot_captured_at = snapshot.captured_at
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_snapshot(
    session: Session, snapshot_id: uuid.UUID
) -> Optional[ResearchAgentRun]:
    """Fetch the run holding a snapshot by snapshot id, or ``None``."""
    row = session.get(ResearchAgentRun, snapshot_id)
    if row is None or row.snapshot_json is None:
        return None
    return row


def list_snapshots(
    session: Session, release_id: Optional[str] = None
) -> Sequence[ResearchAgentRun]:
    """List snapshot-bearing runs (optionally for one release), oldest-first."""
    statement = select(ResearchAgentRun).where(
        ResearchAgentRun.snapshot_json.is_not(None)
    )
    if release_id is not None:
        statement = statement.where(
            ResearchAgentRun.snapshot_json["release_id"].astext == release_id
        )
    statement = statement.order_by(ResearchAgentRun.snapshot_captured_at.asc())
    return list(session.execute(statement).scalars().all())


def row_to_release(row: AgentRelease) -> AgentReleaseV1:
    """Rehydrate a stored release row into the versioned model.

    ``release_json`` also carries packaging evidence that is not part of the
    release model, so only the model's own fields are validated; the
    producer test results determine usability. Terminal operator status wins.
    """
    from .models import AgentReleaseV1 as AgentReleaseV1Model

    payload = dict(row.release_json or {})
    model_fields = set(AgentReleaseV1Model.model_fields)
    filtered = {key: value for key, value in payload.items() if key in model_fields}
    if not isinstance(filtered.get("tests", []), list):
        filtered["tests"] = []  # Old aggregate verdicts do not qualify any platform.
    release = AgentReleaseV1Model.model_validate(filtered)
    if row.status in {"DISABLED", "RETIRED", "BLOCKED"}:
        payload["qualification_status"] = row.status
    return release.model_copy(update={"qualification_status": derive_qualification_status(payload)})


def row_to_snapshot(row: ResearchAgentRun) -> CapabilitySnapshotV1:
    """Rehydrate a snapshot-bearing run row into the versioned model."""
    from .models import CapabilitySnapshotV1 as CapabilitySnapshotV1Model

    return CapabilitySnapshotV1Model.model_validate(row.snapshot_json)


def disable_release(session: Session, release_id: str) -> Optional[AgentRelease]:
    """Irreversibly disable a release, including subsequent identical imports."""
    row = session.get(AgentRelease, release_id)
    if row is None:
        return None
    row.status = QualificationStatus.DISABLED.value
    row.release_json = dict(row.release_json or {}, qualification_status=row.status)
    session.commit()
    session.refresh(row)
    return row


def release_summary(row: AgentRelease) -> dict[str, Any]:
    release = row_to_release(row)
    return {
        "release_id": row.release_id,
        "agent_id": row.agent_id,
        "source_manifest_digest": row.source_manifest_digest,
        "status": release.qualification_status.value,
        "tests": [item.model_dump(mode="json") for item in release.tests],
        "created_at": row.created_at.isoformat() if isinstance(row.created_at, datetime) else None,
    }


def snapshot_summary(row: ResearchAgentRun) -> dict[str, Any]:
    """Return a compact snapshot summary safe for list responses."""
    payload = row.snapshot_json or {}
    captured_at = row.snapshot_captured_at
    return {
        "snapshot_id": str(payload.get("snapshot_id") or row.agent_run_id),
        "release_id": payload.get("release_id"),
        "adapter_version": payload.get("adapter_version"),
        "protocol_version": payload.get("protocol_version"),
        "captured_at": (
            captured_at.isoformat() if isinstance(captured_at, datetime) else None
        ),
    }
