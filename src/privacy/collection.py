"""Whether an account's data may be collected, and the opt-out that stops it.

The opt-out lives in ``user.data_collection_opted_out_at`` rather than in the
preference JSON: clients rewrite that JSON wholesale from their local state, so
a stale IDE would otherwise undo a withdrawal nobody asked it to undo. Only the
privacy endpoints write the column.

While an account is opted out nothing is collected about it, whatever a client
sends: the classic handlers persist nothing, agent content and agent memory are
refused, and it holds no ACTIVE study enrollment (the opt-out withdraws it and
joining refuses until the account opts back in).

Writers that race an opt-out or erase check with :func:`lock_collection_allowed`:
its share lock conflicts with the opt-out's update of the account row, so a
write either commits before the erase starts (and is erased with the rest) or
waits for it and then sees the opt-out.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import delete, func, select, update

from database.db_schemas import BehavioralTelemetry, Context, ContextualTelemetry, User
from research.participants import identity as participant_identity

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def is_collection_allowed(db: Session, user_id: uuid.UUID) -> bool:
    """Whether data about ``user_id`` may be stored now (False for unknown accounts)."""
    row = db.execute(
        select(User.data_collection_opted_out_at).where(User.user_id == user_id)
    ).first()
    return row is not None and row[0] is None


def opted_out_at(db: Session, user_id: uuid.UUID) -> Optional[datetime]:
    """When the account opted out of data collection, or ``None`` while it collects."""
    return db.execute(
        select(User.data_collection_opted_out_at).where(User.user_id == user_id)
    ).scalar_one_or_none()


def lock_collection_allowed(db: Session, user_id: uuid.UUID) -> bool:
    """:func:`is_collection_allowed`, holding a share lock on the account row.

    The lock lasts until the caller's transaction ends. An opt-out (and so an
    erase) updates the same row first, so the two serialize: either the opt-out
    commits first and this returns False, or the opt-out waits until the
    caller's write has committed and then withdraws or erases it. Study joins
    and the storage writers that can race an erase take it.
    """
    row = db.execute(
        select(User.data_collection_opted_out_at)
        .where(User.user_id == user_id)
        .with_for_update(read=True)
    ).first()
    return row is not None and row[0] is None


def lock_account(db: Session, user_id: uuid.UUID) -> None:
    """Share-lock the account row until the transaction ends, whatever its opt-out state.

    For writers that stay allowed while an account is opted out (content-free
    agent bookkeeping) but must not slip past a concurrent erase; see
    :func:`lock_collection_allowed`.
    """
    db.execute(select(User.user_id).where(User.user_id == user_id).with_for_update(read=True))


def _context_storage_allowed(opted_out_at: Optional[datetime], preference: Optional[str]) -> bool:
    if opted_out_at is not None:
        return False
    try:
        parsed = json.loads(preference or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and bool(parsed.get("store_context", False))


def allows_context_storage(user: User) -> bool:
    """Whether ``user`` lets their project's code context be persisted."""
    return _context_storage_allowed(user.data_collection_opted_out_at, user.preference)


def lock_context_storage_allowed(db: Session, user_id: uuid.UUID) -> bool:
    """:func:`allows_context_storage` for ``user_id``, under the account-row share lock."""
    row = db.execute(
        select(User.data_collection_opted_out_at, User.preference)
        .where(User.user_id == user_id)
        .with_for_update(read=True)
    ).first()
    return row is not None and _context_storage_allowed(row[0], row[1])


def discard_if_opted_out(
    db: Session,
    user_id: uuid.UUID,
    *,
    context_id: Optional[uuid.UUID] = None,
    contextual_telemetry_id: Optional[uuid.UUID] = None,
    behavioral_telemetry_id: Optional[uuid.UUID] = None,
) -> bool:
    """Write-time check for a request accepted before the account opted out.

    Handlers check before they enqueue their storage tasks; the tasks check
    again, under :func:`lock_collection_allowed`, right before they write. When
    collection is allowed, False is returned and the lock is held until the
    caller's next commit, which carries its top-level row (a query): rows written
    after that commit hang off it and go with it by cascade, or fail their
    foreign key if it was erased meanwhile. Otherwise the request's context and
    telemetry rows (stored by an earlier task) are deleted and True is returned:
    the caller stores nothing more.
    """
    if lock_collection_allowed(db, user_id):
        return False
    for model, column, row_id in (
        (Context, Context.context_id, context_id),
        (ContextualTelemetry, ContextualTelemetry.contextual_telemetry_id, contextual_telemetry_id),
        (BehavioralTelemetry, BehavioralTelemetry.behavioral_telemetry_id, behavioral_telemetry_id),
    ):
        if row_id is not None:
            db.execute(delete(model).where(column == row_id))
    return True


def opt_out(
    db: Session, user_id: uuid.UUID, *, now: Optional[datetime] = None
) -> list[uuid.UUID]:
    """Withdraw consent: stop collection and end the active study enrollment.

    Idempotent: a repeated opt-out keeps the original timestamp. Returns the ids
    of the enrollments it withdrew.
    """
    timestamp = now or datetime.now(timezone.utc)
    # Update the account row before touching enrollments: its row lock is what
    # serializes the opt-out with a concurrent join (see lock_collection_allowed).
    db.execute(
        update(User)
        .where(User.user_id == user_id)
        .values(
            data_collection_opted_out_at=func.coalesce(
                User.data_collection_opted_out_at, timestamp
            )
        )
    )
    return participant_identity.withdraw_active_enrollments(db, user_id, now=timestamp)


def opt_in(db: Session, user_id: uuid.UUID) -> None:
    """Resume collection under the account's preferences; it does not re-enroll."""
    db.execute(
        update(User)
        .where(User.user_id == user_id)
        .values(data_collection_opted_out_at=None)
    )
