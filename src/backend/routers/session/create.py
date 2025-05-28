import logging

from fastapi import APIRouter, Cookie, Depends

from App import App
from backend.Responses import (
    CreateSessionError,
    CreateSessionPostResponse,
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    JsonResponseWithStatus,
)
from database import crud

router = APIRouter()


@router.post(
    "/",
    response_model=CreateSessionPostResponse,
    responses={
        "201": {"model": CreateSessionPostResponse},
        "401": {"model": InvalidOrExpiredAuthToken},
        "429": {"model": ErrorResponse},
        "500": {"model": CreateSessionError},
    },
    tags=["Create Session"],
)
def create_session(
    app: App = Depends(App.get_instance), auth_token: str = Cookie("auth_token")
) -> JsonResponseWithStatus:
    """
    Create a new session
    1. Validate the provided auth token
    2. If valid, create a session and return the session token
    3. If invalid, return an appropriate error response
    """
    session_manager = app.get_session_manager()
    config = app.get_config()
    db_session = app.get_db_session()

    try:
        user_id = session_manager.get_user_id_by_auth_token(auth_token)

        # Validate auth token
        if user_id is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )
        logging.log(logging.INFO, f"Creating session for user_id: {user_id}")

        # Create session
        created_session = crud.create_session(db_session, user_id=str(user_id))
        session_token = created_session.session_id
        session_manager.update_session(
            session_token, {"user_id": str(user_id), "data": {}}
        )
        # session_token = session_manager.create_session(user_id)
        response_obj = JsonResponseWithStatus(
            status_code=201,
            content=CreateSessionPostResponse(),
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
        logging.log(logging.ERROR, f"Error creating session: {e}")
        return JsonResponseWithStatus(
            status_code=500,
            content=CreateSessionError(),
        )


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
