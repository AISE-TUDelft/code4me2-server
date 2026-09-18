"""Persistence helpers for bootstrap session authorization.

These functions take a caller-managed SQLAlchemy ``Session`` so the core package
never imports ``App``. Session capabilities are signed and verified statelessly
(HMAC), so no capability row is written; the authoritative revocation epoch
lives on the enrollment row and is the only server-side state consulted here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.orm import Session

from database.research_schemas import ResearchEnrollment


def revocation_epoch_for(
    session: Session, enrollment_id: uuid.UUID
) -> Optional[int]:
    """Return the authoritative revocation epoch for an enrollment, or ``None``."""
    row = session.get(ResearchEnrollment, enrollment_id)
    if row is None:
        return None
    return row.revocation_epoch
