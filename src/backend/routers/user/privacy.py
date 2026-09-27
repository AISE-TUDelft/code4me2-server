"""Self-service privacy controls for the signed-in account (GDPR Art. 7(3), 17).

Mounted under ``/api/user/privacy``:

* ``GET /``           — collection status, stored-data counts, deletion eligibility
* ``PUT /collection`` — opt out of data collection, or back in
* ``POST /erase``     — opt out and erase everything collected about the account

The rules live in :mod:`privacy`; this router resolves the caller, owns the
transaction and shapes the response. Account deletion stays at
``DELETE /api/user/delete``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from App import App
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from database.db_schemas import Study
from privacy import collection, erasure
from research.participants import identity as participant_identity

router = APIRouter()


class DataCollection(BaseModel):
    enabled: bool
    opted_out_at: Optional[datetime] = None


class ActiveStudy(BaseModel):
    study_id: uuid.UUID
    name: str


class StoredDataCounts(BaseModel):
    queries: int
    chats: int
    agent_runs: int
    study_enrollments: int
    study_events: int


class AccountDeletion(BaseModel):
    allowed: bool
    blocked_reason: Optional[str] = None


class PrivacyStatus(BaseModel):
    data_collection: DataCollection
    active_study: Optional[ActiveStudy] = None
    stored_data: StoredDataCounts
    account_deletion: AccountDeletion


class CollectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ErasureResult(BaseModel):
    erased: StoredDataCounts
    status: PrivacyStatus


def _active_study(db, user_id: uuid.UUID) -> Optional[ActiveStudy]:
    participant = participant_identity.get_participant_by_account(db, user_id)
    if participant is None:
        return None
    enrollment = participant_identity.get_active_enrollment_for_participant(
        db, participant.participant_id
    )
    study = db.get(Study, enrollment.study_id) if enrollment is not None else None
    return ActiveStudy(study_id=study.study_id, name=study.name) if study is not None else None


def _status(db, user_id: uuid.UUID) -> PrivacyStatus:
    opted_out_at = collection.opted_out_at(db, user_id)
    blocked_reason = erasure.account_deletion_blocker(db, user_id)
    return PrivacyStatus(
        data_collection=DataCollection(enabled=opted_out_at is None, opted_out_at=opted_out_at),
        active_study=_active_study(db, user_id),
        stored_data=StoredDataCounts(**erasure.stored_data_summary(db, user_id).as_dict()),
        account_deletion=AccountDeletion(
            allowed=blocked_reason is None, blocked_reason=blocked_reason
        ),
    )


def _live_session_ids(app: App, user_id: uuid.UUID) -> list[uuid.UUID]:
    """The account's live classic session, which the IDE keeps using after an erase."""
    user_info = app.get_redis_manager().get("user_token", str(user_id)) or {}
    try:
        return [uuid.UUID(str(user_info["session_token"]))]
    except (KeyError, ValueError):
        return []


@router.get("", summary="Data-collection status and stored data of the signed-in account")
def get_privacy_status(
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
) -> PrivacyStatus:
    db = app.get_db_session()
    try:
        return _status(db, current_user.user_id)
    finally:
        db.close()


@router.put("/collection", summary="Opt out of data collection, or back in")
def update_data_collection(
    payload: CollectionUpdate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
) -> PrivacyStatus:
    db = app.get_db_session()
    try:
        if payload.enabled:
            collection.opt_in(db, current_user.user_id)
        else:
            collection.opt_out(db, current_user.user_id)
        db.commit()
        return _status(db, current_user.user_id)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/erase", summary="Opt out and erase everything collected about the signed-in account")
def erase_my_data(
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
) -> ErasureResult:
    keep_session_ids = _live_session_ids(app, current_user.user_id)
    db = app.get_db_session()
    try:
        erased = erasure.erase_collected_data(
            db, current_user.user_id, keep_session_ids=keep_session_ids
        )
        db.commit()
        logging.info(f"[privacy] erased collected data: {erased.as_dict()}")
        try:
            # The live session's cached project context must not be written back.
            app.get_redis_manager().mark_context_erased(str(current_user.user_id))
        except Exception as error:
            logging.warning(f"[privacy] could not mark the live context erased: {error}")
        return ErasureResult(
            erased=StoredDataCounts(**erased.as_dict()),
            status=_status(db, current_user.user_id),
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
