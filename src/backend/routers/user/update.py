"""
This module defines a FastAPI router for updating user information.

It includes:
- An endpoint for updating the currently authenticated user.
- Dependency injection for app and authentication token.
- Response model validation and error handling.
"""

import logging

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
import Queries as Queries
from App import App
from backend.Responses import (
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    InvalidPreviousPassword,
    JsonResponseWithStatus,
    UpdateUserError,
    UpdateUserPutResponse,
    UserAlreadyExistsWithThisEmail,
)
from response_models import ResponseUser

router = APIRouter()


@router.put(
    "/",
    response_model=UpdateUserPutResponse,
    responses={
        201: {"model": UpdateUserPutResponse},
        401: {"model": InvalidOrExpiredAuthToken},
        409: {"model": UserAlreadyExistsWithThisEmail},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": UpdateUserError},
    },
)
def update_user(
    user_to_update: Queries.UpdateUser,
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie("auth_token"),
) -> JsonResponseWithStatus:
    """
    Update the currently authenticated user's data.

    Args:
    - user_to_update: Pydantic model containing fields to update.
    - app: Application context, injected by FastAPI.
    - auth_token: Authentication token stored in browser cookies.

    Returns:
    - JSON response with updated user information if successful.
    - Appropriate error response if auth token is missing or invalid.
    """
    logging.info(f"Updating user: {user_to_update.dict()}")

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

        # Check for email conflict only if new email provided and different from current user's email
        if user_to_update.email:
            existing_user = crud.get_user_by_email(
                db_session, str(user_to_update.email)
            )
            if existing_user and str(existing_user.user_id) != user_id:
                return JsonResponseWithStatus(
                    status_code=409,
                    content=UserAlreadyExistsWithThisEmail(),
                )

        # If changing password, verify the previous password is correct
        if user_to_update.previous_password and user_to_update.password:
            valid_password = crud.get_user_by_id_password(
                db=db_session,
                user_id=user_id,
                password=user_to_update.previous_password.get_secret_value(),
            )
            if not valid_password:
                return JsonResponseWithStatus(
                    status_code=401,
                    content=InvalidPreviousPassword(),
                )
        updated_user = crud.update_user(
            db=db_session, user_id=user_id, user_to_update=user_to_update
        )
        return JsonResponseWithStatus(
            status_code=201,
            content=UpdateUserPutResponse(
                user=ResponseUser.model_validate(updated_user)
            ),
        )
    except Exception as e:
        logging.error(f"Error processing user update request: {str(e)}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=UpdateUserError(),
        )


def __init__():
    """
    Module-level initializer.

    This function runs when the module is imported.
    Currently a placeholder for future initialization logic if needed.
    """
    pass
