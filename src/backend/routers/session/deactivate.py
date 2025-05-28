import logging

from fastapi import APIRouter, Cookie, Depends

import Queries
from App import App
from backend.Responses import (
    DeactivateSessionError,
    DeactivateSessionPostResponse,
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
)

router = APIRouter()


@router.put(
    "/",
    response_model=None,
    responses={
        "200": {"model": DeactivateSessionPostResponse},
        "401": {"model": InvalidOrExpiredSessionToken},
        "429": {"model": ErrorResponse},
        "500": {"model": DeactivateSessionError},
    },
    tags=["Deactivate Session"],
)
def deactivate_session(
    deactivate_session_request: Queries.DeactivateSession,
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
        session_token = deactivate_session_request.session_token
        session_info = session_manager.get_session(session_token)
        if not session_info:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        session_manager.move_session_info_to_db(
            db=db_session, session_token=session_token
        )
        session_manager.delete_session(session_token)

        return JsonResponseWithStatus(
            status_code=200, content=DeactivateSessionPostResponse()
        )
    except Exception as e:
        logging.log(logging.ERROR, f"Error deactivating session: {e}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=DeactivateSessionError(),
        )
