"""Server-authoritative A/B assignment of agent profiles to users."""

from __future__ import annotations

import logging
import random
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from database import crud
from database.db_schemas import AgentProfile


def get_profile(db: Session, name: str) -> Optional[AgentProfile]:
    """Resolve a profile by name. Returns None if it doesn't exist."""
    return crud.get_agent_profile(db, name)


def resolve_assignment(db: Session, user_id: uuid.UUID) -> Optional[AgentProfile]:
    """Return the agent profile assigned to ``user_id``.

    Assignment is server-authoritative and sticky: if the user already has an
    assignment (auto or manual) it's returned unchanged, so a participant keeps
    the same arm for the life of the experiment regardless of later changes to
    the active set. Otherwise a profile is drawn at random (even split) and
    persisted as an ``auto`` assignment.

    The client never picks its own arm — that's the point. A self-selected
    bucket would invalidate the comparison.

    Candidate pool: when a study is active *and* has selected agent profiles,
    the draw is restricted to that study's arms. Otherwise it falls back to all
    active profiles, so behaviour is unchanged when no agent study is running
    (backwards compatible with the completion-only platform).

    Returns ``None`` only when no candidate profiles exist at all, which the
    caller surfaces as a 503 — minting a task with no profile would produce
    telemetry that can't be attributed to any condition.
    """
    assignment = crud.get_agent_profile_assignment(db, user_id)
    if assignment is not None and assignment.profile is not None:
        return assignment.profile

    candidates = crud.list_active_study_agent_profiles(db)
    pool = "active study"
    if not candidates:
        candidates = crud.list_active_agent_profiles(db)
        pool = "all active profiles"
    if not candidates:
        logging.error("[Agent/registry] no active agent profiles — cannot assign")
        return None

    chosen = random.choice(candidates)
    crud.set_agent_profile_assignment(
        db, user_id=user_id, profile_id=chosen.profile_id, source="auto"
    )
    logging.info(
        f"[Agent/registry] drew arm {chosen.name!r} for user {str(user_id)[:8]}… "
        f"from {pool} ({len(candidates)} candidate(s))"
    )
    return chosen
