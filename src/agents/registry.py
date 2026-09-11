"""Server-authoritative A/B assignment of agent profiles to users."""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from database import crud
from database.db_schemas import AgentProfile


def get_profile(db: Session, name: str) -> Optional[AgentProfile]:
    """Resolve a profile by name. Returns None if it doesn't exist."""
    return crud.get_agent_profile(db, name)


@dataclass(frozen=True)
class AgentAssignmentResolution:
    """The selected profile and optional immutable experiment arm for one task."""

    profile: AgentProfile
    study_id: Optional[uuid.UUID] = None
    assignment_id: Optional[uuid.UUID] = None
    arm_name: Optional[str] = None
    is_baseline: Optional[bool] = None


def resolve_assignment_context(
    db: Session, user_id: uuid.UUID
) -> Optional[AgentAssignmentResolution]:
    """Resolve the profile and study assignment for a new task.

    An active study owns its participant assignments: one immutable row is
    inserted per ``(study_id, user_id)``. The insert is conflict-safe, so
    concurrent first contacts converge on the arm selected by the winner.
    Outside an agent study there is no experimental assignment; the first active
    profile is used deterministically for operational runs.
    """
    active_arm_links = crud.list_active_study_agent_profile_links(db)
    if active_arm_links:
        study_id = active_arm_links[0].study_id
        assignment = crud.get_agent_study_assignment(db, study_id, user_id)
        if assignment is None:
            chosen_arm = random.choice(active_arm_links)
            profile = chosen_arm.profile
            if profile is None:
                logging.error("[Agent/registry] active study arm has no profile")
                return None
            assignment = crud.create_agent_study_assignment(
                db,
                study_id=study_id,
                user_id=user_id,
                profile_id=profile.profile_id,
                arm_name=profile.name,
                is_baseline=chosen_arm.is_baseline,
                source="auto",
            )
            if assignment is None:
                assignment = crud.get_agent_study_assignment(db, study_id, user_id)

        if assignment is None or assignment.profile is None:
            logging.error("[Agent/registry] could not resolve a study assignment")
            return None
        return AgentAssignmentResolution(
            profile=assignment.profile,
            study_id=assignment.study_id,
            assignment_id=assignment.assignment_id,
            arm_name=assignment.arm_name,
            is_baseline=assignment.is_baseline,
        )

    active_profiles = crud.list_active_agent_profiles(db)
    if not active_profiles:
        logging.error("[Agent/registry] no active agent profiles — cannot assign")
        return None
    return AgentAssignmentResolution(profile=active_profiles[0])


def resolve_assignment(db: Session, user_id: uuid.UUID) -> Optional[AgentProfile]:
    """Return the profile selected for a new task."""
    resolution = resolve_assignment_context(db, user_id)
    return resolution.profile if resolution is not None else None
