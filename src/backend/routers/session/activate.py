import logging
from typing import Union

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
from App import App
from backend.Responses import (
    ActivateSessionError,
    ActivateSessionPostResponse,
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
)

router = APIRouter()


@router.get(
    "/",
    response_model=ActivateSessionPostResponse,
    responses={
        "200": {"model": ActivateSessionPostResponse},
        "401": {
            "model": Union[InvalidOrExpiredSessionToken, InvalidOrExpiredAuthToken]
        },
        "429": {"model": ErrorResponse},
        "500": {"model": ActivateSessionError},
    },
    tags=["Get Session"],
)
def activate_session(
    session_token: str,
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie("auth_token"),
) -> JsonResponseWithStatus:
    """
    Activates the session by following these steps:
    1. Validate the provided auth token
    2. If valid, return confirmation
    3. If invalid, return an appropriate error response
    4. The session might exist in redis or in the database, if it is in the database, it should be fetched from there and put in redis
    if it is in the redis, its expiration time should be updated.

    """
    session_manager = app.get_session_manager()
    db_session = app.get_db_session()

    try:
        # Validate auth token
        user_id = session_manager.get_user_id_by_auth_token(auth_token)
        if user_id is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )
        session_info = session_manager.activate_session(session_token)
        if not session_info:
            # The session is not in the redis, so we need to fetch it from the database if it exists there
            session_model_data = crud.get_session_by_id(db_session, session_token)
            if session_model_data is None:
                return JsonResponseWithStatus(
                    status_code=401,
                    content=InvalidOrExpiredSessionToken(),
                )
            else:
                # The session exists in the database, so we put it in the redis
                session_info = {
                    "user_id": session_model_data.user_id,
                    "data": {
                        "context": session_model_data.multi_file_contexts,
                    },
                }
                session_manager.update_session(session_token, session_info)

        logging.log(logging.INFO, f"Retrieved session info: {session_info}")

        return JsonResponseWithStatus(
            status_code=200, content=ActivateSessionPostResponse()
        )
    except Exception as e:
        logging.log(logging.ERROR, f"Error activating session: {e}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=ActivateSessionError(),
        )


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
