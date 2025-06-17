"""
This module defines a FastAPI router for deleting a user account.

Endpoints:
- DELETE /: Deletes the currently authenticated user's account.

Features:
- Cookie-based authentication using auth_token.
- Optional deletion of related data (sessions, queries).
- Structured JSON responses with appropriate status codes.
"""

import logging
import uuid

from fastapi import APIRouter, Cookie, Depends, Query

import database.crud as crud
from App import App
from backend.Responses import (
    DeleteUserDeleteResponse,
    DeleteUserError,
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    JsonResponseWithStatus,
    UserNotFoundError,
)

# Initialize FastAPI router
router = APIRouter()


@router.delete(
    "",
    response_model=DeleteUserDeleteResponse,
    responses={
        "200": {"model": DeleteUserDeleteResponse},
        "401": {"model": InvalidOrExpiredAuthToken},
        "404": {"model": UserNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": DeleteUserError},
    },
)
def delete_user(
    delete_data: bool = Query(False, description="Delete user's associated data"),
    auth_token: str = Cookie(""),
    app: App = Depends(App.get_instance),
) -> JsonResponseWithStatus:
    """
    Delete the authenticated user's account and optionally their associated data.

    Args:
        delete_data (bool): If True, removes all user-related data (default is False).
        auth_token (str): Auth token provided in cookies to authenticate the user.
        app (App): Dependency-injected app instance with DB and Redis access.

    Returns:
        JsonResponseWithStatus: A success or error response depending on the outcome.
    """
    db_session = app.get_db_session()
    redis_manager = app.get_redis_manager()

    try:
        # Get auth data from Redis using the auth token
        auth_info = redis_manager.get("auth_token", auth_token)

        # If token is invalid or user ID is missing, respond with 401
        if auth_info is None or not auth_info.get("user_id"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )

        user_id = auth_info["user_id"]
        # Check if user exists in the database
        found_user = crud.get_user_by_id(db_session, uuid.UUID(user_id))
        if not found_user:
            return JsonResponseWithStatus(
                status_code=404,
                content=UserNotFoundError(),
            )

        # Delete session/auth token data from Redis
        redis_manager.delete("user_id", user_id, db_session)

        # Optionally delete all associated data
        if delete_data:
            logging.info(f"Performing full wipe-out for user ID: {user_id}")
            crud.delete_user_full_wipe_out(db=db_session, user_id=uuid.UUID(user_id))

        # Delete the user account itself
        crud.delete_user_by_id(db=db_session, user_id=user_id)

        return JsonResponseWithStatus(
            status_code=200,
            content=DeleteUserDeleteResponse(),
        )

    except Exception as e:
        logging.error(f"Error processing user deletion request: {str(e)}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=DeleteUserError(),
        )
    finally:
        db_session.close()


def __init__():
    """
    Optional module-level initializer.

    Placeholder for any future initialization logic required upon import.
    """
    pass
