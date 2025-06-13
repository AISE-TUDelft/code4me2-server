"""
This module defines a FastAPI router for managing session acquisition via an auth token.

It provides:
- A GET endpoint to acquire or create a session token associated with a valid auth token.
- Dependency injection for application context and Redis.
- Proper error handling and HTTP response codes.
"""

import logging
import uuid

from fastapi import APIRouter, Cookie, Depends

import Queries
from App import App
from backend.Responses import (
    AcquireSessionError,
    AcquireSessionGetResponse,
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    JsonResponseWithStatus,
)
from database import crud
from utils import create_uuid

router = APIRouter()


@router.get(
    "",
    response_model=AcquireSessionGetResponse,
    responses={
        "200": {"model": AcquireSessionGetResponse},
        "401": {"model": InvalidOrExpiredAuthToken},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": AcquireSessionError},
    },
)
def acquire_session(
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie(""),
) -> JsonResponseWithStatus:
    """
    Acquire or create a session token linked to the provided auth token.

    Args:
        app (App): Injected FastAPI app instance.
        auth_token (str): Auth token from the client's cookies.

    Returns:
        JsonResponseWithStatus: Contains the session token or appropriate error response.

    Behavior:
        - Returns 401 if auth token is missing or invalid.
        - If no session token exists for the auth token, creates a new session.
        - Sets the session token as an HttpOnly cookie with expiration.
    """
    logging.info(f"Acquiring session for auth_token: {auth_token}")

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
        session_token = auth_info.get("session_token")

        # Create a new session token if none exists or invalid
        if not redis_manager.get("session_token", session_token):
            session_token = create_uuid()
            crud.create_session(
                db_session,
                Queries.CreateSession(user_id=uuid.UUID(user_id)),
                session_token,
            )
            auth_info["session_token"] = session_token

            # Update auth token in Redis with new session token while preserving TTL
            redis_manager.set("auth_token", auth_token, auth_info)

            # Store session token entry separately
            redis_manager.set(
                "session_token",
                session_token,
                {"auth_token": auth_token, "project_tokens": []},
            )

        response_obj = JsonResponseWithStatus(
            status_code=200,
            content=AcquireSessionGetResponse(session_token=session_token),
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
        logging.error(f"Error acquiring a session: {e}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=AcquireSessionError(),
        )


def __init__():
    """
    Module-level initializer placeholder.
    """
    pass
