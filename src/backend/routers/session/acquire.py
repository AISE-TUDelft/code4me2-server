"""
This module defines a FastAPI router for managing session acquisition via an auth token.

It includes:
- A GET endpoint to acquire or create a session token associated with a valid auth token.
- Dependency injection for application context and Redis.
- Error handling and appropriate HTTP response codes.
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
    Acquire or create a session token using the provided auth token.

    - If the auth token is missing or invalid, return 401.
    - If no session is associated yet, create one and store it in Redis.
    """
    logging.info(f"Acquiring session for auth_token: {auth_token}")

    redis_manager = app.get_redis_manager()
    config = app.get_config()
    db_session = app.get_db_session()

    try:
        auth_info = redis_manager.get("auth_token", auth_token)
        # If the token is invalid or missing, return a 401 error
        if auth_info is None or not auth_info.get("user_id"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )
        user_id = auth_info["user_id"]
        session_token = auth_info.get("session_token")
        if not redis_manager.get("session_token", session_token):
            session_token = create_uuid()
            crud.create_session(
                db_session,
                Queries.CreateSession(user_id=uuid.UUID(user_id)),
                session_token,
            )
            auth_info["session_token"] = session_token

            # Update auth_token with new session_token while preserving TTL
            redis_manager.set("auth_token", auth_token, auth_info)

            # Add session_token entry separately
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
        logging.log(logging.ERROR, f"Error acquiring a session: {e}")
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
