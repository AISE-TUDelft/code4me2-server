"""
This module defines a FastAPI router for deleting a user account.

It includes:
- An endpoint for deleting the currently authenticated user.
- Optional deletion of related data (sessions and queries).
- Cookie-based authentication and response handling.
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

# Initialize a FastAPI router for user deletion
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
    delete_data: bool = Query(False, description="Delete user's data"),
    auth_token: str = Cookie("auth_token"),
    app: App = Depends(App.get_instance),
) -> JsonResponseWithStatus:
    """
    Delete the authenticated user's account and optionally their data.

    Args:
        delete_data (bool): Flag indicating whether to delete associated data (default: False).
        auth_token (str): Authentication token stored in browser cookies.
        app (App): Application instance with access to database and session managers.

    Returns:
        JsonResponseWithStatus: A success message or an appropriate error response.
    """
    db_session = app.get_db_session()
    redis_manager = app.get_redis_manager()

    try:

        # Retrieve the user ID from the auth token
        auth_info = redis_manager.get("auth_token", auth_token)

        # If the token is invalid or missing, return a 401 error
        if auth_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )
        user_id = auth_info["user_id"]
        logging.log(logging.INFO, f"Deleting user with ID: {user_id}")

        # Clear user sessions and auth tokens
        found_user = crud.get_user_by_id(db_session, uuid.UUID(user_id))
        if not found_user:
            return JsonResponseWithStatus(
                status_code=404,
                content=UserNotFoundError(),
            )
        # TODO check this later
        redis_manager.delete("auth_token", auth_token, db_session)

        # If delete_data flag is True, also delete user-related data from the database
        if delete_data:
            logging.log(
                logging.INFO,
                f"Deleting user data for user ID: {user_id}",
            )
            # TODO check this later
            # crud.delete_project_cascade
            # crud.delete_context
            # crud.delete_meta_query_cascade
            # crud.delete_behavioural_telemetry
            # crud.delete_had_generation
            # crud.delete_contextual_telemetry
            # crud.delete_chat_cascade

        # Remove the user account
        crud.delete_user_by_id(db=db_session, user_id=user_id)

        # Return appropriate response based on whether the user was deleted
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


def __init__():
    """
    Module-level initializer.

    This function runs when the module is imported.
    Currently a placeholder for future initialization logic if needed.
    """
    pass
