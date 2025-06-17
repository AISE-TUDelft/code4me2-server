import logging
import uuid
from typing import Union

from fastapi import APIRouter, Cookie, Depends

from App import App
from backend.Responses import (
    DeactivateSessionError,
    DeactivateSessionPostResponse,
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
    SessionNotFoundError,
    UserNotFoundError,
)
from database import crud

# Create a router instance for handling session deactivation
router = APIRouter()


@router.put(
    "/",
    response_model=None,
    responses={
        "200": {"model": DeactivateSessionPostResponse},
        "401": {"model": InvalidOrExpiredSessionToken},
        "404": {"model": Union[UserNotFoundError, SessionNotFoundError]},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": DeactivateSessionError},
    },
)
def deactivate_session(
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie(""),
) -> JsonResponseWithStatus:
    """
    Deactivates an active session by validating the auth token, checking the session token,
    and removing the session information from Redis.

    Parameters:
    - app (App): Dependency-injected application context providing access to services.
    - auth_token (str): Auth token retrieved from the cookie, used to identify the user/session.

    Returns:
    - JsonResponseWithStatus: Appropriate response depending on validation and operation outcome.
    """
    # Get necessary components from the application context
    redis_manager = app.get_redis_manager()
    config = app.get_config()
    db_session = app.get_db_session()

    try:
        auth_info = redis_manager.get("auth_token", auth_token)
        # Validate auth token presence and associated user_id
        if auth_info is None or not auth_info.get("user_id"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )

        user_id = auth_info["user_id"]
        if crud.get_user_by_id(db_session, uuid.UUID(user_id)) is None:
            return JsonResponseWithStatus(
                status_code=404,
                content=UserNotFoundError(),
            )

        user_info = redis_manager.get("user_token", user_id)
        if user_info is None or not user_info.get("session_token"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        session_token = user_info.get("session_token")
        if crud.get_session_by_id(db_session, uuid.UUID(session_token)) is None:
            return JsonResponseWithStatus(
                status_code=404,
                content=SessionNotFoundError(),
            )
        # Retrieve session info from Redis
        session_info = redis_manager.get("session_token", session_token)
        # Validate session token presence and validity
        if session_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )
        # Step 3: Delete the session from Redis and return success
        redis_manager.delete("user_token", user_id, db_session)
        redis_manager.delete("session_token", session_token, db_session)
        return JsonResponseWithStatus(
            status_code=200,
            content=DeactivateSessionPostResponse(),
        )

    except Exception as e:
        # Log the error and roll back any pending DB transaction
        logging.error(f"Error deactivating session: {e}")
        db_session.rollback()

        return JsonResponseWithStatus(
            status_code=500,
            content=DeactivateSessionError(),
        )
    finally:
        db_session.close()
