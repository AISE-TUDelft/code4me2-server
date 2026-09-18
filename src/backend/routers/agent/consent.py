"""Server-side consent gate for persisting agent *content*.

Merge decision 3: the mechanism is group-21's server-enforced double gate, with
the default flipped to ON.

* **Server-enforced.** Whether message text, tool arguments/results, diffs and
  raw payloads get written is decided here, from the user's stored preference —
  never from a flag in the request body. A compromised or stale client cannot
  turn content capture on, and cannot turn it off either (which matters for
  research integrity as much as for privacy).
* **Default ON.** When a user row predates this preference the key is absent,
  and it resolves to ``True``. This is a research platform where participants
  give informed consent through a separate process before using it, so opt-out
  is the right default for data collection — while the enforcement path stays
  strict.

Structural telemetry (token counts, latency, span tree, tool *names*, content
*lengths*) is never gated: it carries no user or code text, and it's what the
A/B analysis actually runs on.

Every caller funnels through this module so there is exactly one place where the
decision is made.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from database import crud
from database.db_schemas import (
    STORE_AGENT_CONTENT_DEFAULT,
    STORE_AGENT_CONTENT_KEY,
)


def _preference_allows_content(preference: Optional[str]) -> bool:
    """Read the flag out of a user's stored preference JSON.

    An absent key means the row predates the preference, so the documented
    default applies. Malformed JSON is treated as absent rather than as a
    refusal, because a corrupt preference blob shouldn't silently change what a
    study collects — it should behave like an un-migrated row.
    """
    if not preference:
        return STORE_AGENT_CONTENT_DEFAULT
    try:
        parsed = json.loads(preference)
    except (ValueError, TypeError):
        logging.warning(
            "[Agent/consent] unparseable user preference — "
            f"falling back to default store_agent_content={STORE_AGENT_CONTENT_DEFAULT}"
        )
        return STORE_AGENT_CONTENT_DEFAULT
    if not isinstance(parsed, dict):
        return STORE_AGENT_CONTENT_DEFAULT
    return bool(parsed.get(STORE_AGENT_CONTENT_KEY, STORE_AGENT_CONTENT_DEFAULT))


def resolve_store_agent_content_for_user(
    db: Session,
    user_id: Optional[uuid.UUID],
    *,
    study_id: Optional[uuid.UUID] = None,
) -> bool:
    """Resolve the preference for a known user id.

    Returns False when the user can't be resolved at all: an unattributable
    request has nobody's consent to rely on, so it gets the conservative answer
    even though the default for a real user is True. What is actually collected
    follows the study's published telemetry policy, which is delivered to the
    client and enforced by the privacy filter.
    """
    if user_id is None:
        return False
    try:
        user = crud.get_user_by_id(db, user_id)
    except Exception as e:
        logging.warning(f"[Agent/consent] user lookup failed for {user_id} — {e}")
        return False
    if user is None:
        return False
    return _preference_allows_content(user.preference)


def resolve_store_agent_content(
    db: Session,
    session_id: uuid.UUID,
    *,
    study_id: Optional[uuid.UUID] = None,
) -> bool:
    """Resolve the preference for the user owning ``session_id``.

    This is the plugin/proxy path, where the caller is identified by the
    session cookie.
    """
    try:
        session = crud.get_session_by_id(db, session_id)
    except Exception as e:
        logging.warning(f"[Agent/consent] session lookup failed for {session_id} — {e}")
        return False
    if session is None or session.user_id is None:
        return False
    return resolve_store_agent_content_for_user(
        db, session.user_id, study_id=study_id
    )


def resolve_store_agent_content_for_acp(
    db: Session,
    raw_user_id: str,
    *,
    study_id: Optional[uuid.UUID] = None,
) -> bool:
    """Resolve the preference for an ACP-authorized agent process.

    ``raw_user_id`` comes from the server-derived ACP scope (see
    ``backend.acp_authorization``), so it is trustworthy — but it arrives as a
    string and has to parse as a UUID to be usable.
    """
    try:
        user_uuid = uuid.UUID(str(raw_user_id))
    except (ValueError, TypeError, AttributeError):
        logging.warning(f"[Agent/consent] ACP scope user_id is not a UUID: {raw_user_id!r}")
        return False
    return resolve_store_agent_content_for_user(db, user_uuid, study_id=study_id)
