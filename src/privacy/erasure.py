"""Erase what Code4Me collected about an account (GDPR Art. 17).

``erase_collected_data`` backs the self-service "erase my data" action. It opts
the account out and deletes every row of collected data linked to it, in one
transaction and in foreign-key order:

* agent runs (tasks with their events and edits) and agent memory;
* research participation and telemetry (``research.participants.erasure``);
* classic completion/chat requests with their generations, ground truth, code
  context and telemetry; chats; study config assignments; ended sessions.

It keeps the account, the live sign-in session (the IDE cannot re-acquire a
revoked one) and the project rows, whose stored code context it clears. Studies
and agent profiles the account authored are research resources, not data about
the account, and stay.

``delete_account`` then also removes the remaining sessions, project
memberships, sole-member projects and the account row. It refuses accounts that
own studies or agent profiles (``account_deletion_blocker``): deleting those
would cascade into other people's study data.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Collection, Optional

from sqlalchemy import delete, func, or_, select, update

from database.db_schemas import (
    AgentMemory,
    AgentProfile,
    AgentTask,
    BehavioralTelemetry,
    Chat,
    ConfigAssignmentHistory,
    Context,
    ContextualTelemetry,
    MetaQuery,
    Project,
    ProjectUser,
    Study,
    User,
)
from database.db_schemas import Session as SessionRow
from research.participants import erasure as participant_erasure

from . import collection

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Bound on the ids bound into one IN (...) clause.
_ID_CHUNK = 1000


@dataclass(frozen=True)
class StoredData:
    """Collected data held for an account, by category (also the erasure report)."""

    queries: int = 0
    chats: int = 0
    agent_runs: int = 0
    study_enrollments: int = 0
    study_events: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def _count(db: Session, model, *criteria) -> int:
    return int(db.execute(select(func.count()).select_from(model).where(*criteria)).scalar_one())


def _agent_run_scope(user_id: uuid.UUID):
    # A run belongs to the account that ran it; older rows may carry only the
    # account's classic session.
    return or_(
        AgentTask.owner_user_id == user_id,
        AgentTask.session_id.in_(select(SessionRow.session_id).where(SessionRow.user_id == user_id)),
    )


def stored_data_summary(db: Session, user_id: uuid.UUID) -> StoredData:
    """Count the collected data currently held for the account."""
    research = participant_erasure.participant_data_counts(db, user_id)
    return StoredData(
        queries=_count(db, MetaQuery, MetaQuery.user_id == user_id),
        chats=_count(db, Chat, Chat.user_id == user_id),
        agent_runs=_count(db, AgentTask, _agent_run_scope(user_id)),
        study_enrollments=research.enrollments,
        study_events=research.events,
    )


class AccountDeletionBlocked(Exception):
    """The account owns research resources and cannot delete itself."""


def account_deletion_blocker(db: Session, user_id: uuid.UUID) -> Optional[str]:
    """Why the account cannot delete itself, or ``None`` when it can."""
    owned = []
    studies = _count(db, Study, Study.created_by == user_id)
    if studies:
        owned.append(f"{studies} research {'study' if studies == 1 else 'studies'}")
    profiles = _count(db, AgentProfile, AgentProfile.owner_user_id == user_id)
    if profiles:
        owned.append(f"{profiles} agent {'profile' if profiles == 1 else 'profiles'}")
    if not owned:
        return None
    return (
        f"This account owns {' and '.join(owned)}, which other people's study data can "
        "depend on, so it cannot delete itself. Ask an administrator to remove them first."
    )


def _delete_ids(db: Session, model, column, ids: list) -> None:
    ids = [value for value in ids if value is not None]
    for start in range(0, len(ids), _ID_CHUNK):
        db.execute(delete(model).where(column.in_(ids[start : start + _ID_CHUNK])))


def _erase_agent_data(db: Session, user_id: uuid.UUID) -> int:
    # A task cascades to its events and edits.
    runs = db.execute(delete(AgentTask).where(_agent_run_scope(user_id))).rowcount
    db.execute(delete(AgentMemory).where(AgentMemory.owner_user_id == str(user_id)))
    return runs


def _erase_classic_data(db: Session, user_id: uuid.UUID) -> tuple[int, int]:
    references = db.execute(
        select(
            MetaQuery.context_id,
            MetaQuery.contextual_telemetry_id,
            MetaQuery.behavioral_telemetry_id,
        ).where(MetaQuery.user_id == user_id)
    ).all()
    # A query cascades to its completion/chat query, generations and ground
    # truth. Its context and telemetry rows are referenced without a cascade, so
    # they can only go once the query is gone.
    queries = db.execute(delete(MetaQuery).where(MetaQuery.user_id == user_id)).rowcount
    _delete_ids(db, Context, Context.context_id, [row.context_id for row in references])
    _delete_ids(
        db,
        ContextualTelemetry,
        ContextualTelemetry.contextual_telemetry_id,
        [row.contextual_telemetry_id for row in references],
    )
    _delete_ids(
        db,
        BehavioralTelemetry,
        BehavioralTelemetry.behavioral_telemetry_id,
        [row.behavioral_telemetry_id for row in references],
    )
    chats = db.execute(delete(Chat).where(Chat.user_id == user_id)).rowcount
    db.execute(delete(ConfigAssignmentHistory).where(ConfigAssignmentHistory.user_id == user_id))
    return queries, chats


def _clear_project_context(db: Session, user_id: uuid.UUID) -> None:
    """Clear the stored code context of the account's projects.

    A shared project keeps its context while another member still lets context
    be stored: it is that member's code as much as this account's.
    """
    project_ids = db.execute(
        select(ProjectUser.project_id).where(ProjectUser.user_id == user_id)
    ).scalars().all()
    to_clear = []
    for project_id in project_ids:
        other_members = db.execute(
            select(User)
            .join(ProjectUser, ProjectUser.user_id == User.user_id)
            .where(ProjectUser.project_id == project_id, User.user_id != user_id)
        ).scalars().all()
        if not any(collection.allows_context_storage(member) for member in other_members):
            to_clear.append(project_id)
    if to_clear:
        db.execute(
            update(Project)
            .where(Project.project_id.in_(to_clear))
            .values(multi_file_contexts="{}", multi_file_context_changes="{}")
        )


def erase_collected_data(
    db: Session,
    user_id: uuid.UUID,
    *,
    keep_session_ids: Collection[uuid.UUID] = (),
    now: Optional[datetime] = None,
) -> StoredData:
    """Opt the account out and erase everything collected about it.

    ``keep_session_ids`` names live sign-in sessions to keep. Returns what was
    erased.
    """
    timestamp = now or datetime.now(timezone.utc)
    collection.opt_out(db, user_id, now=timestamp)
    db.flush()
    # Runs and queries reference sessions without a cascade: sessions go last.
    agent_runs = _erase_agent_data(db, user_id)
    research = participant_erasure.erase_participant_data(db, user_id, now=timestamp)
    queries, chats = _erase_classic_data(db, user_id)
    _clear_project_context(db, user_id)
    sessions = delete(SessionRow).where(SessionRow.user_id == user_id)
    if keep_session_ids:
        sessions = sessions.where(SessionRow.session_id.not_in(list(keep_session_ids)))
    db.execute(sessions)
    return StoredData(
        queries=queries,
        chats=chats,
        agent_runs=agent_runs,
        study_enrollments=research.enrollments,
        study_events=research.events,
    )


def delete_account(
    db: Session, user_id: uuid.UUID, *, now: Optional[datetime] = None
) -> StoredData:
    """Erase everything collected about the account, then the account itself.

    Raises :class:`AccountDeletionBlocked` for an account that owns studies or
    agent profiles. Returns what was erased.
    """
    # Lock the account row first: rows that reference it (a new study, say)
    # cannot appear between the ownership check and the delete.
    db.execute(select(User.user_id).where(User.user_id == user_id).with_for_update())
    blocker = account_deletion_blocker(db, user_id)
    if blocker is not None:
        raise AccountDeletionBlocked(blocker)
    erased = erase_collected_data(db, user_id, now=now)
    own_projects = select(ProjectUser.project_id).where(ProjectUser.user_id == user_id)
    shared_projects = select(ProjectUser.project_id).where(ProjectUser.user_id != user_id)
    db.execute(
        delete(Project).where(
            Project.project_id.in_(own_projects),
            Project.project_id.not_in(shared_projects),
        )
    )
    # Remaining memberships cascade with the account row.
    db.execute(delete(User).where(User.user_id == user_id))
    return erased
