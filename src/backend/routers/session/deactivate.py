import logging

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
from App import App
from backend.Responses import (
    DeactivateSessionError,
    DeactivateSessionPostResponse,
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
)
from database import db_schemas

router = APIRouter()


@router.post(
    "/",
    response_model=None,
    responses={
        "204": {"model": DeactivateSessionPostResponse},
        "401": {"model": InvalidOrExpiredSessionToken},
        "429": {"model": ErrorResponse},
        "500": {"model": DeactivateSessionError},
    },
    tags=["Deactivate Session"],
)
def deactivate_session(
    session_token: str,
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie("session_token"),
) -> JsonResponseWithStatus:
    """
    Deactivate an existing session by invalidating the session token.
    """
    session_manager = app.get_session_manager()
    db_session = app.get_db_session()
    try:
        user_id = session_manager.get_user_id_by_auth_token(auth_token)
        if not user_id:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )

        # Validate session token
        session_info = session_manager.get_session(session_token)
        if not session_info:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Put session information in database and remove the session from redis
        if crud.get_session_by_id(db_session, session_token):
            crud.delete_session_by_id(db_session, session_token)

        crud.add_session(
            db_session,
            db_schemas.Session(
                session_id=session_token,
                user_id=user_id,
                multi_file_contexts=session_info.get("data").get("context"),
            ),
        )

        session_manager.delete_session(session_token)

        return JsonResponseWithStatus(
            status_code=204,
            content=None,
        )
    except Exception as e:
        logging.log(logging.ERROR, f"Error deactivating session: {e}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=DeactivateSessionError(),
        )
