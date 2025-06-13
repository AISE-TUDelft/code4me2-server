import logging

from fastapi import APIRouter, Cookie, Depends

from App import App
from backend.Responses import (
    DeactivateSessionError,
    DeactivateSessionPostResponse,
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
)

# Create a router instance for handling session deactivation
router = APIRouter()


@router.put(
    "/",
    response_model=None,
    responses={
        "200": {"model": DeactivateSessionPostResponse},
        "401": {"model": InvalidOrExpiredSessionToken},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": DeactivateSessionError},
    },
    tags=["Deactivate Session"],
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
        # Step 1: Retrieve and validate auth token data from Redis
        auth_info = redis_manager.get("auth_token", auth_token)
        if auth_info is None or not auth_info.get("user_id"):
            # Invalid or missing user_id in token
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )

        user_id = auth_info["user_id"]
        session_token = auth_info.get("session_token")

        # Step 2: Retrieve and validate session info from Redis
        session_info = redis_manager.get("session_info", session_token)
        if not session_info:
            # Session token not found or expired
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Step 3: Delete the session from Redis and return success
        redis_manager.delete("session_token", session_token, db_session)
        return JsonResponseWithStatus(
            status_code=200,
            content=DeactivateSessionPostResponse(),
        )

    except Exception as e:
        # Log the error and roll back any pending DB transaction
        logging.log(logging.ERROR, f"Error deactivating session: {e}")
        db_session.rollback()

        return JsonResponseWithStatus(
            status_code=500,
            content=DeactivateSessionError(),
        )
