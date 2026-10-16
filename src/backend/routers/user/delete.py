"""
This module defines a FastAPI router for deleting a user account.

Endpoints:
- DELETE /: Deletes the currently authenticated user's account.

Features:
- Cookie-based authentication using auth_token.
- Always erases everything collected about the account (privacy.erasure), in
  one transaction, before deleting the account itself.
- Refuses (409) accounts that own research studies or agent profiles, which
  other people's study data depends on.
- Revokes the account's live tokens and clears its cookies.
- Structured JSON responses with appropriate status codes.
"""

import logging
import uuid

from fastapi import APIRouter, Cookie, Depends, Query

import database.crud as crud
from App import App
from backend.Responses import (
    DeleteUserBlockedError,
    DeleteUserDeleteResponse,
    DeleteUserError,
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    JsonResponseWithStatus,
    UserNotFoundError,
)
from privacy import erasure

# Initialize FastAPI router
router = APIRouter()

_ACCOUNT_COOKIES = ("auth_token", "session_token", "project_token")


@router.delete(
    "",
    response_model=DeleteUserDeleteResponse,
    responses={
        "200": {"model": DeleteUserDeleteResponse},
        "401": {"model": InvalidOrExpiredAuthToken},
        "404": {"model": UserNotFoundError},
        "409": {"model": DeleteUserBlockedError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": DeleteUserError},
    },
)
def delete_user(
    delete_data: bool = Query(
        False,
        description="Deprecated and ignored: deleting an account always erases its data.",
    ),
    auth_token: str = Cookie(""),
    app: App = Depends(App.get_instance),
) -> JsonResponseWithStatus:
    """
    Delete the authenticated user's account together with all data collected about it.

    Args:
        delete_data (bool): Ignored; kept so existing clients keep working.
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

        user_id = uuid.UUID(auth_info["user_id"])
        # Check if user exists in the database
        if not crud.get_user_by_id(db_session, user_id):
            return JsonResponseWithStatus(
                status_code=404,
                content=UserNotFoundError(),
            )

        try:
            erasure.delete_account(db_session, user_id)
        except erasure.AccountDeletionBlocked as blocked:
            db_session.rollback()
            return JsonResponseWithStatus(
                status_code=409,
                content=DeleteUserBlockedError(message=str(blocked)),
            )
        db_session.commit()

    except Exception as e:
        logging.error(f"Error processing user deletion request: {str(e)}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=DeleteUserError(),
        )
    finally:
        db_session.close()

    # The account is gone: none of its credentials may keep working. Best effort:
    # a token that outlives a Redis failure names an account that no longer
    # exists, and nothing is ever collected for an unknown account.
    try:
        redis_manager.revoke_user_tokens(str(user_id))
    except Exception as e:
        logging.warning(f"Could not revoke the deleted account's tokens: {str(e)}")

    response = JsonResponseWithStatus(
        status_code=200,
        content=DeleteUserDeleteResponse(),
    )
    for cookie in _ACCOUNT_COOKIES:
        response.delete_cookie(cookie)
    return response


def __init__():
    """
    Optional module-level initializer.

    Placeholder for any future initialization logic required upon import.
    """
    pass
