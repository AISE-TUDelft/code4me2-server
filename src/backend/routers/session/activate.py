import json
import logging
from typing import Union

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
import Queries
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


@router.put(
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
    tags=["Activate Session"],
)
def activate_session(
    activate_session_request: Queries.ActivateSession,
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
    config = app.get_config()
    try:
        # Validate auth token
        user_id = session_manager.get_user_id_by_auth_token(auth_token)
        if user_id is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )
        session_token = activate_session_request.session_token

        session_info = session_manager.get_session(session_token=session_token)
        if session_info:
            session_manager.update_session(
                session_token=session_token, session_data=session_info
            ),
        else:
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
                    "user_id": str(session_model_data.user_id),
                    "data": {
                        "context": json.loads(session_model_data.multi_file_contexts),
                        "context_changes": json.loads(
                            session_model_data.multi_file_context_changes
                        ),
                    },
                }
                session_manager.update_session(session_token, session_info)

        logging.log(logging.INFO, f"Retrieved session info: {session_info}")

        response_obj = JsonResponseWithStatus(
            status_code=200, content=ActivateSessionPostResponse()
        )
        response_obj.set_cookie(
            key="session_token",
            value=session_token,
            httponly=True,
            samesite="lax",
            expires=config.session_token_expires_in_seconds,
        )
        return response_obj
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
